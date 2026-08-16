from __future__ import annotations

import re
import sqlite3
from datetime import timedelta
from typing import Any

from arxiv_cortex.db import transaction
from arxiv_cortex.utils import isoformat, utcnow

KEYWORD_SEPARATOR_RE = re.compile(r"[,;\n]+")
QUERY_TERM_RE = re.compile(r"[\w-]+", re.UNICODE)


def normalize_keyword_phrases(value: str) -> list[str]:
    phrases: list[str] = []
    seen: set[str] = set()
    for raw_phrase in KEYWORD_SEPARATOR_RE.split(value):
        phrase = " ".join(raw_phrase.split())
        key = phrase.casefold()
        if not phrase or key in seen:
            continue
        if len(phrase) > 80:
            raise ValueError("Each keyword phrase must be 80 characters or fewer")
        seen.add(key)
        phrases.append(phrase)
    if not phrases:
        raise ValueError("Add at least one keyword phrase")
    if len(phrases) > 24:
        raise ValueError("A research tag can contain at most 24 keyword phrases")
    return phrases


class SearchTagService:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def list(self, followed_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE enabled = 1" if followed_only else ""
        rows = self.connection.execute(
            f"SELECT * FROM search_tags {where} ORDER BY name COLLATE NOCASE"  # noqa: S608
        )
        return [self._hydrate(row) for row in rows]

    def get(self, tag_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM search_tags WHERE id = ?", (tag_id,)
        ).fetchone()
        return self._hydrate(row) if row else None

    def create(
        self,
        name: str,
        description: str,
        keywords: str,
        *,
        followed: bool = False,
        backfill_days: int = 90,
    ) -> dict[str, Any]:
        values = self.validate_input(name, description, keywords, backfill_days)
        now = isoformat()
        backfill_from = isoformat(utcnow() - timedelta(days=backfill_days))
        try:
            with transaction(self.connection):
                cursor = self.connection.execute(
                    """
                    INSERT INTO search_tags(
                        name, description, keywords, enabled, backfill_from,
                        backfill_complete, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (*values, int(followed), backfill_from, now, now),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("A research tag with that name already exists") from error
        return self.get(int(cursor.lastrowid))  # type: ignore[return-value]

    def update(
        self,
        tag_id: int,
        name: str,
        description: str,
        keywords: str,
        *,
        followed: bool | None = None,
        backfill_days: int = 90,
    ) -> dict[str, Any]:
        values = self.validate_input(name, description, keywords, backfill_days)
        existing = self.get(tag_id)
        if not existing:
            raise LookupError(f"Research tag {tag_id} was not found")
        next_followed = existing["followed"] if followed is None else followed
        query_changed = values[2] != existing["keywords"]
        newly_followed = bool(next_followed and not existing["followed"])
        reset_feed = bool(query_changed or newly_followed)
        backfill_from = isoformat(utcnow() - timedelta(days=backfill_days))
        try:
            with transaction(self.connection):
                cursor = self.connection.execute(
                    """
                    UPDATE search_tags
                    SET name = ?, description = ?, keywords = ?, enabled = ?,
                        backfill_from = CASE WHEN ? THEN ? ELSE backfill_from END,
                        backfill_complete = CASE WHEN ? THEN 0 ELSE backfill_complete END,
                        last_updated_watermark = CASE WHEN ? THEN NULL ELSE last_updated_watermark END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        *values,
                        int(next_followed),
                        int(reset_feed),
                        backfill_from,
                        int(reset_feed),
                        int(reset_feed),
                        isoformat(),
                        tag_id,
                    ),
                )
                if query_changed:
                    self.connection.execute(
                        "DELETE FROM paper_search_tags WHERE tag_id = ?", (tag_id,)
                    )
        except sqlite3.IntegrityError as error:
            raise ValueError("A research tag with that name already exists") from error
        if cursor.rowcount != 1:
            raise LookupError(f"Research tag {tag_id} was not found")
        return self.get(tag_id)  # type: ignore[return-value]

    def set_followed(
        self, tag_ids: list[int], *, backfill_days: int = 90
    ) -> list[int]:
        self._validate_backfill_days(backfill_days)
        normalized = self.validate_ids(tag_ids)
        now = isoformat()
        backfill_from = isoformat(utcnow() - timedelta(days=backfill_days))
        with transaction(self.connection):
            if normalized:
                placeholders = ",".join("?" for _ in normalized)
                self.connection.execute(
                    f"""
                    UPDATE search_tags
                    SET backfill_from = CASE WHEN enabled = 0 THEN ? ELSE backfill_from END,
                        backfill_complete = CASE WHEN enabled = 0 THEN 0 ELSE backfill_complete END,
                        last_updated_watermark = CASE WHEN enabled = 0 THEN NULL ELSE last_updated_watermark END,
                        enabled = 1, updated_at = ?
                    WHERE id IN ({placeholders})
                    """,  # noqa: S608
                    (backfill_from, now, *normalized),
                )
                self.connection.execute(
                    f"UPDATE search_tags SET enabled = 0, updated_at = ? WHERE id NOT IN ({placeholders})",  # noqa: S608
                    (now, *normalized),
                )
            else:
                self.connection.execute(
                    "UPDATE search_tags SET enabled = 0, updated_at = ?", (now,)
                )
        return normalized

    def has_followed(self) -> bool:
        return bool(
            self.connection.execute(
                "SELECT 1 FROM search_tags WHERE enabled = 1 LIMIT 1"
            ).fetchone()
        )

    def delete(self, tag_id: int) -> None:
        with transaction(self.connection):
            cursor = self.connection.execute("DELETE FROM search_tags WHERE id = ?", (tag_id,))
        if cursor.rowcount != 1:
            raise LookupError(f"Research tag {tag_id} was not found")

    @staticmethod
    def validate_input(
        name: str, description: str, keywords: str, backfill_days: int = 90
    ) -> tuple[str, str, str]:
        SearchTagService._validate_backfill_days(backfill_days)
        normalized_name = " ".join(name.split())
        normalized_description = " ".join(description.split())
        if not 1 <= len(normalized_name) <= 60:
            raise ValueError("Tag name must be between 1 and 60 characters")
        if len(normalized_description) > 240:
            raise ValueError("Tag description must be 240 characters or fewer")
        normalized_keywords = "\n".join(normalize_keyword_phrases(keywords))
        return normalized_name, normalized_description, normalized_keywords

    def validate_ids(self, tag_ids: list[int]) -> list[int]:
        normalized = sorted({int(tag_id) for tag_id in tag_ids if int(tag_id) > 0})
        if not normalized:
            return []
        placeholders = ",".join("?" for _ in normalized)
        found = {
            int(row["id"])
            for row in self.connection.execute(
                f"SELECT id FROM search_tags WHERE id IN ({placeholders})",  # noqa: S608
                normalized,
            )
        }
        missing = sorted(set(normalized) - found)
        if missing:
            raise ValueError(f"Unknown research tags: {', '.join(map(str, missing))}")
        return normalized

    @staticmethod
    def _validate_backfill_days(backfill_days: int) -> None:
        if not 1 <= backfill_days <= 3650:
            raise ValueError("Backfill must be between 1 and 3650 days")

    @staticmethod
    def arxiv_query(phrases: list[str]) -> str:
        expressions: list[str] = []
        terms_used = 0
        for phrase in phrases[:24]:
            terms = QUERY_TERM_RE.findall(phrase)[:8]
            if not terms or terms_used + len(terms) > 48:
                break
            expressions.append(f'all:"{" ".join(terms)}"')
            terms_used += len(terms)
        if not expressions:
            raise ValueError("Add at least one searchable keyword phrase")
        return "(" + " OR ".join(expressions) + ")"

    @staticmethod
    def _hydrate(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        phrases = normalize_keyword_phrases(item["keywords"])
        item["phrases"] = phrases
        item["query"] = ", ".join(phrases)
        item["phrase_count"] = len(phrases)
        item["followed"] = bool(item.get("enabled"))
        item["arxiv_query"] = SearchTagService.arxiv_query(phrases)
        return item
