from __future__ import annotations

from arxiv_cortex.db import database_connection
from arxiv_cortex.services.arxiv_sync import ArxivRequestError, ArxivSourcePage, FeedQuery
from arxiv_cortex.services.papers import PaperService
from arxiv_cortex.services.search_tags import SearchTagService
from arxiv_cortex.services.settings import SettingsService
from arxiv_cortex.web import RESEARCH_AREAS
from tests.conftest import fake_result


def csrf(client) -> str:
    client.get("/onboarding")
    with client.session_transaction() as session:
        return session["_csrf_token"]


def test_first_run_redirects_to_onboarding(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/onboarding")


def test_discovery_search_is_local_only_by_default(app, client, seed_paper):
    seed_paper("2608.10001", "Local trace security", "Reasoning trace protections")

    class UnexpectedRemoteSource:
        def search_page(self, *_args, **_kwargs):
            raise AssertionError("default search must not call arXiv")

    app.extensions["arxiv_source"] = UnexpectedRemoteSource()
    response = client.get("/?q=trace+security")

    assert response.status_code == 200
    assert b"Local trace security" in response.data
    assert b'name="local_only" value="1" checked' in response.data
    assert b"Searching all of arXiv" not in response.data


def test_discovery_can_search_all_arxiv_and_cache_displayed_results(app, client, seed_paper):
    seed_paper("2608.10002", "Local seed", "A paper that enables discovery")
    remote_paper = fake_result(
        "2608.22222v2",
        title="Stealing reasoning traces from proprietary LLM APIs",
        summary="A remote result not previously held in the local index.",
        category="cs.CR",
    )

    class FakeRemoteSource:
        def __init__(self):
            self.calls = []

        def search_page(self, query, *, offset, limit, cutoff=None):
            self.calls.append((query, offset, limit, cutoff))
            return ArxivSourcePage([remote_paper], total=26, has_next=True)

    source = FakeRemoteSource()
    app.extensions["arxiv_source"] = source
    response = client.get(
        "/?q=stealing+reasoning+traces&local_only=0&category=cs.CR&days=30"
    )

    assert response.status_code == 200
    assert b"Stealing reasoning traces from proprietary LLM APIs" in response.data
    assert b"Searching all of arXiv" in response.data
    assert b"Displayed results are added to your local index" in response.data
    assert b'name="local_only" value="1" checked' not in response.data
    assert b"local_only=0" in response.data
    query, offset, limit, cutoff = source.calls[0]
    assert isinstance(query, FeedQuery)
    assert query.expression == (
        '(all:"stealing" AND all:"reasoning" AND all:"traces") AND cat:cs.CR'
    )
    assert (offset, limit) == (0, 25)
    assert cutoff is not None

    with database_connection(app.config["DATABASE"]) as connection:
        cached = PaperService(connection).get("2608.22222")
        assert cached is not None
        assert cached["version"] == 2


def test_remote_search_failure_preserves_query_and_falls_back_locally(
    app, client, seed_paper
):
    seed_paper("2608.10003", "Local fallback paper", "fallback phrase")

    class BrokenRemoteSource:
        def search_page(self, *_args, **_kwargs):
            raise ArxivRequestError("offline")

    app.extensions["arxiv_source"] = BrokenRemoteSource()
    response = client.get("/?q=fallback+phrase&local_only=0")

    assert response.status_code == 200
    assert b"Could not reach arXiv" in response.data
    assert b"Showing matches already stored on this device" in response.data
    assert b"Local fallback paper" in response.data
    assert b'value="fallback phrase"' in response.data


def test_onboarding_groups_unique_arxiv_topics_under_broad_research_areas(client):
    response = client.get("/onboarding")
    assert response.status_code == 200
    assert b"Computer Science" in response.data
    assert b"Robotics &amp; Autonomy" in response.data
    assert b"Policy, Law &amp; Society" in response.data
    assert b'value="cs.CY"' in response.data
    assert b'value="quant-ph"' in response.data

    categories = [category for area in RESEARCH_AREAS for category, _label in area["topics"]]
    assert len(categories) == len(set(categories))


def test_onboarding_keeps_refined_topics_selected_after_validation_error(client):
    token = csrf(client)
    response = client.post(
        "/onboarding",
        data={
            "_csrf_token": token,
            "categories": ["cs.RO"],
            "custom_categories": "bad!",
            "backfill_days": "90",
        },
    )
    assert response.status_code == 200
    assert b"Invalid arXiv categories" in response.data
    assert b'value="cs.RO" checked' in response.data
    assert b"1 selected" in response.data


def test_onboarding_requires_csrf_and_creates_subscription(app, client):
    response = client.post("/onboarding", data={"categories": "cs.LG"})
    assert response.status_code == 400
    token = csrf(client)
    response = client.post(
        "/onboarding",
        data={"_csrf_token": token, "categories": ["cs.LG"], "backfill_days": "90"},
    )
    assert response.status_code == 302
    assert "/settings?run=" in response.headers["Location"]
    with database_connection(app.config["DATABASE"]) as connection:
        assert connection.execute(
            "SELECT enabled FROM feed_subscriptions WHERE category = 'cs.LG'"
        ).fetchone()[0] == 1


def test_onboarding_allows_a_followed_keyword_search_without_a_field(app, client):
    token = csrf(client)
    response = client.post(
        "/onboarding",
        data={"_csrf_token": token, "tag_ids": ["1"], "backfill_days": "90"},
    )
    assert response.status_code == 302
    assert "/settings?run=" in response.headers["Location"]
    with database_connection(app.config["DATABASE"]) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM feed_subscriptions WHERE enabled = 1"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT enabled FROM search_tags WHERE id = 1"
        ).fetchone()[0] == 1

    discover = client.get("/")
    assert discover.status_code == 200
    assert b"All followed sources" in discover.data


