from __future__ import annotations

from arxiv_cortex.db import database_connection
from arxiv_cortex.services.papers import PaperQuery, PaperService
from arxiv_cortex.services.recommendations import RecommendationService


def prepare_embeddings(app):
    return app.extensions["embedding_service"].index_pending()


def test_state_transitions_are_independent_and_idempotent(app, seed_paper):
    seed_paper("2401.10001", "State machine", "A language paper")
    with database_connection(app.config["DATABASE"]) as connection:
        service = PaperService(connection)
        assert service.set_saved("2401.10001", True)["state"]["saved"]
        assert service.set_read("2401.10001", True)["state"]["read"]
        dismissed = service.set_dismissed("2401.10001", True)
        assert dismissed["state"] == {
            "saved": False,
            "read": True,
            "dismissed": True,
            "last_opened_at": None,
        }
        restored = service.set_saved("2401.10001", True)
        assert restored["state"]["saved"] is True
        assert restored["state"]["dismissed"] is False


def test_keyword_search_weights_and_filters(app, seed_paper):
    seed_paper("2401.10002", "Vision transformer systems", "Efficient learning")
    seed_paper("2401.10003", "Efficient systems", "A transformer for vision tasks")
    with database_connection(app.config["DATABASE"]) as connection:
        page = PaperService(connection).list(PaperQuery(query="vision transformer"))
        assert [item["arxiv_id"] for item in page.items] == ["2401.10002", "2401.10003"]
        assert page.items[0]["score"] > page.items[1]["score"] > 0


def test_comma_separated_keyword_phrases_match_any_group(app, seed_paper):
    seed_paper("2401.10012", "Hardware verification", "Correct AI accelerators")
    seed_paper("2401.10013", "Fault injection", "Testing a training accelerator")
    seed_paper("2401.10015", "Hardware for AI systems", "A separate verification study")
    seed_paper("2401.10014", "Unrelated agents", "Multiagent coordination")
    with database_connection(app.config["DATABASE"]) as connection:
        page = PaperService(connection).list(
            PaperQuery(query="hardware verification, training accelerator")
        )
        assert {item["arxiv_id"] for item in page.items} == {
            "2401.10012",
            "2401.10013",
        }


def test_abstract_change_invalidates_and_replaces_embedding(app, seed_paper):
    seed_paper("2401.10011", "Mutable paper", "A quantum result")
    assert prepare_embeddings(app) == 1
    with database_connection(app.config["DATABASE"]) as connection:
        before = bytes(
            connection.execute(
                "SELECT vector FROM paper_embeddings pe JOIN papers p ON p.id = pe.paper_id "
                "WHERE p.arxiv_id = '2401.10011'"
            ).fetchone()["vector"]
        )

    seed_paper("2401.10011", "Mutable paper", "A vision result")
    assert prepare_embeddings(app) == 1
    with database_connection(app.config["DATABASE"]) as connection:
        row = connection.execute(
            """
            SELECT pe.vector, pe.content_hash AS embedding_hash, p.content_hash AS paper_hash
            FROM paper_embeddings pe JOIN papers p ON p.id = pe.paper_id
            WHERE p.arxiv_id = '2401.10011'
            """
        ).fetchone()
        assert row["embedding_hash"] == row["paper_hash"]
        assert bytes(row["vector"]) != before


def test_similarity_recommendations_and_negative_feedback(app, seed_paper):
    seed_paper("2401.10004", "Quantum foundations", "A quantum theory paper")
    seed_paper("2401.10005", "Quantum circuits", "A practical quantum study")
    seed_paper("2401.10006", "Visual learning", "A vision representation paper")
    seed_paper("2401.10007", "Language agents", "A language model paper")
    assert prepare_embeddings(app) == 4

    with database_connection(app.config["DATABASE"]) as connection:
        papers = PaperService(connection)
        papers.set_saved("2401.10004", True)
        papers.set_dismissed("2401.10006", True)
        service = RecommendationService(connection, app.extensions["embedding_index"])
        similar = service.similar("2401.10004", limit=3)
        assert similar[0]["arxiv_id"] == "2401.10005"
        recommendation = service.recommend(days=30, limit=5)
        assert recommendation.cold_start is False
        assert recommendation.items[0]["arxiv_id"] == "2401.10005"
        assert recommendation.items[0]["why"] == ["Quantum foundations"]
        assert {item["arxiv_id"] for item in recommendation.items}.isdisjoint(
            {"2401.10004", "2401.10006"}
        )


def test_cold_start_returns_newest_unread_papers(app, seed_paper):
    seed_paper("2401.10008", "Cold start", "A vision paper")
    seed_paper("2401.10009", "Dismissed cold start", "A vision paper")
    seed_paper("2401.10010", "Read cold start", "A language paper")
    with database_connection(app.config["DATABASE"]) as connection:
        papers = PaperService(connection)
        papers.set_dismissed("2401.10009", True)
        papers.set_read("2401.10010", True)
        result = RecommendationService(
            connection, app.extensions["embedding_index"]
        ).recommend(days=30)
        assert result.cold_start is True
        assert [item["arxiv_id"] for item in result.items] == ["2401.10008"]
