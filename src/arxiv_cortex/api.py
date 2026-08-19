from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request, send_from_directory

from arxiv_cortex.db import get_db
from arxiv_cortex.services.papers import PaperQuery, PaperService
from arxiv_cortex.services.recommendations import RecommendationService
from arxiv_cortex.services.settings import SettingsService
from arxiv_cortex.utils import decode_cursor, encode_cursor

api = Blueprint("api", __name__)


@api.get("/health")
def health():
    connection = get_db()
    settings = SettingsService(connection)
    papers = PaperService(connection)
    model_id = settings.get("embedding_model", "") or ""
    last_run = connection.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    field_subscriptions = len(settings.subscriptions(enabled_only=True))
    research_tags = int(connection.execute("SELECT COUNT(*) FROM search_tags").fetchone()[0])
    tag_subscriptions = int(
        connection.execute("SELECT COUNT(*) FROM search_tags WHERE enabled = 1").fetchone()[0]
    )
    return jsonify(
        {
            "data": {
                "status": "ok",
                "papers": papers.count(),
                "embeddings": papers.embedding_count(model_id),
                "subscriptions": field_subscriptions + tag_subscriptions,
                "field_subscriptions": field_subscriptions,
                "research_tags": research_tags,
                "tag_subscriptions": tag_subscriptions,
                "last_sync": dict(last_run) if last_run else None,
            }
        }
    )


@api.get("/papers")
def papers_list():
    try:
        offset = decode_cursor(request.args.get("cursor"))
    except ValueError as error:
        return _error("invalid_cursor", str(error), 400)
    limit = _limit()
    if limit is None:
        return _error("invalid_limit", "limit must be between 1 and 100", 400)
    sort = request.args.get("sort", "newest")
    days = _integer("days", 30 if sort == "recommended" else 0)
    if days is None or days < 0:
        return _error("invalid_days", "days must be a non-negative integer", 400)
    state = request.args.get("state") or None
    if state not in {None, "saved", "read", "unread", "dismissed"}:
        return _error("invalid_state", "Unsupported paper state", 400)
    connection = get_db()
    if sort == "recommended":
        result = RecommendationService(
            connection, current_app.extensions["embedding_index"]
        ).recommend(
            category=request.args.get("category", ""),
            days=days,
            offset=offset,
            limit=limit,
        )
        items = result.items
        total = None
        has_next = len(items) == limit
    elif sort in {"newest", "oldest", "relevance"}:
        query_text = request.args.get("q", "")
        page = PaperService(connection).list(
            PaperQuery(
                query=query_text,
                category=request.args.get("category", ""),
                days=days,
                state=state,
                active_categories_only=not bool(query_text.strip()),
                sort="oldest" if sort == "oldest" else "newest",
                offset=offset,
                limit=limit,
            )
        )
        items, total, has_next = page.items, page.total, page.has_next
    else:
        return _error("invalid_sort", "sort must be newest, oldest, relevance, or recommended", 400)
    return jsonify(
        {
            "data": [_public(item) for item in items],
            "meta": {
                "limit": limit,
                "total": total,
                "next_cursor": encode_cursor(offset + limit) if has_next else None,
            },
        }
    )


@api.get("/papers/<path:arxiv_id>")
def paper_get(arxiv_id: str):
    paper = PaperService(get_db()).get(arxiv_id)
    if not paper:
        return _error("not_found", f"Paper {arxiv_id} was not found", 404)
    return jsonify({"data": _public(paper)})


@api.get("/papers/<path:arxiv_id>/similar")
def paper_similar(arxiv_id: str):
    limit = _limit()
    if limit is None:
        return _error("invalid_limit", "limit must be between 1 and 100", 400)
    days = _integer("days", 0)
    if days is None or days < 0:
        return _error("invalid_days", "days must be a non-negative integer", 400)
    try:
        offset = decode_cursor(request.args.get("cursor"))
        items = RecommendationService(
            get_db(), current_app.extensions["embedding_index"]
        ).similar(
            arxiv_id,
            category=request.args.get("category", ""),
            days=days,
            offset=offset,
            limit=limit,
        )
    except LookupError:
        return _error("not_found", f"Paper {arxiv_id} was not found", 404)
    except ValueError as error:
        return _error("invalid_cursor", str(error), 400)
    return jsonify(
        {
            "data": [_public(item) for item in items],
            "meta": {
                "limit": limit,
                "next_cursor": encode_cursor(offset + limit) if len(items) == limit else None,
            },
        }
    )


@api.get("/recommendations")
def recommendations():
    limit = _limit()
    if limit is None:
        return _error("invalid_limit", "limit must be between 1 and 100", 400)
    days = _integer("days", 30)
    if days not in {0, 7, 30, 90}:
        return _error("invalid_days", "days must be 0, 7, 30, or 90", 400)
    try:
        offset = decode_cursor(request.args.get("cursor"))
    except ValueError as error:
        return _error("invalid_cursor", str(error), 400)
    result = RecommendationService(
        get_db(), current_app.extensions["embedding_index"]
    ).recommend(
        category=request.args.get("category", ""),
        days=days,
        offset=offset,
        limit=limit,
    )
    return jsonify(
        {
            "data": [_public(item) for item in result.items],
            "meta": {
                "cold_start": result.cold_start,
                "reason": result.reason,
                "next_cursor": encode_cursor(offset + limit)
                if len(result.items) == limit
                else None,
            },
        }
    )


@api.get("/openapi.json")
def openapi():
    return send_from_directory(current_app.static_folder, "openapi.json")


def _public(item: dict) -> dict:
    private_fields = {"database_id", "citation_count", "citation_updated_at"}
    return {key: value for key, value in item.items() if key not in private_fields}


def _limit() -> int | None:
    value = _integer("limit", 25)
    maximum = int(current_app.config["API_MAX_LIMIT"])
    return value if value is not None and 1 <= value <= maximum else None


def _integer(name: str, default: int) -> int | None:
    try:
        return int(request.args.get(name, str(default)))
    except ValueError:
        return None


def _error(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message}}), status
