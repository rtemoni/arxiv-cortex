from __future__ import annotations

import pytest

from arxiv_cortex.db import database_connection
from arxiv_cortex.services.groups import PaperGroupService
from arxiv_cortex.services.papers import PaperQuery, PaperService


def test_groups_are_many_to_many_and_filter_saved_papers(app, seed_paper):
    seed_paper("2401.30001", "Grouped paper", "A paper for two projects")
    seed_paper("2401.30002", "Other saved paper", "A paper outside the project")

    with database_connection(app.config["DATABASE"]) as connection:
        papers = PaperService(connection)
        papers.set_saved("2401.30001", True)
        papers.set_saved("2401.30002", True)
        groups = PaperGroupService(connection)
        thesis = groups.create("  Thesis   reading  ")
        seminar = groups.create("Seminar")

        assigned = groups.assign_paper(
            "2401.30001", [thesis["id"], seminar["id"], thesis["id"]]
        )
        assert [group["name"] for group in assigned] == ["Seminar", "Thesis reading"]
        assert [group["paper_count"] for group in groups.list()] == [1, 1]

        page = papers.list(
            PaperQuery(
                state="saved",
                group_id=thesis["id"],
                active_categories_only=False,
            )
        )
        assert [paper["arxiv_id"] for paper in page.items] == ["2401.30001"]

        groups.attach_to_papers(page.items)
        assert {group["name"] for group in page.items[0]["groups"]} == {
            "Seminar",
            "Thesis reading",
        }


def test_group_validation_and_removing_from_library_clear_membership(app, seed_paper):
    seed_paper("2401.30003", "Temporary library paper", "An organized paper")

    with database_connection(app.config["DATABASE"]) as connection:
        papers = PaperService(connection)
        groups = PaperGroupService(connection)
        group = groups.create("Experiments")

        with pytest.raises(LookupError):
            groups.assign_paper("2401.30003", [group["id"]])

        papers.set_saved("2401.30003", True)
        groups.assign_paper("2401.30003", [group["id"]])
        papers.set_saved("2401.30003", False)
        assert groups.get(group["id"])["paper_count"] == 0

        with pytest.raises(ValueError, match="already exists"):
            groups.create(" experiments ")
        with pytest.raises(ValueError, match="Enter a name"):
            groups.create("   ")

        deleted_name = groups.delete(group["id"])
        assert deleted_name == "Experiments"
        assert papers.get("2401.30003") is not None
