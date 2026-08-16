from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import unquote_plus

import pytest
import requests

from arxiv_cortex.db import database_connection
from arxiv_cortex.services.arxiv_sync import (
    ArxivClientSource,
    ArxivRequestError,
    ArxivSourcePage,
    ArxivSyncService,
    FeedQuery,
    result_to_record,
)
from arxiv_cortex.services.papers import PaperQuery, PaperService
from arxiv_cortex.services.remote_search import arxiv_search_expression
from arxiv_cortex.services.search_tags import SearchTagService
from arxiv_cortex.services.settings import SettingsService
from arxiv_cortex.utils import isoformat
from tests.conftest import fake_result


class FakeSource:
    def __init__(self, submitted=None, updated=None):
        self.submitted = submitted or []
        self.updated = updated or []
        self.calls = []

    def results(self, category: str, sort: str, *, cutoff=None, on_retry=None):
        self.calls.append((category, sort))
        return iter(self.submitted if sort == "submitted" else self.updated)


def create_run(connection) -> int:
    cursor = connection.execute(
        "INSERT INTO sync_runs(status, trigger, created_at) VALUES ('running', 'test', ?)",
        (isoformat(),),
    )
    return int(cursor.lastrowid)


def test_result_normalization_handles_versions_and_whitespace():
    result = fake_result(
        "2401.01234v4",
        title="  A   spaced title ",
        summary="Line one\n line two",
    )
    record = result_to_record(result)
    assert record.arxiv_id == "2401.01234"
    assert record.version == 4
    assert record.title == "A spaced title"
    assert record.abstract == "Line one line two"
    assert record.content_hash


def test_interactive_search_builds_a_broad_term_query_with_optional_category():
    expression = arxiv_search_expression("stealing reasoning traces", "cs.CR")
    assert expression == (
        '(all:"stealing" AND all:"reasoning" AND all:"traces") AND cat:cs.CR'
    )


def test_interactive_search_fetches_one_relevance_page_and_accepts_no_results(monkeypatch):
    source = ArxivClientSource(page_size=200, delay_seconds=0, retries=3)
    captured_urls = []
    responses = iter([response(200), response(200)])
    monkeypatch.setattr(
        source.session,
        "get",
        lambda url, **_kwargs: (captured_urls.append(url), next(responses))[1],
    )
    feeds = iter(
        [
            parsed_feed([fake_result("2608.00001v1")], 52),
            parsed_feed([], 0),
        ]
    )
    monkeypatch.setattr(
        "arxiv_cortex.services.arxiv_sync.feedparser.parse", lambda _content: next(feeds)
    )
    monkeypatch.setattr(source.arxiv.Result, "_from_feed_entry", lambda entry: entry)

    page = source.search_page(
        FeedQuery("Interactive search", 'all:"reasoning"'), offset=25, limit=25
    )
    assert isinstance(page, ArxivSourcePage)
    assert [item.get_short_id() for item in page.results] == ["2608.00001v1"]
    assert page.total == 52
    assert page.has_next is True
    decoded = unquote_plus(captured_urls[0])
    assert "sortBy=relevance" in decoded
    assert "start=25" in decoded
    assert "max_results=25" in decoded

    empty = source.search_page(
        FeedQuery("Interactive search", 'all:"noresults"'), offset=0, limit=25
    )
    assert empty.results == []
    assert empty.total == 0
    assert empty.has_next is False


def test_initial_backfill_then_revision_sync_is_idempotent(app):
    now = datetime.now(UTC)
    first = fake_result(
        "2401.00001v1",
        title="First version",
        summary="An initial quantum abstract",
        published=now - timedelta(days=2),
        updated=now - timedelta(days=2),
    )
    source = FakeSource(submitted=[first])
    with database_connection(app.config["DATABASE"]) as connection:
        SettingsService(connection).update_subscriptions(["cs.LG"], 90)
        run_id = create_run(connection)
    counts = ArxivSyncService(app.config["DATABASE"], source).sync_all(run_id)
    assert (counts.seen, counts.added, counts.updated) == (1, 1, 0)
    assert source.calls == [("cs.LG", "submitted")]

    revised = fake_result(
        "2401.00001v2",
        title="Revised version",
        summary="A better quantum abstract",
        published=now - timedelta(days=2),
        updated=now + timedelta(minutes=1),
    )
    second = fake_result(
        "2401.00002v1",
        title="A new paper",
        published=now,
        updated=now,
    )
    source.updated = [revised, second]
    with database_connection(app.config["DATABASE"]) as connection:
        run_id = create_run(connection)
    counts = ArxivSyncService(app.config["DATABASE"], source).sync_all(run_id)
    assert (counts.seen, counts.added, counts.updated) == (2, 1, 1)

    with database_connection(app.config["DATABASE"]) as connection:
        paper = connection.execute(
            "SELECT latest_version, title FROM papers WHERE arxiv_id = '2401.00001'"
        ).fetchone()
        assert dict(paper) == {"latest_version": 2, "title": "Revised version"}
        assert PaperService(connection).list(PaperQuery(query="revised")).total == 1
        assert PaperService(connection).list(PaperQuery(query="first version")).total == 0
        assert connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 2
        run_id = create_run(connection)
    counts = ArxivSyncService(app.config["DATABASE"], source).sync_all(run_id)
    assert counts.added == 0
    assert counts.updated == 0


