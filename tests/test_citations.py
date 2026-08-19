from __future__ import annotations

from arxiv_cortex.db import database_connection
from arxiv_cortex.services.citations import CitationService
from arxiv_cortex.services.papers import PaperService


class FakeCitationSource:
    def __init__(self):
        self.calls: list[list[str]] = []

    def fetch(self, identifiers):
        self.calls.append(list(identifiers))
        return [
            {"paperId": "s2-first", "citationCount": 19},
            {"paperId": "s2-second", "citationCount": 4},
        ]


def test_citation_refresh_updates_saved_papers_and_respects_freshness(
    app, seed_paper
):
    seed_paper("2401.50001", "First cited paper", "Abstract")
    seed_paper("2401.50002", "Second cited paper", "Abstract")
    seed_paper("2401.50003", "Unsaved paper", "Abstract")
    with database_connection(app.config["DATABASE"]) as connection:
        papers = PaperService(connection)
        papers.set_saved("2401.50001", True)
        papers.set_saved("2401.50002", True)

    source = FakeCitationSource()
    service = CitationService(app.config["DATABASE"], source)
    assert service.refresh_saved() == 2
    assert source.calls == [["ARXIV:2401.50001", "ARXIV:2401.50002"]]

    with database_connection(app.config["DATABASE"]) as connection:
        rows = connection.execute(
            """
            SELECT arxiv_id, citation_count, semantic_scholar_id, citation_updated_at
            FROM papers ORDER BY arxiv_id
            """
        ).fetchall()
        assert [(row["citation_count"], row["semantic_scholar_id"]) for row in rows] == [
            (19, "s2-first"),
            (4, "s2-second"),
            (None, None),
        ]
        assert rows[0]["citation_updated_at"]
        assert rows[2]["citation_updated_at"] is None

    assert service.refresh_saved() == 0
    assert len(source.calls) == 1
