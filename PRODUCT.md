# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Arxiv Cortex serves one researcher working privately on a local device. They discover papers, organize a personal library, read source PDFs, and retain evidence and notes for later research.

## Product Purpose

Arxiv Cortex is a lightweight research radar and reading workspace for arXiv and manually imported papers. It keeps discovery, saved state, semantic neighbors, PDF highlights, and research notes local. Success means the researcher can move from finding a paper to preserving an exact, versioned passage and their interpretation without leaving the application.

## Positioning

The product combines a deliberately small local research feed with exact on-device semantic ranking and source-grounded annotations. It does not require an account, hosted database, external vector service, or cloud note system.

## Operating Context

The application runs as a Flask web server backed by SQLite, normally bound to loopback or exposed privately through an HTTPS Tailscale proxy. Papers originate from followed arXiv searches or fields and from explicitly imported webpages or PDFs. PDFs are cached only when the researcher opens the embedded reader; highlights remain an application layer tied to that exact cached document.

## Capabilities and Constraints

- The application remains single-user and local-first, with no authentication or collaboration model.
- Browser mutations are POST-only and CSRF-protected; the public `/api/v1` surface remains read-only.
- PDF highlights use selectable text and page geometry. The source PDF is never modified.
- Highlights and their notes are document-version-specific; the paper synthesis note spans versions.
- OCR, freehand markup, PDF export, sharing, and automatic annotation migration are outside the first annotation release.
- Flask, server-rendered Jinja/HTMX, SQLite/FTS5, and a no-build locally vendored frontend remain the operating stack.

## Brand Commitments

Preserve the existing Arxiv Cortex name, restrained research-tool voice, arXiv-red accent, compact desktop information density, and familiar native control behavior.

## Evidence on Hand

- The incumbent interface and tokens live in `src/arxiv_cortex/templates` and `src/arxiv_cortex/static/app.css`.
- Product architecture and compatibility decisions live in `README.md` and `docs/PLAN.md`.
- The repository contains realistic paper metadata, state, grouping, import, and web-service test fixtures.

## Product Principles

- Keep research material private and inspectable on the user's device.
- Preserve exact source provenance instead of silently moving annotations between revisions.
- Make common research actions fast without hiding important state changes.
- Add infrastructure only when the local workload demonstrates a need for it.
- Keep source PDFs immutable and make Cortex-owned annotations recoverable through ordinary data backups.

## Accessibility & Inclusion

Keyboard operation, visible focus, responsive layouts, reduced-motion support, and text-layer access are required. Image-only PDFs remain readable visually but must clearly explain that selectable-text highlighting requires OCR, which is not included in this release.
