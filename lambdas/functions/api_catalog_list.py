"""GET /catalog

The closed list of forms, for the picker on the landing page. One Query over
the `CATALOG` index partition — no Scan, no GSI.

`?include_drafts=1` is for the authoring page, which has to show an entry
somebody is still writing. The public picker never asks for it: a half-written
guide on a government form is worse than no guide at all.
"""
from common.api import handler
from common.catalog import PUBLISHED, listing


@handler
def lambda_handler(event, _context):
    params = event.get("queryStringParameters") or {}
    include_drafts = str(params.get("include_drafts", "")).lower() in ("1", "true", "yes")
    entries = listing(status=None if include_drafts else PUBLISHED)
    return {"entries": entries, "count": len(entries)}
