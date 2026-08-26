#!/usr/bin/env python3
"""
promote_demo.py — worked example of stage A3 output.

The calibration UI (calibrate.html) writes exactly this file. Here it is done in
code for the first ten fields of itc101 so the rest of the pipeline is runnable
end-to-end today, and so you can see the confirmed schema's shape.

Two real corrections are applied that the geometry pass could not make on its own:
  1. `מספר זהות` — the comb's outer right edge is the section border, not a drawn
     separator, so the detector found 8 cells instead of 9. Rebuilt from
     (left anchor, pitch, 9).
  2. cell height — separators are only ~5pt tall tick marks; the writable band
     runs up to the row header. Heights are raised so digits are legible.
"""

import json
from pathlib import Path

SRC = Path("registry/itc101/schema.draft.json")
DST = Path("registry/itc101/schema.json")

draft = json.loads(SRC.read_text(encoding="utf-8"))
cand = {c["id"]: c for c in draft["fields"]}


def comb_from(cid, *, cells=None, left=None, pitch=None, y0=None, y1=None):
    c = cand[cid]
    pitch = pitch or c["pitch"]
    cells = cells or c["cells"]
    left = c["cell_boxes"][0][0] if left is None else left
    y0 = c["cell_boxes"][0][1] if y0 is None else y0
    y1 = c["cell_boxes"][0][3] if y1 is None else y1
    boxes = [[round(left + i * pitch, 2), y0, round(left + (i + 1) * pitch, 2), y1]
             for i in range(cells)]
    return boxes, [boxes[0][0], y0, boxes[-1][2], y1]


def cb(cid):
    return cand[cid]["bbox"]


fields, groups = [], []


def add_comb(fid, label, cid, validator, **kw):
    boxes, bbox = comb_from(cid, **kw)
    fields.append({"id": fid, "label_he": label, "section": kw.pop("section", ""),
                   "page": cand[cid]["page"], "type": "comb", "validator": validator,
                   "cells": len(boxes), "fill_direction": "ltr",
                   "cell_boxes": boxes, "bbox": bbox, "source_candidate": cid})


def add_text(fid, label, page, bbox, validator="text", align="right"):
    fields.append({"id": fid, "label_he": label, "page": page, "type": "text",
                   "validator": validator, "align": align, "bbox": bbox})


def add_radio(gid, label, options):
    members = []
    xs0 = min(cb(c)[0] for _, c in options); ys0 = min(cb(c)[1] for _, c in options)
    xs1 = max(cb(c)[2] for _, c in options); ys1 = max(cb(c)[3] for _, c in options)
    gbox = [xs0 - 4, ys0 - 4, xs1 + 4, ys1 + 4]
    for value, cid in options:
        fid = f"{gid}__{value}"
        members.append(fid)
        fields.append({"id": fid, "label_he": cand[cid]["label_he"], "page": cand[cid]["page"],
                       "type": "radio_option", "group": gid, "value": value,
                       "mark_bbox": cb(cid), "mark_style": "X",
                       "group_bbox": gbox, "bbox": cb(cid)})
    groups.append({"id": gid, "kind": "radio", "label_he": label,
                   "page": cand[options[0][1]]["page"], "members": members, "bbox": gbox})


# ---- section א -----------------------------------------------------------
add_comb("tax_year", "שנת המס", "p0_comb_000", "tax_year")
add_comb("employer_tax_file", "מספר תיק ניכויים", "p0_comb_001", "tax_file_number",
         y0=172.0, y1=183.0)

# ---- section ב -----------------------------------------------------------
add_comb("employee_id", "מספר זהות של העובד/ת", "p0_comb_002", "israeli_id",
         cells=9, left=438.10, pitch=11.35, y0=220.0, y1=236.0)   # correction #1
add_comb("employee_birth_date", "תאריך לידה", "p0_comb_004", "date_ddmmyy",
         y0=221.0, y1=236.0)
add_comb("employee_aliya_date", "תאריך עליה", "p0_comb_003", "date_ddmmyy",
         y0=221.0, y1=236.0)
add_text("employee_last_name", "שם משפחה", 0, [313.9, 219.0, 438.1, 234.0])
add_text("employee_first_name", "שם פרטי", 0, [210.0, 219.0, 313.9, 234.0])

add_radio("gender", "מין", [("male", "p0_cb_006"), ("female", "p0_cb_013")])
add_radio("israel_resident", "תושב ישראל",
          [("yes", "p0_cb_004"), ("no", "p0_cb_011")])
add_radio("marital_status", "מצב משפחתי", [
    ("single",    "p0_cb_003"),   # רווק/ה
    ("married",   "p0_cb_002"),   # נשוי/אה
    ("divorced",  "p0_cb_001"),   # גרוש/ה
    ("widowed",   "p0_cb_010"),   # אלמן/ה
    ("separated", "p0_cb_009"),   # פרוד/ה — needs פ"ש approval
])

# a cross-field rule the LLM must never be trusted to remember
for f in fields:
    if f["id"] == "employee_aliya_date":
        f["required_when"] = ["marital_status__separated", None]  # placeholder example

schema = {
    "form_id": "itc101",
    "schema_version": "1.0.0",
    "status": "CONFIRMED (partial — 10 of ~135 candidates promoted)",
    "source_pdf": draft["source_pdf"],
    "pdf_sha256": draft["pdf_sha256"],
    "direction": draft["direction"],
    "pages": draft["pages"],
    "page_size": draft["page_size"],
    "coordinate_system": draft["coordinate_system"],
    "groups": groups,
    "fields": fields,
}
# the placeholder rule above would misfire; strip it for the demo run
for f in schema["fields"]:
    f.pop("required_when", None)

DST.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"confirmed schema: {len(fields)} fields, {len(groups)} radio groups → {DST}")
for f in fields:
    print(f"  {f['id']:<24} {f['type']:<13} {f.get('label_he','')}")
