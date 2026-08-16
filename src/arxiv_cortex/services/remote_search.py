from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from arxiv_cortex.db import transaction
from arxiv_cortex.services.arxiv_sync import (
    ArxivSourcePage,
    ArxivSyncService,
    FeedQuery,
    result_to_record,
)
from arxiv_cortex.services.papers import PaperService
from arxiv_cortex.utils import utcnow

LOGGER = logging.getLogger(__name__)
SEARCH_TERM_RE = re.compile(r"[\w-]+", re.UNICODE)
CATEGORY_RE = re.compile(r"[A-Za-z-]+(?:\.[A-Za-z-]+)?")


class RemoteSearchSource(Protocol):
    def search_page(
        self,
        query: FeedQuery,
        *,
        offset: int,
        limit: int,
        cutoff=None,
    ) -> ArxivSourcePage: ...


@dataclass(slots=True)
class RemoteSearchResult:
    items: list[dict[str, Any]]
    total: int
    has_next: bool


def arxiv_search_expression(query: str, category: str = "") -> str:
    terms = SEARCH_TERM_RE.findall(query)[:20]
    if not terms:
        raise ValueError("Enter at least one word to search all of arXiv")
    if category and not CATEGORY_RE.fullmatch(category):
        raise ValueError("Invalid arXiv category")
    expression = " AND ".join(f'all:"{term}"' for term in terms)
    if category:
        expression = f"({expression}) AND cat:{category}"
    return expression


class RemoteSearchService:
    def __init__(self, connection: sqlite3.Connection, source: RemoteSearchSource):
        self.connection = connection
        self.source = source

    def search(
        self,
        query: str,
        *,
        category: str = "",
        days: int = 0,
        offset: int = 0,
        limit: int = 25,
    ) -> RemoteSearchResult:
        expression = arxiv_search_expression(query, category)
        cutoff = utcnow() - timedelta(days=days) if days else None
        page = self.source.search_page(
            FeedQuery(label="Interactive search", expression=expression),
            offset=offset,
            limit=limit,
            cutoff=cutoff,
        )

        records = []
        for result in page.results:
            try:
                records.append(result_to_record(result))
            except (AttributeError, TypeError, ValueError) as error:
                LOGGER.warning("Skipping malformed interactive arXiv result: %s", error)

        with transaction(self.connection):
            for record in records:
                ArxivSyncService._upsert(self.connection, record)

        items = PaperService(self.connection).get_by_arxiv_ids(
            [record.arxiv_id for record in records]
        )
        for item in items:
            item["remote_result"] = True
        return RemoteSearchResult(
            items=items,
            total=page.total,
            has_next=page.has_next,
        )
