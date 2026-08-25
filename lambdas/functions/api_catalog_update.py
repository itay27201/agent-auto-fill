"""PATCH /catalog/{catalog_id} — edit metadata, edit the guide, publish.

Publishing lives here and nowhere else. The authoring agent has tools that
write guide sections, but none that publish: it drafts and a person attests,
the same rule `set_field` enforces for form values. A model that could both
invent an eligibility rule and publish it to a government catalog would be the
single worst failure mode in this system.

Publishing refuses an empty guide. An entry whose whole value proposition is
"we already know this form" should not reach the picker saying nothing about
it — that is a worse experience than the upload flow it replaces.
"""
from common import catalog as cat, guide as gd
from common.api import ApiError, body_of, handler, path_param
from common.store import registry_store

_META = ("name", "agency", "description", "language")


@handler
def lambda_handler(event, _context):
    cid = path_param(event, "catalog_id")
    body = body_of(event)
    try:
        entry = cat.get(cid)
    except cat.NotFound:
        raise ApiError("catalog entry not found", 404) from None

    changes = {k: str(body[k]).strip() for k in _META if k in body}
    if changes.get("name") == "":
        raise ApiError("name cannot be empty", 400)

    # Two ways to edit the guide: whole-file, from the authoring page's
    # textarea, or one section at a time, which is what the agent's tools do.
    guide = None
    if "guide_markdown" in body:
        guide = gd.parse(body["guide_markdown"] or "")
    elif "sections" in body or "field_notes" in body:
        guide = cat.load_guide(entry.get("guide_key")) or gd.empty()
        for name, text in (body.get("sections") or {}).items():
            gd.set_section(guide, name, text)
        for fid, text in (body.get("field_notes") or {}).items():
            gd.set_field_note(guide, fid, text)

    if guide is not None:
        guide["meta"] = {**(guide.get("meta") or {}), "catalog_id": cid,
                         "name": changes.get("name") or entry.get("name", ""),
                         "agency": changes.get("agency") or entry.get("agency", ""),
                         "language": changes.get("language") or entry.get("language", "")}
        changes["guide_key"] = cat.put_guide(cid, guide)
        changes["has_guide"] = gd.is_filled(guide)

    status = body.get("status")
    if status is not None:
        if status not in (cat.DRAFT, cat.PUBLISHED):
            raise ApiError(f"status must be {cat.DRAFT!r} or {cat.PUBLISHED!r}", 400)
        if status == cat.PUBLISHED:
            filled = changes.get("has_guide")
            if filled is None:
                filled = gd.is_filled(cat.load_guide(entry.get("guide_key")))
            if not filled:
                raise ApiError(
                    "the guide is still empty — write at least an overview before "
                    "publishing, or people will pick this form and learn nothing "
                    "the document does not already say",
                    422,
                )
        changes["status"] = status

    updated = cat.update(cid, **changes)

    # Publishing links the entry into the SHA-256 registry, so someone who
    # uploads their own copy of this exact form lands on the same schema and
    # picks up the guide — see ingest_classify's cache-hit branch.
    if changes.get("status") == cat.PUBLISHED and updated.get("doc_hash"):
        registry_store(
            updated["doc_hash"], updated["schema_key"], updated.get("doc_type", ""),
            form_name=updated.get("name", ""),
            catalog_id=cid, guide_key=updated.get("guide_key", ""),
        )

    return {"catalog_id": cid, "entry": updated}
