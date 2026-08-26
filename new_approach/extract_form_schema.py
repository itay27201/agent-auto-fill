#!/usr/bin/env python3
"""
extract_form_schema.py — Stage A1/A2 of the form registry pipeline.

Deterministically extracts fillable-field candidates from a vector (born-digital)
PDF form and emits a DRAFT schema plus annotated page images for LLM labelling
and human calibration.

No OCR. No LLM. No guessing about coordinates — everything here comes from the
PDF's own content stream, so it is exact and reproducible.

Detects:
  * checkbox marks   — ZapfDingbats / Wingdings glyphs ('o', 'q', ...) with per-glyph bbox
  * comb fields      — regularly-spaced vertical separators (ID numbers, dates, phone)
  * underline fields — short horizontal rules that act as write-on lines
  * radio groups     — checkbox marks aligned in a column or row

Usage:
    python extract_form_schema.py itc101.pdf -o registry/itc101 --form-id itc101 --rtl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path

import pymupdf

# --------------------------------------------------------------------------
# tuning constants — every one of these is a knob you may need per form family
# --------------------------------------------------------------------------
DINGBAT_FONTS = ("dingbat", "wingding", "webding", "zapf")
LINE_TOL = 0.6          # pt: max deviation to call a segment horizontal/vertical
MIN_SEG_LEN = 3.0       # pt: ignore hairline artefacts (logos are full of them)
COMB_MIN_SEPARATORS = 3  # a comb needs at least this many interior separators
COMB_RUN_GAP = 2.6      # split a y-cluster into separate combs at gaps > this * pitch
COMB_MAX_PITCH = 40.0   # pt: wider than this is a table column, not a digit cell
LABEL_GAP = 8.0         # pt: max horizontal distance from mark to its label span
SECTION_RULE_RATIO = 0.80  # horizontal line covering >80% of page width = section rule
UNDERLINE_MIN = 22.0    # pt: shortest horizontal rule we treat as a write-on line
UNDERLINE_HEIGHT = 12.0  # pt: assumed writable height above an underline
GROUP_Y_GAP = 2.4       # column grouping: max vertical gap in units of mark height
GROUP_ALIGN_TOL = 1.5   # pt: x/y alignment tolerance for grouping marks

FOOTNOTE_RE = re.compile(r"^\s*\(\d{1,2}\)\s*")
KNOWN_COMB_LENGTHS = {9: "israeli_id_or_tax_file", 8: "date_ddmmyyyy", 10: "phone"}

# Label lexicon: when the auto-attached caption matches, we know how many cells the
# field MUST have. Any mismatch is a geometry miss (usually an unmarked outer edge)
# and gets flagged loudly for the human calibration step rather than silently kept.
LABEL_LEXICON = [
    (re.compile(r"מספר\s*זהות"),        9,  "israeli_id"),
    (re.compile(r"תיק\s*ניכויים"),      9,  "tax_file_number"),
    (re.compile(r"מספר\s*דרכון"),       None, "passport"),
    (re.compile(r"תאריך"),              6,  "date_ddmmyy"),
    (re.compile(r"טלפון\s*נייד"),       10, "mobile"),
    (re.compile(r"מספר\s*טלפון"),       9,  "phone"),
    (re.compile(r"מיקוד"),              7,  "postal_code"),
    (re.compile(r"שנת\s*המס"),          4,  "tax_year"),
]


def lexicon_check(cand: "Candidate") -> None:
    """Cross-validate a comb's geometric cell count against its caption."""
    if cand.kind != "comb" or not cand.label_he:
        return
    for pattern, expected, kind in LABEL_LEXICON:
        if pattern.search(cand.label_he):
            cand.guessed_type = kind
            if expected is None:
                cand.needs_review = True
            elif cand.cells == expected:
                cand.confidence = 0.95
                cand.needs_review = False
            else:
                cand.needs_review = True
                cand.confidence = 0.3
                cand.guessed_type = (f"{kind}!MISMATCH expected {expected} cells, "
                                     f"geometry found {cand.cells} — outer edge is "
                                     f"probably undrawn; fix in calibration")
            return


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------
@dataclass
class Candidate:
    id: str
    kind: str                       # checkbox | comb | underline
    page: int
    bbox: list                      # [x0, y0, x1, y1] in PDF points, origin top-left
    label_he: str = ""
    label_source: str = ""          # left_span | right_span | above_span | none
    needs_review: bool = True
    confidence: float = 0.0
    # checkbox-only
    glyph: str | None = None
    group_id: str | None = None
    # comb-only
    cells: int | None = None
    pitch: float | None = None
    cell_boxes: list | None = None
    guessed_type: str | None = None


