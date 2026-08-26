"""PATCH /sessions/{session_id}/fields

Manual edits from the person typing directly into the form, plus the
confirm action that promotes an agent-drafted value to attested. Two
writers (the person here, the agent over the WebSocket) share this state,
so every update is a conditional write keyed on `expected_version` — a lost
race comes back as a per-item conflict, not a silent overwrite.
"""
from common import schema as sch
from common.api import ApiError, body_of, caller, handler, path_param
from common.store import (
    VersionConflict,
    confirm_value,
    get_session,
    load_schema,
    set_value,
)


@handler
def lambda_handler(event, _context):
    sid = path_param(event, "session_id")
    body = body_of(event)
    sess = get_session(sid)
    if not sess:
        raise ApiError("session not found", 404)
    if sess.get("owner") not in (caller(event), "anonymous"):
        raise ApiError("forbidden", 403)
    if sess.get("status") != "ready":
        raise ApiError(f"session is {sess.get('status')}, not ready", 409)

    updates = body.get("updates") or []
    if not isinstance(updates, list) or not updates:
        raise ApiError("updates must be a non-empty list", 400)

    fields_by_id = {f.field_id: f for f in sch.schema_from_list(load_schema(sess["schema_key"]))}
    actor = caller(event)

    results = [_apply_one(sid, u, fields_by_id, actor) for u in updates]
    return {
        "results": results,
        "written": sum(1 for r in results if r.get("ok")),
        "rejected": sum(1 for r in results if not r.get("ok")),
    }


def _apply_one(sid: str, u: dict, fields_by_id: dict, actor: str) -> dict:
    fid = u.get("field_id")
    f = fields_by_id.get(fid)
    if not f:
        return {"field_id": fid, "ok": False, "error": "no such field_id"}

    expected_version = u.get("expected_version")

    try:
        # `version` is returned on both paths so the caller adopts what the store
        # actually landed on rather than assuming its own +1. A client that guesses
        # drifts the moment it missed a write, and every later edit to that field
        # comes back as a spurious conflict.
        if u.get("confirm"):
            stored = confirm_value(sid, fid, actor=actor, expected_version=expected_version)
            return {"field_id": fid, "ok": True, "confirmed": True,
                    "version": stored.get("version")}

        value = u.get("value")
        err = sch.validate_value(f, value)
        if err:
            return {"field_id": fid, "ok": False, "error": err}

        # Same normalization the agent's writes go through: a date typed here is
        # stored in the one shape the renderer expects, whichever way it was
        # entered. Returned so the panel can show what was actually stored.
        value = sch.normalize_value(f, value)
        stored = set_value(sid, fid, value, source="user", actor=actor,
                           expected_version=expected_version, confirmed=True)
        return {"field_id": fid, "ok": True, "value": value,
                "version": stored.get("version")}
    except VersionConflict:
        return {"field_id": fid, "ok": False, "error": "version_conflict"}
