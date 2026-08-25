"""POST /catalog/{catalog_id}/sources — presigned PUT for a reference document.

The blank form itself rarely states who is eligible, what to attach, or when
it is due. That lives in the instruction booklet the agency publishes beside
it. This is how those booklets reach the authoring agent.

Same shape as `POST /sessions`: the file goes browser → S3 directly, because
routing it through Lambda would cap it at the 6MB synchronous payload limit,
and instruction booklets are routinely bigger than the form.

Unlike an upload, this fires no S3 trigger — the notification on DocsBucket is
prefix-filtered to `uploads/`, and these land in ArtifactsBucket. The authoring
agent reads them on demand instead.
"""
import re

from common import catalog as cat, config
from common.api import ApiError, body_of, handler, path_param
from common.aws import s3

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@handler
def lambda_handler(event, _context):
    cid = path_param(event, "catalog_id")
    if not cat.exists(cid):
        raise ApiError("catalog entry not found", 404)

    body = body_of(event)
    filename = (body.get("filename") or "").strip()
    if not filename:
        raise ApiError("filename is required", 400)
    content_type = body.get("content_type") or "application/octet-stream"

    source_id = _SAFE.sub("_", filename)[-180:] or "source"
    key = f"{cat.sources_prefix(cid)}{source_id}"

    # content_type is a signed parameter: the browser's PUT must send exactly
    # this header back or S3 rejects it with SignatureDoesNotMatch.
    upload_url = s3().generate_presigned_url(
        "put_object",
        Params={"Bucket": config.ARTIFACTS_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=config.UPLOAD_URL_TTL,
    )
    return {"source_id": source_id, "key": key,
            "upload_url": upload_url, "content_type": content_type}