def test_onboarding_requires_one_source_of_either_type(client):
    token = csrf(client)
    response = client.post(
        "/onboarding",
        data={"_csrf_token": token, "backfill_days": "90"},
    )
    assert response.status_code == 200
    assert b"Follow at least one keyword search or arXiv field" in response.data


def test_onboarding_validates_every_source_before_creating_a_custom_tag(app, client):
    token = csrf(client)
    response = client.post(
        "/onboarding",
        data={
            "_csrf_token": token,
            "custom_categories": "bad!",
            "tag_name": "Should stay a draft",
            "tag_keywords": "draft phrase",
            "backfill_days": "90",
        },
    )
    assert response.status_code == 200
    assert b"Invalid arXiv categories" in response.data
    assert b'value="Should stay a draft"' in response.data
    with database_connection(app.config["DATABASE"]) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM search_tags WHERE name = 'Should stay a draft'"
        ).fetchone()[0] == 0


def test_job_status_explains_deferred_arxiv_retry(app, client):
    with database_connection(app.config["DATABASE"]) as connection:
        cursor = connection.execute(
            """
            INSERT INTO sync_runs(
                status, trigger, categories_total, current_category, retry_attempt,
                retry_status, retry_reason, next_attempt_at, created_at
            ) VALUES ('running', 'test', 1, 'cs.MA', 1, 429, 'Rate exceeded.',
                '2026-08-11T07:00:00Z', '2026-08-11T06:55:00Z')
            """
        )
        run_id = int(cursor.lastrowid)

    response = client.get(f"/jobs/{run_id}")
    assert response.status_code == 200
    assert b"waiting for arXiv" in response.data
    assert b"arXiv returned HTTP 429" in response.data
    assert b"Retry 1 is scheduled for 2026-08-11T07:00:00Z" in response.data


