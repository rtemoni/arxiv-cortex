from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from arxiv_cortex.db import transaction
from arxiv_cortex.services.papers import _fts_expression
from arxiv_cortex.utils import isoformat


class AnnotationConflict(RuntimeError):
    pass


class AnnotationService:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def paper_workspace(self, arxiv_id: str) -> dict[str, Any]:
        paper = self._paper(arxiv_id)
        note_row = self.connection.execute(
            "SELECT body, revision, created_at, updated_at FROM paper_notes WHERE paper_id = ?",
            (paper["id"],),
        ).fetchone()
        documents = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT d.*, COUNT(ph.id) AS highlight_count,
                       MAX(ph.updated_at) AS last_annotated_at
                FROM documents d
                LEFT JOIN paper_highlights ph ON ph.document_id = d.id
                WHERE d.paper_id = ? AND d.status = 'ready'
                GROUP BY d.id
                ORDER BY (MAX(ph.updated_at) IS NOT NULL) DESC,
                         MAX(ph.updated_at) DESC, d.created_at DESC
                """,
                (paper["id"],),
            )
        ]
        for document in documents:
            document["highlights"] = self._highlights(
                paper_id=int(paper["id"]), document_id=int(document["id"])
            )
        return {
            "paper_note": dict(note_row) if note_row else self._empty_note(),
            "documents": documents,
        }

    def annotations_for_document(self, arxiv_id: str, document_id: int) -> dict[str, Any]:
        paper = self._paper(arxiv_id)
        document = self._document(int(paper["id"]), document_id)
        note_row = self.connection.execute(
            "SELECT body, revision, created_at, updated_at FROM paper_notes WHERE paper_id = ?",
            (paper["id"],),
        ).fetchone()
        return {
            "document": document,
            "paper_note": dict(note_row) if note_row else self._empty_note(),
            "highlights": self._highlights(paper_id=int(paper["id"]), document_id=document_id),
        }

    def create_highlight(
        self,
        arxiv_id: str,
        *,
        document_id: int,
        quote: str,
        fragments: list[dict[str, Any]],
        client_request_id: str,
        pdf_fingerprint: str = "",
        page_count: int | None = None,
    ) -> dict[str, Any]:
        paper = self._paper(arxiv_id)
        document = self._document(int(paper["id"]), document_id)
        normalized_quote = " ".join(str(quote).split())
        if not normalized_quote:
            raise ValueError("Select text in the PDF before creating a highlight")
        if len(normalized_quote) > 20_000:
            raise ValueError("The selected passage is too long to highlight")
        request_id = str(client_request_id).strip()
        if not request_id or len(request_id) > 128:
            raise ValueError("The highlight request identifier is invalid")
        normalized_fragments = self._validate_fragments(fragments, page_count)
        existing = self.connection.execute(
            "SELECT id FROM paper_highlights WHERE client_request_id = ?",
            (request_id,),
        ).fetchone()
        if existing:
            return self.get_highlight(int(paper["id"]), int(existing["id"]))

        now = isoformat()
        with transaction(self.connection):
            cursor = self.connection.execute(
                """
                INSERT INTO paper_highlights(
                    paper_id, document_id, quote, client_request_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (paper["id"], document["id"], normalized_quote, request_id, now, now),
            )
            highlight_id = int(cursor.lastrowid)
            for ordinal, fragment in enumerate(normalized_fragments):
                self.connection.execute(
                    """
                    INSERT INTO paper_highlight_fragments(
                        highlight_id, page_number, ordinal, page_rotation, quads_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        highlight_id,
                        fragment["page_number"],
                        ordinal,
                        fragment["page_rotation"],
                        json.dumps(fragment["quads"], separators=(",", ":")),
                    ),
                )
            self.connection.execute(
                """
                UPDATE documents
                SET pdf_fingerprint = COALESCE(NULLIF(?, ''), pdf_fingerprint),
                    page_count = COALESCE(?, page_count)
                WHERE id = ?
                """,
                (str(pdf_fingerprint).strip()[:256], page_count, document_id),
            )
        return self.get_highlight(int(paper["id"]), highlight_id)

    def get_highlight(self, paper_id: int, highlight_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            """
            SELECT ph.*, d.revision_label, d.stale
            FROM paper_highlights ph
            JOIN documents d ON d.id = ph.document_id
            WHERE ph.id = ? AND ph.paper_id = ?
            """,
            (highlight_id, paper_id),
        ).fetchone()
        if not row:
            raise LookupError("Highlight not found")
        highlight = dict(row)
        highlight["fragments"] = self._fragments(highlight_id)
        return highlight

    def update_highlight_note(
        self,
        arxiv_id: str,
        highlight_id: int,
        *,
        note: str,
        revision: int,
    ) -> dict[str, Any]:
        paper = self._paper(arxiv_id)
        normalized_note = str(note)
        if len(normalized_note) > 50_000:
            raise ValueError("Highlight notes must be 50,000 characters or fewer")
        cursor = self.connection.execute(
            """
            UPDATE paper_highlights
            SET note = ?, revision = revision + 1, updated_at = ?
            WHERE id = ? AND paper_id = ? AND revision = ?
            """,
            (normalized_note, isoformat(), highlight_id, paper["id"], revision),
        )
        if cursor.rowcount == 0:
            if not self.connection.execute(
                "SELECT 1 FROM paper_highlights WHERE id = ? AND paper_id = ?",
                (highlight_id, paper["id"]),
            ).fetchone():
                raise LookupError("Highlight not found")
            raise AnnotationConflict("This highlight was updated in another tab")
        return self.get_highlight(int(paper["id"]), highlight_id)

    def delete_highlight(self, arxiv_id: str, highlight_id: int) -> None:
        paper = self._paper(arxiv_id)
        cursor = self.connection.execute(
            "DELETE FROM paper_highlights WHERE id = ? AND paper_id = ?",
            (highlight_id, paper["id"]),
        )
        if cursor.rowcount == 0:
            raise LookupError("Highlight not found")

    def upsert_paper_note(self, arxiv_id: str, *, body: str, revision: int) -> dict[str, Any]:
        paper = self._paper(arxiv_id)
        normalized_body = str(body)
        if len(normalized_body) > 50_000:
            raise ValueError("Paper notes must be 50,000 characters or fewer")
        existing = self.connection.execute(
            "SELECT revision FROM paper_notes WHERE paper_id = ?", (paper["id"],)
        ).fetchone()
        now = isoformat()
        if not existing:
            if revision not in {0, 1}:
                raise AnnotationConflict("This paper note was updated in another tab")
            self.connection.execute(
                """
                INSERT INTO paper_notes(paper_id, body, revision, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                """,
                (paper["id"], normalized_body, now, now),
            )
        else:
            cursor = self.connection.execute(
                """
                UPDATE paper_notes
                SET body = ?, revision = revision + 1, updated_at = ?
                WHERE paper_id = ? AND revision = ?
                """,
                (normalized_body, now, paper["id"], revision),
            )
            if cursor.rowcount == 0:
                raise AnnotationConflict("This paper note was updated in another tab")
        row = self.connection.execute(
            "SELECT body, revision, created_at, updated_at FROM paper_notes WHERE paper_id = ?",
            (paper["id"],),
        ).fetchone()
        return dict(row)

    def library(
        self,
        *,
        query: str = "",
        group_id: int | None = None,
        notes_only: bool = False,
        sort: str = "updated",
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions = [
            "(EXISTS (SELECT 1 FROM paper_highlights h WHERE h.paper_id = p.id) "
            "OR COALESCE(pn.body, '') != '')"
        ]
        params: list[object] = []
        if group_id is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM paper_group_memberships pgm "
                "WHERE pgm.paper_id = p.id AND pgm.group_id = ?)"
            )
            params.append(group_id)
        if notes_only:
            conditions.append(
                "(COALESCE(pn.body, '') != '' OR EXISTS (SELECT 1 FROM paper_highlights hn "
                "WHERE hn.paper_id = p.id AND hn.note != ''))"
            )
        expression = _fts_expression(query)
        if query and not expression:
            conditions.append("0 = 1")
        elif expression:
            conditions.append(
                "(p.id IN (SELECT paper_id FROM paper_highlights WHERE id IN "
                "(SELECT rowid FROM paper_highlight_fts WHERE paper_highlight_fts MATCH ?)) "
                "OR p.id IN (SELECT rowid FROM paper_note_fts WHERE paper_note_fts MATCH ?) "
                "OR p.id IN (SELECT rowid FROM paper_fts WHERE paper_fts MATCH ?))"
            )
            params.extend([expression, expression, expression])
        where = " AND ".join(conditions)
        total = int(
            self.connection.execute(
                f"SELECT COUNT(*) FROM papers p LEFT JOIN paper_notes pn ON pn.paper_id = p.id WHERE {where}",  # noqa: S608
                params,
            ).fetchone()[0]
        )
        activity_sql = """
            CASE WHEN COALESCE(pn.updated_at, '') >= COALESCE(
                (SELECT MAX(hu.updated_at) FROM paper_highlights hu WHERE hu.paper_id = p.id), ''
            ) THEN COALESCE(pn.updated_at, '') ELSE COALESCE(
                (SELECT MAX(hu.updated_at) FROM paper_highlights hu WHERE hu.paper_id = p.id), ''
            ) END
        """
        if sort == "paper":
            order_sql = "p.title COLLATE NOCASE ASC"
        elif sort == "newest":
            order_sql = "COALESCE((SELECT MAX(hc.created_at) FROM paper_highlights hc WHERE hc.paper_id = p.id), pn.created_at) DESC"
        else:
            order_sql = f"{activity_sql} DESC"  # noqa: S608
        rows = self.connection.execute(
            f"""
            SELECT p.id, p.arxiv_id, p.title, p.source_kind, p.source_name,
                   pn.body AS paper_note_body, pn.revision AS paper_note_revision,
                   pn.updated_at AS paper_note_updated_at,
                   {activity_sql} AS activity_at
            FROM papers p
            LEFT JOIN paper_notes pn ON pn.paper_id = p.id
            WHERE {where}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,  # noqa: S608
            [*params, limit, offset],
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["highlights"] = self._highlights(paper_id=int(row["id"]), notes_only=notes_only)
            output.append(item)
        return output, total

    def _paper(self, arxiv_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT id, arxiv_id, title FROM papers WHERE arxiv_id = ?", (arxiv_id,)
        ).fetchone()
        if not row:
            raise LookupError(f"Paper {arxiv_id} not found")
        return row

    def _document(self, paper_id: int, document_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM documents WHERE id = ? AND paper_id = ? AND status = 'ready'",
            (document_id, paper_id),
        ).fetchone()
        if not row:
            raise LookupError("Document not found")
        return dict(row)

    def _highlights(
        self,
        *,
        paper_id: int,
        document_id: int | None = None,
        notes_only: bool = False,
    ) -> list[dict[str, Any]]:
        params: list[object] = [paper_id]
        condition = "ph.paper_id = ?"
        if document_id is not None:
            condition += " AND ph.document_id = ?"
            params.append(document_id)
        if notes_only:
            condition += " AND ph.note != ''"
        rows = self.connection.execute(
            f"""
            SELECT ph.*, d.revision_label, d.stale
            FROM paper_highlights ph
            JOIN documents d ON d.id = ph.document_id
            WHERE {condition}
            ORDER BY ph.created_at DESC
            """,  # noqa: S608
            params,
        ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["fragments"] = self._fragments(int(row["id"]))
            item["page_start"] = min(
                (fragment["page_number"] for fragment in item["fragments"]), default=1
            )
            item["page_end"] = max(
                (fragment["page_number"] for fragment in item["fragments"]), default=1
            )
            output.append(item)
        return output

    def _fragments(self, highlight_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT page_number, ordinal, page_rotation, quads_json
            FROM paper_highlight_fragments
            WHERE highlight_id = ? ORDER BY ordinal
            """,
            (highlight_id,),
        ).fetchall()
        return [
            {
                "page_number": int(row["page_number"]),
                "ordinal": int(row["ordinal"]),
                "page_rotation": int(row["page_rotation"]),
                "quads": json.loads(row["quads_json"]),
            }
            for row in rows
        ]

    @staticmethod
    def _validate_fragments(
        fragments: list[dict[str, Any]], page_count: int | None
    ) -> list[dict[str, Any]]:
        if not isinstance(fragments, list) or not 1 <= len(fragments) <= 100:
            raise ValueError("A highlight must contain between 1 and 100 page fragments")
        output = []
        for fragment in fragments:
            if not isinstance(fragment, dict):
                raise ValueError("The highlight geometry is invalid")
            try:
                page_number = int(fragment.get("page_number"))
                rotation = int(fragment.get("page_rotation", 0)) % 360
            except (TypeError, ValueError) as error:
                raise ValueError("The highlight page is invalid") from error
            if page_number < 1 or (page_count and page_number > page_count):
                raise ValueError("The highlight page is outside this PDF")
            if rotation not in {0, 90, 180, 270}:
                raise ValueError("The highlight page rotation is invalid")
            raw_quads = fragment.get("quads")
            if not isinstance(raw_quads, list) or not 1 <= len(raw_quads) <= 256:
                raise ValueError("The highlight must contain visible text lines")
            quads = []
            for raw_quad in raw_quads:
                if not isinstance(raw_quad, list) or len(raw_quad) != 4:
                    raise ValueError("The highlight line geometry is invalid")
                try:
                    quad = [round(float(value), 4) for value in raw_quad]
                except (TypeError, ValueError) as error:
                    raise ValueError("The highlight line geometry is invalid") from error
                if any(not math.isfinite(value) or abs(value) > 100_000 for value in quad):
                    raise ValueError("The highlight line geometry is invalid")
                if quad[2] <= quad[0] or quad[3] <= quad[1]:
                    raise ValueError("The highlight line has no visible area")
                quads.append(quad)
            output.append(
                {
                    "page_number": page_number,
                    "page_rotation": rotation,
                    "quads": quads,
                }
            )
        output.sort(key=lambda item: item["page_number"])
        return output

    @staticmethod
    def _empty_note() -> dict[str, Any]:
        return {"body": "", "revision": 0, "created_at": None, "updated_at": None}
