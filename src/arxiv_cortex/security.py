from __future__ import annotations

import hmac
import secrets

from flask import abort, request, session


def csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def validate_csrf() -> None:
    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return
    supplied = request.headers.get("X-CSRFToken") or request.form.get("_csrf_token") or ""
    expected = session.get("_csrf_token", "")
    if not expected or not hmac.compare_digest(expected, supplied):
        abort(400, description="The form security token is missing or expired. Refresh and try again.")
