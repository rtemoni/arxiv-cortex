from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from arxiv_cortex.db import database_connection
from arxiv_cortex.services.groups import PaperGroupService
from arxiv_cortex.services.imports import ImportedPaper, PaperImportError, PaperImportService
from arxiv_cortex.services.papers import PaperQuery, PaperService


def test_curated_external_paper_joins_search_library_groups_and_embeddings(app):
    with database_connection(app.config["DATABASE"]) as connection:
        imported = PaperImportService(connection).import_metadata(
            ImportedPaper(
                title="Trustworthy accelerator execution",
                abstract="Remote attestation for confidential GPU workloads.",
                authors=["Ada Researcher", "Grace Scientist"],
                source_kind="usenix",
                source_name="USENIX",
                source_identifier="usenixsecurity24/example",
                venue="USENIX Security 2024",
                webpage_url="https://www.usenix.org/conference/example",
                pdf_url="https://www.usenix.org/system/files/example.pdf",
                categories=["external.security"],
            )
        )
        papers = PaperService(connection)
        papers.set_saved(imported["arxiv_id"], True)
        groups = PaperGroupService(connection)
        first = groups.create("AI Verification")
        second = groups.create("Systems")
        groups.add_paper(imported["arxiv_id"], first["id"])
        groups.add_paper(imported["arxiv_id"], second["id"])

        result = papers.list(
            PaperQuery(query="confidential GPU", state="saved", active_categories_only=False)
        )
        groups.attach_to_papers(result.items)

        assert [paper["title"] for paper in result.items] == [
            "Trustworthy accelerator execution"
        ]
        assert result.items[0]["source"] == {
            "kind": "usenix",
            "identifier": "usenixsecurity24/example",
            "name": "USENIX",
            "venue": "USENIX Security 2024",
            "date_known": False,
        }
        assert result.items[0]["links"]["webpage"].startswith("https://www.usenix.org/")
        assert {group["name"] for group in result.items[0]["groups"]} == {
            "AI Verification",
            "Systems",
        }

    assert app.extensions["embedding_service"].index_pending() == 1


def test_html_import_reads_citation_metadata_and_pdf_link(app):
    html = """
    <html><head>
      <meta charset="utf-8">
      <meta name="citation_title" content="A network verification paper">
      <meta name="citation_author" content="First Author">
      <meta name="citation_author" content="Second Author">
      <meta name="citation_pdf_url" content="/system/files/paper.pdf">
    </head><body>
      <div class="field field-name-field-paper-description">
        <div><p>An abstract supplied by the publisher.</p></div>
      </div>
      <h2>Presentation materials</h2><p>Slides</p>
    </body></html>
    """

    class Response:
        headers = {"Content-Type": "text/html; charset=utf-8"}
        url = "https://www.usenix.org/conference/nsdi24/presentation/example"
        status_code = 200
        is_redirect = False
        is_permanent_redirect = False

        def raise_for_status(self):
            return None

        def iter_content(self, _size):
            yield html.encode()

        @property
        def text(self):
            return self._content.decode()

    session = SimpleNamespace(get=lambda *_args, **_kwargs: Response())
    def resolver(*_args):
        return [(None, None, None, None, ("93.184.216.34", 443))]
    with database_connection(app.config["DATABASE"]) as connection:
        paper = PaperImportService(
            connection, session=session, resolve_host=resolver
        ).import_url("https://www.usenix.org/conference/nsdi24/presentation/example")

    assert paper["title"] == "A network verification paper"
    assert paper["authors"] == ["First Author", "Second Author"]
    assert paper["abstract"] == "An abstract supplied by the publisher."
    assert paper["source"]["name"] == "USENIX"
    assert paper["links"]["pdf"] == "https://www.usenix.org/system/files/paper.pdf"


def test_importer_rejects_private_and_credentialed_urls(app):
    def private_resolver(*_args):
        return [(None, None, None, None, ("127.0.0.1", 80))]

    with database_connection(app.config["DATABASE"]) as connection:
        importer = PaperImportService(connection, resolve_host=private_resolver)
        with pytest.raises(PaperImportError, match="Private or local"):
            importer.import_url("http://localhost/paper.pdf")
        with pytest.raises(PaperImportError, match="public HTTP"):
            importer.import_url("https://user:password@example.com/paper.pdf")
        with pytest.raises(PaperImportError, match="could not be resolved"):
            importer.import_url("https://example.com:99999/paper.pdf")


def test_importer_converts_connection_failures_to_recoverable_errors(app):
    class FailingSession:
        def get(self, *_args, **_kwargs):
            raise requests.Timeout("timed out")

    def resolver(*_args):
        return [(None, None, None, None, ("93.184.216.34", 443))]

    with database_connection(app.config["DATABASE"]) as connection:
        importer = PaperImportService(
            connection, session=FailingSession(), resolve_host=resolver
        )
        with pytest.raises(PaperImportError, match="could not be reached"):
            importer.import_url("https://example.com/paper")


def test_unknown_external_dates_do_not_match_published_windows(app):
    with database_connection(app.config["DATABASE"]) as connection:
        importer = PaperImportService(connection)
        unknown = importer.import_metadata(
            ImportedPaper(
                title="Undated paper",
                abstract="No publication date was supplied.",
                pdf_url="https://example.com/undated.pdf",
            )
        )
        known = importer.import_metadata(
            ImportedPaper(
                title="Recent dated paper",
                abstract="A publication date was supplied.",
                published_at="2026-08-01",
                pdf_url="https://example.com/recent.pdf",
            )
        )
        papers = PaperService(connection)
        papers.set_saved(unknown["arxiv_id"], True)
        papers.set_saved(known["arxiv_id"], True)
        result = papers.list(
            PaperQuery(days=30, state="saved", active_categories_only=False)
        )

    assert [paper["title"] for paper in result.items] == ["Recent dated paper"]