@dataclass
class Group:
    id: str
    page: int
    kind: str                       # radio_candidate | multiselect_candidate
    orientation: str                # vertical | horizontal
    members: list = field(default_factory=list)
    label_he: str = ""
    bbox: list = field(default_factory=list)


# --------------------------------------------------------------------------
# primitive extraction
# --------------------------------------------------------------------------
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def text_spans(page) -> list[dict]:
    """All non-dingbat text spans with bbox, sorted top-to-bottom."""
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if _is_dingbat(span["font"]):
                    continue
                txt = span["text"].strip()
                if txt:
                    out.append({"text": txt, "bbox": list(span["bbox"]),
                                "size": span["size"], "font": span["font"]})
    out.sort(key=lambda s: (s["bbox"][1], s["bbox"][0]))
    return out


def _is_dingbat(font_name: str) -> bool:
    f = font_name.lower()
    return any(k in f for k in DINGBAT_FONTS)


def mark_glyphs(page) -> list[dict]:
    """
    Per-character bboxes for every dingbat glyph.

    rawdict (not dict) is essential here: a single span can contain several
    marks plus padding spaces, e.g. 'q ' or '   '. We need each glyph on its own.
    """
    out = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if not _is_dingbat(span["font"]):
                    continue
                for ch in span.get("chars", []):
                    c = ch["c"]
                    if not c.strip():
                        continue
                    x0, y0, x1, y1 = ch["bbox"]
                    if (x1 - x0) < 3 or (y1 - y0) < 3:
                        continue  # bullet dots, not boxes
                    out.append({"glyph": c, "bbox": [x0, y0, x1, y1],
                                "size": span["size"]})
    out.sort(key=lambda m: (m["bbox"][1], m["bbox"][0]))
    return out


def segments(page) -> tuple[list, list]:
    """Split all vector line items into (horizontal, vertical) segments."""
    horiz, vert = [], []
    for d in page.get_drawings():
        for item in d["items"]:
            if item[0] == "l":
                a, b = item[1], item[2]
                if abs(a.y - b.y) < LINE_TOL and abs(a.x - b.x) >= MIN_SEG_LEN:
                    horiz.append((min(a.x, b.x), max(a.x, b.x), (a.y + b.y) / 2))
                elif abs(a.x - b.x) < LINE_TOL and abs(a.y - b.y) >= MIN_SEG_LEN:
                    vert.append(((a.x + b.x) / 2, min(a.y, b.y), max(a.y, b.y)))
            elif item[0] == "re":
                r = item[1]
                horiz.append((r.x0, r.x1, r.y0))
                horiz.append((r.x0, r.x1, r.y1))
                vert.append((r.x0, r.y0, r.y1))
                vert.append((r.x1, r.y0, r.y1))
    return horiz, vert


