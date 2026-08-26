"""GET /catalog/{catalog_id}

The full entry, plus the guide as both raw markdown (what the authoring page
puts in its editor) and parsed sections (what the reading panel renders).
Sending both saves the frontend from shipping a markdown parser it would only
use to re-derive what the backend already knows.
"""
from common import catalog as cat, guide_checks as gchk
from common.api import ApiError, handler, path_param
from common.store import load_schema


@handler
def lambda_handler(event, _context):
    cid = path_param(event, "catalog_id")
    try:
        entry = cat.get(cid)
    except cat.NotFound:
        raise ApiError("catalog entry not found", 404) from None

    markdown = cat.load_guide_markdown(entry.get("guide_key"))
    fields = load_schema(entry["schema_key"]) if entry.get("schema_key") else []
    # The report rides along so the authoring page knows what is missing from
    # the moment it loads, not only after the agent's first reply. Without it,
    # publishing straight after a reload has nothing to warn against.
    report = gchk.check(cat.parse_guide(markdown), fields, entry.get("language", ""))
    body = {
        **entry,
        "guide": cat.parse_guide(markdown),
        "guide_markdown": markdown,
        "sources": cat.list_sources(cid),
        "report": report,
        "summary": gchk.summary(report),
    }

    # The authoring page needs field_ids to write per-field notes against; the
    # picker does not, and a 60-field schema is not worth sending to it.
    params = event.get("queryStringParameters") or {}
    if str(params.get("include_fields", "")).lower() in ("1", "true", "yes"):
        body["fields"] = [
            {"field_id": f.get("field_id"), "label": f.get("label"),
             "type": f.get("type"), "section": f.get("section")}
            for f in fields
        ]

    return body
