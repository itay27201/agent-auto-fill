"""Textract, for the documents that have no geometry to read.

`geometry.py` gets a form's boxes out of the PDF itself — the ruled lines and
glyph positions are right there, exact and free. That covers every digitally
produced government form, which is nearly all of them.

It cannot cover a scan. A photographed or faxed form is one flat image: no text
layer, no path objects, nothing to measure. For those, and only those, this
module asks Textract where the writing areas are.

Kept separate from `geometry.py` on purpose. That module is pure computation and
its tests run with no credentials and no network; this one is an AWS call, and
folding it in would make the geometry suite need a mock to test arithmetic.

Two honest caveats, both of which is why this is the fallback and not the
default:

  Cost.     AnalyzeDocument with FORMS and TABLES is billed per page, where
            reading the PDF's own geometry is free.
  Hebrew.   Textract's key/value detection is markedly weaker in Hebrew than in
            English. On a Hebrew scan expect it to find some boxes and miss
            others — which is survivable, because a field it misses is left
            unplaced rather than placed wrongly, and the viewer's box editor is
            how the rest get placed.
"""
from __future__ import annotations

import logging

from .aws import textract
from .geometry import clamp

log = logging.getLogger()

# AnalyzeDocument's synchronous Bytes form caps at 10MB. Our page rasters are
# well under that at 150 DPI, but a high-DPI scan is not, and a page skipped
# with a warning beats an ingest that dies on one oversized image.
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def regions_from_image(png: bytes) -> list[dict]:
    """Candidate writing areas for one page image, in the same shape
    `geometry.candidate_regions` returns: `bbox`, `found_by`, `nearby_text`.

    Returning the same shape is the whole design. Enrich, `_place`, the sanity
    checks and the numbered-image trick all work on regions and do not care
    where a region came from, so the scanned path is the ordinary path with a
    different source of coordinates.
    """
    if len(png) > MAX_IMAGE_BYTES:
        log.warning("page image is %.1fMB, past AnalyzeDocument's limit — skipping OCR",
                    len(png) / 1e6)
        return []

    try:
        resp = textract().analyze_document(
            Document={"Bytes": png}, FeatureTypes=["FORMS", "TABLES"]
        )
    except Exception:
        log.exception("Textract failed on a page; continuing without its regions")
        return []

    blocks = {b["Id"]: b for b in resp.get("Blocks", [])}
    regions = [*_value_regions(blocks), *_empty_cells(blocks)]

    # Same ordering and numbering as the PDF path: down the page, then right to
    # left, because these forms are right-to-left.
    regions.sort(key=lambda r: (round(r["bbox"][1], 2), -r["bbox"][2]))
    for i, r in enumerate(regions, start=1):
        r["region_id"] = i
    return regions


def _value_regions(blocks: dict) -> list[dict]:
    """KEY_VALUE_SET pairs: the VALUE half is where someone writes, and the KEY
    half is the printed label — which is exactly the `nearby_text` the model
    needs to tell two identically-labelled boxes apart."""
    out = []
    for block in blocks.values():
        if block.get("BlockType") != "KEY_VALUE_SET":
            continue
        if "KEY" not in (block.get("EntityTypes") or []):
            continue

        label = _text_of(block, blocks)
        for value_id in _related(block, "VALUE"):
            value = blocks.get(value_id)
            if not value:
                continue
            bbox = _bbox(value)
            if not bbox:
                continue
            # A VALUE block that already has text is a filled-in field on
            # somebody's completed copy, not a blank to write in. Keep it — the
            # box is still correct — but say so, so enrich is not surprised.
            written = _text_of(value, blocks)
            out.append({
                "bbox": bbox,
                "found_by": "textract_value",
                "nearby_text": ([{"side": "right", "text": label[:60]}] if label else []),
                **({"already_contains": written[:60]} if written else {}),
            })
    return out


def _empty_cells(blocks: dict) -> list[dict]:
    """Table cells with nothing in them. A ruled government form is mostly
    table, and the cells Textract finds are the same rectangles the PDF path
    reconstructs from ruled lines."""
    out = []
    for block in blocks.values():
        if block.get("BlockType") != "CELL":
            continue
        if _text_of(block, blocks).strip():
            continue  # holds the form's own printing: a label, not a blank
        bbox = _bbox(block)
        if bbox:
            out.append({"bbox": bbox, "found_by": "textract_cell", "nearby_text": []})
    return out


def _bbox(block: dict) -> list[float] | None:
    """Textract normalizes to 0..1 with a top-left origin — the same convention
    as `FormField.bbox`, so there is no conversion to get wrong here."""
    geom = (block.get("Geometry") or {}).get("BoundingBox")
    if not geom:
        return None
    box = clamp([
        geom["Left"], geom["Top"],
        geom["Left"] + geom["Width"], geom["Top"] + geom["Height"],
    ])
    return box if box[2] > box[0] and box[3] > box[1] else None


def _related(block: dict, kind: str) -> list[str]:
    for rel in block.get("Relationships") or []:
        if rel.get("Type") == kind:
            return rel.get("Ids") or []
    return []


def _text_of(block: dict, blocks: dict) -> str:
    words = []
    for child_id in _related(block, "CHILD"):
        child = blocks.get(child_id) or {}
        if child.get("BlockType") == "WORD":
            words.append(child.get("Text", ""))
        elif child.get("BlockType") == "SELECTION_ELEMENT":
            if child.get("SelectionStatus") == "SELECTED":
                words.append("[X]")
    return " ".join(w for w in words if w).strip()