# --------------------------------------------------------------------------
# comb (digit-cell) detection
# --------------------------------------------------------------------------
def detect_combs(vert: list, page_no: int, counter) -> list[Candidate]:
    """
    Cluster vertical separators sharing a y-extent, then split each cluster into
    evenly-spaced runs. Cell count = separators + 1, because the outer edges of a
    comb are formed by the surrounding rule, not by a vertical line of their own.
    Missing interior separators (pitch gaps of ~2x) are reconstructed.
    """
    clusters: dict[tuple, list] = defaultdict(list)
    for x, y0, y1 in vert:
        clusters[(round(y0), round(y1))].append(x)

    out: list[Candidate] = []
    for (y0, y1), xs in sorted(clusters.items()):
        if len(xs) < COMB_MIN_SEPARATORS:
            continue
        xs = sorted(set(round(x, 2) for x in xs))
        diffs = [b - a for a, b in zip(xs, xs[1:]) if b - a > 0.5]
        if not diffs:
            continue
        pitch = statistics.median(sorted(diffs)[: max(1, len(diffs) // 2)])
        if not (4.0 < pitch < COMB_MAX_PITCH):
            continue

        # split into runs wherever the gap is far larger than the pitch
        runs, cur = [], [xs[0]]
        for a, b in zip(xs, xs[1:]):
            (cur.append(b) if (b - a) <= COMB_RUN_GAP * pitch else (runs.append(cur), cur := [b]))
        runs.append(cur)

        for run in runs:
            if len(run) < COMB_MIN_SEPARATORS:
                continue
            seps = _fill_missing(run, pitch)
            n_cells = len(seps) + 1
            if not (2 <= n_cells <= 24):
                continue
            left = seps[0] - pitch
            boxes = [[round(left + i * pitch, 2), float(y0),
                      round(left + (i + 1) * pitch, 2), float(y1)]
                     for i in range(n_cells)]
            guessed = KNOWN_COMB_LENGTHS.get(n_cells)
            out.append(Candidate(
                id=f"p{page_no}_comb_{next(counter):03d}",
                kind="comb", page=page_no,
                bbox=[boxes[0][0], float(y0), boxes[-1][2], float(y1)],
                cells=n_cells, pitch=round(pitch, 3), cell_boxes=boxes,
                guessed_type=guessed,
                confidence=0.85 if guessed else 0.55,
                needs_review=guessed is None,
            ))
    return out


def _fill_missing(xs: list[float], pitch: float) -> list[float]:
    """Reinsert separators the artwork omitted (a gap of ~k*pitch means k-1 missing)."""
    out = [xs[0]]
    for a, b in zip(xs, xs[1:]):
        k = round((b - a) / pitch)
        for i in range(1, max(k, 1)):
            out.append(a + i * pitch)
        out.append(b)
    return sorted(set(round(x, 2) for x in out))


# --------------------------------------------------------------------------
# underline (write-on line) detection
# --------------------------------------------------------------------------
def detect_underlines(horiz, comb_ys, page_width, page_no, counter) -> list[Candidate]:
    out = []
    seen = set()
    for x0, x1, y in horiz:
        length = x1 - x0
        if length < UNDERLINE_MIN:
            continue
        if length > SECTION_RULE_RATIO * page_width:
            continue                                  # section divider, not a field
        if any(abs(y - cy) < 1.5 for cy in comb_ys):
            continue                                  # baseline of a comb
        key = (round(x0), round(x1), round(y))
        if key in seen:
            continue
        seen.add(key)
        out.append(Candidate(
            id=f"p{page_no}_line_{next(counter):03d}",
            kind="underline", page=page_no,
            bbox=[round(x0, 2), round(y - UNDERLINE_HEIGHT, 2), round(x1, 2), round(y, 2)],
            confidence=0.4,
        ))
    return out


# --------------------------------------------------------------------------
# label association (RTL-aware)
# --------------------------------------------------------------------------
def attach_label(cand: Candidate, spans: list[dict], rtl: bool) -> None:
    """
    In an RTL form the caption sits to the LEFT of its mark and its right edge
    touches the mark's left edge (verified on itc101: label x1 == mark x0 exactly).
    We try that side first, then the mirror side, then the line above.
    """
    x0, y0, x1, y1 = cand.bbox
    cy = (y0 + y1) / 2
    primary, secondary = ("left", "right") if rtl else ("right", "left")

    def _score(span, side):
        sx0, sy0, sx1, sy1 = span["bbox"]
        if not (sy0 - 2 <= cy <= sy1 + 2):
            return None
        gap = (x0 - sx1) if side == "left" else (sx0 - x1)
        return gap if -1.0 <= gap <= LABEL_GAP else None

    for side in (primary, secondary):
        best, best_gap = None, 1e9
        for s in spans:
            g = _score(s, side)
            if g is not None and g < best_gap:
                best, best_gap = s, g
        if best:
            cand.label_he = FOOTNOTE_RE.sub("", best["text"]).strip()
            cand.label_source = f"{side}_span"
            cand.confidence = max(cand.confidence, 0.7)
            return

    above = [s for s in spans
             if s["bbox"][3] <= y0 + 1 and y0 - s["bbox"][3] < 14
             and s["bbox"][0] < x1 and s["bbox"][2] > x0]
    if above:
        best = max(above, key=lambda s: s["bbox"][3])
        cand.label_he = FOOTNOTE_RE.sub("", best["text"]).strip()
        cand.label_source = "above_span"
        cand.confidence = max(cand.confidence, 0.45)
    else:
        cand.label_source = "none"


# --------------------------------------------------------------------------
# grouping marks into radio / multi-select candidates
# --------------------------------------------------------------------------
def group_marks(marks: list[Candidate], page_no: int) -> list[Group]:
    groups, used = [], set()
    gi = 0

    def _emit(members, orientation):
        nonlocal gi
        if len(members) < 2:
            return
        gid = f"p{page_no}_grp_{gi:03d}"
        gi += 1
        for m in members:
            m.group_id = gid
        xs0 = min(m.bbox[0] for m in members); ys0 = min(m.bbox[1] for m in members)
        xs1 = max(m.bbox[2] for m in members); ys1 = max(m.bbox[3] for m in members)
        groups.append(Group(id=gid, page=page_no,
                            kind="radio_candidate", orientation=orientation,
                            members=[m.id for m in members],
                            bbox=[xs0, ys0, xs1, ys1]))

    # vertical columns: same x, stacked
    by_x = defaultdict(list)
    for m in marks:
        by_x[round(m.bbox[0] / GROUP_ALIGN_TOL)].append(m)
    for _, col in by_x.items():
        col.sort(key=lambda m: m.bbox[1])
        run = [col[0]]
        for prev, cur in zip(col, col[1:]):
            h = prev.bbox[3] - prev.bbox[1]
            (run.append(cur) if (cur.bbox[1] - prev.bbox[1]) <= GROUP_Y_GAP * h
             else (_emit(run, "vertical"), run.clear(), run.append(cur)))
        _emit(run, "vertical")
        for m in run:
            used.add(m.id)

    # horizontal rows among marks not already in a column group
    rest = [m for m in marks if m.group_id is None]
    by_y = defaultdict(list)
    for m in rest:
        by_y[round(m.bbox[1] / GROUP_ALIGN_TOL)].append(m)
    for _, row in by_y.items():
        row.sort(key=lambda m: m.bbox[0])
        _emit(row, "horizontal")

    return groups


# --------------------------------------------------------------------------
# annotated overlay for the vision-labelling and calibration steps
# --------------------------------------------------------------------------
COLORS = {"checkbox": (0.85, 0.10, 0.10), "comb": (0.10, 0.35, 0.90),
          "underline": (0.05, 0.60, 0.25)}


def render_overlay(src_doc, page_no, cands, out_png: Path, dpi=200):
    tmp = pymupdf.open()
    tmp.insert_pdf(src_doc, from_page=page_no, to_page=page_no)
    page = tmp[0]
    for i, c in enumerate(cands):
        color = COLORS.get(c.kind, (0.5, 0.5, 0.5))
        r = pymupdf.Rect(c.bbox)
        page.draw_rect(r, color=color, width=0.8)
        if c.kind == "comb" and c.cell_boxes:
            for cb in c.cell_boxes:
                page.draw_rect(pymupdf.Rect(cb), color=color, width=0.3, dashes="[1 2] 0")
        page.insert_text((r.x0 - 1, max(r.y0 - 1.5, 6)), str(i),
                         fontsize=5.5, color=color, fontname="helv")
    page.get_pixmap(dpi=dpi).save(out_png)
    tmp.close()


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def build(pdf_path: Path, out_dir: Path, form_id: str, rtl: bool, dpi: int) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf_path)

    if doc.is_form_pdf:
        print("!! This PDF already has AcroForm fields — fill by field name instead "
              "of rebuilding a coordinate schema. Aborting is probably what you want.")

    all_cands: list[Candidate] = []
    all_groups: list[Group] = []
    per_page_stats = []

    for pno, page in enumerate(doc):
        cb_counter = iter(range(1000))
        comb_counter = iter(range(1000))
        line_counter = iter(range(1000))

        spans = text_spans(page)
        marks = mark_glyphs(page)
        horiz, vert = segments(page)
        pw = page.rect.width

        cands: list[Candidate] = []
        for m in marks:
            cands.append(Candidate(
                id=f"p{pno}_cb_{next(cb_counter):03d}", kind="checkbox", page=pno,
                bbox=[round(v, 2) for v in m["bbox"]], glyph=m["glyph"], confidence=0.9,
            ))
        combs = detect_combs(vert, pno, comb_counter)
        cands += combs
        comb_ys = {c.bbox[3] for c in combs} | {c.bbox[1] for c in combs}
        cands += detect_underlines(horiz, comb_ys, pw, pno, line_counter)

        for c in cands:
            attach_label(c, spans, rtl)
            lexicon_check(c)

        groups = group_marks([c for c in cands if c.kind == "checkbox"], pno)
        for g in groups:
            members = [c for c in cands if c.id in g.members]
            g.label_he = " / ".join(m.label_he for m in members if m.label_he)[:120]

        render_overlay(doc, pno, cands, out_dir / f"page_{pno}.annotated.png", dpi)
        doc[pno].get_pixmap(dpi=dpi).save(out_dir / f"page_{pno}.png")

        all_cands += cands
        all_groups += groups
        per_page_stats.append({
            "page": pno, "checkboxes": sum(c.kind == "checkbox" for c in cands),
            "combs": sum(c.kind == "comb" for c in cands),
            "underlines": sum(c.kind == "underline" for c in cands),
            "groups": len(groups),
            "labelled": sum(bool(c.label_he) for c in cands),
        })

    schema = {
        "form_id": form_id,
        "schema_version": "draft-1",
        "status": "DRAFT — requires LLM labelling (A2) and human calibration (A3)",
        "source_pdf": pdf_path.name,
        "pdf_sha256": sha256(pdf_path),
        "direction": "rtl" if rtl else "ltr",
        "pages": len(doc),
        "page_size": [round(doc[0].rect.width, 2), round(doc[0].rect.height, 2)],
        "coordinate_system": "pdf points, origin top-left (PyMuPDF convention)",
        "stats": per_page_stats,
        "groups": [asdict(g) for g in all_groups],
        "fields": [asdict(c) for c in all_cands],
    }
    (out_dir / "schema.draft.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
    doc.close()
    return schema


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out-dir", default="registry/form")
    ap.add_argument("--form-id", default=None)
    ap.add_argument("--rtl", action="store_true", help="Hebrew/Arabic form: labels sit left of marks")
    ap.add_argument("--dpi", type=int, default=200)
    a = ap.parse_args()

    pdf = Path(a.pdf)
    schema = build(pdf, Path(a.out_dir), a.form_id or pdf.stem, a.rtl, a.dpi)

    print(f"form_id : {schema['form_id']}")
    print(f"sha256  : {schema['pdf_sha256'][:16]}…")
    for s in schema["stats"]:
        print(f"  page {s['page']}: {s['checkboxes']:>3} checkbox  {s['combs']:>3} comb  "
              f"{s['underlines']:>3} underline  {s['groups']:>3} groups  "
              f"{s['labelled']:>3} auto-labelled")
    print(f"→ {a.out_dir}/schema.draft.json")


if __name__ == "__main__":
    main()
