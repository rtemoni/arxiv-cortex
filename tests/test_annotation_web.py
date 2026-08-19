from __future__ import annotations

from types import SimpleNamespace

from arxiv_cortex.services.documents import PdfDocumentService

MINIMAL_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


class PdfResponse:
    headers = {"Content-Type": "application/pdf"}
    url = "https://arxiv.org/pdf/2401.50001"
    status_code = 200
    is_redirect = False
    is_permanent_redirect = False

    def raise_for_status(self):
        return None

    def iter_content(self, _size):
        yield MINIMAL_PDF

    @property
    def content(self):
        return self._content


def csrf(client) -> str:
    client.get("/onboarding")
    with client.session_transaction() as session:
        return session["_csrf_token"]


def test_pdf_reader_highlight_note_and_global_library_routes(app, client, seed_paper):
    seed_paper("2401.50001", "Interactive evidence", "A paper with exact evidence")

    def factory(connection):
        return PdfDocumentService(
            connection,
            data_dir=app.config["DATA_DIR"],
            session=SimpleNamespace(get=lambda *_args, **_kwargs: PdfResponse()),
            resolve_host=lambda *_args: [(None, None, None, None, ("151.101.3.42", 443))],
        )

    app.config["PDF_DOCUMENT_SERVICE_FACTORY"] = factory
    detail = client.get("/papers/2401.50001")
    assert detail.status_code == 200
    assert b"Read &amp; highlight" in detail.data
    assert b"data-reader" in detail.data
    assert client.get("/static/reader.js").status_code == 200
    assert client.get("/static/vendor/pdfjs/build/pdf.mjs").status_code == 200

    assert client.post("/papers/2401.50001/documents/ensure", json={}).status_code == 400
    token = csrf(client)
    ensured = client.post(
        "/papers/2401.50001/documents/ensure",
        json={},
        headers={"X-CSRFToken": token},
    )
    assert ensured.status_code == 200
    document = ensured.json["document"]
    assert "artifact_path" not in document

    content = client.get(document["content_url"], headers={"Range": "bytes=0-7"})
    assert content.status_code == 206
    assert content.data == MINIMAL_PDF[:8]
    assert "private" in content.headers["Cache-Control"]

    created = client.post(
        "/papers/2401.50001/highlights",
        json={
            "document_id": document["id"],
            "quote": "<script>quoted evidence</script>",
            "fragments": [{"page_number": 1, "page_rotation": 0, "quads": [[10, 20, 90, 32]]}],
            "client_request_id": "web-request-1",
            "pdf_fingerprint": "fingerprint",
            "page_count": 1,
        },
        headers={"X-CSRFToken": token},
    )
    assert created.status_code == 201
    highlight = created.json["highlight"]

    updated = client.post(
        f"/papers/2401.50001/highlights/{highlight['id']}",
        json={"note": "A decisive result", "revision": highlight["revision"]},
        headers={"X-CSRFToken": token},
    )
    assert updated.status_code == 200
    conflict = client.post(
        f"/papers/2401.50001/highlights/{highlight['id']}",
        json={"note": "stale tab", "revision": highlight["revision"]},
        headers={"X-CSRFToken": token},
    )
    assert conflict.status_code == 409

    note = client.post(
        "/papers/2401.50001/note",
        json={"body": "My synthesis", "revision": 0},
        headers={"X-CSRFToken": token},
    )
    assert note.status_code == 200

    annotations = client.get(f"/papers/2401.50001/annotations?document={document['id']}")
    assert annotations.status_code == 200
    assert annotations.json["paper_note"]["body"] == "My synthesis"
    assert annotations.json["highlights"][0]["note"] == "A decisive result"

    detail = client.get(
        f"/papers/2401.50001?reader=1&document={document['id']}&highlight={highlight['id']}"
    )
    assert b"My synthesis" in detail.data
    assert b"&lt;script&gt;quoted evidence&lt;/script&gt;" in detail.data
    assert b"<script>quoted evidence</script>" not in detail.data

    library = client.get("/highlights?q=decisive")
    assert library.status_code == 200
    assert b"Interactive evidence" in library.data
    assert b"A decisive result" in library.data
    assert b"data-reader" not in library.data

    deleted = client.post(
        f"/papers/2401.50001/highlights/{highlight['id']}/delete",
        json={},
        headers={"X-CSRFToken": token},
    )
    assert deleted.status_code == 204
