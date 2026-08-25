"""Step 4 of ingest, plus the Step Functions failure handler.

Cache hit: reuse the registry's schema for this session instead of writing
a fresh copy. Cache miss: persist the schema this run just produced and add
it to the registry so the next person to upload this form skips ingest
almost entirely.
"""
import logging

from common.store import (
    copy_schema,
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
    else:
        schema_key = put_schema(sid, fields)
        registry_store(event["doc_hash"], schema_key, event["doc_type"])

    seed_fields(sid, fields)
    update_session(
        sid,
        status="ready",
        schema_key=schema_key,
        doc_type=event["doc_type"],
        progress="ready",
        field_count=len(fields),
    )
    log.info("session %s ready with %d fields", sid, len(fields))
    return {"session_id": sid, "status": "ready", "field_count": len(fields)}


def on_failure(event, _context):
    """Step Functions Catch target. `session_id` survives into `event`
    because every Task's Catch uses ResultPath "$.error" instead of "$"."""
    sid = event.get("session_id")
    error = event.get("error") or {}
    log.error("ingest failed for session %s: %s", sid, error)
    if sid:
        update_session(sid, status="failed", progress="failed", error=str(error)[:2000])
    return {"session_id": sid, "status": "failed"}
