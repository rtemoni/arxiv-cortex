from __future__ import annotations

import logging
import random
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

import feedparser
import requests

from arxiv_cortex.db import database_connection, transaction
from arxiv_cortex.services.search_tags import SearchTagService, normalize_keyword_phrases
from arxiv_cortex.utils import (
    canonicalize_arxiv_id,
    isoformat,
    metadata_hash,
    paper_content_hash,
    parse_datetime,
    utcnow,
)

LOGGER = logging.getLogger(__name__)
USER_AGENT = "ArxivCortex/0.1 (private local research discovery server)"
ARXIV_QUERY_URL = "https://export.arxiv.org/api/query"
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
RATE_LIMIT_DELAYS = (300.0, 900.0, 1800.0, 3600.0)


class _UserAgentSession(requests.Session):
    def prepare_request(self, request: requests.Request) -> requests.PreparedRequest:
        request.headers["User-Agent"] = USER_AGENT
        return super().prepare_request(request)


@dataclass(frozen=True, slots=True)
class FeedQuery:
    label: str
    expression: str


class ArxivSource(Protocol):
    def results(
        self,
        query: str | FeedQuery,
        sort: str,
        *,
        cutoff: datetime | None = None,
        on_retry: Callable[[RetryNotice | None], None] | None = None,
    ) -> Iterable[Any]: ...


@dataclass(frozen=True, slots=True)
class ArxivSourcePage:
    results: list[Any]
    total: int
    has_next: bool


@dataclass(frozen=True, slots=True)
class RetryNotice:
    category: str
    offset: int
    attempt: int
    max_attempts: int
    status_code: int | None
    reason: str
    delay_seconds: float
    next_attempt_at: datetime


class ArxivRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: str = "",
    ):
        self.status_code = status_code
        self.response_body = response_body
        detail = f"arXiv API returned HTTP {status_code}" if status_code else message
        if response_body:
            detail = f"{detail}: {response_body}"
        super().__init__(detail)


