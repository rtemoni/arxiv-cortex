from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

import numpy as np

from arxiv_cortex.services.embeddings import EmbeddingIndex
from arxiv_cortex.services.papers import PaperQuery, PaperService


@dataclass(slots=True)
class RecommendationResult:
    items: list[dict[str, Any]]
    cold_start: bool
    reason: str


class RecommendationService:
    def __init__(self, connection: sqlite3.Connection, index: EmbeddingIndex):
        self.connection = connection
        self.index = index
        self.papers = PaperService(connection)

    def similar(
        self,
        arxiv_id: str,
        *,
        category: str = "",
        days: int = 0,
        limit: int = 25,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        source = self.papers.get(arxiv_id)
        if not source:
            raise LookupError(f"Paper {arxiv_id} not found")
        ids, matrix, positions = self.index.snapshot()
        source_id = int(source["database_id"])
        position = positions.get(source_id)
        if position is None or matrix.size == 0:
            return []
        candidates = self.papers.candidate_ids(category=category, days=days)
        candidates.discard(source_id)
        scores = matrix @ matrix[position]
        ranked = [
            (int(ids[index]), float(scores[index]))
            for index in np.argsort(-scores)
            if int(ids[index]) in candidates
        ]
        ranked = ranked[offset : offset + limit]
        items = self.papers.get_by_database_ids([paper_id for paper_id, _ in ranked])
        score_by_id = dict(ranked)
        for item in items:
            item["score"] = score_by_id[int(item["database_id"])]
        return items

    def recommend(
        self,
        *,
        category: str = "",
        days: int = 30,
        limit: int = 25,
        offset: int = 0,
    ) -> RecommendationResult:
        saved_ids = self.papers.state_ids("saved_at")
        if not saved_ids:
            page = self.papers.list(
                PaperQuery(
                    category=category,
                    days=days,
                    exclude_interacted=True,
                    offset=offset,
                    limit=limit,
                )
            )
            return RecommendationResult(
                items=page.items,
                cold_start=True,
                reason="Save a few papers and this feed will learn your interests.",
            )

        ids, matrix, positions = self.index.snapshot()
        positive_positions = [positions[paper_id] for paper_id in saved_ids if paper_id in positions]
        if not positive_positions or matrix.size == 0:
            return RecommendationResult(
                items=[],
                cold_start=True,
                reason="Your saved papers are waiting to be embedded.",
            )
        profile = matrix[positive_positions].mean(axis=0)
        dismissed_ids = self.papers.state_ids("dismissed_at")
        negative_positions = [positions[paper_id] for paper_id in dismissed_ids if paper_id in positions]
        if negative_positions:
            profile = profile - 0.35 * matrix[negative_positions].mean(axis=0)
        norm = float(np.linalg.norm(profile))
        if norm <= np.finfo(np.float32).eps:
            return RecommendationResult([], True, "Add more varied papers to refine the profile.")
        profile = profile / norm

        candidates = self.papers.candidate_ids(
            category=category,
            days=days,
            exclude_interacted=True,
        )
        scores = matrix @ profile
        ranked = [
            (int(ids[index]), float(scores[index]))
            for index in np.argsort(-scores)
            if int(ids[index]) in candidates
        ]
        ranked = ranked[offset : offset + limit]
        items = self.papers.get_by_database_ids([paper_id for paper_id, _ in ranked])
        score_by_id = dict(ranked)

        saved_items = self.papers.get_by_database_ids(saved_ids)
        saved_title_by_id = {int(item["database_id"]): item["title"] for item in saved_items}
        for item in items:
            paper_id = int(item["database_id"])
            item["score"] = score_by_id[paper_id]
            candidate_vector = matrix[positions[paper_id]]
            influences = sorted(
                (
                    (float(candidate_vector @ matrix[positions[saved_id]]), saved_id)
                    for saved_id in saved_ids
                    if saved_id in positions
                ),
                reverse=True,
            )[:3]
            item["why"] = [saved_title_by_id[saved_id] for _, saved_id in influences]
        return RecommendationResult(
            items=items,
            cold_start=False,
            reason="Ranked from your library, with dismissed papers used as negative feedback.",
        )
