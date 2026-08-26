"""Where the boxes actually are, read out of the PDF instead of guessed.

The old flat-document path asked a vision model to *estimate* four normalized
floats per field from a page image. That is the one thing vision models are
worst at, and on a dense right-to-left table like the Israeli 101 form it
produced values that were confidently wrong: the employer's name stamped across
the instructions paragraph, the deduction-file number sitting in the name cell.
Nothing downstream noticed, and the wrong schema went into the SHA-256 registry,
which has no TTL — so every later upload of that form inherited it.

A printed government form is not a picture. It is a table of ruled cells with
text in some of them, and the PDF says exactly where every rule and every glyph
sits. This module reads that out:

    text_boxes(page)         every printed string, with its box
    rules(page)              the horizontal and vertical ruled lines
    candidate_regions(page)  the boxes a person writes in — ruled cells and
                             underlines with no text of their own

The model is then given those regions with id numbers, and picks one per field
rather than inventing coordinates. Its worst case degrades from "a box in the
wrong place" to "the wrong box", which `sanity_check` and the viewer's box
editor can both catch.

Everything here is in normalized [x0, y0, x1, y1] with a top-left origin, 0..1
of page width and height — the same convention as `schema.FormField.bbox`, so
nothing downstream has to convert.

pypdfium2 is already in the layer for rasterizing, so this costs no new
dependency, no new AWS call, and no IAM change.
"""
from __future__ import annotations

import logging

log = logging.getLogger()

# A rule thinner than this (as a fraction of the page) is a line; anything
# fatter is a filled shape we should not mistake for one. 0.004 of an A4 height
# is about 3pt — comfortably above a hairline, below any real box.
_RULE_THICKNESS = 0.004
# Two rules within this of each other are the same rule drawn twice, which is
# extremely common: a table border is often a stroke plus a fill edge.
_MERGE_TOL = 0.004
# Whether two drawn lines are the SAME line. A different question from
# `_MERGE_TOL`'s "do these grid positions cluster", and it needs a much tighter
# answer: ~1pt rather than ~3.4pt. A duplicate stroke sits hundredths of a point
# from its twin and still merges, but on the 101 a 3.1pt gap was enough for the
# instructions frame to absorb the tax-year underline and delete it, leaving
# `שנת המס` with no region at all.
_COLLINEAR_TOL = 0.0012
# A break in a table border smaller than this is a seam between two strokes, not
# a missing edge. Borders are frequently drawn one segment per cell.
_COVER_GAP = 0.012
# A rule shorter than this is a tick mark or an underscore in body text, not
# part of the form's grid.
_MIN_RULE_LEN = 0.02
# A candidate region smaller than this in either dimension cannot be written in.
_MIN_REGION_W = 0.015
_MIN_REGION_H = 0.008
# ...and one larger than this is a page border or a section frame, not a box.
_MAX_REGION_W = 0.97
_MAX_REGION_H = 0.30
# How much printed text a region may contain and still count as blank. A stray
# antialiased comma from the row above should not disqualify a whole cell.
_BLANK_TEXT_TOLERANCE = 0.06

# A checkbox is neither a path nor a ruled cell. On the 101 every one of the 61
# of them is a ZapfDingbats glyph — `o` and `q`, which draw as empty squares — so
# `text_boxes` reports them as the form's own printing and all three tests that
# follow throw the box away: `_is_blank` sees a character in the cell,
# `_printed_fraction` reports the box 98-100% covered, and the minimum-size floor
# calls it too small to write in. They are, in fact, the most common writing area
# on the page.
#
# The font is the discriminator, not the size. A digit is about the same size and
# shape as a dingbat square, so a size-and-aspect heuristic would offer half the
# form's serial numbers as checkboxes.
_SYMBOL_FONTS = ("dingbat", "wingding", "webding", "symbol", "marlett")
# ...with the box codepoints as a fallback, for a form whose symbol font is
# embedded under a name that gives nothing away.
_BOX_GLYPHS = "☐☑☒■□▪▫◻◼❑❒❏❐"
_CHECKBOX_MIN, _CHECKBOX_MAX = 0.004, 0.025
# Width over height. ZapfDingbats' square measures 1.41 because the glyph's
# advance is wider than its box; anything far outside this is a letter.
_CHECKBOX_ASPECT = (0.5, 2.0)

# A comb is a box divided into one cell per character — the nine squares an
# Israeli ID number goes into, the eight a date does. Its dividers are far too
# short to be rules (0.006 of the page against `_MIN_RULE_LEN`'s 0.02), so
# `rules` drops them, and the whole number ends up drawn as one string starting
# at the box's left edge.
_COMB_MIN_TICK = 0.003
_COMB_MIN_TICKS = 3
# How far the widest cell may stray from the average before this stops being a
# comb and starts being a row of unrelated tick marks. Measured across the 101's
# combs: the worst real one is 0.25 out, because the gap to the box's own edge
# runs a little wider than the gaps between dividers.
_COMB_EVENNESS = 0.3