class ArxivClientSource:
    def __init__(
        self,
        page_size: int = 200,
        delay_seconds: float = 3.1,
        retries: int = 5,
        *,
        connect_timeout: float = 10.0,
        read_timeout: float = 90.0,
    ):
        import arxiv

        self.arxiv = arxiv
        self.page_size = page_size
        self.delay_seconds = delay_seconds
        self.retries = retries
        self.timeout = (connect_timeout, read_timeout)
        self.session = _UserAgentSession()
        self._last_request_completed: float | None = None
        self._request_lock = threading.Lock()

    def results(
        self,
        category: str | FeedQuery,
        sort: str,
        *,
        cutoff: datetime | None = None,
        on_retry: Callable[[RetryNotice | None], None] | None = None,
    ) -> Iterable[Any]:
        sort_by = (
            self.arxiv.SortCriterion.SubmittedDate
            if sort == "submitted"
            else self.arxiv.SortCriterion.LastUpdatedDate
        )
        feed_query = (
            category
            if isinstance(category, FeedQuery)
            else FeedQuery(label=category, expression=f"cat:{category}")
        )
        query = feed_query.expression
        if sort == "submitted" and cutoff is not None:
            start = cutoff.astimezone(UTC).strftime("%Y%m%d%H%M")
            end = (utcnow() + timedelta(minutes=1)).strftime("%Y%m%d%H%M")
            query = f"{query} AND submittedDate:[{start} TO {end}]"

        offset = 0
        total_results: int | None = None
        seen: set[str] = set()
        while True:
            url = self._page_url(query, sort_by.value, offset)
            feed = self._fetch_page(
                url,
                category=feed_query.label,
                offset=offset,
                first_page=offset == 0,
                on_retry=on_retry,
            )
            entries = list(feed.entries)
            if not entries:
                return
            if total_results is None:
                total_results = self._total_results(feed)
            for entry in entries:
                try:
                    result = self.arxiv.Result._from_feed_entry(entry)
                except self.arxiv.Result.MissingFieldError as error:
                    LOGGER.warning(
                        "Skipping partial arXiv result in %s: %s", feed_query.label, error
                    )
                    continue
                identifier = (
                    result.get_short_id()
                    if hasattr(result, "get_short_id")
                    else str(getattr(result, "entry_id", id(result)))
                )
                if identifier in seen:
                    continue
                seen.add(identifier)
                yield result
            offset += len(entries)
            if (total_results is not None and offset >= total_results) or len(entries) < self.page_size:
                return

    def search_page(
        self,
        query: FeedQuery,
        *,
        offset: int,
        limit: int,
        cutoff: datetime | None = None,
    ) -> ArxivSourcePage:
        """Fetch one interactive search page without entering long retry cooldowns."""
        expression = query.expression
        if cutoff is not None:
            start = cutoff.astimezone(UTC).strftime("%Y%m%d%H%M")
            end = (utcnow() + timedelta(minutes=1)).strftime("%Y%m%d%H%M")
            expression = f"{expression} AND submittedDate:[{start} TO {end}]"
        url = self._page_url(
            expression,
            self.arxiv.SortCriterion.Relevance.value,
            offset,
            max_results=limit,
        )
        feed = self._fetch_page(
            url,
            category=query.label,
            offset=offset,
            first_page=offset == 0,
            on_retry=None,
            max_retries=0,
            confirm_empty_first_page=False,
        )
        results: list[Any] = []
        seen: set[str] = set()
        for entry in feed.entries:
            try:
                result = self.arxiv.Result._from_feed_entry(entry)
            except self.arxiv.Result.MissingFieldError as error:
                LOGGER.warning("Skipping partial arXiv search result: %s", error)
                continue
            identifier = (
                result.get_short_id()
                if hasattr(result, "get_short_id")
                else str(getattr(result, "entry_id", id(result)))
            )
            if identifier in seen:
                continue
            seen.add(identifier)
            results.append(result)
        total = self._total_results(feed)
        known_total = total if total is not None else offset + len(feed.entries)
        return ArxivSourcePage(
            results=results,
            total=known_total,
            has_next=(offset + len(feed.entries)) < known_total,
        )

    def _page_url(
        self,
        query: str,
        sort_by: str,
        offset: int,
        *,
        max_results: int | None = None,
    ) -> str:
        return f"{ARXIV_QUERY_URL}?{urlencode({
            'search_query': query,
            'id_list': '',
            'sortBy': sort_by,
            'sortOrder': self.arxiv.SortOrder.Descending.value,
            'start': offset,
            'max_results': max_results or self.page_size,
        })}"

    def _fetch_page(
        self,
        url: str,
        *,
        category: str,
        offset: int,
        first_page: bool,
        on_retry: Callable[[RetryNotice | None], None] | None,
        max_retries: int | None = None,
        confirm_empty_first_page: bool = True,
    ) -> Any:
        retries = self.retries if max_retries is None else max_retries
        empty_confirmations = 0
        retried = False
        for attempt in range(retries + 1):
            with self._request_lock:
                self._respect_rate_limit()
                try:
                    LOGGER.info("Requesting arXiv page at offset %d: %s", offset, url)
                    response = self.session.get(url, timeout=self.timeout)
                except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as error:
                    self._last_request_completed = time.monotonic()
                    request_error = error
                    response = None
                else:
                    self._last_request_completed = time.monotonic()

            if response is None:
                if attempt >= retries:
                    raise ArxivRequestError(
                        f"arXiv API request failed: {request_error}"
                    ) from request_error
                retried = True
                self._wait_for_retry(
                    category=category,
                    offset=offset,
                    attempt=attempt,
                    status_code=None,
                    reason=request_error.__class__.__name__,
                    headers={},
                    on_retry=on_retry,
                    max_attempts=retries,
                )
                continue

            if response.status_code != 200:
                body = " ".join(response.text.split())[:160]
                error = ArxivRequestError(
                    "arXiv API request failed",
                    status_code=response.status_code,
                    response_body=body,
                )
                if response.status_code not in RETRYABLE_HTTP_STATUSES or attempt >= retries:
                    raise error
                retried = True
                self._wait_for_retry(
                    category=category,
                    offset=offset,
                    attempt=attempt,
                    status_code=response.status_code,
                    reason=body or f"HTTP {response.status_code}",
                    headers=response.headers,
                    on_retry=on_retry,
                    max_attempts=retries,
                )
                continue

            feed = feedparser.parse(response.content)
            if feed.bozo and not feed.entries:
                reason = f"Invalid Atom response: {feed.get('bozo_exception', 'parse error')}"
                if attempt >= retries:
                    raise ArxivRequestError(reason)
                retried = True
                self._wait_for_retry(
                    category=category,
                    offset=offset,
                    attempt=attempt,
                    status_code=200,
                    reason=reason,
                    headers=response.headers,
                    on_retry=on_retry,
                    max_attempts=retries,
                )
                continue

            if not feed.entries:
                if first_page and not confirm_empty_first_page:
                    return feed
                if first_page:
                    empty_confirmations += 1
                    if empty_confirmations >= 3:
                        LOGGER.warning(
                            "Accepted an empty first page for %s after %d confirmations",
                            category,
                            empty_confirmations,
                        )
                        if retried and on_retry:
                            on_retry(None)
                        return feed
                    reason = f"Unexpected empty first page ({empty_confirmations}/3 confirmations)"
                else:
                    reason = "Unexpected empty page before the reported result count"
                if attempt >= retries:
                    raise ArxivRequestError(reason)
                retried = True
                self._wait_for_retry(
                    category=category,
                    offset=offset,
                    attempt=attempt,
                    status_code=200,
                    reason=reason,
                    headers=response.headers,
                    on_retry=on_retry,
                    max_attempts=retries,
                )
                continue

            if retried and on_retry:
                on_retry(None)
            return feed
        raise AssertionError("unreachable")

    def _respect_rate_limit(self) -> None:
        if self._last_request_completed is None:
            return
        elapsed = time.monotonic() - self._last_request_completed
        if elapsed < self.delay_seconds:
            time.sleep(self.delay_seconds - elapsed)

    def _wait_for_retry(
        self,
        *,
        category: str,
        offset: int,
        attempt: int,
        status_code: int | None,
        reason: str,
        headers: Mapping[str, str],
        on_retry: Callable[[RetryNotice | None], None] | None,
        max_attempts: int | None = None,
    ) -> None:
        delay = self._retry_delay(attempt, status_code, headers)
        notice = RetryNotice(
            category=category,
            offset=offset,
            attempt=attempt + 1,
            max_attempts=self.retries if max_attempts is None else max_attempts,
            status_code=status_code,
            reason=reason,
            delay_seconds=delay,
            next_attempt_at=utcnow() + timedelta(seconds=delay),
        )
        LOGGER.warning(
            "arXiv request for %s at offset %d failed (%s); retry %d/%d in %.1fs",
            category,
            offset,
            reason,
            notice.attempt,
            notice.max_attempts,
            delay,
        )
        if on_retry:
            on_retry(notice)
        time.sleep(delay)

    def _retry_delay(
        self,
        attempt: int,
        status_code: int | None,
        headers: Mapping[str, str],
    ) -> float:
        if status_code == 429:
            retry_after = self._retry_after(headers.get("Retry-After"))
            if retry_after is not None:
                return max(self.delay_seconds, retry_after)
            base = RATE_LIMIT_DELAYS[min(attempt, len(RATE_LIMIT_DELAYS) - 1)]
            return base + random.uniform(0, min(60.0, base * 0.1))
        ceiling = min(10.0 * (2**attempt), 300.0)
        return max(self.delay_seconds, random.uniform(0, ceiling))

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return max(0.0, (parsed.astimezone(UTC) - utcnow()).total_seconds())

    @staticmethod
    def _total_results(feed: Any) -> int | None:
        value = feed.feed.get("opensearch_totalresults")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None


