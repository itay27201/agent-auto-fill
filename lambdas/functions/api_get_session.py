"""GET /sessions/{session_id}

While ingest is running this is just the session's status/progress — the
frontend polls it. Once `status` is "ready" it also carries the full field
schema, current values, and presigned view URLs for the rasterized pages
(ArtifactsBucket is fully private, so the frontend can't read them directly).
"""
from common import catalog as cat, config, schema as sch
from common.api import ApiError, caller, handler, path_param
from common.aws import s3
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
        return sess

    fields = sch.schema_from_list(load_schema(sess["schema_key"]))
    values = get_values(sid)
    page_urls = [_view_url(k) for k in sess.get("page_keys") or []]

    return {
        **sess,
        "fields": sch.schema_to_list(fields),
        "values": values,
        "page_urls": page_urls,
        # Sessions started from the catalog — and uploads that hash-matched a
        # published form — carry official guidance. Sent with the schema so
        # the guide panel costs no extra round trip.
        "guide": cat.load_guide(sess.get("guide_key")),
    }


def _view_url(key: str) -> str:
    return s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": config.ARTIFACTS_BUCKET, "Key": key},
        ExpiresIn=config.VIEW_URL_TTL,
    )