# ---------------------------------------------------------------- conversion

def norm(rect, pw: float, ph: float) -> list[float]:
    """PDF rect [left, bottom, right, top] with a bottom-left origin, to
    normalized [x0, y0, x1, y1] with a top-left origin.

    Lives here rather than in ingest_extract because both the AcroForm path and
    the flat path need it and two copies would drift.
    """
    if not rect:
        return [0.0, 0.0, 0.0, 0.0]
    left, bottom, right, top = (float(x) for x in rect)
    pw = pw or 1.0
    ph = ph or 1.0
    return [
        round(min(left, right) / pw, 5),
        round(1.0 - max(top, bottom) / ph, 5),
        round(max(left, right) / pw, 5),
        round(1.0 - min(top, bottom) / ph, 5),
    ]


def area(bbox) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def intersection(a, b) -> float:
    """Area of overlap between two normalized boxes."""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def clamp(bbox) -> list[float]:
    """Squeeze into 0..1 and put the corners the right way round. A model that
    returns [x1, y0, x0, y1] has made an ordering mistake, not a placement one,
    and throwing the box away would lose a field that is nearly correct."""
    x0, y0, x1, y1 = (float(v) for v in bbox)
    x0, x1 = sorted((min(max(x0, 0.0), 1.0), min(max(x1, 0.0), 1.0)))
    y0, y1 = sorted((min(max(y0, 0.0), 1.0), min(max(y1, 0.0), 1.0)))
    return [round(x0, 5), round(y0, 5), round(x1, 5), round(y1, 5)]


# ---------------------------------------------------------------------- text

def text_boxes(page) -> list[dict]:
    """Every printed string on the page with its normalized box.

    Two jobs downstream: deciding which candidate regions are blank (a cell with
    text in it is a label, not a writing area), and `sanity_check`'s refusal to
    stamp a value over the form's own printing.
    """
    pw, ph = page.get_size()
    try:
        textpage = page.get_textpage()
    except Exception:
        log.warning("no text page available — document is probably a scan")
        return []

    try:
        return _line_rects(textpage, pw, ph) or _char_rects(textpage, pw, ph)
    except Exception:
        log.exception("text extraction failed; continuing without a text layer")
        return []
    finally:
        try:
            textpage.close()
        except Exception:
            pass


def _line_rects(textpage, pw: float, ph: float) -> list[dict]:
    """pdfium's own line segmentation. Cheap and usually right."""
    out = []
    for i in range(textpage.count_rects()):
        left, bottom, right, top = textpage.get_rect(i)
        text = (textpage.get_text_bounded(left=left, bottom=bottom,
                                          right=right, top=top) or "").strip()
        if text:
            out.append({"bbox": norm((left, bottom, right, top), pw, ph), "text": text})
    return out


def _char_rects(textpage, pw: float, ph: float) -> list[dict]:
    """Fallback: glyph boxes grouped into runs.

    Used when pdfium reports no line rects, which happens on PDFs whose text is
    drawn glyph by glyph with no line structure — not rare in forms typeset by
    older government tooling.
    """
    chars = []
    for i in range(textpage.count_chars()):
        try:
            box = textpage.get_charbox(i)
        except Exception:
            continue
        ch = textpage.get_text_range(i, 1)
        if ch and ch.strip():
            chars.append((norm(box, pw, ph), ch))

    out: list[dict] = []
    for bbox, ch in chars:
        prev = out[-1] if out else None
        # Same run if it shares a baseline and starts within a character's width
        # of where the last one ended — on either side, so RTL groups too.
        if prev and abs(prev["bbox"][1] - bbox[1]) < 0.004 and (
            abs(bbox[0] - prev["bbox"][2]) < 0.012 or abs(prev["bbox"][0] - bbox[2]) < 0.012
        ):
            prev["bbox"] = [min(prev["bbox"][0], bbox[0]), min(prev["bbox"][1], bbox[1]),
                            max(prev["bbox"][2], bbox[2]), max(prev["bbox"][3], bbox[3])]
            prev["text"] += ch
        else:
            out.append({"bbox": list(bbox), "text": ch})
    for r in out:
        r["text"] = r["text"].strip()
    return [r for r in out if r["text"]]


# --------------------------------------------------------------------- rules

