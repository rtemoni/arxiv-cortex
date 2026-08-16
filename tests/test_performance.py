from __future__ import annotations

import os
import time

import numpy as np
import pytest


@pytest.mark.performance
@pytest.mark.skipif(
    os.getenv("ARXIV_CORTEX_RUN_PERF") != "1",
    reason="Set ARXIV_CORTEX_RUN_PERF=1 to run the 100k-vector performance fixture",
)
def test_exact_similarity_100k_vectors_under_300ms():
    rng = np.random.default_rng(42)
    matrix = rng.normal(size=(100_000, 384)).astype(np.float32)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True)
    query = matrix[123]
    matrix @ query  # warm BLAS
    started = time.perf_counter()
    scores = matrix @ query
    elapsed = time.perf_counter() - started
    assert int(np.argmax(scores)) == 123
    assert elapsed < 0.300
