from __future__ import annotations

import atexit
import logging
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from arxiv_cortex.db import database_connection, transaction
from arxiv_cortex.services.arxiv_sync import ArxivClientSource, ArxivSource, ArxivSyncService
from arxiv_cortex.services.embeddings import EmbeddingService
from arxiv_cortex.utils import isoformat, sanitize_error, utcnow

LOGGER = logging.getLogger(__name__)


class JobManager:
    def __init__(
        self,
        database_path: str | Path,
        embedding_service: EmbeddingService,
        *,
        source_factory=None,
        page_size: int = 500,
        delay_seconds: float = 3.1,
        retries: int = 5,
        lease_seconds: int = 21600,
        enabled: bool = True,
    ):
        self.database_path = database_path
        self.embedding_service = embedding_service
        self.page_size = page_size
        self.delay_seconds = delay_seconds
        self.retries = retries
        self.lease_seconds = lease_seconds
        self.source_factory = source_factory or self._default_source
        self.enabled = enabled
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="arxiv-cortex")
        self._lock = threading.Lock()
        self._futures: dict[int, Future[None]] = {}
        self._index_future: Future[int] | None = None
        atexit.register(self.shutdown)

    def _default_source(self) -> ArxivSource:
        return ArxivClientSource(self.page_size, self.delay_seconds, self.retries)

    def submit_sync(self, trigger: str = "manual") -> int:
        with database_connection(self.database_path) as connection:
            queued = connection.execute(
                """
                SELECT id FROM sync_runs WHERE status = 'queued'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if queued:
                return int(queued["id"])
            cursor = connection.execute(
                "INSERT INTO sync_runs(status, trigger, created_at) VALUES ('queued', ?, ?)",
                (trigger, isoformat()),
            )
            run_id = int(cursor.lastrowid)
        if not self.enabled:
            return run_id
        with self._lock:
            self._futures[run_id] = self.executor.submit(self._run, run_id)
        return run_id

    def run_sync_inline(self, trigger: str = "cli") -> int:
        with database_connection(self.database_path) as connection:
            cursor = connection.execute(
                "INSERT INTO sync_runs(status, trigger, created_at) VALUES ('queued', ?, ?)",
                (trigger, isoformat()),
            )
            run_id = int(cursor.lastrowid)
        self._run(run_id)
        return run_id

    def wait(self, run_id: int, timeout: float | None = None) -> None:
        future = self._futures.get(run_id)
        if future:
            future.result(timeout=timeout)

    def submit_indexing(self) -> None:
        """Index newly imported interactive-search papers on the serial executor."""
        if not self.enabled:
            return
        with self._lock:
            if self._index_future and not self._index_future.done():
                return
            self._index_future = self.executor.submit(self._index_pending)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)

    def _index_pending(self) -> int:
        try:
            return self.embedding_service.index_pending()
        except BaseException:
            LOGGER.exception("Background embedding after interactive search failed")
            return 0

    def _run(self, run_id: int) -> None:
        owner = uuid.uuid4().hex
        if not self._acquire_lease("sync", owner):
            self._fail(run_id, "Another synchronization job holds the lease")
            return
        try:
            self._mark_running(run_id)
            source = self.source_factory()
            ArxivSyncService(self.database_path, source).sync_all(run_id)

            def embedding_progress(count: int) -> None:
                with database_connection(self.database_path) as connection:
                    connection.execute(
                        "UPDATE sync_runs SET embeddings_generated = ? WHERE id = ?",
                        (count, run_id),
                    )
                self._renew_lease("sync", owner)

            generated = self.embedding_service.index_pending(embedding_progress)
            with database_connection(self.database_path) as connection:
                connection.execute(
                    """
                    UPDATE sync_runs SET status = 'succeeded', embeddings_generated = ?,
                        completed_at = ?, current_category = NULL, retry_attempt = 0,
                        retry_status = NULL, retry_reason = NULL, next_attempt_at = NULL
                    WHERE id = ?
                    """,
                    (generated, isoformat(), run_id),
                )
        except BaseException as error:
            LOGGER.exception("Synchronization run %s failed", run_id)
            self._fail(run_id, sanitize_error(error))
        finally:
            self._release_lease("sync", owner)

    def _mark_running(self, run_id: int) -> None:
        with database_connection(self.database_path) as connection:
            connection.execute(
                "UPDATE sync_runs SET status = 'running', started_at = ? WHERE id = ?",
                (isoformat(), run_id),
            )

    def _fail(self, run_id: int, message: str) -> None:
        with database_connection(self.database_path) as connection:
            connection.execute(
                """
                UPDATE sync_runs SET status = 'failed', error = ?, completed_at = ?,
                    next_attempt_at = NULL WHERE id = ?
                """,
                (message, isoformat(), run_id),
            )

    def _acquire_lease(self, name: str, owner: str) -> bool:
        expires = isoformat(utcnow() + timedelta(seconds=self.lease_seconds))
        now = isoformat()
        with database_connection(self.database_path) as connection, transaction(connection):
            connection.execute("DELETE FROM job_leases WHERE expires_at <= ?", (now,))
            cursor = connection.execute(
                "INSERT OR IGNORE INTO job_leases(name, owner, expires_at) VALUES (?, ?, ?)",
                (name, owner, expires),
            )
        return cursor.rowcount == 1

    def _renew_lease(self, name: str, owner: str) -> None:
        expires = isoformat(utcnow() + timedelta(seconds=self.lease_seconds))
        with database_connection(self.database_path) as connection:
            connection.execute(
                "UPDATE job_leases SET expires_at = ? WHERE name = ? AND owner = ?",
                (expires, name, owner),
            )

    def _release_lease(self, name: str, owner: str) -> None:
        with database_connection(self.database_path) as connection:
            connection.execute(
                "DELETE FROM job_leases WHERE name = ? AND owner = ?", (name, owner)
            )


def recover_interrupted_jobs(database_path: str | Path) -> None:
    with database_connection(database_path) as connection, transaction(connection):
        connection.execute(
            """
            UPDATE sync_runs SET status = 'failed', completed_at = ?,
                error = 'Application stopped before the job completed'
            WHERE status IN ('queued', 'running')
            """,
            (isoformat(),),
        )
        # Leases are owned by the previous process and cannot still be active
        # when application startup invokes this recovery routine.
        connection.execute("DELETE FROM job_leases")