def rules(page) -> tuple[list[list[float]], list[list[float]]]:
    """The page's ruled lines, as (horizontal, vertical) normalized boxes.

    A form's grid is drawn as path objects. A path whose bounding box is thin in
    one dimension and long in the other is a rule; one that is thin in both is a
    tick; one that is fat in both is a filled shape, which we ignore rather than
    mistake for a cell — a shaded section header is not a place to write.

    Note that pdfium reports a path's bounds including its stroke width, so a
    hairline rule measures a point or two thick rather than zero. `_RULE_THICKNESS`
    is set above that, not at it.
    """
    import pypdfium2.raw as pdfium_c

    pw, ph = page.get_size()
    horizontal: list[list[float]] = []
    vertical: list[list[float]] = []

    try:
        objects = list(page.get_objects(max_depth=4))
    except Exception:
        log.exception("could not walk page objects; no ruled lines this page")
        return [], []

    for obj in objects:
        if getattr(obj, "type", None) != pdfium_c.FPDF_PAGEOBJ_PATH:
            continue
        try:
            bbox = norm(obj.get_bounds(), pw, ph)
        except Exception:
            continue

        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if h <= _RULE_THICKNESS and w >= _MIN_RULE_LEN:
            horizontal.append(bbox)
        elif w <= _RULE_THICKNESS and h >= _MIN_RULE_LEN:
            vertical.append(bbox)
        elif w > _RULE_THICKNESS and h > _RULE_THICKNESS:
            # A drawn rectangle: its four edges are a cell even when the page
            # has no grid at all. Contribute them as rules so the same cell
            # builder picks it up.
            if w <= _MAX_REGION_W and h <= _MAX_REGION_H:
                horizontal.append([bbox[0], bbox[1], bbox[2], bbox[1]])
                horizontal.append([bbox[0], bbox[3], bbox[2], bbox[3]])
                vertical.append([bbox[0], bbox[1], bbox[0], bbox[3]])
                vertical.append([bbox[2], bbox[1], bbox[2], bbox[3]])

    return _merge(horizontal, axis=1), _merge(vertical, axis=0)


def _merge(lines: list[list[float]], axis: int) -> list[list[float]]:
    """Collapse rules that sit on top of each other.

    Table borders are routinely drawn twice — once as a stroke, once as the edge
    of a filled cell — and a duplicated rule turns one row of cells into two
    rows of hairline slivers.

    `axis` is the coordinate that stays constant: 1 (y) for horizontal rules,
    0 (x) for vertical ones.

    Collinear rules are grouped *before* they are merged, and only then sorted
    along the span. Sorting the whole list by `(axis, span)` — which this used to
    do — looks equivalent and is not: two rules on the same column whose x
    differs by a ten-thousandth order by that hair rather than by position down
    the page, so a rule near the bottom can precede one near the top. The
    "touching" test below then passes trivially and `max()` absorbs the second
    rule into the first, deleting it. On page one of the 101 that destroyed 45 of
    96 vertical rules, including the column separators for `שם משפחה` and
    `שם פרטי` — which merged five columns into two and put the employee's ID
    number under the wrong heading.
    """
    if not lines:
        return []
    span = 1 - axis  # the coordinate the rule extends along

    out = []
    for group in _collinear(lines, axis):
        group.sort(key=lambda b: b[span])
        run = list(group[0])
        for line in group[1:]:
            # Only merge rules that actually touch. Two separate cell borders on
            # the same row must stay separate or the gap between them disappears
            # and takes a column boundary with it.
            if line[span] <= run[span + 2] + _MERGE_TOL:
                run[span + 2] = max(run[span + 2], line[span + 2])
                run[axis + 2] = max(run[axis + 2], line[axis + 2])
            else:
                out.append(run)
                run = list(line)
        out.append(run)
    return out


def _collinear(lines: list[list[float]], axis: int) -> list[list[list[float]]]:
    """Rules sitting on the same line, grouped.

    Anchored on each group's *first* member rather than its last, so a run of
    rules a tolerance apart cannot chain into one group that drifts across a real
    column boundary. That matches what the old `prev[axis]` comparison did, which
    was the one part of it worth keeping.
    """
    groups: list[list[list[float]]] = []
    for line in sorted(lines, key=lambda b: b[axis]):
        if groups and line[axis] - groups[-1][0][axis] <= _MERGE_TOL:
            groups[-1].append(list(line))
        else:
            groups.append([list(line)])
    return groups


def cells(horizontal: list[list[float]], vertical: list[list[float]]) -> list[list[float]]:
    """Rectangles bounded by rules on all four sides.

    The obvious implementation — one sorted list of every y on the page, one of
    every x, pair adjacent values — is wrong on any page with more than one
    table, and a government form is eight tables. Section א's columns get sliced
    by the children table's columns three hundred points further down, and the
    real cell is never even a candidate. On the 101 that reconstructed **one**
    cell for the whole of page one.

    So the columns are scoped to the row: for each band between two horizontal
    rules, only the vertical rules that actually span that band are allowed to
    divide it. Each table then reconstructs against its own grid and ignores
    every other table on the page. Same page, same rules: 60 cells.
    """
    ys = _cluster(b[1] for b in horizontal)
    if len(ys) < 2:
        return []

    out = []
    for top, bottom in zip(ys, ys[1:]):
        if not (_MIN_REGION_H <= bottom - top <= _MAX_REGION_H):
            continue
        in_band = [v for v in vertical
                   if v[1] <= top + _COVER_GAP and v[3] >= bottom - _COVER_GAP]
        xs = _cluster(v[0] for v in in_band)
        for left, right in zip(xs, xs[1:]):
            if not (_MIN_REGION_W <= right - left <= _MAX_REGION_W):
                continue
            if (_covered(horizontal, top, left, right, axis=1)
                    and _covered(horizontal, bottom, left, right, axis=1)):
                out.append([round(left, 5), round(top, 5), round(right, 5), round(bottom, 5)])
    return out


