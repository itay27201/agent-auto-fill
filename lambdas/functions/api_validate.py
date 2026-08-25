"""POST /sessions/{session_id}/validate

Read-only check: format errors, missing required fields, and drafts still
awaiting the human's confirmation. Confirming a value is a PATCH
/fields action, not this endpoint's job.
"""
from common import schema as sch
from common.api import ApiError, caller, handler, path_param
from common.store import get_session, get_values, load_schema


@handler
def lambda_handler(event, _context):
    sid = path_param(event, "session_id")
    sess = get_session(sid)
    if not sess:
        raise ApiError("session not found", 404)
    if sess.get("owner") not in (caller(event), "anonymous"):
        raise ApiError("forbidden", 403)
    if sess.get("status") != "ready":
        raise ApiError(f"session is {sess.get('status')}, not ready", 409)

    fields = sch.schema_from_list(load_schema(sess["schema_key"]))
    values = get_values(sid)
    return sch.validate_all(fields, values)
