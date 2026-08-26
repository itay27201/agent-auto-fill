"""POST /catalog/{catalog_id}/sessions — start filling a known form.

This is the point of the catalog. The normal path is an upload followed by a
four-step state machine with a Bedrock vision pass in it; a person waits, and
the system pays, to rediscover a schema it already has. Here there is nothing
to discover: copy the schema, seed the fields, done. No S3 upload, no Step
Functions execution, no model call — a couple of hundred milliseconds, and the
frontend's first poll already returns `ready`.

The session points at the catalog's master document and its page images rather
than copying them. Page rasters are the bulk of the bytes and they are
identical for every person filling the same form.
"""
import uuid

from common import catalog as cat, config
from common.api import ApiError, caller, handler, path_param
from common.store import (
    copy_schema,
    create_session,
    load_schema,
    put_form_map,
    seed_fields,
    update_session,
)


@handler
def lambda_handler(event, _context):
    cid = path_param(event, "catalog_id")
    try:
        entry = cat.get(cid)
    except cat.NotFound:
        raise ApiError("catalog entry not found", 404) from None

    if entry.get("status") != cat.PUBLISHED:
        raise ApiError("this form is still a draft and cannot be filled yet", 409)

    sid = uuid.uuid4().hex
    create_session(sid, entry["source_key"], entry.get("name") or cid, caller(event))

    schema_key = copy_schema(entry["schema_key"], sid)
    fields = load_schema(schema_key)
    seed_fields(sid, fields)

    # Copied rather than referenced, for the same reason the schema is: a person
    # who moves a box in this session must not edit the catalog's master. That
    # is a separate, deliberate act — see api_catalog_update's schema_updates.
    form_map = cat.load_guide_markdown(entry.get("form_map_key"))
    form_map_key = put_form_map(sid, form_map) if form_map else ""

    session = update_session(
        sid,
        status="ready",
        progress="ready",
        # The master lives in ArtifactsBucket, not DocsBucket — DocsBucket
        # expires its whole contents after seven days. api_render reads this.
        doc_bucket=config.ARTIFACTS_BUCKET,
        doc_type=entry.get("doc_type"),
        schema_key=schema_key,
        form_map_key=form_map_key,
        page_keys=entry.get("page_keys") or [],
        page_count=len(entry.get("page_keys") or []),
        field_count=len(fields),
        catalog_id=cid,
        guide_key=entry.get("guide_key") or "",
    )

    return {"session_id": sid, "status": session["status"], "catalog_id": cid}
