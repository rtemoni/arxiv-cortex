from __future__ import annotations

import sqlite3
from typing import Any

from arxiv_cortex.db import transaction
from arxiv_cortex.utils import isoformat

MAX_GROUP_NAME_LENGTH = 60


class PaperGroupService:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def list(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT pg.*, COUNT(ps.paper_id) AS paper_count
            FROM paper_groups pg
            LEFT JOIN paper_group_memberships pgm ON pgm.group_id = pg.id
            LEFT JOIN paper_state ps
              ON ps.paper_id = pgm.paper_id AND ps.saved_at IS NOT NULL
            GROUP BY pg.id
            ORDER BY pg.name COLLATE NOCASE, pg.id
            """
        ).fetchall()
        return [self._serialize(row) for row in rows]

    def get(self, group_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT pg.*, COUNT(ps.paper_id) AS paper_count
            FROM paper_groups pg
            LEFT JOIN paper_group_memberships pgm ON pgm.group_id = pg.id
            LEFT JOIN paper_state ps
              ON ps.paper_id = pgm.paper_id AND ps.saved_at IS NOT NULL
            WHERE pg.id = ?
            GROUP BY pg.id
            """,
            (group_id,),
        ).fetchone()
        return self._serialize(row) if row else None

    def get_by_name(self, name: str) -> dict[str, Any] | None:
        normalized = self._validate_name(name)
        row = self.connection.execute(
            "SELECT id FROM paper_groups WHERE name = ? COLLATE NOCASE", (normalized,)
        ).fetchone()
        return self.get(int(row["id"])) if row else None

    def create(self, name: str) -> dict[str, Any]:
        normalized = self._validate_name(name)
        now = isoformat()
        try:
            with transaction(self.connection):
                cursor = self.connection.execute(
                    "INSERT INTO paper_groups(name, created_at, updated_at) VALUES (?, ?, ?)",
                    (normalized, now, now),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("A group with that name already exists") from error
        return self.get(int(cursor.lastrowid))  # type: ignore[return-value]

    def rename(self, group_id: int, name: str) -> dict[str, Any]:
        normalized = self._validate_name(name)
        try:
            with transaction(self.connection):
                cursor = self.connection.execute(
                    "UPDATE paper_groups SET name = ?, updated_at = ? WHERE id = ?",
                    (normalized, isoformat(), group_id),
                )
                if cursor.rowcount == 0:
                    raise LookupError(f"Group {group_id} not found")
        except sqlite3.IntegrityError as error:
            raise ValueError("A group with that name already exists") from error
        return self.get(group_id)  # type: ignore[return-value]

    def delete(self, group_id: int) -> str:
        group = self.get(group_id)
        if not group:
            raise LookupError(f"Group {group_id} not found")
        with transaction(self.connection):
            self.connection.execute("DELETE FROM paper_groups WHERE id = ?", (group_id,))
        return str(group["name"])

    def assign_paper(self, arxiv_id: str, group_ids: list[int]) -> list[dict[str, Any]]:
        unique_group_ids = list(dict.fromkeys(group_ids))
        paper = self.connection.execute(
            """
            SELECT p.id
            FROM papers p
            JOIN paper_state ps ON ps.paper_id = p.id AND ps.saved_at IS NOT NULL
            WHERE p.arxiv_id = ?
            """,
            (arxiv_id,),
        ).fetchone()
        if not paper:
            raise LookupError(f"Saved paper {arxiv_id} not found")

        if unique_group_ids:
            placeholders = ",".join("?" for _ in unique_group_ids)
            valid_ids = {
                int(row["id"])
                for row in self.connection.execute(
                    f"SELECT id FROM paper_groups WHERE id IN ({placeholders})",  # noqa: S608
                    unique_group_ids,
                )
            }
            if valid_ids != set(unique_group_ids):
                raise ValueError("One of the selected groups no longer exists")

        paper_id = int(paper["id"])
        with transaction(self.connection):
            self.connection.execute(
                "DELETE FROM paper_group_memberships WHERE paper_id = ?", (paper_id,)
            )
            self.connection.executemany(
                """
                INSERT INTO paper_group_memberships(group_id, paper_id, added_at)
                VALUES (?, ?, ?)
                """,
                [(group_id, paper_id, isoformat()) for group_id in unique_group_ids],
            )
        return self.for_paper_ids([paper_id]).get(paper_id, [])

    def add_paper(self, arxiv_id: str, group_id: int) -> None:
        paper = self.connection.execute(
            """
            SELECT p.id
            FROM papers p
            JOIN paper_state ps ON ps.paper_id = p.id AND ps.saved_at IS NOT NULL
            WHERE p.arxiv_id = ?
            """,
            (arxiv_id,),
        ).fetchone()
        if not paper:
            raise LookupError(f"Saved paper {arxiv_id} not found")
        if not self.get(group_id):
            raise LookupError(f"Group {group_id} not found")
        with transaction(self.connection):
            self.connection.execute(
                """
                INSERT OR IGNORE INTO paper_group_memberships(group_id, paper_id, added_at)
                VALUES (?, ?, ?)
                """,
                (group_id, int(paper["id"]), isoformat()),
            )

    def save_paper(self, arxiv_id: str, group_id: int | None = None) -> None:
        paper = self.connection.execute(
            "SELECT id FROM papers WHERE arxiv_id = ?", (arxiv_id,)
        ).fetchone()
        if not paper:
            raise LookupError(f"Paper {arxiv_id} not found")
        if group_id is not None and not self.get(group_id):
            raise LookupError(f"Group {group_id} not found")
        paper_id = int(paper["id"])
        now = isoformat()
        with transaction(self.connection):
            self.connection.execute(
                """
                INSERT INTO paper_state(paper_id, saved_at)
                VALUES (?, ?)
                ON CONFLICT(paper_id) DO UPDATE SET
                    saved_at = excluded.saved_at,
                    dismissed_at = NULL
                """,
                (paper_id, now),
            )
            if group_id is not None:
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO paper_group_memberships(group_id, paper_id, added_at)
                    VALUES (?, ?, ?)
                    """,
                    (group_id, paper_id, now),
                )

    def attach_to_papers(self, papers: list[dict[str, Any]]) -> None:
        groups_by_paper = self.for_paper_ids(
            [int(paper["database_id"]) for paper in papers]
        )
        for paper in papers:
            paper["groups"] = groups_by_paper.get(int(paper["database_id"]), [])

    def for_paper_ids(self, paper_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        if not paper_ids:
            return {}
        placeholders = ",".join("?" for _ in paper_ids)
        rows = self.connection.execute(
            f"""
            SELECT pgm.paper_id, pg.id, pg.name
            FROM paper_group_memberships pgm
            JOIN paper_groups pg ON pg.id = pgm.group_id
            WHERE pgm.paper_id IN ({placeholders})
            ORDER BY pg.name COLLATE NOCASE, pg.id
            """,  # noqa: S608
            paper_ids,
        ).fetchall()
        output: dict[int, list[dict[str, Any]]] = {paper_id: [] for paper_id in paper_ids}
        for row in rows:
            output[int(row["paper_id"])].append(
                {"id": int(row["id"]), "name": row["name"]}
            )
        return output

    @staticmethod
    def _validate_name(name: str) -> str:
        normalized = " ".join(name.split())
        if not normalized:
            raise ValueError("Enter a name for the group")
        if len(normalized) > MAX_GROUP_NAME_LENGTH:
            raise ValueError(f"Group names must be {MAX_GROUP_NAME_LENGTH} characters or fewer")
        return normalized

    @staticmethod
    def _serialize(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "name": row["name"],
            "paper_count": int(row["paper_count"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
