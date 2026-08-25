"""POST /sessions

Starts a session and hands back a presigned PUT URL. The document goes
straight from the browser to S3 — it never passes through this Lambda,
which would cap it at the 6MB synchronous payload limit.
"""
import re
import uuid

from common import config
from common.api import ApiError, body_of, caller, handler
from common.aws import s3
from common.store import create_session

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@handler
def lambda_handler(event, _context):
    body = body_of(event)
    filename = (body.get("filename") or "").strip()
    if not filename:
        raise ApiError("filename is required", 400)
    content_type = body.get("content_type") or "application/octet-stream"

    sid = uuid.uuid4().hex
    safe_name = _SAFE.sub("_", filename)[-180:] or "document"
    doc_key = f"uploads/{sid}/{safe_name}"

    create_session(sid, doc_key, filename, caller(event))

    # content_type is a signed parameter: the browser's PUT must send the
    # identical Content-Type header or S3 rejects it with SignatureDoesNotMatch.
    upload_url = s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": config.DOCS_BUCKET, "Key": doc_key, "ContentType": content_type},
        ExpiresIn=config.UPLOAD_URL_TTL,
    )

    return {"session_id": sid, "upload_url": upload_url, "doc_key": doc_key, "content_type": content_type}
