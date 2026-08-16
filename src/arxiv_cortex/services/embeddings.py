from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import numpy as np

from arxiv_cortex.db import database_connection, transaction
from arxiv_cortex.utils import isoformat


class EmbeddingProvider(Protocol):
    def encode(self, texts: list[str]) -> np.ndarray: ...


class SentenceTransformerProvider:
    def __init__(self, model_id: str):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model_id)

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.asarray(
            self.model.encode(
                texts,
                batch_size=min(64, max(1, len(texts))),
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            ),
            dtype=np.float32,
        )


class EmbeddingIndex:
    def __init__(self, database_path: str | Path):
        self.database_path = database_path
        self._lock = threading.RLock()
        self._fingerprint: tuple[str, int, str] | None = None
        self._ids = np.empty(0, dtype=np.int64)
        self._matrix = np.empty((0, 0), dtype=np.float32)
        self._positions: dict[int, int] = {}

    def invalidate(self) -> None:
        with self._lock:
            self._fingerprint = None

    def snapshot(self) -> tuple[np.ndarray, np.ndarray, dict[int, int]]:
        with self._lock:
            self._refresh()
            return self._ids, self._matrix, self._positions

    def vector(self, paper_id: int) -> np.ndarray | None:
        ids, matrix, positions = self.snapshot()
        del ids
        position = positions.get(paper_id)
        return matrix[position] if position is not None else None

    def _refresh(self) -> None:
        with database_connection(self.database_path) as connection:
            model_row = connection.execute(
                "SELECT value FROM settings WHERE key = 'embedding_model'"
            ).fetchone()
            model_id = model_row["value"] if model_row else ""
            stats = connection.execute(
                """
                SELECT COUNT(*) AS count, COALESCE(MAX(generated_at), '') AS newest
                FROM paper_embeddings WHERE model_id = ?
                """,
                (model_id,),
            ).fetchone()
            fingerprint = (model_id, int(stats["count"]), stats["newest"])
            if fingerprint == self._fingerprint:
                return
            rows = connection.execute(
                """
                SELECT paper_id, dimension, vector
                FROM paper_embeddings
                WHERE model_id = ?
                ORDER BY paper_id
                """,
                (model_id,),
            ).fetchall()

        if not rows:
            self._ids = np.empty(0, dtype=np.int64)
            self._matrix = np.empty((0, 0), dtype=np.float32)
            self._positions = {}
            self._fingerprint = fingerprint
            return

        dimension = int(rows[0]["dimension"])
        valid_rows = [row for row in rows if int(row["dimension"]) == dimension]
        self._ids = np.asarray([int(row["paper_id"]) for row in valid_rows], dtype=np.int64)
        self._matrix = np.vstack(
            [np.frombuffer(row["vector"], dtype=np.float32, count=dimension) for row in valid_rows]
        )
        self._positions = {int(paper_id): index for index, paper_id in enumerate(self._ids)}
        self._fingerprint = fingerprint


class EmbeddingService:
    def __init__(
        self,
        database_path: str | Path,
        index: EmbeddingIndex,
        batch_size: int = 64,
        provider_factory: Callable[[str], EmbeddingProvider] | None = None,
    ):
        self.database_path = database_path
        self.index = index
        self.batch_size = batch_size
        self.provider_factory = provider_factory or SentenceTransformerProvider

    def index_pending(self, progress: Callable[[int], None] | None = None) -> int:
        generated = 0
        provider: EmbeddingProvider | None = None
        while True:
            with database_connection(self.database_path) as connection:
                model_row = connection.execute(
                    "SELECT value FROM settings WHERE key = 'embedding_model'"
                ).fetchone()
                model_id = model_row["value"]
                rows = connection.execute(
                    """
                    SELECT p.id, p.title, p.abstract, p.content_hash
                    FROM papers p
                    LEFT JOIN paper_embeddings pe
                      ON pe.paper_id = p.id AND pe.model_id = ?
                    WHERE pe.paper_id IS NULL OR pe.content_hash != p.content_hash
                    ORDER BY p.updated_at DESC
                    LIMIT ?
                    """,
                    (model_id, self.batch_size),
                ).fetchall()
            if not rows:
                break
            if provider is None:
                provider = self.provider_factory(model_id)
            texts = [f"{row['title'].strip()}\n\n{row['abstract'].strip()}" for row in rows]
            vectors = np.asarray(provider.encode(texts), dtype=np.float32)
            if vectors.ndim != 2 or len(vectors) != len(rows):
                raise RuntimeError("Embedding provider returned an unexpected matrix shape")
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            vectors = vectors / np.maximum(norms, np.finfo(np.float32).eps)
            now = isoformat()
            with database_connection(self.database_path) as connection, transaction(connection):
                for row, vector in zip(rows, vectors, strict=True):
                    connection.execute(
                        """
                        INSERT INTO paper_embeddings(
                            paper_id, model_id, dimension, vector, content_hash, generated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(paper_id, model_id) DO UPDATE SET
                            dimension = excluded.dimension,
                            vector = excluded.vector,
                            content_hash = excluded.content_hash,
                            generated_at = excluded.generated_at
                        """,
                        (
                            row["id"],
                            model_id,
                            int(vector.shape[0]),
                            sqlite3.Binary(np.ascontiguousarray(vector).tobytes()),
                            row["content_hash"],
                            now,
                        ),
                    )
            generated += len(rows)
            self.index.invalidate()
            if progress:
                progress(generated)
        return generated

    def reset_active_model(self) -> int:
        with database_connection(self.database_path) as connection:
            model_id = connection.execute(
                "SELECT value FROM settings WHERE key = 'embedding_model'"
            ).fetchone()["value"]
            with transaction(connection):
                cursor = connection.execute(
                    "DELETE FROM paper_embeddings WHERE model_id = ?", (model_id,)
                )
        self.index.invalidate()
        return int(cursor.rowcount)
