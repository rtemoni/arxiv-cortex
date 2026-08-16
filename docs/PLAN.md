# Arxiv Cortex implementation plan and v2 handoff

This file is the durable plan for future agents. It records the product decisions behind v1, defines how revisions should preserve compatibility, and specifies v2 without authorizing v2 implementation as part of the initial release.

## Product contract

Arxiv Cortex is a greenfield, single-researcher local application. V1 must remain lightweight and understandable: Flask, Jinja/HTMX, SQLite/FTS5, NumPy exact-vector search, one local MiniLM model, and one in-process background worker. It does not use authentication, a JavaScript build system, Redis, Celery, a hosted database, or a separate vector database.

The supported v1 corpus is a chosen set of followed keyword searches and/or fields, normally 10,000–100,000 papers. The product is not a whole-arXiv mirror. Metadata and abstracts may be stored; PDFs are not downloaded by v1.

Primary user flows:

1. Select arXiv categories and start a 90-day backfill.
2. Browse newest papers while synchronization and embedding continue.
3. Search title, author, and abstract text with category/date filters.
4. Save papers, mark them read, dismiss irrelevant papers, and undo each action.
5. Rank papers by similarity to any indexed paper.
6. View a personalized recent feed based on saved and dismissed papers.
7. Inspect synchronization progress, errors, category subscriptions, and index counts.
8. Query the same read-only service layer through `/api/v1`.
9. Save editable research tags and independently choose which tags fetch matching papers as feed sources.

## V1 architecture and invariants

### Storage

- `papers` uses a canonical versionless arXiv ID and stores only the latest metadata/version.
- `paper_authors` preserves author order; `paper_categories` supports category filtering.
- `paper_fts` is an external-content FTS5 table maintained by triggers.
- `paper_embeddings` has a composite `(paper_id, model_id)` key and records dimension, source content hash, vector, and generation time.
- `paper_state` is a single implicit user's state. Saved and dismissed are mutually exclusive; read is independent.
- `feed_subscriptions` owns per-category backfill status and update watermark.
- `sync_runs` is the user-visible audit record. `job_leases` prevents overlapping jobs.
- `search_tags` stores single-user, editable searches plus per-tag follow, backfill, and watermark state; `paper_search_tags` records which followed query fetched each paper.
- Numbered SQL migrations are append-only. Never rewrite a migration that may have shipped; add the next migration instead.

### Ingestion

- All arXiv requests share one sequential client, one connection, at least a 3.1-second delay, and bounded retry/backoff behavior.
- Initial backfill sorts by submission date and stops at the configured date boundary.
- Incremental sync sorts by `lastUpdatedDate` and stops at `watermark - 48 hours`.
- Upserts are idempotent. A partial failure may commit papers but must not advance the subscription watermark.
- Existing data is retained when a category is disabled. Saved papers are never automatically pruned.
- Store both a metadata hash and a title/abstract content hash. Only the latter invalidates embeddings.

### Search and recommendations

- FTS5 weights title above author above abstract and never interpolates raw user SQL.
- Plain search terms use AND semantics. Comma-, semicolon-, or newline-separated groups are exact phrases joined with OR; research tags use this grouped form.
- V1 ships only `sentence-transformers/all-MiniLM-L6-v2`, but callers must not assume a fixed vector dimension.
- Vectors are normalized float32 values. Similarity is an exact matrix dot product.
- Personalized profile: `normalize(mean(saved) - 0.35 * mean(dismissed))`.
- Read-only papers are neutral. Saved, dismissed, and read papers are excluded from recommendation candidates.
- The default candidate window is 30 days, with 7/30/90/all-time controls.
- With no saved embeddings, return newest papers and label the feed as cold start.
- “Why this?” lists up to three saved papers with the highest direct similarity to the candidate.

### Web and API

