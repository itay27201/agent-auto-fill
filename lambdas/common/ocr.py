"""Textract: the boxes the PDF's own geometry cannot give us, and their labels.

`geometry.py` reconstructs a form's cells from its ruled lines. That is exact
and free wherever it works, and on the Israeli 101 it reaches about three
quarters of page one. It cannot reach the rest, for two reasons that no amount
of tuning fixes: a scan has no lines to read at all, and even on a clean vector
form a writing area with no ruled box around it leaves nothing to reconstruct.

So this runs in two situations, neither of which is "every upload":

  a scanned page   no text layer and no paths — OCR is the only source of
                   geometry there is.
  define time      while a form is being *defined*: a catalog rebuild or an
                   explicit re-ingest. Once per document, inherited by every
                   session that form ever has.

Its regions are unioned with the PDF's rather than replacing them
(`geometry.merge_regions`), and the PDF wins a tie — a cell reconstructed from
ruled lines is exact, an OCR box is inferred.

The part worth the money is `textract_label`. Textract does not merely find a
box; `KEY_VALUE_SET` says which printed label each box belongs to, and that
link is exactly what cell reconstruction throws away. The model gets a named
candidate instead of a bare rectangle, and stays free to override it.

Kept separate from `geometry.py` on purpose. That module is pure computation and
its tests run with no credentials and no network; this one is an AWS call, and
folding it in would make the geometry suite need a mock to test arithmetic.

Two honest caveats:

  Cost.     AnalyzeDocument with FORMS and TABLES is billed per page, where
            reading the PDF's own geometry is free. Hence the define-time gate.
  Hebrew.   Textract's key/value detection is markedly weaker in Hebrew than in
            English. Expect it to find some boxes and miss others — survivable,
            because a field it misses is left unplaced rather than placed
            wrongly, and the viewer's box editor places the rest.
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
            region = {
                "bbox": bbox,
                "found_by": "textract_value",
                "nearby_text": ([{"side": "right", "text": label[:60]}] if label else []),
                **({"already_contains": written[:60]} if written else {}),
            }
            if label:
                # The reason this whole path is worth its cost. Textract does not
                # just find a box, it says which printed label the box belongs to
                # — and that key/value link is precisely what reconstructing
                # cells from ruled lines cannot recover. Passed to the model as a
                # named candidate rather than a bare rectangle; it confirms or
                # overrides it against the page image, so a wrong guess here
                # costs a hint and never a box.
                region["textract_label"] = label[:80]
            out.append(region)
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