@dataclass(slots=True)
class PaperRecord:
    arxiv_id: str
    version: int
    title: str
    abstract: str
    authors: list[str]
    categories: list[str]
    primary_category: str
    published_at: str
    updated_at: str
    doi: str | None
    journal_ref: str | None
    comment: str | None
    license_url: str | None
    abstract_url: str
    pdf_url: str
    metadata_hash: str
    content_hash: str


@dataclass(slots=True)
class SyncCounts:
    seen: int = 0
    added: int = 0
    updated: int = 0

    def add(self, other: SyncCounts) -> None:
        self.seen += other.seen
        self.added += other.added
        self.updated += other.updated


def result_to_record(result: Any) -> PaperRecord:
    short_id = result.get_short_id() if hasattr(result, "get_short_id") else result.entry_id
    arxiv_id, version = canonicalize_arxiv_id(short_id)
    title = " ".join(str(result.title).split())
    abstract = " ".join(str(result.summary).split())
    authors = [" ".join(str(author.name).split()) for author in result.authors]
    categories = sorted(set(result.categories))
    primary_category = result.primary_category or (categories[0] if categories else "unknown")
    published_at = isoformat(result.published)
    updated_at = isoformat(result.updated)
    abstract_url = f"https://arxiv.org/abs/{arxiv_id}"
    pdf_url = str(getattr(result, "pdf_url", None) or f"https://arxiv.org/pdf/{arxiv_id}")
    payload = {
        "arxiv_id": arxiv_id,
        "version": version,
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "categories": categories,
        "primary_category": primary_category,
        "published_at": published_at,
        "updated_at": updated_at,
        "doi": getattr(result, "doi", None),
        "journal_ref": getattr(result, "journal_ref", None),
        "comment": getattr(result, "comment", None),
        "license_url": getattr(result, "license", None),
    }
    return PaperRecord(
        arxiv_id=arxiv_id,
        version=version,
        title=title,
        abstract=abstract,
        authors=authors,
        categories=categories,
        primary_category=primary_category,
        published_at=published_at,
        updated_at=updated_at,
        doi=payload["doi"],
        journal_ref=payload["journal_ref"],
        comment=payload["comment"],
        license_url=str(payload["license_url"]) if payload["license_url"] else None,
        abstract_url=abstract_url,
        pdf_url=pdf_url,
        metadata_hash=metadata_hash(payload),
        content_hash=paper_content_hash(title, abstract),
    )