def _cluster(values, tol: float = _MERGE_TOL) -> list[float]:
    """Collapse near-identical rule positions into one line each.

    Rounding to a fixed number of places does not do this: two strokes 0.2pt
    apart round to two different values and produce a grid line pair with a
    hairline "cell" between them, which then fails every size check. Page one of
    the 101 has 58 raw y-positions and 50 real ones.
    """
    out: list[list[float]] = []
    for v in sorted(values):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return [sum(group) / len(group) for group in out]


def _covered(lines, position: float, start: float, end: float, axis: int) -> bool:
    """Is [start, end] covered at `position` by the UNION of collinear rules?

    Asking for a single rule that spans the whole edge is too strict: a table
    border is routinely drawn in pieces, often one segment per cell, and one
    unlucky break rejects the row. Gaps under `_COVER_GAP` are tolerated because
    a real one is a hairline seam between two strokes, not a missing border.
    """
    span = 1 - axis
    pieces = sorted(
        (max(line[span], start), min(line[span + 2], end))
        for line in lines
        if abs(line[axis] - position) <= _MERGE_TOL
        and line[span] < end and line[span + 2] > start
    )
    reach = start
    for begins, ends in pieces:
        if begins > reach + _COVER_GAP:
            return False
        reach = max(reach, ends)
    return reach >= end - _COVER_GAP


# --------------------------------------------------------- candidate regions

def candidate_regions(page) -> list[dict]:
    """The places on this page a person writes.

    A ruled cell containing no printed text, or the strip above an underline.
    Each gets a stable id, its box, how it was found, and the printed text
    nearest it — which is what lets the model tell three boxes labelled `שם`
    apart without ever handling a coordinate.
    """
    text = text_boxes(page)
    horizontal, vertical = rules(page)

    found: list[tuple[str, list[float]]] = [("cell", c) for c in cells(horizontal, vertical)]
    found += [("rule", b) for b in _underlines(horizontal, vertical, text)]

    blank_cells = [b for kind, b in found if kind == "cell" and _is_blank(b, text)]

    regions = []
    for kind, bbox in found:
        if not _is_blank(bbox, text):
            # Not empty — but on this kind of form that does not mean "not a
            # writing area". See `_under_label`.
            if _has_box_beneath(bbox, blank_cells):
                continue  # the form gave this label its own box; use that one
            below = _under_label(bbox, text)
            if below is None:
                continue  # it holds the form's own text: a label, not a box
            bbox, kind = below, "under_label"
        if any(intersection(bbox, r["bbox"]) / max(area(bbox), 1e-9) > 0.7 for r in regions):
            continue  # already covered by a region we kept
        regions.append({"bbox": bbox, "found_by": kind})

    # Checkboxes are appended rather than run through the loop above, and are
    # deliberately exempt from both its tests. A tick box is never blank — it
    # holds the very glyph that makes it a tick box — and it legitimately sits
    # inside a larger region, so the "already covered" rule would discard every
    # one that shares a row with a cell.
    boxes = checkbox_glyphs(page, text)
    regions += boxes

    # Tick squares are printed characters, so without this every region beside
    # one reports `{"side": "left", "text": "o"}` among its nearest text. That is
    # noise in the one field the model uses to tell two identically-labelled
    # boxes apart, and on this form it is a third of what page one sends. They
    # still count as printing everywhere it matters — `_is_blank` and
    # `_printed_fraction` both work off the unfiltered list.
    glyphs = {tuple(b["bbox"]) for b in boxes}
    labels = [t for t in text if tuple(t["bbox"]) not in glyphs]
    for r in regions:
        r["nearby_text"] = nearby_text(r["bbox"], labels)

    ticks = comb_ticks(page)
    for r in regions:
        if r.get("is_checkbox"):
            continue
        comb = _comb_for(r["bbox"], ticks)
        if comb:
            r["comb"] = comb

    # Reading order, top to bottom then right to left. RTL because these forms
    # are, and because the model is asked to reason about "the cell to the
    # right of the label" — an order that fights the page makes that harder.
    regions.sort(key=lambda r: (round(r["bbox"][1], 2), -r["bbox"][2]))
    for i, r in enumerate(regions, start=1):
        r["region_id"] = i
    return regions


