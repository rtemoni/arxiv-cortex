from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from flask import Flask

from arxiv_cortex import db
from arxiv_cortex.config import default_config
from arxiv_cortex.services.arxiv_sync import ArxivClientSource
from arxiv_cortex.services.citations import CitationService, SemanticScholarSource
from arxiv_cortex.services.embeddings import EmbeddingIndex, EmbeddingService
from arxiv_cortex.services.jobs import JobManager, recover_interrupted_jobs
from arxiv_cortex.services.scheduler import SyncScheduler


def create_app(test_config: dict[str, object] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(default_config())
    if test_config:
        app.config.update(test_config)

    data_dir = Path(app.config["DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = db.load_or_create_secret(app.config["SECRET_FILE"])

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db.init_app(app)
    recover_interrupted_jobs(app.config["DATABASE"])

    embedding_index = EmbeddingIndex(app.config["DATABASE"])
    embedding_service = EmbeddingService(
        app.config["DATABASE"],
        embedding_index,
        batch_size=int(app.config["EMBEDDING_BATCH_SIZE"]),
        provider_factory=app.config.get("EMBEDDING_PROVIDER_FACTORY"),
    )
    configured_source_factory = app.config.get("ARXIV_SOURCE_FACTORY")
    arxiv_source = (
        configured_source_factory()
        if configured_source_factory
        else ArxivClientSource(
            int(app.config["SYNC_PAGE_SIZE"]),
            float(app.config["SYNC_DELAY_SECONDS"]),
            int(app.config["SYNC_RETRIES"]),
        )
    )
    jobs = JobManager(
        app.config["DATABASE"],
        embedding_service,
        source_factory=lambda: arxiv_source,
        page_size=int(app.config["SYNC_PAGE_SIZE"]),
        delay_seconds=float(app.config["SYNC_DELAY_SECONDS"]),
        retries=int(app.config["SYNC_RETRIES"]),
        lease_seconds=int(app.config["SYNC_LEASE_SECONDS"]),
        enabled=bool(app.config["BACKGROUND_JOBS_ENABLED"] and not app.config.get("TESTING")),
        citation_service=(
            CitationService(
                app.config["DATABASE"],
                (
                    app.config["CITATION_SOURCE_FACTORY"]()
                    if app.config.get("CITATION_SOURCE_FACTORY")
                    else SemanticScholarSource(
                        api_key=str(app.config["SEMANTIC_SCHOLAR_API_KEY"]),
                        timeout_seconds=float(app.config["CITATION_TIMEOUT_SECONDS"]),
                    )
                ),
            )
            if not app.config.get("TESTING") or app.config.get("CITATION_SOURCE_FACTORY")
            else None
        ),
    )
    app.extensions["embedding_index"] = embedding_index
    app.extensions["embedding_service"] = embedding_service
    app.extensions["job_manager"] = jobs
    app.extensions["arxiv_source"] = arxiv_source
    app.extensions["citation_service"] = jobs.citation_service

    scheduler = SyncScheduler(app.config["DATABASE"], jobs)
    app.extensions["sync_scheduler"] = scheduler
    if app.config.get("SCHEDULER_ENABLED") and not app.config.get("TESTING"):
        scheduler.start()

    from arxiv_cortex.api import api
    from arxiv_cortex.cli import register_cli
    from arxiv_cortex.web import web

    app.register_blueprint(web)
    app.register_blueprint(api, url_prefix="/api/v1")
    register_cli(app)

    @app.template_filter("short_date")
    def short_date(value: str | None) -> str:
        if not value:
            return ""
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d %b %Y")
        except ValueError:
            return value

    return app


__all__ = ["create_app"]