class ArxivSyncService:
    def __init__(self, database_path: str | Path, source: ArxivSource):
        self.database_path = database_path
        self.source = source

    def sync_all(self, run_id: int) -> SyncCounts:
        with database_connection(self.database_path) as connection:
            category_subscriptions = connection.execute(
                "SELECT * FROM feed_subscriptions WHERE enabled = 1 ORDER BY category"
            ).fetchall()
            tag_subscriptions = connection.execute(
                "SELECT * FROM search_tags WHERE enabled = 1 ORDER BY name COLLATE NOCASE"
            ).fetchall()
            subscriptions = [
                ("category", dict(subscription)) for subscription in category_subscriptions
            ] + [("tag", dict(subscription)) for subscription in tag_subscriptions]
            connection.execute(
                "UPDATE sync_runs SET categories_total = ? WHERE id = ?",
                (len(subscriptions), run_id),
            )

        total = SyncCounts()
        for index, (kind, subscription) in enumerate(subscriptions, start=1):
            counts = (
                self._sync_category(subscription, run_id)
                if kind == "category"
                else self._sync_tag(subscription, run_id)
            )
            total.add(counts)
            with database_connection(self.database_path) as connection:
                connection.execute(
                    """
                    UPDATE sync_runs SET categories_done = ?, papers_seen = ?,
                        papers_added = ?, papers_updated = ? WHERE id = ?
                    """,
                    (index, total.seen, total.added, total.updated, run_id),
                )
        with database_connection(self.database_path) as connection:
            connection.execute(
                "UPDATE sync_runs SET current_category = NULL WHERE id = ?",
                (run_id,),
            )
        return total

    def _sync_category(self, subscription: dict[str, Any], run_id: int) -> SyncCounts:
        category = subscription["category"]
        return self._sync_feed(
            subscription=subscription,
            source=category,
            label=category,
            run_id=run_id,
            tag_id=None,
        )

    def _sync_tag(self, subscription: dict[str, Any], run_id: int) -> SyncCounts:
        tag_id = int(subscription["id"])
        phrases = normalize_keyword_phrases(subscription["keywords"])
        source = FeedQuery(
            label=f"Tag: {subscription['name']}",
            expression=SearchTagService.arxiv_query(phrases),
        )
        return self._sync_feed(
            subscription=subscription,
            source=source,
            label=source.label,
            run_id=run_id,
            tag_id=tag_id,
        )

    def _sync_feed(
        self,
        *,
        subscription: dict[str, Any],
        source: str | FeedQuery,
        label: str,
        run_id: int,
        tag_id: int | None,
    ) -> SyncCounts:
        initial = not bool(subscription["backfill_complete"])
        sort = "submitted" if initial else "updated"
        if initial:
            cutoff = parse_datetime(subscription["backfill_from"])
        else:
            watermark = parse_datetime(subscription["last_updated_watermark"])
            cutoff = (watermark - timedelta(hours=48)) if watermark else utcnow() - timedelta(days=2)
        if cutoff is None:
            raise RuntimeError(f"Subscription {label} has no synchronization cutoff")

        batch: list[PaperRecord] = []
        counts = SyncCounts()
        newest_updated = parse_datetime(subscription["last_updated_watermark"])
        self._set_current_category(run_id, label)
        for result in self.source.results(
            source,
            sort,
            cutoff=cutoff,
            on_retry=lambda notice: self._record_retry(run_id, notice),
        ):
            try:
                record = result_to_record(result)
            except (AttributeError, TypeError, ValueError) as error:
                LOGGER.warning("Skipping malformed arXiv result in %s: %s", label, error)
                continue
            record_boundary = parse_datetime(record.published_at if initial else record.updated_at)
            if record_boundary is not None and record_boundary <= cutoff:
                break
            updated = parse_datetime(record.updated_at)
            if updated and (newest_updated is None or updated > newest_updated):
                newest_updated = updated
            batch.append(record)
            if len(batch) >= 100:
                counts.add(
                    self._flush(
                        batch,
                        tag_id=tag_id,
                        tag_keywords=subscription.get("keywords"),
                    )
                )
                batch.clear()
        if batch:
            counts.add(
                self._flush(
                    batch,
                    tag_id=tag_id,
                    tag_keywords=subscription.get("keywords"),
                )
            )

        with database_connection(self.database_path) as connection, transaction(connection):
            if tag_id is None:
                connection.execute(
                    """
                    UPDATE feed_subscriptions
                    SET backfill_complete = 1,
                        last_updated_watermark = ?,
                        last_synced_at = ?
                    WHERE category = ?
                    """,
                    (isoformat(newest_updated or utcnow()), isoformat(), label),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE search_tags
                    SET backfill_complete = 1,
                        last_updated_watermark = ?,
                        last_synced_at = ?
                    WHERE id = ? AND enabled = 1 AND keywords = ?
                    """,
                    (
                        isoformat(newest_updated or utcnow()),
                        isoformat(),
                        tag_id,
                        subscription["keywords"],
                    ),
                )
                if cursor.rowcount == 0:
                    current = connection.execute(
                        "SELECT keywords FROM search_tags WHERE id = ?", (tag_id,)
                    ).fetchone()
                    if current and current["keywords"] != subscription["keywords"]:
                        connection.execute(
                            "DELETE FROM paper_search_tags WHERE tag_id = ?", (tag_id,)
                        )
        self._record_retry(run_id, None)
        return counts

    def _set_current_category(self, run_id: int, category: str) -> None:
        with database_connection(self.database_path) as connection:
            connection.execute(
                "UPDATE sync_runs SET current_category = ? WHERE id = ?",
                (category, run_id),
            )

    def _record_retry(self, run_id: int, notice: RetryNotice | None) -> None:
        with database_connection(self.database_path) as connection:
            if notice is None:
                connection.execute(
                    """
                    UPDATE sync_runs SET retry_attempt = 0, retry_status = NULL,
                        retry_reason = NULL, next_attempt_at = NULL
                    WHERE id = ?
                    """,
                    (run_id,),
                )
                return
            connection.execute(
                """
                UPDATE sync_runs SET current_category = ?, retry_attempt = ?,
                    retry_status = ?, retry_reason = ?, next_attempt_at = ?
                WHERE id = ?
                """,
                (
                    notice.category,
                    notice.attempt,
                    notice.status_code,
                    notice.reason,
                    isoformat(notice.next_attempt_at),
                    run_id,
                ),
            )

    def _flush(
        self,
        records: list[PaperRecord],
        *,
        tag_id: int | None = None,
        tag_keywords: str | None = None,
    ) -> SyncCounts:
        counts = SyncCounts()
        with database_connection(self.database_path) as connection, transaction(connection):
            tag_is_current = tag_id is not None and bool(
                connection.execute(
                    """
                    SELECT 1 FROM search_tags
                    WHERE id = ? AND enabled = 1 AND keywords = ?
                    """,
                    (tag_id, tag_keywords),
                ).fetchone()
            )
            for record in records:
                outcome = self._upsert(connection, record)
                if tag_is_current:
                    paper_id = connection.execute(
                        "SELECT id FROM papers WHERE arxiv_id = ?", (record.arxiv_id,)
                    ).fetchone()["id"]
                    connection.execute(
                        "INSERT OR IGNORE INTO paper_search_tags(paper_id, tag_id) VALUES (?, ?)",
                        (paper_id, tag_id),
                    )
                counts.seen += 1
                if outcome == "added":
                    counts.added += 1
                elif outcome == "updated":
                    counts.updated += 1
        return counts

    @staticmethod
    def _upsert(connection: sqlite3.Connection, record: PaperRecord) -> str:
        existing = connection.execute(
            "SELECT id, metadata_hash, latest_version FROM papers WHERE arxiv_id = ?",
            (record.arxiv_id,),
        ).fetchone()
        fetched_at = isoformat()
        values = (
            record.arxiv_id,
            record.version,
            record.title,
            record.abstract,
            ", ".join(record.authors),
            record.primary_category,
            record.published_at,
            record.updated_at,
            record.doi,
            record.journal_ref,
            record.comment,
            record.license_url,
            record.abstract_url,
            record.pdf_url,
            record.metadata_hash,
            record.content_hash,
            fetched_at,
        )
        if existing is None:
            cursor = connection.execute(
                """
                INSERT INTO papers(
                    arxiv_id, latest_version, title, abstract, authors_text, primary_category,
                    published_at, updated_at, doi, journal_ref, comment, license_url,
                    abstract_url, pdf_url, metadata_hash, content_hash, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            paper_id = int(cursor.lastrowid)
            outcome = "added"
        elif (
            existing["metadata_hash"] != record.metadata_hash
            or int(existing["latest_version"]) != record.version
        ):
            paper_id = int(existing["id"])
            connection.execute(
                """
                UPDATE papers SET latest_version = ?, title = ?, abstract = ?, authors_text = ?,
                    primary_category = ?, published_at = ?, updated_at = ?, doi = ?,
                    journal_ref = ?, comment = ?, license_url = ?, abstract_url = ?, pdf_url = ?,
                    metadata_hash = ?, content_hash = ?, fetched_at = ?
                WHERE arxiv_id = ?
                """,
                (
                    record.version,
                    record.title,
                    record.abstract,
                    ", ".join(record.authors),
                    record.primary_category,
                    record.published_at,
                    record.updated_at,
                    record.doi,
                    record.journal_ref,
                    record.comment,
                    record.license_url,
                    record.abstract_url,
                    record.pdf_url,
                    record.metadata_hash,
                    record.content_hash,
                    fetched_at,
                    record.arxiv_id,
                ),
            )
            connection.execute("DELETE FROM paper_authors WHERE paper_id = ?", (paper_id,))
            connection.execute("DELETE FROM paper_categories WHERE paper_id = ?", (paper_id,))
            outcome = "updated"
        else:
            connection.execute(
                "UPDATE papers SET fetched_at = ? WHERE id = ?", (fetched_at, existing["id"])
            )
            return "unchanged"

        connection.executemany(
            "INSERT INTO paper_authors(paper_id, position, name) VALUES (?, ?, ?)",
            [(paper_id, position, name) for position, name in enumerate(record.authors)],
        )
        connection.executemany(
            "INSERT INTO paper_categories(paper_id, category) VALUES (?, ?)",
            [(paper_id, category) for category in record.categories],
        )
        return outcome