def _underlines(horizontal, vertical, text) -> list[list[float]]:
    """"Fill in the blank on this line" — a rule with no cell around it.

    The writing area is the strip immediately *above* the rule, since that is
    where a pen goes. Height is taken from the nearest text on the same line so
    the box matches the form's own type size.
    """
    out = []
    for line in horizontal:
        left, y = line[0], line[1]
        right = line[2]
        if right - left < _MIN_REGION_W:
            continue
        # Skip rules that belong to a grid — those already became cells, and
        # keeping them would put a second region across every table row. A rule
        # is part of a grid if any vertical rule crosses it.
        if any(v[1] - _MERGE_TOL <= y <= v[3] + _MERGE_TOL
               and left - _MERGE_TOL <= v[0] <= right + _MERGE_TOL
               for v in vertical):
            continue
        neighbours = [t for t in text if t["bbox"][3] <= y + 0.004 and t["bbox"][3] >= y - 0.05
                      and t["bbox"][2] > left and t["bbox"][0] < right]
        height = max((t["bbox"][3] - t["bbox"][1]) for t in neighbours) if neighbours else 0.018
        out.append([left, max(0.0, y - height * 1.2), right, y])
    return out


def _has_box_beneath(bbox, blank_cells) -> bool:
    """Does an empty cell sit directly under this one, in the same column?

    Two layouts look identical to `_under_label` and mean opposite things. A form
    that puts `שם` in the top corner of the box you write in wants the space
    under the label. A form with a header row of labels and empty cells beneath
    wants the cell beneath — and treating the header's leftover space as a second
    writing area would offer two boxes for one field.

    The form itself settles it: if it drew an empty box under this label, that is
    the box.
    """
    x0, _, x1, y1 = bbox
    width = max(x1 - x0, 1e-9)
    for c in blank_cells:
        if abs(c[1] - y1) > _COVER_GAP:
            continue
        overlap = min(x1, c[2]) - max(x0, c[0])
        if overlap / width > 0.6:
            return True
    return False


def checkbox_glyphs(page, text=None) -> list[dict]:
    """The little squares a person ticks, read off the page as glyphs.

    Returned in the same shape as every other region so `_place`, `sanity_check`
    and the numbered-image trick do not have to care where a region came from —
    plus `is_checkbox`, which tells `sanity_check` to skip the two tests that
    would otherwise reject all of them, and tells the renderer to draw an X
    rather than a string.

    `nearby_text` is left to the caller rather than filled in here, because the
    labels these boxes want are the ones with every *other* tick square already
    filtered out, and that list does not exist until all of them are known.
    """
    if text is None:
        text = text_boxes(page)

    pw, ph = page.get_size()
    try:
        textpage = page.get_textpage()
    except Exception:
        log.warning("no text page available — no checkbox glyphs this page")
        return []

    out = []
    try:
        for i in range(textpage.count_chars()):
            ch = textpage.get_text_range(i, 1)
            if not ch or not ch.strip():
                continue
            # The overwhelming majority of characters on these forms are Hebrew
            # body text. Rejecting those before asking pdfium for a font name
            # keeps this to a few dozen ctypes calls per page instead of a few
            # thousand.
            if "֐" <= ch <= "׿" or ch.isdigit():
                continue
            if not _is_mark_glyph(ch, _font_name(textpage, i)):
                continue
            try:
                bbox = clamp(norm(textpage.get_charbox(i), pw, ph))
            except Exception:
                continue
            w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if not (_CHECKBOX_MIN <= w <= _CHECKBOX_MAX
                    and _CHECKBOX_MIN <= h <= _CHECKBOX_MAX):
                continue
            if not (_CHECKBOX_ASPECT[0] <= w / h <= _CHECKBOX_ASPECT[1]):
                continue
            out.append({"bbox": bbox, "found_by": "checkbox", "is_checkbox": True})
    except Exception:
        log.exception("checkbox scan failed; continuing without its regions")
    finally:
        try:
            textpage.close()
        except Exception:
            pass
    return out


def comb_ticks(page) -> list[list[float]]:
    """The short dividers that split one box into one cell per character.

    Deliberately not folded into `rules`. These are an order of magnitude shorter
    than a rule, and letting them into the grid would divide every row they touch
    into nine columns — `_MIN_RULE_LEN` exists to keep them out. They are useful
    only once a region is known, as a description of how that one box is divided.
    """
    import pypdfium2.raw as pdfium_c

    pw, ph = page.get_size()
    try:
        objects = list(page.get_objects(max_depth=4))
    except Exception:
        log.exception("could not walk page objects; no comb cells this page")
        return []

    ticks = []
    for obj in objects:
        if getattr(obj, "type", None) != pdfium_c.FPDF_PAGEOBJ_PATH:
            continue
        try:
            bbox = norm(obj.get_bounds(), pw, ph)
        except Exception:
            continue
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if w <= _RULE_THICKNESS and _COMB_MIN_TICK <= h < _MIN_RULE_LEN:
            ticks.append(bbox)
    return ticks