def test_hardware_verification_tag_populates_discovery_search(client, seed_paper):
    seed_paper(
        "2401.20020",
        "Hardware verification for AI accelerators",
        "Reliable inference systems",
    )
    seed_paper("2401.20021", "Unrelated coordination", "Multiagent planning")

    response = client.get("/?tag=1")
    assert response.status_code == 200
    assert b"Hardware Verification" in response.data
    assert b"14 keyword phrases" in response.data
    assert b"Hardware verification for AI accelerators" in response.data
    assert b"Unrelated coordination" not in response.data
    assert b"hardware verification, AI accelerator verification" in response.data


def test_settings_can_create_update_and_delete_research_tags(app, client):
    token = csrf(client)
    created = client.post(
        "/settings/tags",
        data={
            "_csrf_token": token,
            "name": "Model internals",
            "description": "Interpretability work",
            "keywords": "linear probe\nactivation steering",
        },
    )
    assert created.status_code == 302
    assert created.headers["Location"].endswith("/settings#research-tags")
    with database_connection(app.config["DATABASE"]) as connection:
        tag = connection.execute(
            "SELECT * FROM search_tags WHERE name = 'Model internals'"
        ).fetchone()
        tag_id = int(tag["id"])
        assert tag["enabled"] == 0

    updated = client.post(
        f"/settings/tags/{tag_id}",
        data={
            "_csrf_token": token,
            "name": "Mechanistic interpretability",
            "description": "",
            "keywords": "sparse autoencoder",
        },
    )
    assert updated.status_code == 302
    page = client.get("/settings")
    assert b"Mechanistic interpretability" in page.data
    assert b"sparse autoencoder" in page.data

    deleted = client.post(
        f"/settings/tags/{tag_id}/delete", data={"_csrf_token": token}
    )
    assert deleted.status_code == 302
    with database_connection(app.config["DATABASE"]) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM search_tags WHERE id = ?", (tag_id,)
        ).fetchone()[0] == 0


def test_research_tag_validation_preserves_create_and_update_drafts(app, client):
    token = csrf(client)
    invalid_create = client.post(
        "/settings/tags",
        data={
            "_csrf_token": token,
            "name": "Draft tag",
            "description": "Keep this description",
            "keywords": "",
        },
        follow_redirects=True,
    )
    assert invalid_create.status_code == 200
    assert b'class="new-tag-disclosure" open' in invalid_create.data
    assert b'role="alert">Add at least one keyword phrase' in invalid_create.data
    assert b'value="Draft tag"' in invalid_create.data
    assert b'value="Keep this description"' in invalid_create.data

    with database_connection(app.config["DATABASE"]) as connection:
        tag = SearchTagService(connection).create("Second tag", "", "unique phrase")

    invalid_update = client.post(
        f"/settings/tags/{tag['id']}",
        data={
            "_csrf_token": token,
            "name": "Hardware Verification",
            "description": "Preserve edited copy",
            "keywords": "edited exact phrase",
        },
        follow_redirects=True,
    )
    assert invalid_update.status_code == 200
    assert f'id="tag-{tag["id"]}" open'.encode() in invalid_update.data
    assert b'role="alert">A research tag with that name already exists' in invalid_update.data
    assert b'value="Preserve edited copy"' in invalid_update.data
    assert b"edited exact phrase" in invalid_update.data


def test_settings_can_follow_a_tag_without_any_fields(app, client):
    token = csrf(client)
    response = client.post(
        "/settings/tags",
        data={
            "_csrf_token": token,
            "name": "Evaluation security",
            "description": "",
            "keywords": "agent evaluation security",
            "followed": "1",
        },
    )
    assert response.status_code == 302
    assert "/settings?run=" in response.headers["Location"]
    with database_connection(app.config["DATABASE"]) as connection:
        assert connection.execute(
            "SELECT enabled FROM search_tags WHERE name = 'Evaluation security'"
        ).fetchone()[0] == 1
        assert SettingsService(connection).has_feed_sources() is True

    page = client.get("/settings")
    assert b"Following" in page.data
    assert b"No field subscription is required" in page.data


