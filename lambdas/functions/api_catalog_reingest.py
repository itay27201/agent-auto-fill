"""POST /catalog/{catalog_id}/reingest — rebuild a form's boxes from its master.

A catalog entry keeps its own copy of `schema.json`, and `POST /catalog/{cid}/sessions`
copies that copy. It never consults the schema registry, so `config.SCHEMA_VERSION`
— which does re-ingest a stale *registry* entry automatically — cannot reach it.
That was deliberate: an entry can hold boxes a person placed by hand and a guide
a person reviewed, and discarding those to pick up a better default is not a
trade the system gets to make on its own. But it left no way to rebuild an entry
at all, so a form defined by an older pipeline kept serving its old boxes forever.

This is that way, and it is a person pressing a button rather than anything
automatic.

It deliberately does not write to the entry. It starts an ordinary session
against the entry's stored master and returns the session id; you look at the
result, place anything ingest declined, and then adopt it with
`PATCH /catalog/{cid}` `{"adopt_schema_from_session": "<sid>"}`. Same shape as
the rest of this system: the machine drafts and a person attests.

Reusing the normal ingest pipeline rather than adding a second one is the same
argument `api_catalog_create` makes — one pipeline, one set of bugs. The master
lives in ArtifactsBucket under `catalog/<cid>/`, and the state machine reads from
DocsBucket, so the one piece of real work here is the copy between them.
"""
import json
import uuid

from common import catalog as cat, config
from common.api import ApiError, caller, handler, path_param
from common.aws import s3, sfn
from common.store import create_session, update_session


@handler
def lambda_handler(event, _context):
    cid = path_param(event, "catalog_id")
    try:
        entry = cat.get(cid)
    except cat.NotFound:
        raise ApiError("catalog entry not found", 404) from None

    source_key = entry.get("source_key")
    if not source_key:
        raise ApiError("this entry has no stored master document to re-read", 409)

    sid = uuid.uuid4().hex
    filename = source_key.rsplit("/", 1)[-1]
    doc_key = f"uploads/{sid}/{filename}"

    # The state machine reads the document from DocsBucket. The master is in
    # ArtifactsBucket, because DocsBucket expires its whole contents after seven
    # days and a catalog master has to outlive that. So it is copied in, and the
    # copy expires with the session while the master stays put.
    s3().copy_object(
        Bucket=config.DOCS_BUCKET,
        CopySource={"Bucket": config.ARTIFACTS_BUCKET, "Key": source_key},
        Key=doc_key,
    )

    create_session(sid, doc_key, entry.get("name") or cid, caller(event))
    update_session(
        sid,
        status="processing",
        progress="classifying",
        # Skip the registry: this form's hash is very likely already in it,
        # pointing at exactly the schema we are trying to replace.
        force_reingest=True,
        # Buy the Textract pass. This is the definition of a form, which happens
        # once and is inherited by every session it ever has — the one place in
        # this system where spending more per page is obviously correct.
        define_time=True,
        rebuilding_catalog_id=cid,
    )

    sfn().start_execution(
        stateMachineArn=config.INGEST_STATE_MACHINE_ARN,
        name=f"{sid}-reingest",
        input=json.dumps({
            "session_id": sid,
            "key": doc_key,
            "bucket": config.DOCS_BUCKET,
            "define_time": True,
        }),
    )

    return {
        "session_id": sid,
        "catalog_id": cid,
        "status": "processing",
        "next": (f"Poll GET /sessions/{sid} until it is ready, review the boxes, "
                 f"then PATCH /catalog/{cid} with "
                 f'{{"adopt_schema_from_session": "{sid}"}} to publish them.'),
    }
