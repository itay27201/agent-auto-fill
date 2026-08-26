"""Step 1 of ingest, plus the S3 trigger that kicks the whole pipeline off.

`_detect` only asks two questions: does the file have real AcroForm fields,
and how many pages does it have. Everything else — labels, sections, what a
flat scan's boxes look like — is enrich's job, not this one's.
"""
import hashlib
import io
import json
import logging

from common import config
from common.aws import s3, sfn
from common.store import get_session, load_schema, registry_lookup, update_session

log = logging.getLogger()
log.setLevel(logging.INFO)


def on_upload(event, _context):
    """S3 ObjectCreated trigger on uploads/{sid}/{filename}. Starts the
    ingest state machine; classification itself happens as the machine's
    first step so it's retryable/observable like the rest of the pipeline."""
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
        sid = _sid_from_key(key)
        if not sid:
            log.warning("upload key does not match uploads/{sid}/... convention: %s", key)
            continue

        sess = get_session(sid)
        if not sess or sess.get("doc_key") != key:
            # A stale or duplicate event for a session that no longer
            # matches this key — ignore rather than process the wrong doc.
            log.info("ignoring upload event for unknown/mismatched session: %s", key)
            continue

        update_session(sid, status="processing", progress="classifying")
        sfn().start_execution(
            stateMachineArn=config.INGEST_STATE_MACHINE_ARN,
            name=f"{sid}-{record['eventTime'].replace(':', '-')}",
            input=json.dumps({"session_id": sid, "key": key, "bucket": bucket}),
        )


def lambda_handler(event, _context):
    """State machine step 1: classify the document and check the schema
    registry for a free ride on a form we've already seen."""
    sid = event["session_id"]
    body = s3().get_object(Bucket=config.DOCS_BUCKET, Key=event["key"])["Body"].read()
    doc_type, page_count = _detect(body, event["key"])
    doc_hash = _sha256(body)

    # `registry_lookup` already declines an entry an older pipeline built, so
    # the only thing left to honour here is someone explicitly asking for
    # another pass at a document whose cached schema is wrong for it.
    cached = None if (get_session(sid) or {}).get("force_reingest") else registry_lookup(doc_hash)
    if cached:
        # A registry entry that came from a published catalog form carries its
        # catalog_id. Attaching the guide here means someone who uploads their
        # own copy of a known form gets the official guidance for free,
        # without going through the picker.
        linked = {k: cached[k] for k in ("catalog_id", "guide_key") if cached.get(k)}
        update_session(sid, doc_type=doc_type, doc_hash=doc_hash, progress="ready", **linked)
        return {
            **event,
            "doc_type": doc_type,
            "doc_hash": doc_hash,
            "cache_hit": True,
            "cached_schema_key": cached["schema_key"],
            "cached_form_map_key": cached.get("form_map_key", ""),
            # Pre-populated so enrich's `if cache_hit: return event`
            # short-circuit stays valid and finalize can read event["fields"]
            # uniformly on either path.
            "fields": load_schema(cached["schema_key"]),
        }

    update_session(sid, doc_type=doc_type, doc_hash=doc_hash, page_count=page_count,
                   progress="extracting")
    return {**event, "doc_type": doc_type, "doc_hash": doc_hash, "cache_hit": False}


def _detect(body: bytes, filename: str) -> tuple[str, int]:
    if filename.lower().endswith(".docx"):
        return "docx", 0

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(body))
    doc_type = "pdf_acroform" if reader.get_fields() else "pdf_flat"
    return doc_type, len(reader.pages)


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _sid_from_key(key: str) -> str | None:
    parts = key.split("/")
    return parts[1] if len(parts) >= 3 and parts[0] == "uploads" else None