- HTML routes and `/api/v1` routes call the same services.
- Browser state changes are POST-only and CSRF-protected. arXiv text is always escaped.
- HTMX is vendored locally and every form also works as a conventional HTML form.
- The API is read-only, loopback-bound, non-CORS, cursor-paginated, and capped at 100 items.
- Preserve current API field names. Add optional response fields compatibly; use `/api/v2` only for breaking wire changes.

## Revision protocol

Before changing an invariant, a future agent should:

1. Add a dated decision note to this file describing the problem, chosen approach, rejected alternatives, migration impact, and rollback path.
2. Add a new SQL migration instead of editing an applied migration.
3. Keep v1 API responses backward compatible or introduce a new API version.
4. Preserve the single-user loopback security assumption unless the revision explicitly implements authentication, authorization, and remote-deployment tests together.
5. Benchmark any replacement for exact vector search against the existing warm-query fixture before introducing operational infrastructure.
6. Keep arXiv access within the published rate limit across all new workers and commands.
7. Update README, OpenAPI, fixtures, and acceptance tests in the same change.

Decision note template:

```text
### YYYY-MM-DD — Short decision title
Problem:
Decision:
Alternatives rejected:
Schema/API impact:
Migration and rollback:
Acceptance evidence:
```

### 2026-08-11 — Capacity-aware arXiv synchronization

Problem: The first live metadata backfill received repeated HTTP 503 responses followed by the arXiv system-capacity form of HTTP 429. The original `1, 2, 4, 8, 16` second generator-level retry loop restarted pagination at offset zero, hid retry state from the UI, ignored `Retry-After`, and asked arXiv to sort a category's complete history before applying the 90-day cutoff locally.

Decision: Own page fetching in the arXiv source adapter while retaining `arxiv.Result` parsing. Keep sequential GET requests and the 3.1-second minimum delay. Retry only transient network failures and HTTP 429/500/502/503/504; honor `Retry-After`, use long 429 cooldowns, and use full-jitter exponential backoff capped at five minutes for other transient failures. Retry at the failed offset, require three consistent successful empty first pages, use explicit connect/read timeouts, and constrain initial queries with `submittedDate`. Persist the current category, retry reason/status, and next-attempt time for the Settings UI.

Alternatives rejected: The stock `arxiv.Client` retry recursion does not expose response headers/body, does not provide status-specific exponential backoff, and cannot expose retry progress. Restarting the full sync later would lose page-level recovery. OAI-PMH remains appropriate for future bulk harvesting but is unnecessary for a 90-day personal-category backfill.

Schema/API impact: Migration `002` adds optional retry-progress columns to `sync_runs`. No REST fields were removed or renamed; `/api/v1/health` may now include the added optional columns inside `last_sync`.

Migration and rollback: Migration `002` is additive. Older application code ignores the new columns. Rollback is therefore an application rollback without a database downgrade; the columns may remain safely.

Acceptance evidence: Unit coverage includes `Retry-After`, 429 fallback cooldowns, jittered 503 retries, permanent 400 failures, page-offset recovery, bounded backfill queries, empty-first-page confirmation, persisted retry state, and rendered job messaging. The complete local suite passes with live-network and performance tests remaining opt-in.

### 2026-08-12 — Editable research tags

Problem: Category subscriptions are useful for ingestion breadth but too coarse for recurring research questions. Re-entering a long set of hardware-verification terms for AI training and inference was error-prone, and a single AND-only query could not express useful alternatives.

Decision: Add single-user research tags as editable named saved searches. Each tag stores one exact keyword phrase per line and matches any configured phrase through a safely generated FTS5 expression. Ship a “Hardware Verification” preset focused on correctness, fault tolerance, and formal assurance for AI accelerators used in training and inference. Expose tags in Settings and as one-click controls directly beneath the Discover search field. Validation failures reopen the relevant editor with inline feedback and preserve every submitted value through a short-lived session draft.

Alternatives rejected: Encoding tags as arXiv categories cannot represent cross-category topics. Storing an opaque FTS5 expression would expose parser syntax and make safe editing difficult. A separate tag-to-paper materialization table would require continuous recomputation and would duplicate data already searchable through FTS5.

