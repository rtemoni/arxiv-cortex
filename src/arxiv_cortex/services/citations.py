from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

import requests

from arxiv_cortex.db import database_connection, transaction
from arxiv_cortex.utils import isoformat, utcnow

SEMANTIC_SCHOLAR_BATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/batch"


class CitationSourceError(RuntimeError):
    pass


class CitationSource(Protocol):
    def fetch(self, identifiers: Sequence[str]) -> list[dict[str, Any] | None]: ...


class SemanticScholarSource:
    def __init__(
        self,
        *,
        api_key: str = "",
        timeout_seconds: float = 20,
        session: requests.Session | None = None,
    ):
        self.api_key = api_key.strip()
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()

    def fetch(self, identifiers: Sequence[str]) -> list[dict[str, Any] | None]:
        if not identifiers:
            return []
        headers = {
            "Accept": "application/json",
            "User-Agent": "Arxiv-Cortex/0.1 citation metadata",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        try:
            response = self.session.post(
                SEMANTIC_SCHOLAR_BATCH_URL,
                params={"fields": "paperId,citationCount"},
                json={"ids": list(identifiers)},
                headers=headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            raise CitationSourceError(f"Semantic Scholar citation lookup failed: {error}") from error
        if not isinstance(payload, list) or len(payload) != len(identifiers):
            raise CitationSourceError("Semantic Scholar returned an unexpected citation response")
        return [item if isinstance(item, dict) else None for item in payload]


class CitationService:
    def __init__(self, database_path: str | Path, source: CitationSource):
        self.database_path = database_path
        self.source = source

    def refresh_saved(self, *, max_age_days: int = 7, batch_size: int = 500) -> int:
        cutoff = isoformat(utcnow() - timedelta(days=max_age_days))
        with database_connection(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT p.id, p.arxiv_id, p.source_kind, p.doi
                FROM papers p
                JOIN paper_state ps ON ps.paper_id = p.id
                WHERE ps.saved_at IS NOT NULL
                  AND (p.citation_updated_at IS NULL OR p.citation_updated_at <= ?)
                ORDER BY p.id
                """,
                (cutoff,),
            ).fetchall()

        candidates = [
            (int(row["id"]), identifier)
            for row in rows
            if (identifier := self._identifier(row))
        ]
        refreshed = 0
        for start in range(0, len(candidates), max(1, min(batch_size, 500))):
            batch = candidates[start : start + max(1, min(batch_size, 500))]
            results = self.source.fetch([identifier for _paper_id, identifier in batch])
            if len(results) != len(batch):
                raise CitationSourceError("Citation source returned a mismatched batch")
            refreshed += self._store_batch(batch, results)
        return refreshed

    def _store_batch(
        self,
        batch: list[tuple[int, str]],
        results: list[dict[str, Any] | None],
    ) -> int:
        now = isoformat()
        matched = 0
        with database_connection(self.database_path) as connection, transaction(connection):
            for (paper_id, _identifier), result in zip(batch, results, strict=True):
                citation_count = result.get("citationCount") if result else None
                if citation_count is not None:
                    if not isinstance(citation_count, int) or citation_count < 0:
                        raise CitationSourceError("Citation source returned an invalid count")
                    matched += 1
                semantic_scholar_id = result.get("paperId") if result else None
                connection.execute(
                    """
                    UPDATE papers
                    SET citation_count = COALESCE(?, citation_count),
                        semantic_scholar_id = COALESCE(?, semantic_scholar_id),
                        citation_updated_at = ?
                    WHERE id = ?
                    """,
                    (citation_count, semantic_scholar_id, now, paper_id),
                )
        return matched

    @staticmethod
    def _identifier(row: sqlite3.Row) -> str | None:
        if row["source_kind"] == "arxiv":
            return f"ARXIV:{re.sub(r'v\d+$', '', str(row['arxiv_id']))}"
        doi = str(row["doi"] or "").strip()
        return f"DOI:{doi}" if doi else None
