from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import requests

from arxiv_cortex.db import transaction
from arxiv_cortex.services.imports import PaperImportError, PaperImportService
from arxiv_cortex.utils import isoformat


class PdfDocumentError(ValueError):
    pass


class PdfDocumentService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        data_dir: str | Path,
        max_bytes: int = 50 * 1024 * 1024,
        session: requests.Session | None = None,
        resolve_host=None,
    ):
        self.connection = connection
        self.data_dir = Path(data_dir).resolve()
        self.max_bytes = max_bytes
        self.session = session
        self.resolve_host = resolve_host

    def ensure(self, arxiv_id: str) -> dict[str, Any]:
        paper = self.connection.execute(
            """
            SELECT id, arxiv_id, latest_version, source_kind, updated_at, pdf_url
            FROM papers WHERE arxiv_id = ?
            """,
            (arxiv_id,),
        ).fetchone()
        if not paper:
            raise LookupError(f"Paper {arxiv_id} not found")
        source_url = str(paper["pdf_url"] or "").strip()
        if not source_url:
            raise PdfDocumentError("This paper does not have a PDF source")

        is_arxiv = paper["source_kind"] == "arxiv"
        revision_label = f"v{int(paper['latest_version'])}" if is_arxiv else "external"
        request_key = self._request_key(int(paper["id"]), source_url, revision_label)
        if is_arxiv:
            existing = self.connection.execute(
                "SELECT * FROM documents WHERE request_key = ?", (request_key,)
            ).fetchone()
            if (
                existing
                and existing["status"] == "ready"
                and self.path_for(dict(existing)).is_file()
            ):
                self.connection.execute(
                    "UPDATE documents SET last_opened_at = ? WHERE id = ?",
                    (isoformat(), existing["id"]),
                )
                return self.get(int(existing["id"]))  # type: ignore[return-value]

        try:
            content = self._download(source_url)
            checksum = hashlib.sha256(content).hexdigest()
            if not is_arxiv:
                revision_label = f"PDF {checksum[:12]}"
                request_key = self._request_key(int(paper["id"]), source_url, checksum)
                existing = self.connection.execute(
                    "SELECT * FROM documents WHERE request_key = ?", (request_key,)
                ).fetchone()
                if (
                    existing
                    and existing["status"] == "ready"
                    and self.path_for(dict(existing)).is_file()
                ):
                    with transaction(self.connection):
                        self.connection.execute(
                            "UPDATE documents SET stale = (id != ?), last_opened_at = CASE WHEN id = ? THEN ? ELSE last_opened_at END WHERE paper_id = ?",
                            (existing["id"], existing["id"], isoformat(), paper["id"]),
                        )
                    return self.get(int(existing["id"]))  # type: ignore[return-value]
            artifact_path = self._store(str(paper["arxiv_id"]), revision_label, checksum, content)
        except OSError as error:
            cache_error = PdfDocumentError("The PDF could not be written to the local cache")
            self._record_failure(
                int(paper["id"]), request_key, source_url, revision_label, str(cache_error)
            )
            raise cache_error from error
        except PdfDocumentError as error:
            self._record_failure(
                int(paper["id"]), request_key, source_url, revision_label, str(error)
            )
            raise

        now = isoformat()
        with transaction(self.connection):
            self.connection.execute(
                "UPDATE documents SET stale = 1 WHERE paper_id = ? AND request_key != ?",
                (paper["id"], request_key),
            )
            self.connection.execute(
                """
                INSERT INTO documents(
                    paper_id, request_key, source_url, revision_label, source_checksum,
                    artifact_path, byte_size, status, error, stale, created_at,
                    fetched_at, last_opened_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', NULL, 0, ?, ?, ?)
                ON CONFLICT(request_key) DO UPDATE SET
                    source_checksum = excluded.source_checksum,
                    artifact_path = excluded.artifact_path,
                    byte_size = excluded.byte_size,
                    status = 'ready', error = NULL, stale = 0,
                    fetched_at = excluded.fetched_at,
                    last_opened_at = excluded.last_opened_at
                """,
                (
                    paper["id"],
                    request_key,
                    source_url,
                    revision_label,
                    checksum,
                    artifact_path,
                    len(content),
                    now,
                    now,
                    now,
                ),
            )
        row = self.connection.execute(
            "SELECT id FROM documents WHERE request_key = ?", (request_key,)
        ).fetchone()
        return self.get(int(row["id"]))  # type: ignore[return-value]

    @staticmethod
    def _request_key(paper_id: int, source_url: str, version: str) -> str:
        return hashlib.sha256(f"{paper_id}\0{source_url}\0{version}".encode()).hexdigest()

    def get(self, document_id: int, *, arxiv_id: str | None = None) -> dict[str, Any] | None:
        params: list[object] = [document_id]
        condition = "d.id = ?"
        if arxiv_id is not None:
            condition += " AND p.arxiv_id = ?"
            params.append(arxiv_id)
        row = self.connection.execute(
            f"""
            SELECT d.*, p.arxiv_id, p.title
            FROM documents d JOIN papers p ON p.id = d.paper_id
            WHERE {condition}
            """,  # noqa: S608
            params,
        ).fetchone()
        return dict(row) if row else None

    def list_for_paper(self, arxiv_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT d.*,
                       COUNT(ph.id) AS highlight_count,
                       MAX(ph.updated_at) AS last_annotated_at
                FROM documents d
                JOIN papers p ON p.id = d.paper_id
                LEFT JOIN paper_highlights ph ON ph.document_id = d.id
                WHERE p.arxiv_id = ? AND d.status = 'ready'
                GROUP BY d.id
                ORDER BY (MAX(ph.updated_at) IS NOT NULL) DESC,
                         MAX(ph.updated_at) DESC, d.created_at DESC
                """,
                (arxiv_id,),
            )
        ]

    def update_metadata(
        self,
        document_id: int,
        *,
        fingerprint: str = "",
        page_count: int | None = None,
    ) -> None:
        if page_count is not None and not 1 <= page_count <= 100_000:
            raise PdfDocumentError("The PDF page count is invalid")
        normalized_fingerprint = fingerprint.strip()[:256]
        self.connection.execute(
            """
            UPDATE documents
            SET pdf_fingerprint = COALESCE(NULLIF(?, ''), pdf_fingerprint),
                page_count = COALESCE(?, page_count)
            WHERE id = ?
            """,
            (normalized_fingerprint, page_count, document_id),
        )

    def path_for(self, document: dict[str, Any]) -> Path:
        relative = str(document.get("artifact_path") or "")
        if not relative:
            return self.data_dir / "missing"
        candidate = (self.data_dir / relative).resolve()
        if self.data_dir not in candidate.parents:
            raise PdfDocumentError("The cached PDF path is invalid")
        return candidate

    def _download(self, source_url: str) -> bytes:
        importer = PaperImportService(
            self.connection,
            session=self.session,
            resolve_host=self.resolve_host,
        )
        try:
            response = importer._fetch(source_url, max_bytes=self.max_bytes)
        except PaperImportError as error:
            message = str(error).replace("import", "cache")
            raise PdfDocumentError(message) from error
        content_type = response.headers.get("Content-Type", "").lower()
        content = response.content
        if "pdf" not in content_type and not source_url.lower().split("?", 1)[0].endswith(".pdf"):
            raise PdfDocumentError("The source did not return a PDF document")
        if not content.lstrip().startswith(b"%PDF-"):
            raise PdfDocumentError("The source returned an invalid PDF document")
        return content

    def _store(self, arxiv_id: str, revision_label: str, checksum: str, content: bytes) -> str:
        safe_paper = re.sub(r"[^A-Za-z0-9._-]+", "_", arxiv_id).strip("._") or "paper"
        safe_revision = re.sub(r"[^A-Za-z0-9._-]+", "-", revision_label).strip(".-")
        directory = self.data_dir / "documents" / safe_paper / f"{safe_revision}-{checksum[:12]}"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / "source.pdf"
        if not destination.exists():
            descriptor, temporary_name = tempfile.mkstemp(
                prefix="source-", suffix=".tmp", dir=directory
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, destination)
            except BaseException:
                Path(temporary_name).unlink(missing_ok=True)
                raise
        return str(destination.relative_to(self.data_dir))

    def _record_failure(
        self,
        paper_id: int,
        request_key: str,
        source_url: str,
        revision_label: str,
        error: str,
    ) -> None:
        now = isoformat()
        self.connection.execute(
            """
            INSERT INTO documents(
                paper_id, request_key, source_url, revision_label, status,
                error, created_at
            ) VALUES (?, ?, ?, ?, 'failed', ?, ?)
            ON CONFLICT(request_key) DO UPDATE SET status = 'failed', error = excluded.error
            """,
            (paper_id, request_key, source_url, revision_label, error[:500], now),
        )