def test_followed_tag_backfills_and_materializes_a_tag_only_feed(app):
    now = datetime.now(UTC)
    paper = fake_result(
        "2401.00111v1",
        title="Secure accelerator evaluation",
        summary="Hardware assurance for AI inference",
        category="cs.AR",
        published=now - timedelta(days=1),
    )
    source = FakeSource(submitted=[paper])
    with database_connection(app.config["DATABASE"]) as connection:
        tag = SearchTagService(connection).create(
            "Accelerator assurance",
            "",
            "hardware assurance\nAI accelerator verification",
            followed=True,
        )
        run_id = create_run(connection)

    counts = ArxivSyncService(app.config["DATABASE"], source).sync_all(run_id)
    assert (counts.seen, counts.added) == (1, 1)
    query, sort = source.calls[0]
    assert isinstance(query, FeedQuery)
    assert query.label == "Tag: Accelerator assurance"
    assert query.expression == '(all:"hardware assurance" OR all:"AI accelerator verification")'
    assert sort == "submitted"

    with database_connection(app.config["DATABASE"]) as connection:
        mapping = connection.execute(
            "SELECT pst.tag_id FROM paper_search_tags pst JOIN papers p ON p.id = pst.paper_id "
            "WHERE p.arxiv_id = '2401.00111'"
        ).fetchone()
        assert mapping["tag_id"] == tag["id"]
        page = PaperService(connection).list(PaperQuery())
        assert [item["arxiv_id"] for item in page.items] == ["2401.00111"]
        assert connection.execute(
            "SELECT last_updated_watermark FROM search_tags WHERE id = ?", (tag["id"],)
        ).fetchone()[0]


def test_tag_query_changed_during_sync_cannot_restore_stale_provenance(app):
    now = datetime.now(UTC)
    old_match = fake_result(
        "2401.00112v1",
        title="Old tag query match",
        summary="An old-query paper",
        published=now - timedelta(days=1),
    )
    with database_connection(app.config["DATABASE"]) as connection:
        tag = SearchTagService(connection).create(
            "Mutable feed", "", "old query", followed=True
        )
        run_id = create_run(connection)

    class EditingSource:
        def results(self, query, sort, *, cutoff=None, on_retry=None):
            with database_connection(app.config["DATABASE"]) as connection:
                SearchTagService(connection).update(
                    tag["id"], "Mutable feed", "", "new query", followed=True
                )
            yield old_match

    ArxivSyncService(app.config["DATABASE"], EditingSource()).sync_all(run_id)

    with database_connection(app.config["DATABASE"]) as connection:
        current = connection.execute(
            "SELECT keywords, backfill_complete, last_updated_watermark FROM search_tags "
            "WHERE id = ?",
            (tag["id"],),
        ).fetchone()
        assert dict(current) == {
            "keywords": "new query",
            "backfill_complete": 0,
            "last_updated_watermark": None,
        }
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_search_tags WHERE tag_id = ?", (tag["id"],)
        ).fetchone()[0] == 0
        assert PaperService(connection).list(PaperQuery()).total == 0


def test_failed_sync_does_not_advance_watermark(app):
    class BrokenSource:
        def results(self, category, sort, *, cutoff=None, on_retry=None):
            raise RuntimeError("temporary failure")

    with database_connection(app.config["DATABASE"]) as connection:
        SettingsService(connection).update_subscriptions(["cs.LG"], 90)
        before = connection.execute(
            "SELECT last_updated_watermark FROM feed_subscriptions WHERE category = 'cs.LG'"
        ).fetchone()[0]
        run_id = create_run(connection)
    with suppress(RuntimeError):
        ArxivSyncService(app.config["DATABASE"], BrokenSource()).sync_all(run_id)
    with database_connection(app.config["DATABASE"]) as connection:
        after = connection.execute(
            "SELECT last_updated_watermark FROM feed_subscriptions WHERE category = 'cs.LG'"
        ).fetchone()[0]
    assert before == after


