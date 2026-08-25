"""Step 3 of ingest: turn raw extraction into a usable schema.

This is the only expensive step, and it runs once per distinct document
because of the registry cache.

Two prompts, one output shape:

  pdf_acroform  we already have field names, types and geometry, but names
                like "Text14" are useless to the agent. The model looks at
                the page images and supplies human labels, sections, help
                text and validation. It must not invent or drop fields.

  pdf_flat      no structure at all. The model reads the page images and
                returns the full field list with normalized bboxes.
"""
import base64
import json
import logging

from common import config
from common.aws import bedrock, s3
from common.store import update_session

log = logging.getLogger()
log.setLevel(logging.INFO)

SYSTEM = """You analyze government and administrative forms and describe every input a person must complete.

Return ONLY a JSON array. No prose, no markdown fences.

Each element:
{
  "field_id": "stable_snake_case_id",
  "label": "the visible label, in the document's own language",
  "type": "text|textarea|number|date|select|multiselect|checkbox|signature",
  "page": 1,
  "bbox": [x0, y0, x1, y1],
  "section": "the heading this field sits under",
  "required": true,
  "options": ["..."],
  "validation": "regex, or empty string",
  "help": "plain-language explanation of what this field wants",
  "max_length": null
}

Rules:
- bbox is the AREA THE USER WRITES INTO, not the label. Normalized 0..1 of
  page width and height, origin at the TOP-LEFT.
- Keep labels in the source language. Do not translate them.
- "help" is for someone who has never filled this form. Explain what the
  field wants and where to find the information. Write it in the same
  language as the form.
- Set "required" true only where the form marks it or clearly implies it.
- Use "validation" for well-defined formats (national ID, postal code,
  phone). Leave it empty if unsure — a wrong regex blocks a correct answer.
- Checkboxes that are mutually exclusive should be one "select" field with
  the choices in "options", not several checkboxes.
- Never invent a field that is not visible in the document."""

ACRO_TASK = """This PDF has real form fields. Their ids, types and positions are already known and are AUTHORITATIVE — do not change field_id, type, page or bbox, and do not add or remove entries.

Your job is to look at the page images and fill in the human-readable parts: label, section, required, help, validation, and options where the extracted list has none.

Return one element per entry in the extracted list below, in the same order, preserving field_id, type, page and bbox exactly.

Extracted fields:
"""

FLAT_TASK = """This document has no form fields — it is flat or scanned. Read the page images and identify every place a person must write, tick, or sign.

Estimate each bbox carefully from the image: the blank space after a label, the ruled line, or the box outline. Getting the box right matters more than getting many boxes."""


def lambda_handler(event, _context):
    if event.get("cache_hit"):
        return event

    sid = event["session_id"]
    doc_type = event["doc_type"]
    candidates = event.get("candidates") or []
    page_keys = event.get("page_keys") or []

    content = []
    for i, key in enumerate(page_keys, start=1):
        img = s3().get_object(Bucket=config.ARTIFACTS_BUCKET, Key=key)["Body"].read()
        content.append({"text": f"--- page {i} ---"})
        content.append({"image": {"format": "png", "source": {"bytes": img}}})

    if doc_type == "pdf_acroform" and candidates:
        content.append({"text": ACRO_TASK + json.dumps(_slim(candidates), ensure_ascii=False, indent=1)})
    else:
        content.append({"text": FLAT_TASK})

    fields = _invoke(content)

    if doc_type == "pdf_acroform" and candidates:
        fields = _reconcile(candidates, fields)

    fields = _dedupe(fields)
    update_session(sid, progress="finalizing")
    log.info("enriched to %d fields", len(fields))
    return {**event, "fields": fields}


def _invoke(content: list[dict]) -> list[dict]:
    resp = bedrock().converse(
        modelId=config.BEDROCK_MODEL_ID,
        system=[
            {"text": SYSTEM},
            # Cache the system block: every document in a batch reuses it.
            {"cachePoint": {"type": "default"}},
        ],
        messages=[{"role": "user", "content": content}],
        inferenceConfig={"maxTokens": 8192, "temperature": 0},
    )
    text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
    return _parse(text)


def _parse(text: str) -> list[dict]:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        t = t[4:] if t.lower().startswith("json") else t
    start, end = t.find("["), t.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON array in model output: {text[:300]}")
    return json.loads(t[start:end + 1])


def _slim(candidates: list[dict]) -> list[dict]:
    """Send only what the model needs to align its answer — dropping rects
    and on-state values keeps the prompt small and stops the model
    hallucinating changes to them."""
    return [
        {"field_id": c["field_id"], "type": c.get("type", "text"),
         "page": c.get("page", 1), "bbox": c.get("bbox"),
         "options": c.get("options", [])}
        for c in candidates
    ]


def _reconcile(candidates: list[dict], enriched: list[dict]) -> list[dict]:
    """Structure from the PDF wins; language from the model wins.

    A model that renamed, dropped or moved a field silently would produce a
    schema the renderer cannot write back, so extraction is authoritative
    for everything mechanical.
    """
    by_id = {e.get("field_id"): e for e in enriched}
    out = []
    for c in candidates:
        fid = c.get("field_id") or c.get("name") or f"field_{len(out) + 1}"
        e = by_id.get(fid, {})
        backend = {
            "kind": "acroform",
            "name": c["name"],
            "page": c.get("page", 1),
            "rect": c.get("rect"),
        }
        if "checked_value" in c:
            backend["checked_value"] = c["checked_value"]
            backend["unchecked_value"] = c.get("unchecked_value", "/Off")

        ftype = c.get("type", "text")
        options = c.get("options") or e.get("options") or []
        if c.get("type") == "radio_group":
            ftype = "select"
            options = [o["value"] for o in c.get("radio_options", [])]
            backend["radio_options"] = c.get("radio_options", [])

        out.append({
            "field_id": fid,
            "label": e.get("label") or c["name"],
            "type": ftype,
            "page": c.get("page", 1),
            "bbox": c.get("bbox") or [0, 0, 0, 0],
            "section": e.get("section", ""),
            "required": bool(e.get("required", False)),
            "options": options,
            "validation": e.get("validation", "") or "",
            "help": e.get("help", ""),
            "max_length": c.get("max_length") or e.get("max_length"),
            "backend": backend,
        })
    return out


def _dedupe(fields: list[dict]) -> list[dict]:
    seen, out = set(), []
    for f in fields:
        fid = f.get("field_id") or ""
        if not fid:
            continue
        base, n = fid, 2
        while fid in seen:
            fid, n = f"{base}_{n}", n + 1
        seen.add(fid)
        f["field_id"] = fid
        f.setdefault("backend", {"kind": "overlay"})
        out.append(f)
    return out