def test_discovery_escapes_arxiv_metadata(app, client, seed_paper):
    seed_paper("2401.20001", "<script>alert(1)</script>", "Safe abstract")
    response = client.get("/")
    assert response.status_code == 200
    assert b"&lt;script&gt;alert(1)&lt;/script&gt;" in response.data
    assert b"<script>alert(1)</script>" not in response.data


def test_abstracts_are_marked_for_local_tex_rendering(client, seed_paper):
    seed_paper(
        "2401.20022",
        "Balanced allocations",
        r"The $2k$\textit{-choices rule} gives $O(\log n)$ load.",
    )
    discover = client.get("/")
    assert discover.status_code == 200
    assert b'class="arxiv-text"' in discover.data
    assert b"$2k$\\textit{-choices rule}" in discover.data
    assert b"cdn.jsdelivr.net" not in discover.data

    detail = client.get("/papers/2401.20022")
    assert detail.status_code == 200
    assert b'class="arxiv-text"' in detail.data
    assert client.get("/static/vendor/katex/katex.min.js").status_code == 200
    assert client.get("/static/vendor/katex/katex.min.css").status_code == 200


def test_htmx_state_action_and_html_fallback(app, client, seed_paper):
    seed_paper("2401.20002", "State from browser", "A quantum abstract")
    token = csrf(client)
    response = client.post(
        "/papers/2401.20002/save",
        data={"_csrf_token": token, "active": "1"},
        headers={"HX-Request": "true", "X-CSRFToken": token},
    )
    assert response.status_code == 200
    assert b"Saved" in response.data
    assert b'class="paper-actions"' in response.data

    response = client.post(
        "/papers/2401.20002/read",
        data={"_csrf_token": token, "active": "1"},
        headers={"Referer": "http://localhost/library"},
    )
    assert response.status_code == 302


def test_api_contract_health_search_detail_and_limits(app, client, seed_paper):
    seed_paper("2401.20003", "API vision paper", "A visual transformer")
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json["data"]["papers"] == 1

    result = client.get("/api/v1/papers?q=vision&limit=1")
    assert result.status_code == 200
    assert result.json["data"][0]["arxiv_id"] == "2401.20003"
    assert "database_id" not in result.json["data"][0]

    detail = client.get("/api/v1/papers/2401.20003")
    assert detail.json["data"]["links"]["pdf"].endswith("2401.20003")
    assert client.get("/api/v1/papers?limit=101").status_code == 400
    assert client.get("/api/v1/papers?cursor=bad!").status_code == 400
    assert client.get("/api/v1/openapi.json").status_code == 200


def test_similar_api_returns_empty_until_embeddings_exist(app, client, seed_paper):
    seed_paper("2401.20004", "Unembedded source", "Abstract")
    response = client.get("/api/v1/papers/2401.20004/similar")
    assert response.status_code == 200
    assert response.json["data"] == []
    assert client.get("/api/v1/papers/9999.99999/similar").status_code == 404
    assert client.get("/api/v1/papers/2401.20004/similar?days=nope").status_code == 400
    assert client.get("/api/v1/recommendations?days=365").status_code == 400


def test_library_contains_only_saved_papers_and_filters_read_status(
    app, client, seed_paper
):
    seed_paper("2401.20005", "Saved and unread", "Library paper")
    seed_paper("2401.20006", "Saved and read", "Library paper")
    seed_paper("2401.20007", "Read but not saved", "History paper")
    with database_connection(app.config["DATABASE"]) as connection:
        papers = PaperService(connection)
        papers.set_saved("2401.20005", True)
        papers.set_saved("2401.20006", True)
        papers.set_read("2401.20006", True)
        papers.set_read("2401.20007", True)

    response = client.get("/library?read=unread")
    assert response.status_code == 200
    assert b"Saved and unread" in response.data
    assert b"Saved and read" not in response.data
    assert b"Read but not saved" not in response.data