def test_failed_tag_sync_does_not_advance_watermark(app):
    class BrokenSource:
        def results(self, query, sort, *, cutoff=None, on_retry=None):
            raise RuntimeError("temporary failure")

    with database_connection(app.config["DATABASE"]) as connection:
        tag = SearchTagService(connection).create(
            "Broken feed", "", "robust evaluation", followed=True
        )
        before = connection.execute(
            "SELECT last_updated_watermark FROM search_tags WHERE id = ?", (tag["id"],)
        ).fetchone()[0]
        run_id = create_run(connection)
    with suppress(RuntimeError):
        ArxivSyncService(app.config["DATABASE"], BrokenSource()).sync_all(run_id)
    with database_connection(app.config["DATABASE"]) as connection:
        after = connection.execute(
            "SELECT last_updated_watermark FROM search_tags WHERE id = ?", (tag["id"],)
        ).fetchone()[0]
    assert before == after


def response(status: int, *, body: str = "", content: bytes = b"feed", headers=None):
    return SimpleNamespace(
        status_code=status,
        text=body,
        content=content,
        headers=headers or {},
    )


def parsed_feed(entries, total: int):
    return SimpleNamespace(
        entries=entries,
        feed={"opensearch_totalresults": str(total)},
        bozo=False,
    )


