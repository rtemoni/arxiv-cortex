from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from arxiv_cortex import create_app
from arxiv_cortex.db import database_connection, transaction
from arxiv_cortex.services.arxiv_sync import ArxivSyncService, PaperRecord
from arxiv_cortex.utils import isoformat, metadata_hash, paper_content_hash


class FakeEmbeddingProvider:
    def encode(self, texts: list[str]) -> np.ndarray:
        vectors = []
        for text in texts:
            lowered = text.lower()
            vector = np.asarray(
                [
                    2.0 if "quantum" in lowered else 0.2,
                    2.0 if "vision" in lowered else 0.2,
                    2.0 if "language" in lowered else 0.2,
                ],
                dtype=np.float32,
            )
            vectors.append(vector)
        return np.vstack(vectors)


@pytest.fixture
def app(tmp_path: Path):
    data_dir = tmp_path / "data"
    application = create_app(
        {
            "TESTING": True,
            "DATA_DIR": data_dir,
            "DATABASE": data_dir / "test.sqlite3",
            "SECRET_FILE": data_dir / ".secret",
            "SECRET_KEY": "test-secret",
            "BACKGROUND_JOBS_ENABLED": False,
            "SCHEDULER_ENABLED": False,
            "EMBEDDING_PROVIDER_FACTORY": lambda _model_id: FakeEmbeddingProvider(),
        }
    )
    yield application
    application.extensions["job_manager"].shutdown()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def seed_paper(app):
    counter = 0

    def seed(
        arxiv_id: str,
        title: str,
        abstract: str,
        *,
        category: str = "cs.LG",
        published_at: datetime | None = None,
        version: int = 1,
    ) -> PaperRecord:
        nonlocal counter
        counter += 1
        published = published_at or datetime.now(UTC) - timedelta(days=counter)
        payload = {
            "arxiv_id": arxiv_id,
            "version": version,
            "title": title,
            "abstract": abstract,
            "authors": ["Ada Researcher", "Grace Scientist"],
            "categories": [category],
            "primary_category": category,
            "published_at": isoformat(published),
            "updated_at": isoformat(published),
        }
        record = PaperRecord(
            arxiv_id=arxiv_id,
            version=version,
            title=title,
            abstract=abstract,
            authors=payload["authors"],
            categories=payload["categories"],
            primary_category=category,
            published_at=payload["published_at"],
            updated_at=payload["updated_at"],
            doi=None,
            journal_ref=None,
            comment=None,
            license_url=None,
            abstract_url=f"https://arxiv.org/abs/{arxiv_id}",
            pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
            metadata_hash=metadata_hash(payload),
            content_hash=paper_content_hash(title, abstract),
        )
        with database_connection(app.config["DATABASE"]) as connection, transaction(connection):
            ArxivSyncService._upsert(connection, record)
            connection.execute(
                """
                INSERT OR IGNORE INTO feed_subscriptions(
                    category, enabled, backfill_from, backfill_complete, created_at
                ) VALUES (?, 1, ?, 1, ?)
                """,
                (category, isoformat(datetime.now(UTC) - timedelta(days=90)), isoformat()),
            )
        return record

    return seed


def fake_result(
    arxiv_id: str,
    *,
    title: str = "A test paper",
    summary: str = "A useful abstract.",
    category: str = "cs.LG",
    published: datetime | None = None,
    updated: datetime | None = None,
):
    published = published or datetime.now(UTC) - timedelta(days=1)
    updated = updated or published
    return SimpleNamespace(
        get_short_id=lambda: arxiv_id,
        entry_id=f"https://arxiv.org/abs/{arxiv_id}",
        title=title,
        summary=summary,
        authors=[SimpleNamespace(name="Ada Researcher")],
        categories=[category],
        primary_category=category,
        published=published,
        updated=updated,
        doi=None,
        journal_ref=None,
        comment=None,
        license=None,
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
    )