Schema/API impact: Migration `003` adds `search_tags`. The read-only REST response contract is unchanged. The `q` parameter now documents grouped OR semantics when phrases are separated by commas, semicolons, or newlines; plain space-separated queries retain their existing AND behavior.

Migration and rollback: Migration `003` is additive and seeds the initial preset idempotently. Older application code ignores the table, so application rollback needs no database downgrade. The table and user-created tags can remain safely.

Acceptance evidence: Service tests cover phrase normalization, CRUD, duplicate names, limits, and grouped FTS generation. Flask tests cover tag-driven discovery plus CSRF-protected create, update, and delete flows, including value-preserving inline validation. Desktop and 390-pixel mobile reviews confirm the list, editor, and tag-powered Discover state. The complete local suite passes with 42 tests and two opt-in tests skipped.

### 2026-08-12 — Followable keyword feeds without fields

Problem: Research tags were reusable filters over papers already fetched through category subscriptions. A researcher interested in a narrow cross-category topic still had to follow at least one broad field, bringing unrelated papers into the local corpus and making the onboarding requirement misleading.

Decision: Make every research tag independently followable. A followed tag is sent to the arXiv API as a bounded Boolean query over exact `all:` phrases, owns the same backfill and incremental watermark lifecycle as a category, and shares the single sequential rate-limited client. Record tag-to-paper provenance in `paper_search_tags`. Treat a paper as part of the active feed when it belongs to either an enabled field or an enabled tag. Onboarding requires at least one source of either type and presents keyword searches before optional fields. Tags may still be retained as saved local searches without being followed.

Alternatives rejected: Requiring a synthetic catch-all field defeats narrow ingestion. Expanding tags into a fixed category list loses cross-category matches and requires ongoing taxonomy maintenance. Running a second keyword synchronization worker would violate the single-client rate-limit model and could overlap requests.

Schema/API impact: Migration `004` adds follow/backfill/watermark columns to `search_tags` and the additive `paper_search_tags` provenance table. Existing tags migrate as saved-only so upgrading does not unexpectedly start multiple large backfills. `/api/v1/health` keeps `subscriptions` and now counts both source types, with additive `field_subscriptions` and `tag_subscriptions` fields.

Migration and rollback: Migration `004` is additive. Older application code can ignore the new columns and table. Existing category subscriptions are unchanged, and no retained paper or library state is deleted when either source type is disabled.

## V2 scope: local full-text evidence retrieval

V2 adds full-text retrieval for attached agent harnesses. It returns evidence only. The external harness owns prompts, model selection, tool orchestration, answer synthesis, and citation rendering. V2 does not add a built-in chat endpoint, MCP server, or framework-specific agent SDK.

### Dependency and process boundary

- Install v2 with the optional `rag` dependency group.
- Define a `DocumentParser` protocol whose output is a parser-neutral `ParsedDocument` containing ordered elements, section hierarchy, text/table/caption kind, and page provenance.
- Ship Docling as the default `DocumentParser` implementation.
- Run parsing in a subprocess launched by the existing single job queue. Capture stdout/stderr, timeout the process, and translate failure into a sanitized job error.
- The Flask process must never import or retain Docling's parser models during normal metadata browsing.

### Ingestion policy and lifecycle

- A paper is eligible when the user explicitly selects “Index full text” or saves it while `auto_index_saved` is enabled. Default that setting to enabled.
- Download PDFs only for local personal/research use. The agent API must never return PDF bytes; it returns arXiv source links and extracted evidence.
- Store artifacts under `data/documents/<safe-arxiv-id>/v<version>/`:
  - `source.pdf`
  - `parsed.json`
  - `document.md`
- Verify download content type, size limit, checksum, and arXiv ID/version before parsing.
- When arXiv reports a new version, mark the previous document stale. If it remains saved and auto-index is enabled, queue the new version. Retain older artifacts until an explicit user deletion.
- Removing a paper from the library does not automatically delete its PDF or index.

### V2 schema

Add new numbered migrations for:

