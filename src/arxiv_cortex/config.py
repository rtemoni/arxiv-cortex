from __future__ import annotations

import os
from pathlib import Path


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def default_config() -> dict[str, object]:
    data_dir = Path(os.getenv("ARXIV_CORTEX_DATA_DIR", "data")).expanduser().resolve()
    return {
        "DATA_DIR": data_dir,
        "DATABASE": data_dir / "arxiv-cortex.sqlite3",
        "SECRET_FILE": data_dir / ".flask-secret",
        "BACKGROUND_JOBS_ENABLED": True,
        "SCHEDULER_ENABLED": _bool_env("ARXIV_CORTEX_SCHEDULER_ENABLED", False),
        "EMBEDDING_BATCH_SIZE": int(os.getenv("ARXIV_CORTEX_EMBEDDING_BATCH_SIZE", "64")),
        "SYNC_PAGE_SIZE": int(os.getenv("ARXIV_CORTEX_SYNC_PAGE_SIZE", "200")),
        "SYNC_DELAY_SECONDS": float(os.getenv("ARXIV_CORTEX_SYNC_DELAY_SECONDS", "3.1")),
        "SYNC_RETRIES": int(os.getenv("ARXIV_CORTEX_SYNC_RETRIES", "5")),
        "SYNC_LEASE_SECONDS": 60 * 60 * 6,
        "PER_PAGE": 25,
        "API_MAX_LIMIT": 100,
        "PDF_CACHE_MAX_BYTES": int(
            os.getenv("ARXIV_CORTEX_PDF_CACHE_MAX_BYTES", str(50 * 1024 * 1024))
        ),
    }
