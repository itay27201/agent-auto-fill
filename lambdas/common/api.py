"""REST plumbing shared by every api_* handler: CORS, JSON in/out, and
error translation. Kept intentionally thin — this is not a framework.
"""
from __future__ import annotations

import base64
import functools
import json
import logging

from . import config

log = logging.getLogger()
log.setLevel(logging.INFO)


class ApiError(Exception):
    """Raise this to end a handler with a specific status code and, optionally,
    extra fields merged into the error body (e.g. validation details)."""

    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra


def body_of(event: dict) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ApiError(f"invalid JSON body: {e}", 400) from e


def path_param(event: dict, name: str) -> str:
    value = (event.get("pathParameters") or {}).get(name)
    if not value:
        raise ApiError(f"missing path parameter: {name}", 400)
    return value


def caller(event: dict) -> str:
    """Who is making this request. Prefers a Cognito claim so this keeps
    working unchanged once an authorizer is attached to the API — until
    then every request is 'anonymous', which is the accepted state for a
    dev-only deployment with no real documents flowing through it."""
    claims = (
        (event.get("requestContext") or {}).get("authorizer") or {}
    ).get("claims") or {}
    return claims.get("sub") or "anonymous"


_CORS_HEADERS = {
    "Access-Control-Allow-Origin": config.ALLOWED_ORIGIN,
    "Access-Control-Allow-Headers": "content-type,authorization",
    "Access-Control-Allow-Methods": "GET,POST,PATCH,OPTIONS",
}


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {**_CORS_HEADERS, "Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def handler(fn):
    """Wrap a REST Lambda handler: JSON body in, JSON response out, CORS
    headers on every response (including errors), ApiError -> its status
    code, anything else -> a logged 500."""

    @functools.wraps(fn)
    def wrapped(event, context):
        try:
            result = fn(event, context)
            return _response(200, result if result is not None else {})
        except ApiError as e:
            return _response(e.status, {"message": e.message, **e.extra})
        except Exception as e:  # noqa: BLE001 - last line of defense for a Lambda handler
            log.exception("unhandled error in %s", fn.__name__)
            return _response(500, {"message": f"{type(e).__name__}: {e}"})

    return wrapped
