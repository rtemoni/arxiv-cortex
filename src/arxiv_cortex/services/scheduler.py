from __future__ import annotations

from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from arxiv_cortex.db import database_connection
from arxiv_cortex.services.jobs import JobManager


class SyncScheduler:
    def __init__(self, database_path: str | Path, jobs: JobManager):
        self.database_path = database_path
        self.jobs = jobs
        self.scheduler = BackgroundScheduler(daemon=True)

    def start(self) -> None:
        self.reload()
        if not self.scheduler.running:
            self.scheduler.start()

    def reload(self) -> None:
        with database_connection(self.database_path) as connection:
            values = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM settings WHERE key IN ('sync_time', 'scheduler_enabled')"
                )
            }
        if values.get("scheduler_enabled", "true").lower() != "true":
            self.scheduler.remove_job("daily-sync") if self.scheduler.get_job("daily-sync") else None
            return
        hour, minute = (int(part) for part in values.get("sync_time", "06:00").split(":"))
        self.scheduler.add_job(
            lambda: self.jobs.submit_sync("scheduled"),
            CronTrigger(hour=hour, minute=minute),
            id="daily-sync",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
