"""PATCH /sessions/{session_id}/schema — move a field's box.

Until this existed there was no way, anywhere in the system, to correct a box
that ingest had put in the wrong place. `api_set_fields` writes values,
`api_catalog_update` writes metadata and guide prose, the authoring agent writes
guide prose. A wrong bbox was permanent, and worse than permanent: it went into
the SHA-256 registry, which has no TTL, so every later upload of that form
inherited it.

Deliberately a separate route from `PATCH .../fields`. That one does per-field
conditional writes on `version` because two writers — the person and the agent —
race on the same values. Geometry has exactly one writer, a person dragging a
box, and it lives in a single S3 object that is rewritten whole. Sharing a route
would mean sharing a concurrency story that fits neither.

A correction here is session-local. Pushing it up to the catalog, so the next
person to pick the form gets the fixed box, is `PATCH /catalog/{cid}` with
`schema_updates` — a separate, deliberate act, the same way publishing a guide
is.
"""
import logging

from common import form_map, geometry as geo
from common.api import ApiError, body_of, caller, handler, path_param
from common.store import append_event, get_session, load_schema, put_form_map, put_schema

log = logging.getLogger()
log.setLevel(logging.INFO)


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

    fields = load_schema(sess["schema_key"])
    by_id = {f.get("field_id"): f for f in fields}
    actor = caller(event)

    results, changed = [], 0
    for u in updates:
        result = _apply_one(u, by_id, len(fields))
        results.append(result)
        if result.get("ok"):
            changed += 1

    if not changed:
        return {"results": results, "written": 0,
                "rejected": len(results) - changed, "fields": fields}

    # One rewrite for the whole batch. The schema is a single object; writing it
    # once per field would multiply the window in which a crash leaves half a
    # correction applied.
    put_schema(sid, fields)
    _refresh_form_map(sid, sess, fields)
    append_event(sid, "bbox_fixed", actor=actor,
                 field_ids=",".join(r["field_id"] for r in results if r.get("ok"))[:400])

    log.info("session %s: %d boxes placed by hand", sid, changed)
    return {"results": results, "written": changed,
            "rejected": len(results) - changed, "fields": fields}


def _apply_one(u: dict, by_id: dict, total: int) -> dict:
    fid = u.get("field_id")
    f = by_id.get(fid)
    if not f:
        return {"field_id": fid, "ok": False, "error": "no such field_id"}

    bbox = u.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return {"field_id": fid, "ok": False, "error": "bbox must be [x0, y0, x1, y1]"}
    try:
        bbox = geo.clamp([float(v) for v in bbox])
    except (TypeError, ValueError):
        return {"field_id": fid, "ok": False, "error": "bbox values must be numbers"}

    if geo.area(bbox) <= 0:
        return {"field_id": fid, "ok": False, "error": "bbox has no area"}

    page = u.get("page", f.get("page", 1))
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        return {"field_id": fid, "ok": False, "error": "page must be a number"}

    f["bbox"] = bbox
    f["page"] = page
    # A person looking at the page outranks anything ingest decided, including
    # its own refusal to place the box. `sanity_check` exists to stop a model
    # guessing; it has no business overruling someone who can see the form.
    f["bbox_confidence"] = "ok"
    f["bbox_source"] = "user"
    f["bbox_note"] = ""
    return {"field_id": fid, "ok": True, "bbox": bbox, "page": page,
            "of_total": total}


def _refresh_form_map(sid: str, sess: dict, fields: list[dict]) -> None:
    """The map describes where boxes are, so moving one makes it stale — and a
    stale map is worse than none, because the agent trusts it. Regenerating is
    pure computation over the schema, so it is cheaper than reasoning about
    whether this particular edit mattered."""
    if not sess.get("form_map_key"):
        return
    try:
        put_form_map(sid, form_map.render(fields))
    except Exception:
        # The boxes are already saved. A stale map is a worse prompt, not a lost
        # correction, so this must not fail the request that fixed them.
        log.exception("could not regenerate the form map for %s", sid)
