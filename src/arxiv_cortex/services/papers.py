from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from arxiv_cortex.db import transaction
from arxiv_cortex.utils import isoformat, utcnow

WORD_RE = re.compile(r"[\w-]+", re.UNICODE)
QUERY_GROUP_RE = re.compile(r"[,;\n]+")
PaperState = Literal["saved", "read", "unread", "dismissed"]
ReadStatus = Literal["read", "unread"]

ACTIVE_SOURCE_SQL = """
(
    EXISTS (
        SELECT 1 FROM paper_categories pc
        JOIN feed_subscriptions fs ON fs.category = pc.category AND fs.enabled = 1
        WHERE pc.paper_id = p.id
    )
    OR EXISTS (
        SELECT 1 FROM paper_search_tags pst
        JOIN search_tags st ON st.id = pst.tag_id AND st.enabled = 1
        WHERE pst.paper_id = p.id
    )
)
"""


@dataclass(slots=True)
class PaperQuery:
    query: str = ""
    category: str = ""
    days: int = 0
    state: PaperState | None = None
    read_status: ReadStatus | None = None
    group_id: int | None = None
    hide_read: bool = False
    exclude_interacted: bool = False
    active_categories_only: bool = True
    sort: str = "newest"
    offset: int = 0
    limit: int = 25


@dataclass(slots=True)
class Page:
    items: list[dict[str, Any]]
    offset: int
    limit: int
    has_next: bool
    total: int


def _fts_expression(value: str) -> str:
    groups = [group for group in QUERY_GROUP_RE.split(value) if group.strip()]
    if len(groups) <= 1:
        terms = WORD_RE.findall(value)
        return " AND ".join(f'"{term}"' for term in terms[:20])

    expressions = []
    terms_used = 0
    for group in groups[:24]:
        terms = WORD_RE.findall(group)[:8]
        if not terms:
            continue
        if terms_used + len(terms) > 48:
            break
        expressions.append(f'("{" ".join(terms)}")')
        terms_used += len(terms)
        if terms_used >= 48:
            break
    return " OR ".join(expressions)


