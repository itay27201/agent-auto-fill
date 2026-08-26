"""Step 4 of ingest, plus the Step Functions failure handler.

Cache hit: reuse the registry's schema for this session instead of writing
a fresh copy. Cache miss: persist the schema this run just produced and add
it to the registry so the next person to upload this form skips ingest
almost entirely.
"""
import logging

from common.store import (
    copy_form_map,
    copy_schema,
    put_form_map,
    put_schema,
    registry_store,
    seed_fields,
    update_session,
)

log = logging.getLogger()
log.setLevel(logging.INFO)


def lambda_handler(event, _context):
    sid = event["session_id"]
    fields = event["fields"]

    if event.get("cache_hit"):
        schema_key = copy_schema(event["cached_schema_key"], sid)
        # The map is cached beside the schema, so a hit gets the layout
        # knowledge too. Entries written before form maps existed have none.
        form_map_key = (copy_form_map(event["cached_form_map_key"], sid)
                        if event.get("cached_form_map_key") else "")
    else:
        schema_key = put_schema(sid, fields)
        form_map_key = put_form_map(sid, event.get("form_map") or "")
        registry_store(event["doc_hash"], schema_key, event["doc_type"],
                       form_map_key=form_map_key)

    seed_fields(sid, fields)
    unplaced = [f["field_id"] for f in fields if f.get("bbox_confidence") == "low"]
    update_session(
        sid,
        status="ready",
        schema_key=schema_key,
        form_map_key=form_map_key,
        doc_type=event["doc_type"],
        progress="ready",
        field_count=len(fields),
        # Surfaced so the viewer can say "3 of 47 boxes need placing" rather
        # than silently drawing 44 boxes and looking complete.
        unplaced_count=len(unplaced),
    )
    log.info("session %s ready with %d fields, %d unplaced", sid, len(fields), len(unplaced))
    return {"session_id": sid, "status": "ready", "field_count": len(fields),
            "unplaced_count": len(unplaced)}


def on_failure(event, _context):
    """Step Functions Catch target. `session_id` survives into `event`
    because every Task's Catch uses ResultPath "$.error" instead of "$"."""
    sid = event.get("session_id")
    error = event.get("error") or {}
    log.error("ingest failed for session %s: %s", sid, error)
    if sid:
        update_session(sid, status="failed", progress="failed", error=str(error)[:2000])
    return {"session_id": sid, "status": "failed"}