- `documents`: paper ID, arXiv version, source/parser checksums, parser ID/version, status, artifact paths, created/indexed timestamps, stale flag, and sanitized error.
- `document_sections`: document ID, stable section ID, parent ID, ordinal, heading, heading path JSON, element kind, page start/end, and normalized text.
- `chunks`: document/section ID, stable content-derived chunk ID, ordinal, text, contextualized embedding text, token count, page start/end, and content hash.
- `chunk_fts`: external-content FTS5 index over contextualized chunk text.
- `chunk_embeddings`: chunk ID, model ID, dimension, normalized float32 vector, content hash, and generation time.

Use SHA-256 over document version, section path, ordinal, and normalized chunk text for deterministic chunk IDs. Preserve IDs when unrelated sections change.

### Parsing and chunking

- Convert PDFs to a parser-neutral document before writing database rows.
- Use Docling HybridChunker aligned to the active embedding tokenizer.
- Target at most 220 tokens per chunk so MiniLM's context limit is not exceeded.
- Preserve heading breadcrumbs, captions, table headers, page start/end, and original uncontextualized passage text.
- Embed `paper title + heading breadcrumb + passage`, but return the original passage separately.
- Treat references, acknowledgements, tables, captions, and body prose as distinct element kinds so callers can filter them later.
- A parsing/indexing transaction becomes active only after every chunk and embedding succeeds. Failed replacements must leave the prior active document searchable.

### Hybrid retrieval

Implement `PassageSearchService` over the same service boundary used by REST:

1. Validate and normalize query/filter input.
2. Retrieve the top 50 chunk FTS5 results.
3. Embed the query and retrieve the top 50 exact cosine results.
4. Fuse ranks with reciprocal rank fusion: `score += 1 / (60 + rank)` for each list.
5. Apply paper/category/date/library/current-version filters.
6. Return requested results with component and fused scores.

Each result must include `chunk_id`, `arxiv_id`, paper title/version, heading path, element kind, page start/end, exact passage text, lexical/vector/fused scores, canonical arXiv URL, and stale/current status.

Search current documents by default. `include_stale=true` is explicit. Continue exact vector search through 250,000 active chunks. If the warm p95 exceeds 500 ms or memory exceeds the documented target at that scale, create a separate decision note and benchmark an HNSW implementation; do not silently add a vector service.

### Agent-facing REST additions

Extend the existing OpenAPI contract with:

- `GET /api/v1/search/passages`
- `GET /api/v1/passages/{chunk_id}`
- `GET /api/v1/papers/{arxiv_id}/document`
- `GET /api/v1/papers/{arxiv_id}/outline`

`search/passages` accepts query text, paper IDs, category/date constraints, `library_only`, `include_stale`, cursor, and limit. It returns evidence only. Ingestion and deletion remain UI/CLI operations, not agent-callable API mutations.

If agent access is later exposed beyond loopback, authentication is a prerequisite. Tailscale exposure must use HTTPS; adding a listener alone is not an acceptable security revision.

### V2 acceptance tests

- Parse fixtures containing two-column prose, headings, tables, captions, references, and a scanned-page failure.
- Verify deterministic chunk IDs and stable IDs after an unrelated section revision.
- Verify page/heading provenance in every retrieval result.
- Verify failed parsing cannot replace the active index.
- Verify a revised paper becomes stale and the newest successful version becomes the default.
- Verify lexical-only, semantic-only, and mixed queries produce the expected reciprocal-rank order.
- Verify `library_only` and `include_stale` filters.
- Verify PDF bytes and local artifact paths never appear in agent responses.
- Benchmark 250,000 chunk vectors and record warm latency and memory.

## Deferred beyond v2

- Multi-user accounts, lab sharing, public hosting, and access-control policy.
- Built-in LLM answer generation or chat history.
- Email digests, social popularity, citation-graph ranking, notes, and named collections.
- Whole-arXiv bulk mirroring and automatic PDF ingestion for all metadata.
- ANN infrastructure before measured exact-search limits require it.