def test_arxiv_client_uses_descriptive_user_agent_and_jittered_503_backoff(monkeypatch):
    source = ArxivClientSource(page_size=10, delay_seconds=0, retries=2)
    prepared = source.session.prepare_request(
        requests.Request("GET", "https://export.arxiv.org/api/query", headers={"user-agent": "x"})
    )
    assert prepared.headers["User-Agent"].startswith("ArxivCortex/")

    delays = []
    notices = []
    responses = iter([response(503), response(503), response(200)])
    monkeypatch.setattr(source.session, "get", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(
        "arxiv_cortex.services.arxiv_sync.feedparser.parse",
        lambda _content: parsed_feed([object()], 1),
    )
    monkeypatch.setattr("arxiv_cortex.services.arxiv_sync.random.uniform", lambda _a, b: b)
    monkeypatch.setattr("arxiv_cortex.services.arxiv_sync.time.sleep", delays.append)

    feed = source._fetch_page(
        "https://example.test/page",
        category="cs.LG",
        offset=0,
        first_page=True,
        on_retry=notices.append,
    )
    assert len(feed.entries) == 1
    assert delays == [10, 20]
    assert [notice.status_code for notice in notices[:-1]] == [503, 503]
    assert notices[-1] is None


def test_arxiv_client_honors_retry_after_for_429(monkeypatch):
    source = ArxivClientSource(page_size=10, delay_seconds=0, retries=1)
    responses = iter(
        [
            response(429, body="Rate exceeded.", headers={"Retry-After": "120"}),
            response(200),
        ]
    )
    delays = []
    notices = []
    monkeypatch.setattr(source.session, "get", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(
        "arxiv_cortex.services.arxiv_sync.feedparser.parse",
        lambda _content: parsed_feed([object()], 1),
    )
    monkeypatch.setattr("arxiv_cortex.services.arxiv_sync.time.sleep", delays.append)

    source._fetch_page(
        "https://example.test/page",
        category="cs.LG",
        offset=0,
        first_page=True,
        on_retry=notices.append,
    )
    assert delays == [120]
    assert notices[0].status_code == 429
    assert notices[0].reason == "Rate exceeded."


def test_arxiv_client_uses_long_cooldown_for_429_without_header(monkeypatch):
    source = ArxivClientSource(page_size=10, delay_seconds=0, retries=1)
    responses = iter([response(429, body="Rate exceeded."), response(200)])
    delays = []
    monkeypatch.setattr(source.session, "get", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(
        "arxiv_cortex.services.arxiv_sync.feedparser.parse",
        lambda _content: parsed_feed([object()], 1),
    )
    monkeypatch.setattr("arxiv_cortex.services.arxiv_sync.random.uniform", lambda _a, _b: 0)
    monkeypatch.setattr("arxiv_cortex.services.arxiv_sync.time.sleep", delays.append)

    source._fetch_page(
        "https://example.test/page",
        category="cs.LG",
        offset=0,
        first_page=True,
        on_retry=None,
    )
    assert delays == [300]


def test_arxiv_client_does_not_retry_permanent_400(monkeypatch):
    source = ArxivClientSource(page_size=10, delay_seconds=0, retries=5)
    calls = 0

    def get(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return response(400, body="Bad request")

    monkeypatch.setattr(source.session, "get", get)
    with pytest.raises(ArxivRequestError, match="HTTP 400"):
        source._fetch_page(
            "https://example.test/page",
            category="cs.LG",
            offset=0,
            first_page=True,
            on_retry=None,
        )
    assert calls == 1


def test_arxiv_client_retries_failed_page_without_restarting(monkeypatch):
    source = ArxivClientSource(page_size=2, delay_seconds=0, retries=1)
    requested_urls = []
    responses = iter(
        [
            response(200, content=b"page-1"),
            response(503),
            response(200, content=b"page-2"),
        ]
    )
    entry_one, entry_two, entry_three = object(), object(), object()

    def get(url, **_kwargs):
        requested_urls.append(url)
        return next(responses)

    def parse(content):
        if content == b"page-1":
            return parsed_feed([entry_one, entry_two], 3)
        return parsed_feed([entry_three], 3)

    monkeypatch.setattr(source.session, "get", get)
    monkeypatch.setattr("arxiv_cortex.services.arxiv_sync.feedparser.parse", parse)
    monkeypatch.setattr(source.arxiv.Result, "_from_feed_entry", staticmethod(lambda entry: entry))
    monkeypatch.setattr("arxiv_cortex.services.arxiv_sync.random.uniform", lambda _a, _b: 0)
    monkeypatch.setattr("arxiv_cortex.services.arxiv_sync.time.sleep", lambda _seconds: None)

    assert list(source.results("cs.LG", "updated")) == [entry_one, entry_two, entry_three]
    assert ["start=0" in requested_urls[0], "start=2" in requested_urls[1], "start=2" in requested_urls[2]] == [True, True, True]


def test_initial_backfill_query_is_bounded_by_submission_date(monkeypatch):
    source = ArxivClientSource(page_size=200, delay_seconds=0, retries=0)
    captured = []
    entry = object()

    def fetch(url, **_kwargs):
        captured.append(unquote_plus(url))
        return parsed_feed([entry], 1)

    monkeypatch.setattr(source, "_fetch_page", fetch)
    monkeypatch.setattr(source.arxiv.Result, "_from_feed_entry", staticmethod(lambda item: item))
    cutoff = datetime(2026, 5, 13, 6, 1, tzinfo=UTC)

    assert list(source.results("cs.MA", "submitted", cutoff=cutoff)) == [entry]
    assert "cat:cs.MA AND submittedDate:[202605130601 TO " in captured[0]
    assert "max_results=200" in captured[0]


def test_keyword_backfill_uses_the_custom_arxiv_query_and_date_bound(monkeypatch):
    source = ArxivClientSource(page_size=200, delay_seconds=0, retries=0)
    captured = []
    entry = object()

    def fetch(url, **_kwargs):
        captured.append(unquote_plus(url))
        return parsed_feed([entry], 1)

    monkeypatch.setattr(source, "_fetch_page", fetch)
    monkeypatch.setattr(source.arxiv.Result, "_from_feed_entry", staticmethod(lambda item: item))
    cutoff = datetime(2026, 5, 13, 6, 1, tzinfo=UTC)
    query = FeedQuery("Tag: Safety", '(all:"AI security" OR all:"model poisoning")')

    assert list(source.results(query, "submitted", cutoff=cutoff)) == [entry]
    assert '(all:"AI security" OR all:"model poisoning") AND submittedDate:[202605130601 TO ' in captured[0]


def test_empty_first_page_requires_three_consistent_responses(monkeypatch):
    source = ArxivClientSource(page_size=10, delay_seconds=0, retries=2)
    calls = 0
    delays = []

    def get(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return response(200)

    monkeypatch.setattr(source.session, "get", get)
    monkeypatch.setattr(
        "arxiv_cortex.services.arxiv_sync.feedparser.parse",
        lambda _content: parsed_feed([], 0),
    )
    monkeypatch.setattr("arxiv_cortex.services.arxiv_sync.random.uniform", lambda _a, b: b)
    monkeypatch.setattr("arxiv_cortex.services.arxiv_sync.time.sleep", delays.append)

    assert list(source.results("cs.LG", "submitted")) == []
    assert calls == 3
    assert delays == [10, 20]
