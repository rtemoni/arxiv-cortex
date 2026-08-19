from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from arxiv_cortex.db import database_connection
from arxiv_cortex.services.annotations import AnnotationConflict, AnnotationService
from arxiv_cortex.services.documents import PdfDocumentError, PdfDocumentService

MINIMAL_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


class PdfResponse:
    headers = {"Content-Type": "application/pdf"}
    url = "https://arxiv.org/pdf/2401.40001"
    status_code = 200
    is_redirect = False
    is_permanent_redirect = False

    def __init__(self, content: bytes = MINIMAL_PDF):
        self.content = content

    def raise_for_status(self):
        return None

    def iter_content(self, _size):
        yield self.content


def public_resolver(*_args):
    return [(None, None, None, None, ("151.101.3.42", 443))]


def pdf_service(connection, data_dir: Path, *, content: bytes = MINIMAL_PDF):
    session = SimpleNamespace(get=lambda *_args, **_kwargs: PdfResponse(content))
    return PdfDocumentService(
        connection,
        data_dir=data_dir,
        session=session,
        resolve_host=public_resolver,
    )


def test_pdf_cache_is_atomic_deduplicated_and_versioned(app, seed_paper):
    seed_paper("2401.40001", "Versioned evidence", "A paper to annotate", version=1)
    with database_connection(app.config["DATABASE"]) as connection:
        service = pdf_service(connection, app.config["DATA_DIR"])
        first = service.ensure("2401.40001")
        repeated = service.ensure("2401.40001")

        assert first["id"] == repeated["id"]
        assert first["revision_label"] == "v1"
        assert first["source_checksum"]
        assert service.path_for(first).read_bytes() == MINIMAL_PDF
        assert str(service.path_for(first)).startswith(str(app.config["DATA_DIR"]))

    seed_paper("2401.40001", "Versioned evidence", "A revised paper", version=2)
    with database_connection(app.config["DATABASE"]) as connection:
        service = pdf_service(connection, app.config["DATA_DIR"])
        second = service.ensure("2401.40001")
        rows = connection.execute(
            "SELECT revision_label, stale FROM documents ORDER BY id"
        ).fetchall()

    assert second["revision_label"] == "v2"
    assert [(row["revision_label"], row["stale"]) for row in rows] == [
        ("v1", 1),
        ("v2", 0),
    ]


def test_external_pdf_checksums_create_immutable_versions(app, seed_paper):
    seed_paper("2401.40005", "External evidence", "A mutable external URL")
    second_pdf = MINIMAL_PDF.replace(b"1 0 obj", b"2 0 obj")
    payloads = iter([MINIMAL_PDF, MINIMAL_PDF, second_pdf])
    session = SimpleNamespace(get=lambda *_args, **_kwargs: PdfResponse(next(payloads)))
    with database_connection(app.config["DATABASE"]) as connection:
        connection.execute(
            "UPDATE papers SET source_kind = 'pdf', pdf_url = ? WHERE arxiv_id = ?",
            ("https://papers.example/research.pdf", "2401.40005"),
        )
        service = PdfDocumentService(
            connection,
            data_dir=app.config["DATA_DIR"],
            session=session,
            resolve_host=public_resolver,
        )
        first = service.ensure("2401.40005")
        repeated = service.ensure("2401.40005")
        changed = service.ensure("2401.40005")
        rows = connection.execute(
            "SELECT id, source_checksum, stale, artifact_path FROM documents WHERE status = 'ready' ORDER BY id"
        ).fetchall()

    assert repeated["id"] == first["id"]
    assert changed["id"] != first["id"]
    assert [row["stale"] for row in rows] == [1, 0]
    assert rows[0]["source_checksum"] != rows[1]["source_checksum"]
    assert rows[0]["artifact_path"] != rows[1]["artifact_path"]


def test_pdf_cache_rejects_private_invalid_and_oversized_sources(app, seed_paper):
    seed_paper("2401.40002", "Unsafe source", "A paper")

    def private_resolver(*_args):
        return [(None, None, None, None, ("127.0.0.1", 443))]

    with database_connection(app.config["DATABASE"]) as connection:
        private = PdfDocumentService(
            connection,
            data_dir=app.config["DATA_DIR"],
            session=SimpleNamespace(get=lambda *_args, **_kwargs: PdfResponse()),
            resolve_host=private_resolver,
        )
        with pytest.raises(PdfDocumentError, match="Private or local"):
            private.ensure("2401.40002")

        invalid = pdf_service(connection, app.config["DATA_DIR"], content=b"not a pdf")
        with pytest.raises(PdfDocumentError, match="invalid PDF"):
            invalid.ensure("2401.40002")

        oversized = PdfDocumentService(
            connection,
            data_dir=app.config["DATA_DIR"],
            max_bytes=8,
            session=SimpleNamespace(get=lambda *_args, **_kwargs: PdfResponse(MINIMAL_PDF)),
            resolve_host=public_resolver,
        )
        with pytest.raises(PdfDocumentError, match="too large"):
            oversized.ensure("2401.40002")