def _comb_for(bbox, ticks) -> dict | None:
    """How this box is divided into character cells, or None if it is not.

    Evenness is the whole test. Any box on a dense form has some short strokes
    crossing it; only a comb has them at a regular pitch, and a value distributed
    across cells that are not really there is worse than one written straight.
    """
    x0, y0, x1, y1 = bbox
    inside = sorted({round(t[0], 5) for t in ticks
                     if x0 + _MERGE_TOL < t[0] < x1 - _MERGE_TOL
                     and t[3] > y0 and t[1] < y1})
    if len(inside) < _COMB_MIN_TICKS:
        return None

    bounds = [x0, *inside, x1]
    gaps = [b - a for a, b in zip(bounds, bounds[1:])]
    mean = sum(gaps) / len(gaps)
    if mean <= 0 or max(abs(g - mean) for g in gaps) > mean * _COMB_EVENNESS:
        return None
    return {"cells": len(gaps), "xs": [round(v, 5) for v in bounds]}


def _is_mark_glyph(ch: str, font: str) -> bool:
    if ch in _BOX_GLYPHS:
        return True
    lowered = font.lower()
    return any(name in lowered for name in _SYMBOL_FONTS)


def _font_name(textpage, index: int) -> str:
    """The font one character is set in. pypdfium2 has no wrapper for this, so
    it goes through the raw binding."""
    import ctypes

    import pypdfium2.raw as pdfium_c

    try:
        buf = ctypes.create_string_buffer(128)
        flags = ctypes.c_int()
        n = pdfium_c.FPDFText_GetFontInfo(textpage.raw, index, buf, 128, ctypes.byref(flags))
        return buf.raw[:max(0, n - 1)].decode("utf-8", "replace") if n else ""
    except Exception:
        return ""


def merge_regions(primary: list[dict], extra: list[dict]) -> list[dict]:
    """Fold a second source of regions into the first, then renumber.

    The PDF's own geometry is exact where it works and silent where it does not;
    Textract finds boxes in the gaps and knows which printed label each one
    belongs to. Neither is a superset of the other, so they are unioned rather
    than one replacing the other — and `primary` wins a tie, because a cell
    reconstructed from ruled lines is exact while an OCR box is inferred.

    But "overlaps a region we already have" describes two different situations,
    and this used to treat them alike. Scoring the overlap against the *smaller*
    of the two areas means a finer Textract box sitting wholly inside a coarser
    PDF one scores exactly 1.0 and is always discarded — so Textract could only
    ever fill a gap, never correct a region that was too wide. That is precisely
    the case where the PDF is wrong: when the rules dividing a row were not read,
    the row comes back as one box spanning several columns, and the evidence that
    it did is the two or three smaller boxes Textract found inside it.

    So: a candidate of comparable size is the same box found twice, and the PDF
    keeps it. A materially smaller one inside it is a subdivision, and is kept.
    A region with enough of those inside it to account for most of its area was
    under-segmented, and is dropped in favour of them.
    """
    out = [dict(r) for r in primary]
    children: dict[int, list[list[float]]] = {}

    for candidate in extra:
        box = candidate.get("bbox")
        if not box or area(box) <= 0:
            continue

        duplicate, parent = False, None
        for i, r in enumerate(out):
            overlap = intersection(box, r["bbox"])
            if overlap / max(min(area(box), area(r["bbox"])), 1e-9) <= 0.6:
                continue
            # Comparable in size, measured both ways round. One-sided, this
            # would call a wide box a duplicate of any tick square inside it.
            if area(box) > area(r["bbox"]) * 0.6 and area(r["bbox"]) > area(box) * 0.6:
                duplicate = True    # the same box, found twice over
                break
            # A checkbox is never a parent: everything overlaps a tick square
            # without that saying anything about how the page is divided.
            if not r.get("is_checkbox"):
                parent = i
        if duplicate:
            continue

        out.append(dict(candidate))
        if parent is not None:
            children.setdefault(parent, []).append(box)

    superseded = {
        i for i, boxes in children.items()
        if len(boxes) >= 2
        and sum(intersection(b, out[i]["bbox"]) for b in boxes) > area(out[i]["bbox"]) * 0.5
    }
    if superseded:
        log.info("%d region(s) replaced by the finer boxes found inside them", len(superseded))
    out = [r for i, r in enumerate(out) if i not in superseded]

    out.sort(key=lambda r: (round(r["bbox"][1], 2), -r["bbox"][2]))
    for i, r in enumerate(out, start=1):
        r["region_id"] = i
    return out


