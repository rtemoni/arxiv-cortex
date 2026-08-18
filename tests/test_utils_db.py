from __future__ import annotations

import sqlite3

import pytest

from arxiv_cortex.db import database_connection, run_migrations
from arxiv_cortex.services.papers import PaperQuery, PaperService, _fts_expression
from arxiv_cortex.services.search_tags import SearchTagService
from arxiv_cortex.services.settings import SettingsService
from arxiv_cortex.utils import canonicalize_arxiv_id, decode_cursor, encode_cursor


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2401.01234v3", ("2401.01234", 3)),
        ("https://arxiv.org/abs/2401.01234v2", ("2401.01234", 2)),
        ("https://arxiv.org/pdf/hep-ex/0307015v1.pdf", ("hep-ex/0307015", 1)),
    ],
)
def test_canonicalize_arxiv_id(value, expected):
    assert canonicalize_arxiv_id(value) == expected


def test_invalid_arxiv_id_rejected():
    with pytest.raises(ValueError):
        canonicalize_arxiv_id("not-an-id")


def test_cursor_round_trip_and_validation():
    assert decode_cursor(encode_cursor(125)) == 125
    with pytest.raises(ValueError):
        decode_cursor("%%%")


def test_migration_enables_wal_foreign_keys_and_fts(app, seed_paper):
    seed_paper("2401.00001", "Learning visual representations", "A vision transformer study")
    with database_connection(app.config["DATABASE"]) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        migrations = connection.execute("SELECT version FROM schema_migrations").fetchall()
        assert [row["version"] for row in migrations] == ["001", "002", "003", "004"]
        sync_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(sync_runs)")
        }
        assert {
            "current_category",
            "retry_attempt",
            "retry_status",
            "retry_reason",
            "next_attempt_at",
        } <= sync_columns
        page = PaperService(connection).list(PaperQuery(query="transformer"))
        assert [item["arxiv_id"] for item in page.items] == ["2401.00001"]
        tags = SearchTagService(connection).list()
        assert [tag["name"] for tag in tags] == ["Hardware Verification"]
        assert "inference accelerator reliability" in tags[0]["phrases"]
        assert tags[0]["followed"] is False
        tag_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(search_tags)")
        }
        assert {"enabled", "backfill_from", "backfill_complete", "last_updated_watermark"} <= tag_columns
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_search_tags"
        ).fetchone()[0] == 0


def test_saved_and_dismissed_constraint_exists(app, seed_paper):
    seed_paper("2401.00002", "Constraint", "Testing state")
    with database_connection(app.config["DATABASE"]) as connection:
        paper_id = connection.execute(
            "SELECT id FROM papers WHERE arxiv_id = '2401.00002'"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO paper_state(paper_id, saved_at, dismissed_at) VALUES (?, 'x', 'y')",
                (paper_id,),
            )


def test_category_validation_accepts_arxiv_category_families(app):
    with database_connection(app.config["DATABASE"]) as connection:
        result = SettingsService(connection).update_subscriptions(
            ["cs.LG", "cond-mat.mtrl-sci", "physics.optics", "hep-th"]
        )
    assert result == ["cond-mat.mtrl-sci", "cs.LG", "hep-th", "physics.optics"]


def test_search_tag_crud_normalizes_keyword_phrases(app):
    with database_connection(app.config["DATABASE"]) as connection:
        service = SearchTagService(connection)
        tag = service.create(
            "  Interpretability  ",
            "  Model internals and representations. ",
            "activation steering, linear probes\nActivation Steering",
        )
        assert tag["name"] == "Interpretability"
        assert tag["phrases"] == ["activation steering", "linear probes"]

        updated = service.update(tag["id"], "Model internals", "", "sparse autoencoder")
        assert updated["query"] == "sparse autoencoder"
        service.delete(tag["id"])
        assert service.get(tag["id"]) is None


def test_search_tags_can_be_the_only_followed_sources(app):
    with database_connection(app.config["DATABASE"]) as connection:
        settings = SettingsService(connection)
        tags = SearchTagService(connection)
        first = tags.create(
            "Alignment evaluations",
            "",
            "alignment evaluation\ndeceptive alignment",
            followed=True,
            backfill_days=30,
        )
        second = tags.create("Saved for later", "", "activation atlas")

        assert settings.update_subscriptions([]) == []
        assert settings.has_feed_sources() is True
        assert [tag["id"] for tag in tags.list(followed_only=True)] == [first["id"]]
        assert first["arxiv_query"] == '(all:"alignment evaluation" OR all:"deceptive alignment")'

        tags.set_followed([second["id"]], backfill_days=90)
        assert [tag["id"] for tag in tags.list(followed_only=True)] == [second["id"]]
        assert tags.get(first["id"])["followed"] is False


def test_search_tags_persist_when_migrations_rerun(app):
    database = app.config["DATABASE"]
    with database_connection(database) as connection:
        created = SearchTagService(connection).create(
            "Persistent tag",
            "Survives application restarts and redeploy migrations.",
            "durable research state",
            followed=True,
        )

    run_migrations(database)

    with database_connection(database) as connection:
        restored = SearchTagService(connection).get(created["id"])
        assert restored is not None
        assert restored["name"] == "Persistent tag"
        assert restored["followed"] is True


def test_search_tag_validation_rejects_empty_and_duplicate_names(app):
    with database_connection(app.config["DATABASE"]) as connection:
        service = SearchTagService(connection)
        with pytest.raises(ValueError, match="at least one keyword"):
            service.create("Empty", "", " , ; ")
        with pytest.raises(ValueError, match="already exists"):
            service.create("hardware verification", "", "accelerator")


def test_grouped_search_never_truncates_an_exact_phrase():
    query = _fts_expression(
        "one two three four five six seven eight;"
        "nine ten eleven twelve thirteen fourteen fifteen sixteen;"
        "seventeen eighteen nineteen twenty twentyone twentytwo twentythree twentytwo;"
        "alpha beta gamma delta epsilon zeta eta theta;"
        "iota kappa lambda mu nu xi omicron pi;"
        "short group;"
        "this final phrase would cross the bounded term limit"
    )
    assert '("short group")' in query
    assert "this final phrase" not in query
