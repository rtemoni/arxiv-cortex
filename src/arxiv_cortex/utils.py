from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

ARXIV_VERSION_RE = re.compile(r"v(?P<version>\d+)$", re.IGNORECASE)
ARXIV_ID_RE = re.compile(
    r"^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})$", re.IGNORECASE
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime | None = None) -> str:
    current = value or utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def canonicalize_arxiv_id(value: str) -> tuple[str, int]:
    candidate = value.strip().rstrip("/")
    if "/abs/" in candidate:
        candidate = candidate.split("/abs/", 1)[1]
    elif "/pdf/" in candidate:
        candidate = candidate.split("/pdf/", 1)[1]
    candidate = candidate.removesuffix(".pdf")
    match = ARXIV_VERSION_RE.search(candidate)
    version = int(match.group("version")) if match else 1
    base_id = candidate[: match.start()] if match else candidate
    if not ARXIV_ID_RE.match(base_id):
        raise ValueError(f"Invalid arXiv identifier: {value}")
    return base_id, version


def paper_content_hash(title: str, abstract: str) -> str:
    normalized = f"{title.strip()}\n\n{abstract.strip()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def metadata_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(max(0, offset)).encode()).decode().rstrip("=")


def decode_cursor(value: str | None) -> int:
    if not value:
        return 0
    try:
        padded = value + "=" * (-len(value) % 4)
        return max(0, int(base64.urlsafe_b64decode(padded).decode()))
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Invalid cursor") from None


def sanitize_error(error: BaseException, limit: int = 500) -> str:
    message = " ".join(str(error).split()) or error.__class__.__name__
    return message[:limit]