def _under_label(bbox, text, min_free: float = 0.010):
    """The writing space below a label printed inside its own cell, or None.

    Plenty of forms — the Israeli 101 among them — do not give labels a row of
    their own. `שם` is printed in the top corner of the very cell you write the
    name into, and the space underneath it is the box. `_is_blank` throws all of
    those away, which on page one of the 101 is sections א, ב and ו: every field
    the person filling the form actually cares about.

    The test is that the cell's text all hugs its top edge. Text running deeper
    than that is a paragraph or a filled-in value, and the cell is not offered.
    """
    x0, y0, x1, y1 = bbox
    contents = [t for t in text
                if x0 <= (t["bbox"][0] + t["bbox"][2]) / 2 <= x1
                and y0 <= (t["bbox"][1] + t["bbox"][3]) / 2 <= y1]
    if not contents:
        return None

    lowest = max(t["bbox"][3] for t in contents)
    if lowest > y0 + (y1 - y0) * 0.55:
        return None
    free = y1 - lowest
    if free < min_free:
        return None
    # Start just below the label rather than flush against it, so a stamped
    # value does not collide with the form's own printing.
    return [x0, round(lowest + free * 0.08, 5), x1, y1]


def _is_blank(bbox, text) -> bool:
    """Whether a region is somewhere to write rather than somewhere already
    written.

    The test is containment, not coverage. A four-character label sitting in a
    wide table cell covers only a few percent of it, so any area threshold
    permissive enough to tolerate a stray mark also lets every label cell
    through — which is how a label cell ends up offered as a writing area and a
    value gets stamped over the form's own printing. If a string starts inside
    the box, the box is spoken for.
    """
    x0, y0, x1, y1 = bbox
    for t in text:
        tx0, ty0, tx1, ty1 = t["bbox"]
        cx, cy = (tx0 + tx1) / 2, (ty0 + ty1) / 2
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return False
    return _printed_fraction(bbox, text) <= _BLANK_TEXT_TOLERANCE


def _printed_fraction(bbox, text) -> float:
    """How much of this box is covered by the form's own printing.

    A degenerate box is 0, not 1: it is unusable for reasons that have nothing
    to do with text, and reporting it as fully covered would hand the person
    fixing it a diagnosis that is simply untrue.
    """
    box_area = area(bbox)
    if box_area <= 0:
        return 0.0
    covered = sum(intersection(bbox, t["bbox"]) for t in text)
    return min(1.0, covered / box_area)


def nearby_text(bbox, text, limit: int = 4) -> list[dict]:
    """The printed strings closest to a box, tagged with which side they sit on.

    This is the disambiguator. On form 101 the label `שם` appears in at least
    three places; the one in the employer table is the only one whose row also
    reads `מספר תיק ניכויים`, and that is visible here and nowhere else.
    """
    x0, y0, x1, y1 = bbox
    mid_y = (y0 + y1) / 2
    out = []
    for t in text:
        tx0, ty0, tx1, ty1 = t["bbox"]
        if ty0 < y1 and ty1 > y0:                     # shares a row
            if tx1 <= x0:
                out.append((x0 - tx1, "left", t["text"]))
            elif tx0 >= x1:
                out.append((tx0 - x1, "right", t["text"]))
        elif ty1 <= y0 and tx0 < x1 and tx1 > x0:     # sits above
            out.append((y0 - ty1 + 0.5, "above", t["text"]))
        elif ty0 >= y1 and tx0 < x1 and tx1 > x0:     # sits below
            out.append((ty0 - y1 + 0.9, "below", t["text"]))
    out.sort(key=lambda r: r[0])
    return [{"side": side, "text": txt[:60]} for _, side, txt in out[:limit]]


# -------------------------------------------------------------------- snapping

# How much of a box must coincide with a known region before we accept that the
# model meant that region. Chosen by measurement, not taste: across both pages of
# the 101 at four error magnitudes, 0.5 matched 130 boxes correctly and 2 wrongly,
# where 0.3 matched 220 correctly and 14 wrongly. Being silent is cheap here — the
# box is simply left as the model gave it — and being wrong is the entire bug this
# pipeline exists to prevent, so the conservative threshold wins.
_SNAP_MIN_OVERLAP = 0.5


def snap(bbox, regions) -> tuple[list[float], bool]:
    """Pull an estimated box onto the region it overlaps, if one clearly matches.

    Returns the box and whether it moved.

    An earlier version of this snapped each edge to the nearest ruled line
    independently. Measured on the 101 that made boxes *worse*: page one carries
    51 vertical rules, closer together than the error a model makes, so an edge
    lands on the neighbouring column's border and the box drifts further from
    where it was meant. 23 of 42 boxes degraded.

    Matching a whole region by overlap cannot do that. The result is always a
    real region rather than four unrelated lines stitched together, and when
    nothing matches well the estimate is returned untouched — never worse than
    it was.
    """
    if not bbox or len(bbox) != 4 or not regions:
        return bbox, False

    box = clamp(bbox)
    best, score = None, 0.0
    for r in regions:
        candidate = r.get("bbox") if isinstance(r, dict) else r
        if not candidate:
            continue
        overlap = _iou(box, candidate)
        if overlap > score:
            best, score = candidate, overlap

    if best is None or score < _SNAP_MIN_OVERLAP:
        return box, False
    return list(best), True


def _iou(a, b) -> float:
    """Intersection over union — how much two boxes are the same box."""
    inter = intersection(a, b)
    union = area(a) + area(b) - inter
    return inter / union if union > 0 else 0.0