class PaperService:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0])

    def embedding_count(self, model_id: str) -> int:
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM paper_embeddings WHERE model_id = ?", (model_id,)
            ).fetchone()[0]
        )

    def list(self, query: PaperQuery) -> Page:
        where, params, from_sql, score_sql = self._query_parts(query)
        order_sql = "fts_score ASC, p.published_at DESC" if query.query else "p.published_at DESC"
        if query.sort == "oldest":
            order_sql = "p.published_at ASC"

        count_row = self.connection.execute(
            f"SELECT COUNT(DISTINCT p.id) {from_sql} WHERE {where}",  # noqa: S608
            params,
        ).fetchone()
        rows = self.connection.execute(
            f"""
            SELECT p.*, ps.saved_at, ps.read_at, ps.dismissed_at, ps.last_opened_at,
                   {score_sql} AS fts_score
            {from_sql}
            WHERE {where}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,  # noqa: S608
            [*params, query.limit + 1, query.offset],
        ).fetchall()
        has_next = len(rows) > query.limit
        rows = rows[: query.limit]
        return Page(
            items=self._hydrate(rows),
            offset=query.offset,
            limit=query.limit,
            has_next=has_next,
            total=int(count_row[0]),
        )

    def candidate_ids(
        self,
        *,
        category: str = "",
        days: int = 0,
        exclude_interacted: bool = False,
        active_categories_only: bool = True,
    ) -> set[int]:
        conditions = ["1 = 1"]
        params: list[object] = []
        if category:
            conditions.append(
                "EXISTS (SELECT 1 FROM paper_categories pc WHERE pc.paper_id = p.id AND pc.category = ?)"
            )
            params.append(category)
        if active_categories_only:
            conditions.append(ACTIVE_SOURCE_SQL)
        if days > 0:
            conditions.append("p.published_at >= ?")
            params.append(isoformat(utcnow() - timedelta(days=days)))
        if exclude_interacted:
            conditions.append(
                "NOT EXISTS (SELECT 1 FROM paper_state ps WHERE ps.paper_id = p.id AND "
                "(ps.saved_at IS NOT NULL OR ps.read_at IS NOT NULL OR ps.dismissed_at IS NOT NULL))"
            )
        rows = self.connection.execute(
            f"SELECT p.id FROM papers p WHERE {' AND '.join(conditions)}",  # noqa: S608
            params,
        ).fetchall()
        return {int(row["id"]) for row in rows}

    def get(self, arxiv_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT p.*, ps.saved_at, ps.read_at, ps.dismissed_at, ps.last_opened_at,
                   NULL AS fts_score
            FROM papers p
            LEFT JOIN paper_state ps ON ps.paper_id = p.id
            WHERE p.arxiv_id = ?
            """,
            (arxiv_id,),
        ).fetchone()
        return self._hydrate([row])[0] if row else None

    def get_by_database_ids(self, paper_ids: list[int]) -> list[dict[str, Any]]:
        if not paper_ids:
            return []
        placeholders = ",".join("?" for _ in paper_ids)
        rows = self.connection.execute(
            f"""
            SELECT p.*, ps.saved_at, ps.read_at, ps.dismissed_at, ps.last_opened_at,
                   NULL AS fts_score
            FROM papers p
            LEFT JOIN paper_state ps ON ps.paper_id = p.id
            WHERE p.id IN ({placeholders})
            """,  # noqa: S608
            paper_ids,
        ).fetchall()
        items = {int(item["database_id"]): item for item in self._hydrate(rows)}
        return [items[paper_id] for paper_id in paper_ids if paper_id in items]

    def get_by_arxiv_ids(self, arxiv_ids: list[str]) -> list[dict[str, Any]]:
        if not arxiv_ids:
            return []
        placeholders = ",".join("?" for _ in arxiv_ids)
        rows = self.connection.execute(
            f"""
            SELECT p.*, ps.saved_at, ps.read_at, ps.dismissed_at, ps.last_opened_at,
                   NULL AS fts_score
            FROM papers p
            LEFT JOIN paper_state ps ON ps.paper_id = p.id
            WHERE p.arxiv_id IN ({placeholders})
            """,  # noqa: S608
            arxiv_ids,
        ).fetchall()
        items = {item["arxiv_id"]: item for item in self._hydrate(rows)}
        return [items[arxiv_id] for arxiv_id in arxiv_ids if arxiv_id in items]

    def state_ids(self, column: str) -> list[int]:
        if column not in {"saved_at", "read_at", "dismissed_at"}:
            raise ValueError("Unsupported paper state")
        rows = self.connection.execute(
            f"SELECT paper_id FROM paper_state WHERE {column} IS NOT NULL"  # noqa: S608
        ).fetchall()
        return [int(row["paper_id"]) for row in rows]

    def set_saved(self, arxiv_id: str, saved: bool) -> dict[str, Any]:
        paper_id = self._database_id(arxiv_id)
        now = isoformat()
        with transaction(self.connection):
            self._ensure_state(paper_id)
            if saved:
                self.connection.execute(
                    "UPDATE paper_state SET saved_at = ?, dismissed_at = NULL WHERE paper_id = ?",
                    (now, paper_id),
                )
            else:
                self.connection.execute(
                    "UPDATE paper_state SET saved_at = NULL WHERE paper_id = ?", (paper_id,)
                )
                self.connection.execute(
                    "DELETE FROM paper_group_memberships WHERE paper_id = ?", (paper_id,)
                )
        return self.get(arxiv_id)  # type: ignore[return-value]

    def set_read(self, arxiv_id: str, read: bool) -> dict[str, Any]:
        paper_id = self._database_id(arxiv_id)
        with transaction(self.connection):
            self._ensure_state(paper_id)
            self.connection.execute(
                "UPDATE paper_state SET read_at = ? WHERE paper_id = ?",
                (isoformat() if read else None, paper_id),
            )
        return self.get(arxiv_id)  # type: ignore[return-value]

    def set_dismissed(self, arxiv_id: str, dismissed: bool) -> dict[str, Any]:
        paper_id = self._database_id(arxiv_id)
        with transaction(self.connection):
            self._ensure_state(paper_id)
            if dismissed:
                self.connection.execute(
                    "UPDATE paper_state SET dismissed_at = ?, saved_at = NULL WHERE paper_id = ?",
                    (isoformat(), paper_id),
                )
                self.connection.execute(
                    "DELETE FROM paper_group_memberships WHERE paper_id = ?", (paper_id,)
                )
            else:
                self.connection.execute(
                    "UPDATE paper_state SET dismissed_at = NULL WHERE paper_id = ?", (paper_id,)
                )
        return self.get(arxiv_id)  # type: ignore[return-value]

    def mark_opened(self, arxiv_id: str) -> dict[str, Any]:
        paper_id = self._database_id(arxiv_id)
        with transaction(self.connection):
            self._ensure_state(paper_id)
            self.connection.execute(
                "UPDATE paper_state SET last_opened_at = ? WHERE paper_id = ?",
                (isoformat(), paper_id),
            )
        return self.get(arxiv_id)  # type: ignore[return-value]

    def _database_id(self, arxiv_id: str) -> int:
        row = self.connection.execute(
            "SELECT id FROM papers WHERE arxiv_id = ?", (arxiv_id,)
        ).fetchone()
        if not row:
            raise LookupError(f"Paper {arxiv_id} not found")
        return int(row["id"])

    def _ensure_state(self, paper_id: int) -> None:
        self.connection.execute("INSERT OR IGNORE INTO paper_state(paper_id) VALUES (?)", (paper_id,))

    def _query_parts(self, query: PaperQuery) -> tuple[str, list[object], str, str]:
        conditions = ["1 = 1"]
        params: list[object] = []
        fts = _fts_expression(query.query)
        if query.query and not fts:
            conditions.append("0 = 1")
        if fts:
            from_sql = (
                "FROM paper_fts JOIN papers p ON p.id = paper_fts.rowid "
                "LEFT JOIN paper_state ps ON ps.paper_id = p.id"
            )
            conditions.append("paper_fts MATCH ?")
            params.append(fts)
            score_sql = "bm25(paper_fts, 8.0, 4.0, 1.0)"
        else:
            from_sql = "FROM papers p LEFT JOIN paper_state ps ON ps.paper_id = p.id"
            score_sql = "NULL"
        if query.category:
            conditions.append(
                "EXISTS (SELECT 1 FROM paper_categories pc WHERE pc.paper_id = p.id AND pc.category = ?)"
            )
            params.append(query.category)
        if query.group_id is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM paper_group_memberships pgm "
                "WHERE pgm.paper_id = p.id AND pgm.group_id = ?)"
            )
            params.append(query.group_id)
        if query.active_categories_only:
            conditions.append(ACTIVE_SOURCE_SQL)
        if query.days > 0:
            conditions.append(
                "(p.source_kind = 'arxiv' OR p.published_at != p.fetched_at) "
                "AND p.published_at >= ?"
            )
            params.append(isoformat(utcnow() - timedelta(days=query.days)))
        if query.state == "saved":
            conditions.append("ps.saved_at IS NOT NULL")
        elif query.state == "read":
            conditions.append("ps.read_at IS NOT NULL")
        elif query.state == "unread":
            conditions.append("ps.read_at IS NULL")
        elif query.state == "dismissed":
            conditions.append("ps.dismissed_at IS NOT NULL")
        if query.read_status == "read":
            conditions.append("ps.read_at IS NOT NULL")
        elif query.read_status == "unread":
            conditions.append("ps.read_at IS NULL")
        if query.hide_read:
            conditions.append("ps.read_at IS NULL")
        if query.exclude_interacted:
            conditions.append(
                "(ps.paper_id IS NULL OR (ps.saved_at IS NULL AND ps.read_at IS NULL "
                "AND ps.dismissed_at IS NULL))"
            )
        return " AND ".join(conditions), params, from_sql, score_sql

    def _hydrate(self, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        if not rows:
            return []
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        author_rows = self.connection.execute(
            f"SELECT paper_id, name FROM paper_authors WHERE paper_id IN ({placeholders}) ORDER BY paper_id, position",  # noqa: S608
            ids,
        ).fetchall()
        category_rows = self.connection.execute(
            f"SELECT paper_id, category FROM paper_categories WHERE paper_id IN ({placeholders}) ORDER BY paper_id, category",  # noqa: S608
            ids,
        ).fetchall()
        authors: dict[int, list[str]] = {paper_id: [] for paper_id in ids}
        categories: dict[int, list[str]] = {paper_id: [] for paper_id in ids}
        for author in author_rows:
            authors[int(author["paper_id"])].append(author["name"])
        for category in category_rows:
            categories[int(category["paper_id"])].append(category["category"])

        output = []
        for row in rows:
            paper_id = int(row["id"])
            score = row["fts_score"]
            item: dict[str, Any] = {
                "database_id": paper_id,
                "arxiv_id": row["arxiv_id"],
                "version": int(row["latest_version"]),
                "title": row["title"],
                "abstract": row["abstract"],
                "authors": authors[paper_id],
                "categories": categories[paper_id],
                "primary_category": row["primary_category"],
                "published_at": row["published_at"],
                "updated_at": row["updated_at"],
                "doi": row["doi"],
                "journal_ref": row["journal_ref"],
                "comment": row["comment"],
                "license": row["license_url"],
                "links": {
                    "abstract": row["abstract_url"],
                    "webpage": row["abstract_url"],
                    "pdf": row["pdf_url"],
                },
                "source": {
                    "kind": row["source_kind"],
                    "identifier": row["source_identifier"] or row["arxiv_id"],
                    "name": row["source_name"],
                    "venue": row["venue"],
                    "date_known": row["published_at"] != row["fetched_at"],
                },
                "state": {
                    "saved": bool(row["saved_at"]),
                    "read": bool(row["read_at"]),
                    "dismissed": bool(row["dismissed_at"]),
                    "last_opened_at": row["last_opened_at"],
                },
            }
            if score is not None:
                # SQLite's BM25 is smaller-is-better and commonly negative. Keep
                # the SQL ordering, but expose a friendlier higher-is-better score.
                item["score"] = -float(score)
            output.append(item)
        return output
