from __future__ import annotations

from datetime import timedelta

from arxiv_cortex.db import database_connection
from arxiv_cortex.services.arxiv_sync import ArxivSyncService, RetryNotice
from arxiv_cortex.services.jobs import recover_interrupted_jobs
from arxiv_cortex.utils import isoformat, utcnow


def test_restart_recovery_fails_inflight_runs_and_clears_leases(app):
    with database_connection(app.config["DATABASE"]) as connection:
        connection.execute(
            "INSERT INTO sync_runs(status, trigger, created_at) VALUES ('queued', 'test', ?)",
            (isoformat(),),
        )
        connection.execute(
            """
            INSERT INTO sync_runs(status, trigger, created_at, started_at)
            VALUES ('running', 'test', ?, ?)
            """,
            (isoformat(), isoformat()),
        )
        connection.execute(
            "INSERT INTO job_leases(name, owner, expires_at) VALUES ('sync', 'old', ?)",
            (isoformat(utcnow() + timedelta(hours=1)),),
        )

    recover_interrupted_jobs(app.config["DATABASE"])

    with database_connection(app.config["DATABASE"]) as connection:
        rows = connection.execute(
            "SELECT status, error FROM sync_runs ORDER BY id"
        ).fetchall()
        assert all(row["status"] == "failed" for row in rows)
        assert all("Application stopped" in row["error"] for row in rows)
        assert connection.execute("SELECT COUNT(*) FROM job_leases").fetchone()[0] == 0


def test_expired_lease_can_be_reacquired_and_queued_runs_do_not_overlap(app):
    manager = app.extensions["job_manager"]
    with database_connection(app.config["DATABASE"]) as connection:
        connection.execute(
            "INSERT INTO job_leases(name, owner, expires_at) VALUES ('sync', 'old', ?)",
            (isoformat(utcnow() - timedelta(seconds=1)),),
        )
    assert manager._acquire_lease("sync", "new") is True

    first = manager.submit_sync("manual")
    second = manager.submit_sync("scheduled")
    assert second == first


def test_source_change_queues_one_follow_up_behind_a_running_sync(app):
    manager = app.extensions["job_manager"]
    with database_connection(app.config["DATABASE"]) as connection:
        cursor = connection.execute(
            """
            INSERT INTO sync_runs(status, trigger, created_at, started_at)
            VALUES ('running', 'manual', ?, ?)
            """,
            (isoformat(), isoformat()),
        )
        running_id = int(cursor.lastrowid)

    follow_up_id = manager.submit_sync("tag-updated")
    coalesced_id = manager.submit_sync("settings")

    assert follow_up_id != running_id
    assert coalesced_id == follow_up_id
    with database_connection(app.config["DATABASE"]) as connection:
        row = connection.execute(
            "SELECT status, trigger FROM sync_runs WHERE id = ?", (follow_up_id,)
        ).fetchone()
        assert dict(row) == {"status": "queued", "trigger": "tag-updated"}


def test_retry_state_is_persisted_and_cleared(app):
    with database_connection(app.config["DATABASE"]) as connection:
        cursor = connection.execute(
            "INSERT INTO sync_runs(status, trigger, created_at) VALUES ('running', 'test', ?)",
            (isoformat(),),
        )
        run_id = int(cursor.lastrowid)

    service = ArxivSyncService(app.config["DATABASE"], object())
    next_attempt = utcnow() + timedelta(minutes=5)
    service._record_retry(
        run_id,
        RetryNotice(
            category="cs.MA",
            offset=200,
            attempt=2,
            max_attempts=5,
            status_code=503,
            reason="Service unavailable",
            delay_seconds=300,
            next_attempt_at=next_attempt,
        ),
    )
    with database_connection(app.config["DATABASE"]) as connection:
        row = connection.execute("SELECT * FROM sync_runs WHERE id = ?", (run_id,)).fetchone()
        assert row["current_category"] == "cs.MA"
        assert row["retry_attempt"] == 2
        assert row["retry_status"] == 503
        assert row["next_attempt_at"] == isoformat(next_attempt)

    service._record_retry(run_id, None)
    with database_connection(app.config["DATABASE"]) as connection:
        row = connection.execute("SELECT * FROM sync_runs WHERE id = ?", (run_id,)).fetchone()
        assert row["retry_attempt"] == 0
        assert row["retry_status"] is None
        assert row["next_attempt_at"] is None


def test_interactive_search_can_queue_embedding_on_the_serial_executor(app, monkeypatch):
    manager = app.extensions["job_manager"]
    calls = []
    monkeypatch.setattr(
        manager.embedding_service,
        "index_pending",
        lambda: (calls.append("indexed"), 1)[1],
    )
    manager.enabled = True

    manager.submit_indexing()
    assert manager._index_future is not None
    assert manager._index_future.result(timeout=2) == 1
    assert calls == ["indexed"]
