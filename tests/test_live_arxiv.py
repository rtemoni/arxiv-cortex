from __future__ import annotations

import os

import pytest

from arxiv_cortex.services.arxiv_sync import ArxivClientSource, result_to_record


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("ARXIV_CORTEX_RUN_LIVE") != "1",
    reason="Set ARXIV_CORTEX_RUN_LIVE=1 to make one polite arXiv API request",
)
def test_live_arxiv_contract_with_one_request():
    source = ArxivClientSource(page_size=1, delay_seconds=3.1, retries=2)
    result = next(iter(source.results("cs.LG", "submitted")))
    record = result_to_record(result)
    assert record.arxiv_id
    assert record.title
    assert record.abstract_url.startswith("https://arxiv.org/abs/")
