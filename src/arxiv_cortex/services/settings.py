from __future__ import annotations

import re
import sqlite3
from datetime import timedelta

from arxiv_cortex.db import transaction
from arxiv_cortex.utils import isoformat, utcnow

CATEGORY_RE = re.compile(r"^[a-z][a-z0-9-]*(?:\.[a-z0-9-]+)*$", re.IGNORECASE)
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class SettingsService:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self.connection.execute(
            """
            INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, isoformat()),
        )

    def subscriptions(self, enabled_only: bool = False) -> list[dict[str, object]]:
        where = "WHERE enabled = 1" if enabled_only else ""
        rows = self.connection.execute(
            f"SELECT * FROM feed_subscriptions {where} ORDER BY category"  # noqa: S608
        ).fetchall()
        return [dict(row) for row in rows]

    def update_subscriptions(self, categories: list[str], backfill_days: int = 90) -> list[str]:
        normalized = self.validate_subscriptions(categories, backfill_days)

        now = isoformat()
        backfill_from = isoformat(utcnow() - timedelta(days=backfill_days))
        existing = {
            row["category"]: bool(row["enabled"])
            for row in self.connection.execute("SELECT category, enabled FROM feed_subscriptions")
        }
        with transaction(self.connection):
            for category in normalized:
                if category not in existing:
                    self.connection.execute(
                        """
                        INSERT INTO feed_subscriptions(
                            category, enabled, backfill_from, backfill_complete, created_at
                        ) VALUES (?, 1, ?, 0, ?)
                        """,
                        (category, backfill_from, now),
                    )
                else:
                    self.connection.execute(
                        "UPDATE feed_subscriptions SET enabled = 1 WHERE category = ?",
                        (category,),
                    )
            if normalized:
                placeholders = ",".join("?" for _ in normalized)
                self.connection.execute(
                    f"UPDATE feed_subscriptions SET enabled = 0 WHERE category NOT IN ({placeholders})",  # noqa: S608
                    normalized,
                )
            else:
                self.connection.execute("UPDATE feed_subscriptions SET enabled = 0")
        return normalized

    @staticmethod
    def validate_subscriptions(categories: list[str], backfill_days: int = 90) -> list[str]:
        normalized = sorted({category.strip() for category in categories if category.strip()})
        invalid = [category for category in normalized if not CATEGORY_RE.fullmatch(category)]
        if invalid:
            raise ValueError(f"Invalid arXiv categories: {', '.join(invalid)}")
        if not 1 <= backfill_days <= 3650:
            raise ValueError("Backfill must be between 1 and 3650 days")
        return normalized

    def has_feed_sources(self) -> bool:
        return bool(
            self.connection.execute(
                """
                SELECT 1 FROM feed_subscriptions WHERE enabled = 1
                UNION ALL
                SELECT 1 FROM search_tags WHERE enabled = 1
                LIMIT 1
                """
            ).fetchone()
        )

    def update_runtime_settings(self, sync_time: str, recommendation_days: int) -> None:
        if not TIME_RE.fullmatch(sync_time):
            raise ValueError("Sync time must use 24-hour HH:MM format")
        if recommendation_days not in {7, 30, 90, 0}:
            raise ValueError("Recommendation window must be 7, 30, 90, or 0")
        with transaction(self.connection):
            self.set("sync_time", sync_time)
            self.set("recommendation_days", str(recommendation_days))
