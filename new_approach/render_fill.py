#!/usr/bin/env python3
"""
render_fill.py — the ONLY component allowed to touch coordinates.

Takes a confirmed schema + a flat {field_id: value} dict and produces a filled
PDF. The LLM never reaches this code path with anything but field ids and
values; placement is pure lookup.

Also emits per-field crop images for the vision verification loop.

Usage:
    python render_fill.py itc101.pdf registry/itc101/schema.json values.json \
        -o out/filled.pdf --crops out/crops --verify-manifest out/crops/manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pymupdf
from bidi.algorithm import get_display

import validators as V

HEBREW_FONT = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
FONT_ALIAS = "hebfont"
_FONT = pymupdf.Font(fontfile=HEBREW_FONT)


def _width(text: str, size: float) -> float:
    """Text width using the embedded Hebrew face (built-in metrics don't know it)."""
    return _FONT.text_length(text, fontsize=size)
MARK_INSET = 0.18       # shrink the X inside the printed box
MARK_WIDTH = 1.1
TEXT_PAD = 2.0
CROP_PAD = 8.0


class RenderError(RuntimeError):
    pass


# --------------------------------------------------------------------------
def _has_hebrew(s: str) -> bool:
    return any("\u0590" <= c <= "\u05FF" for c in s)


def _shape(text: str) -> str:
    """
    Apply the Unicode bidi algorithm so the visual order matches what a PDF
    viewer will show. Digits inside Hebrew strings stay LTR automatically.
    """
    return get_display(text) if _has_hebrew(text) else text


def draw_mark(page, bbox, style="X", color=(0, 0, 0)):
    r = pymupdf.Rect(bbox)
    pad = min(r.width, r.height) * MARK_INSET
    inner = pymupdf.Rect(r.x0 + pad, r.y0 + pad, r.x1 - pad, r.y1 - pad)
    if style == "X":
        page.draw_line(inner.tl, inner.br, color=color, width=MARK_WIDTH)
        page.draw_line(inner.tr, inner.bl, color=color, width=MARK_WIDTH)
    elif style == "check":
        mid = pymupdf.Point(inner.x0 + inner.width * 0.38, inner.y1)
        page.draw_line(pymupdf.Point(inner.x0, inner.y0 + inner.height * 0.55),
                       mid, color=color, width=MARK_WIDTH)
        page.draw_line(mid, inner.tr, color=color, width=MARK_WIDTH)
    else:
        page.draw_rect(inner, color=color, fill=color)


def draw_comb(page, cell_boxes, value: str, direction="ltr", fontsize=None):
    """One character per cell. Digits are always laid out left-to-right."""
    chars = list(str(value))
    if len(chars) > len(cell_boxes):
        raise RenderError(
            f"value '{value}' has {len(chars)} chars but the field has "
            f"{len(cell_boxes)} cells")
    boxes = cell_boxes if direction == "ltr" else list(reversed(cell_boxes))
    # right-align short values in a comb (leading blanks, like handwriting)
    offset = 0
    for ch, box in zip(chars, boxes[offset:]):
        r = pymupdf.Rect(box)
        size = fontsize or min(r.height * 0.82, r.width * 1.25)
        w = _width(ch, size)
        page.insert_text((r.x0 + (r.width - w) / 2, r.y1 - r.height * 0.16),
                         ch, fontname=FONT_ALIAS, fontsize=size)


def draw_text(page, bbox, value: str, align="right", fontsize=None):
    r = pymupdf.Rect(bbox)
    txt = _shape(str(value))
    size = fontsize or min(r.height * 0.78, 11.0)
    # shrink to fit rather than overflow into the neighbouring field
    for _ in range(14):
        w = _width(txt, size)
        if w <= r.width - 2 * TEXT_PAD or size <= 5.0:
            break
        size -= 0.5
    w = _width(txt, size)
    if align == "right":
        x = r.x1 - TEXT_PAD - w
    elif align == "center":
        x = r.x0 + (r.width - w) / 2
    else:
        x = r.x0 + TEXT_PAD
    page.insert_text((x, r.y1 - r.height * 0.20), txt, fontname=FONT_ALIAS, fontsize=size)


# --------------------------------------------------------------------------
def fill(pdf_path: Path, schema: dict, values: dict,
         out_pdf: Path, crops_dir: Path | None = None,
         strict_hash: bool = True, flatten: bool = True) -> dict:

    actual = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    if strict_hash and actual != schema.get("pdf_sha256"):
        raise RenderError(
            "PDF hash does not match the schema. The form was revised — "
            "recalibrate before filling. Refusing to place values on unknown geometry.")

    fields = {f["id"]: f for f in schema["fields"]}
    unknown = [k for k in values if k not in fields]
    if unknown:
        raise RenderError(f"unknown field ids: {unknown}")

    problems = V.check_dependencies(schema, values)
    if problems:
        raise RenderError("dependency problems: " + " | ".join(problems))

    doc = pymupdf.open(pdf_path)
    for page in doc:
        page.insert_font(fontname=FONT_ALIAS, fontfile=HEBREW_FONT)

    written = {}
    for fid, raw in values.items():
        f = fields[fid]
        page = doc[f["page"]]
        ftype = f["type"]

        if ftype in ("checkbox", "radio_option"):
            if raw in (True, "X", "x", 1, "1"):
                draw_mark(page, f["mark_bbox"], style=f.get("mark_style", "X"))
                written[fid] = "X"
            continue

        clean = V.validate(f.get("validator", "text"), raw)
        if ftype == "comb":
            draw_comb(page, f["cell_boxes"], clean, f.get("fill_direction", "ltr"))
        elif ftype == "text":
            draw_text(page, f["bbox"], clean,
                      align=f.get("align", "right" if schema.get("direction") == "rtl" else "left"))
        else:
            raise RenderError(f"unsupported field type '{ftype}' on {fid}")
        written[fid] = clean

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_pdf, deflate=True)

    manifest = []
    if crops_dir:
        crops_dir.mkdir(parents=True, exist_ok=True)
        for fid in written:
            f = fields[fid]
            box = f.get("bbox") or f.get("mark_bbox")
            if f["type"] in ("checkbox", "radio_option") and f.get("group_bbox"):
                box = f["group_bbox"]          # verify the whole group, catches bleed
            r = pymupdf.Rect(box) + (-CROP_PAD, -CROP_PAD, CROP_PAD, CROP_PAD)
            pix = doc[f["page"]].get_pixmap(clip=r, dpi=300)
            p = crops_dir / f"{fid}.png"
            pix.save(p)
            manifest.append({"field_id": fid, "crop": str(p),
                             "expected": written[fid],
                             "label_he": f.get("label_he", ""),
                             "type": f["type"]})
    doc.close()
    return {"written": written, "manifest": manifest, "out": str(out_pdf)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf"); ap.add_argument("schema"); ap.add_argument("values")
    ap.add_argument("-o", "--out", default="out/filled.pdf")
    ap.add_argument("--crops", default=None)
    ap.add_argument("--verify-manifest", default=None)
    ap.add_argument("--no-hash-check", action="store_true")
    a = ap.parse_args()

    schema = json.loads(Path(a.schema).read_text(encoding="utf-8"))
    values = json.loads(Path(a.values).read_text(encoding="utf-8"))
    res = fill(Path(a.pdf), schema, values, Path(a.out),
               Path(a.crops) if a.crops else None,
               strict_hash=not a.no_hash_check)

    if a.verify_manifest:
        Path(a.verify_manifest).parent.mkdir(parents=True, exist_ok=True)
        Path(a.verify_manifest).write_text(
            json.dumps(res["manifest"], ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"wrote {len(res['written'])} fields → {res['out']}")
    for k, v in res["written"].items():
        print(f"  {k:<28} {v}")


if __name__ == "__main__":
    main()