# ------------------------------------------------------------------ describing

_ORDINALS = ("1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th", "10th")


def describe_position(bbox, siblings, rtl: bool = True) -> str:
    """"row 2, 1st box from the right" — a box's position in words.

    The filling agent is never given coordinates: pixel geometry is not what it
    reasons with, and `agent_view` has always dropped it for good reason. But it
    does have to tell three boxes labelled `שם` apart, and "the one in row 1 of
    the employer table" is something a language model can hold onto where
    `[0.72, 0.41, 0.91, 0.44]` is not.
    """
    if not bbox or len(bbox) != 4:
        return ""
    rows = _rows([b for b in siblings if b and len(b) == 4])
    for i, row in enumerate(rows, start=1):
        if not any(_same_box(bbox, b) for b in row):
            continue
        ordered = sorted(row, key=lambda b: -b[2] if rtl else b[0])
        for j, b in enumerate(ordered):
            if _same_box(bbox, b):
                side = "right" if rtl else "left"
                place = _ORDINALS[j] if j < len(_ORDINALS) else f"{j + 1}th"
                return (f"row {i}, {place} box from the {side}"
                        if len(ordered) > 1 else f"row {i}, the only box")
    return ""


def _rows(boxes: list[list[float]]) -> list[list[list[float]]]:
    """Group boxes into visual rows by vertical overlap."""
    rows: list[list[list[float]]] = []
    for b in sorted(boxes, key=lambda b: b[1]):
        for row in rows:
            # Same row if it overlaps the row's band by more than half its height.
            top, bottom = min(r[1] for r in row), max(r[3] for r in row)
            if min(b[3], bottom) - max(b[1], top) > (b[3] - b[1]) * 0.5:
                row.append(b)
                break
        else:
            rows.append([b])
    return rows


def _same_box(a, b) -> bool:
    return all(abs(x - y) < 1e-4 for x, y in zip(a, b))


# ------------------------------------------------------------ sanity checking

def sanity_check(bbox, text, others=(), label: str = "",
                 is_checkbox: bool = False) -> str | None:
    """Return why this box is unusable, or None if it is fine.

    Every one of these is a real failure seen on the 101 form. They are
    mechanical and cost nothing, and the alternative to running them is stamping
    a person's name across a tax form's instructions and calling it filled.

    A checkbox is exempt from three of them, because for a tick box each one is
    measuring the wrong thing. It is smaller than anything a person writes a word
    in; the X goes *on top of* the form's own square, which is what the square is
    for; and it sits inside whatever larger cell shares its row without that
    being a collision. Applying these to checkboxes rejected every one of the 61
    on this form.
    """
    if not bbox or len(bbox) != 4:
        return "no box"

    box = clamp(bbox)
    if area(box) <= 0:
        return "box is too small to write in"
    if not is_checkbox and area(box) < _MIN_REGION_W * _MIN_REGION_H:
        return "box is too small to write in"

    # Then the most specific diagnosis available. Every branch here rejects, so
    # the ordering decides only which message the person placing the box by hand
    # reads — and "it is on its own label" tells them what to do, where "it
    # overlaps some text" makes them work it out.
    if label:
        for t in text:
            if t["text"].strip() == label.strip() and intersection(box, t["bbox"]) > 0:
                return "box is on its own label rather than beside it"

    if not is_checkbox:
        printed = _printed_fraction(box, text)
        if printed > 0.4:
            return f"box lies on top of the form's own text ({printed:.0%} covered)"

    if box[2] - box[0] > _MAX_REGION_W and box[3] - box[1] > _MAX_REGION_H:
        return "box covers most of the page"

    if not is_checkbox:
        for other in others:
            overlap = intersection(box, other)
            if overlap / max(min(area(box), area(other)), 1e-9) > 0.6:
                return "box overlaps another field's box"

    return None


# ------------------------------------------------------------------ annotate

def annotate(png: bytes, regions: list[dict]) -> bytes:
    """Draw each region's id into the page image.

    The point of the whole module. A vision model asked for a coordinate guesses
    badly; the same model asked to read a number printed inside a box gets it
    right, because that is character recognition rather than spatial estimation.
    """
    import io

    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size

    for r in regions:
        x0, y0, x1, y1 = r["bbox"]
        box = [x0 * w, y0 * h, x1 * w, y1 * h]
        draw.rectangle(box, outline=(255, 0, 0), width=2)
        tag = str(r["region_id"])
        # A filled chip behind the number: these forms are dense, and a bare
        # numeral over a ruled line is not reliably readable.
        tw, th = 7 * len(tag) + 6, 15
        chip = [box[0] + 1, box[1] + 1, box[0] + 1 + tw, box[1] + 1 + th]
        draw.rectangle(chip, fill=(255, 0, 0))
        draw.text((chip[0] + 3, chip[1] + 2), tag, fill=(255, 255, 255))

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
