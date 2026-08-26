"""POST /catalog — promote a finished session into a catalog draft.

Deliberately not a second ingest pipeline. Defining a document starts with the
*existing* upload flow: the author uploads the blank form, classify → extract →
enrich → finalize runs exactly as it does for anyone else, and this endpoint
then copies the result into the catalog prefix. One pipeline, one set of bugs.

The copy is what makes an entry permanent. Everything the session produced
lives under keys that expire — the upload after seven days, the rasters and
the schema with the session TTL — so a catalog entry that merely pointed at
them would work for a week and then quietly stop.

The entry lands as a **draft**. Publishing is a separate, human act.
"""
from common import catalog as cat, config, guide as gd
from common.api import ApiError, body_of, caller, handler
from common.store import get_session, load_form_map, load_schema


@handler
def lambda_handler(event, _context):
    body = body_of(event)
    sid = (body.get("session_id") or "").strip()
    name = (body.get("name") or "").strip()
    if not sid:
        raise ApiError("session_id is required", 400)
    if not name:
        raise ApiError("name is required — it is what people pick from the list", 400)

    sess = get_session(sid)
    if not sess:
        raise ApiError("session not found", 404)
    if sess.get("status") != "ready":
        raise ApiError(f"session is {sess.get('status')}, not ready — "
                       "wait for the document to finish processing", 409)

    cid = (body.get("catalog_id") or "").strip() or cat.unique_id(name)
    if cat.exists(cid):
        raise ApiError(f"catalog entry {cid!r} already exists", 409, catalog_id=cid)

    fields = load_schema(sess["schema_key"])
    ext = (sess.get("filename") or "").rsplit(".", 1)[-1].lower()
    source = cat.source_key(cid, ext if ext in ("pdf", "docx") else "pdf")

    cat.copy_from_docs(sess["doc_key"], source)
    schema_key = cat.put_schema(cid, fields)
    # The layout the session established travels with the schema. Losing it here
    # would mean every person who picks this form from the catalog gets the
    # field list without the map that tells its repeated labels apart.
    form_map = load_form_map(sess.get("form_map_key"))
    form_map_key = cat.put_form_map(cid, form_map) if form_map else ""
    page_keys = cat.copy_pages(cid, sess.get("page_keys") or [])

    language = (body.get("language") or "").strip()
    guide_key = cat.put_guide(cid, gd.empty({
        "catalog_id": cid,
        "name": name,
        "agency": (body.get("agency") or "").strip(),
        "language": language,
        "version": "1",
    }))

    entry = cat.put({
        "catalog_id": cid,
        "name": name,
        "agency": (body.get("agency") or "").strip(),
        "description": (body.get("description") or "").strip(),
        "language": language,
        "status": cat.DRAFT,
        "doc_type": sess.get("doc_type"),
        "doc_hash": sess.get("doc_hash") or "",
        "source_key": source,
        "schema_key": schema_key,
        "form_map_key": form_map_key,
        # Which pipeline generation built these boxes. Unlike the registry, a
        # stale catalog entry is never re-ingested automatically: its schema may
        # carry boxes a person placed by hand and its guide was reviewed, and
        # throwing that away to pick up a better default is not a trade the
        # system gets to make. It is reported as stale and somebody decides.
        "schema_version": config.SCHEMA_VERSION,
        "page_keys": page_keys,
        "guide_key": guide_key,
        "has_guide": False,
        "field_count": len(fields),
        "version": 1,
        # Provenance, not access control. With the Cognito authorizer still
        # commented out in template.yaml, `caller` is "anonymous" for
        # everyone and any user can edit any entry — see the README. This
        # records who wrote it for when auth is switched on.
        "created_by": caller(event),
    })

    return {"catalog_id": cid, "entry": entry}