def test_highlights_notes_conflicts_fts_and_cascades(app, seed_paper):
    seed_paper("2401.40003", "Grounded verification", "Exact quoted evidence")
    with database_connection(app.config["DATABASE"]) as connection:
        document = pdf_service(connection, app.config["DATA_DIR"]).ensure("2401.40003")
        annotations = AnnotationService(connection)
        highlight = annotations.create_highlight(
            "2401.40003",
            document_id=document["id"],
            quote="  Exact   selected evidence. ",
            fragments=[
                {
                    "page_number": 1,
                    "page_rotation": 0,
                    "quads": [[10, 20, 100, 32], [10, 34, 80, 45]],
                },
                {
                    "page_number": 2,
                    "page_rotation": 0,
                    "quads": [[15, 18, 90, 30]],
                },
            ],
            client_request_id="request-1",
            pdf_fingerprint="fingerprint-1",
            page_count=2,
        )
        duplicate = annotations.create_highlight(
            "2401.40003",
            document_id=document["id"],
            quote="ignored duplicate",
            fragments=[{"page_number": 1, "quads": [[1, 1, 2, 2]]}],
            client_request_id="request-1",
        )

        assert highlight["quote"] == "Exact selected evidence."
        assert duplicate["id"] == highlight["id"]
        assert [fragment["page_number"] for fragment in highlight["fragments"]] == [1, 2]

        updated = annotations.update_highlight_note(
            "2401.40003", highlight["id"], note="Supports the core argument", revision=1
        )
        assert updated["revision"] == 2
        with pytest.raises(AnnotationConflict):
            annotations.update_highlight_note(
                "2401.40003", highlight["id"], note="stale tab", revision=1
            )

        paper_note = annotations.upsert_paper_note(
            "2401.40003", body="Synthesis of the verification method", revision=0
        )
        assert paper_note["revision"] == 1
        with pytest.raises(AnnotationConflict):
            annotations.upsert_paper_note("2401.40003", body="stale synthesis", revision=99)

        items, total = annotations.library(query="core argument")
        assert total == 1
        assert items[0]["arxiv_id"] == "2401.40003"
        items, total = annotations.library(query="synthesis verification")
        assert total == 1

        annotations.delete_highlight("2401.40003", highlight["id"])
        assert (
            connection.execute("SELECT COUNT(*) FROM paper_highlight_fragments").fetchone()[0] == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM paper_highlight_fts").fetchone()[0] == 0


def test_highlight_geometry_is_bounded_to_its_document(app, seed_paper):
    seed_paper("2401.40004", "Geometry", "Bounds")
    with database_connection(app.config["DATABASE"]) as connection:
        document = pdf_service(connection, app.config["DATA_DIR"]).ensure("2401.40004")
        annotations = AnnotationService(connection)
        with pytest.raises(ValueError, match="outside this PDF"):
            annotations.create_highlight(
                "2401.40004",
                document_id=document["id"],
                quote="Wrong page",
                fragments=[{"page_number": 3, "quads": [[1, 1, 2, 2]]}],
                client_request_id="bad-page",
                page_count=2,
            )
        with pytest.raises(ValueError, match="no visible area"):
            annotations.create_highlight(
                "2401.40004",
                document_id=document["id"],
                quote="Zero size",
                fragments=[{"page_number": 1, "quads": [[1, 1, 1, 2]]}],
                client_request_id="bad-quad",
            )


def test_annotations_enforce_paper_and_document_ownership(app, seed_paper):
    seed_paper("2401.40006", "First paper", "First document")
    seed_paper("2401.40007", "Second paper", "Second document")
    with database_connection(app.config["DATABASE"]) as connection:
        service = pdf_service(connection, app.config["DATA_DIR"])
        first_document = service.ensure("2401.40006")
        second_document = service.ensure("2401.40007")
        annotations = AnnotationService(connection)
        highlight = annotations.create_highlight(
            "2401.40006",
            document_id=first_document["id"],
            quote="Version-specific evidence",
            fragments=[{"page_number": 1, "quads": [[1, 1, 8, 3]]}],
            client_request_id="owned-highlight",
            page_count=1,
        )

        with pytest.raises(LookupError):
            annotations.create_highlight(
                "2401.40006",
                document_id=second_document["id"],
                quote="Cross-paper geometry",
                fragments=[{"page_number": 1, "quads": [[1, 1, 8, 3]]}],
                client_request_id="cross-paper",
                page_count=1,
            )
        with pytest.raises(LookupError):
            annotations.update_highlight_note(
                "2401.40007", highlight["id"], note="Wrong owner", revision=1
            )

        first = annotations.annotations_for_document("2401.40006", first_document["id"])
        second = annotations.annotations_for_document("2401.40007", second_document["id"])
        assert [item["id"] for item in first["highlights"]] == [highlight["id"]]
        assert second["highlights"] == []
