"""Geometry tests with no AWS: the box a value gets stamped into is now read
out of the PDF instead of guessed by a model, and this is where that is checked.

The failure these exist to prevent is specific and was real. On the Israeli 101
form the enrich pass estimated bboxes from a page image and put the employer's
name across the instructions paragraph, with the deduction-file number in the
name cell. Nothing downstream noticed, and because schemas are cached by
document hash in a registry with no TTL, the wrong boxes would have been
inherited by every later upload of that form.
"""
import io
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pypdfium2 as pdfium
from reportlab.pdfgen import canvas

from common import form_map, geometry as geo
from functions.ingest_enrich import _place

W, H = 595.0, 842.0
LABELS = ["שם", "כתובת", "מספר טלפון", "מספר תיק ניכויים"]


def _form() -> bytes:
    """A stripped-down 101: an instructions paragraph, a four-column ruled table
    with labels above and writing cells below, and a signature underline."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setFont("Helvetica", 9)
    c.drawString(60, 800, "This form must be completed by every employee at the start of employment.")

    x0, top, w, h = 60, 700, 480, 60
    c.rect(x0, top, w, h)
    c.line(x0, top + h / 2, x0 + w, top + h / 2)
    for i in (1, 2, 3):
        c.line(x0 + i * w / 4, top, x0 + i * w / 4, top + h)
    # Right-to-left: the first label sits in the rightmost column.
    for i, label in enumerate(LABELS):
        c.drawString(x0 + (3 - i) * w / 4 + 4, top + h - 12, f"L{i}")

    c.drawString(60, 640, "Signature:")
    c.line(120, 638, 380, 638)
    c.save()
    return buf.getvalue()


def _page():
    return pdfium.PdfDocument(_form())[0]


def test_rules_and_cells_reconstruct_the_printed_grid():
    page = _page()
    horizontal, vertical = geo.rules(page)
    assert len(horizontal) >= 3, horizontal   # table top, middle, bottom
    assert len(vertical) >= 5, vertical       # both edges plus three dividers
    assert len(geo.cells(horizontal, vertical)) == 8, "4 columns x 2 rows"


def test_candidate_regions_are_only_the_places_you_can_write():
    """A cell holding a label is not a writing area. This is containment, not
    coverage: a four-character label fills a few percent of a wide cell, so any
    area threshold loose enough to tolerate a stray mark also lets every label
    cell through — and a label cell offered as a writing area is how a value
    ends up stamped over the form's own printing."""
    regions = geo.candidate_regions(_page())

    assert len(regions) == 5, [r["bbox"] for r in regions]  # 4 blank cells + underline
    assert [r["region_id"] for r in regions] == [1, 2, 3, 4, 5]

    text = geo.text_boxes(_page())
    for r in regions:
        assert geo._is_blank(r["bbox"], text), r

    # Ordered right-to-left within a row, because the form is.
    row = [r for r in regions if r["found_by"] == "cell"]
    assert [round(r["bbox"][2], 3) for r in row] == sorted(
        (round(r["bbox"][2], 3) for r in row), reverse=True)


def test_a_region_carries_the_text_printed_around_it():
    """The disambiguator. Three fields labelled שם are told apart by what else
    is printed in their row, and that is visible here and nowhere else."""
    regions = geo.candidate_regions(_page())
    cells = [r for r in regions if r["found_by"] == "cell"]
    above = [n["text"] for n in cells[0]["nearby_text"] if n["side"] == "above"]
    assert "L0" in above, cells[0]["nearby_text"]

    underline = [r for r in regions if r["found_by"] == "rule"][0]
    assert any(n["side"] == "left" and "Signature" in n["text"]
               for n in underline["nearby_text"]), underline["nearby_text"]


def test_sanity_check_refuses_the_boxes_that_produced_the_bug():
    page = _page()
    text = geo.text_boxes(page)
    paragraph = next(t for t in text if "completed" in t["text"])

    assert "own text" in geo.sanity_check(paragraph["bbox"], text)
    assert "too small" in geo.sanity_check([0.5, 0.5, 0.5, 0.5], text)
    assert geo.sanity_check(None, text) == "no box"

    good = geo.candidate_regions(page)[0]["bbox"]
    assert geo.sanity_check(good, text) is None

    # Two fields cannot claim the same cell.
    assert "another field" in geo.sanity_check(good, text, others=[good])

    # A box on the *label's* cell rather than the writing cell below it — the
    # off-by-one-row failure. A short label fills under 2% of its cell, so the
    # coverage check above cannot see this; it needs its own rule.
    label = next(t for t in text if t["text"] == "L0")
    label_cell = [label["bbox"][0] - 0.01, label["bbox"][1] - 0.008,
                  label["bbox"][0] + 0.19, label["bbox"][3] + 0.012]
    assert geo._printed_fraction(label_cell, text) < 0.4, "coverage alone would let this pass"
    assert "own label" in geo.sanity_check(label_cell, text, label="L0")


def test_clamp_repairs_an_ordering_mistake_without_discarding_the_box():
    assert geo.clamp([0.9, 0.2, 0.1, 1.8]) == [0.1, 0.2, 0.9, 1.0]
    assert geo.clamp([-0.5, 0.0, 0.4, 0.1]) == [0.0, 0.0, 0.4, 0.1]


def test_place_resolves_a_region_id_and_never_trusts_a_coordinate():
    """The whole point of the region indirection: the model names a box, the
    coordinates come from the PDF, so a wrong answer is a wrong box rather than
    a box in the wrong place."""
    page = _page()
    regions = geo.candidate_regions(page)
    pages = [{"page": 1, "regions": regions, "text": geo.text_boxes(page)}]

    fields = [
        {"field_id": "employer_name", "label": "שם", "page": 1,
         "region_id": regions[0]["region_id"]},
        # An estimate that lands on the instructions paragraph — the exact
        # failure from the screenshot.
        {"field_id": "ghost", "label": "שם", "page": 1, "bbox": [0.10, 0.040, 0.61, 0.054]},
        # A region id the page does not have.
        {"field_id": "invented", "label": "x", "page": 1, "region_id": 999},
    ]
    placed = {f["field_id"]: f for f in _place(fields, pages)}

    assert placed["employer_name"]["bbox"] == regions[0]["bbox"]
    assert placed["employer_name"]["bbox_confidence"] == "ok"
    assert placed["employer_name"]["bbox_source"] == "region"
    assert placed["employer_name"]["nearby_text"], "the neighbours travel with the box"

    assert placed["ghost"]["bbox_confidence"] == "low"
    assert "own text" in placed["ghost"]["bbox_note"]
    assert placed["ghost"]["bbox"] == [0.0, 0.0, 0.0, 0.0]

    assert placed["invented"]["bbox_confidence"] == "low"
    assert all("region_id" not in f for f in placed.values()), "region ids are internal"


def test_an_estimated_box_on_a_page_that_has_regions_is_refused():
    """The amplifier that put values on the page.

    `_place` graded an estimate `bbox_confidence: "estimated"`, and nothing reads
    that value — every consumer, the renderer included, tests only for `"low"`.
    So a box the model invented was stamped, counted as placed and drawn in the
    viewer exactly like one read out of the PDF's own geometry.

    A page that produced regions and a model that used none of them has failed to
    identify the box. That is not a licence to place it approximately: an unplaced
    field is visibly unplaced, and a misplaced one looks finished.

    The estimate here is deliberately somewhere harmless — blank paper, well clear
    of the form's printing and of every region — so that `sanity_check` has no
    complaint of its own and this rule is the only thing that can refuse it.
    """
    page = _page()
    regions = geo.candidate_regions(page)
    pages = [{"page": 1, "regions": regions, "text": geo.text_boxes(page)}]

    guessed = [{"field_id": "guessed", "label": "שם", "page": 1,
                "bbox": [0.10, 0.40, 0.30, 0.43]}]
    placed = _place(guessed, pages)[0]

    assert placed["bbox_source"] == "estimated", placed["bbox_source"]
    assert placed["bbox_confidence"] == "low"
    assert str(len(regions)) in placed["bbox_note"], placed["bbox_note"]
    assert placed["bbox"] == [0.0, 0.0, 0.0, 0.0]

    # With no geometry anywhere there is nothing better to offer, so the older
    # reason still stands and the wording stays honest about which case it is.
    alone = _place([{"field_id": "g2", "label": "x", "page": 1,
                     "bbox": [0.10, 0.40, 0.30, 0.43]}], [])[0]
    assert alone["bbox_confidence"] == "low"
    assert "no geometry" in alone["bbox_note"], alone["bbox_note"]


def test_describe_position_says_which_box_in_words():
    """`agent_view` drops geometry on purpose — pixel coordinates are not what a
    language model reasons with. "row 1, 2nd box from the right" is."""
    row = [[0.70, 0.10, 0.90, 0.14], [0.50, 0.10, 0.70, 0.14], [0.30, 0.10, 0.50, 0.14]]
    second_row = [[0.70, 0.30, 0.90, 0.34]]
    all_boxes = row + second_row

    assert geo.describe_position(row[0], all_boxes) == "row 1, 1st box from the right"
    assert geo.describe_position(row[2], all_boxes) == "row 1, 3rd box from the right"
    assert geo.describe_position(second_row[0], all_boxes) == "row 2, the only box"
    assert geo.describe_position(row[0], all_boxes, rtl=False) == "row 1, 3rd box from the left"


def test_form_map_calls_out_the_labels_that_repeat():
    """The map's only job the schema cannot do: saying that these three boxes
    are different boxes."""
    fields = [
        # Section א's top row, right to left: name, then the deduction file no.
        {"field_id": "employer_name", "label": "שם", "section": "א. פרטי המעסיק",
         "page": 1, "bbox": [0.70, 0.10, 0.90, 0.14], "bbox_confidence": "ok",
         "help": "The employer's registered name",
         "nearby_text": [{"side": "left", "text": "מספר תיק ניכויים"}]},
        {"field_id": "employer_dednum", "label": "מספר תיק ניכויים",
         "section": "א. פרטי המעסיק", "page": 1,
         "bbox": [0.40, 0.10, 0.60, 0.14], "bbox_confidence": "ok"},
        {"field_id": "employee_first_name", "label": "שם", "section": "ב. פרטי העובד",
         "page": 1, "bbox": [0.70, 0.30, 0.90, 0.34], "bbox_confidence": "ok"},
        {"field_id": "passport", "label": "מספר דרכון", "section": "ב. פרטי העובד",
         "page": 1, "bbox": [0, 0, 0, 0], "bbox_confidence": "low"},
    ]
    md = form_map.render(fields)

    assert "`employer_name`" in md and "`employee_first_name`" in md
    assert "Repeated labels" in md
    assert "שם: `employer_name`, `employee_first_name`" in md
    assert "row 1, 1st box from the right" in md, "employer_name is rightmost in its row"
    assert "row 1, 2nd box from the right" in md, "the file number sits to its left"
    assert "מספר תיק ניכויים" in md, "the neighbours are what distinguish the twins"
    assert "**not placed**" in md, "an unplaced box must be visibly unplaced"
    # Sections are kept apart so "which שם" is answerable from the table alone,
    # and each appears exactly once even though one of its fields has no box.
    assert md.count("### א. פרטי המעסיק") == 1
    assert md.count("### ב. פרטי העובד") == 1


def test_form_map_survives_a_schema_with_no_geometry_at_all():
    """AcroForm documents and pre-existing sessions have no region data. The map
    degrades to a plain listing rather than throwing."""
    md = form_map.render([{"field_id": "f1", "label": "Name", "page": 1}])
    assert "`f1`" in md
    assert "Repeated labels" not in md


def test_textract_regions_come_back_in_the_same_shape_as_pdf_ones():
    """A scan has no geometry to read, so Textract supplies the coordinates —
    but in the same `bbox`/`found_by`/`nearby_text` shape. Everything
    downstream works on regions and never asks where one came from, so the
    scanned path is the ordinary path with a different source."""
    from unittest import mock

    from common import ocr

    def block(bid, btype, box, **extra):
        left, top, right, bottom = box
        return {"Id": bid, "BlockType": btype, "Geometry": {"BoundingBox": {
            "Left": left, "Top": top, "Width": right - left, "Height": bottom - top}}, **extra}

    response = {"Blocks": [
        block("k1", "KEY_VALUE_SET", (0.75, 0.10, 0.90, 0.13),
              EntityTypes=["KEY"],
              Relationships=[{"Type": "CHILD", "Ids": ["w1"]},
                             {"Type": "VALUE", "Ids": ["v1"]}]),
        block("v1", "KEY_VALUE_SET", (0.50, 0.10, 0.74, 0.13), EntityTypes=["VALUE"]),
        block("w1", "WORD", (0.75, 0.10, 0.90, 0.13), Text="שם"),
        # An empty table cell is a writing area; one holding the form's own
        # printing is a label and must not be offered.
        block("c1", "CELL", (0.20, 0.30, 0.40, 0.34)),
        block("c2", "CELL", (0.20, 0.40, 0.40, 0.44),
              Relationships=[{"Type": "CHILD", "Ids": ["w2"]}]),
        block("w2", "WORD", (0.21, 0.41, 0.30, 0.43), Text="כתובת"),
    ]}

    with mock.patch.object(ocr, "textract",
                           lambda: mock.Mock(analyze_document=lambda **_: response)):
        regions = ocr.regions_from_image(b"x")

    assert [r["found_by"] for r in regions] == ["textract_value", "textract_cell"]
    assert [r["region_id"] for r in regions] == [1, 2]
    assert regions[0]["bbox"] == [0.5, 0.1, 0.74, 0.13]
    assert regions[0]["nearby_text"] == [{"side": "right", "text": "שם"}]
    assert all(geo.sanity_check(r["bbox"], []) is None for r in regions)

    # A Textract failure costs its regions, never the upload. (The logged
    # traceback is the point of the branch, so it is silenced rather than
    # allowed to look like a test failure.)
    def boom():
        raise RuntimeError("boom")

    logging.disable(logging.CRITICAL)
    try:
        with mock.patch.object(ocr, "textract", boom):
            assert ocr.regions_from_image(b"x") == []
    finally:
        logging.disable(logging.NOTSET)


def test_a_schema_from_an_older_pipeline_is_a_cache_miss():
    """Without this the fix is unreachable for every form already ingested.

    The registry is keyed by document SHA-256 and has no TTL, so re-uploading
    the identical file hits the identical entry and never re-runs ingest —
    deleting and re-adding the document does nothing at all. A cache hit has to
    match on pipeline generation too.
    """
    from unittest import mock

    from common import config, store

    def lookup(item):
        table = mock.Mock()
        table.get_item.return_value = {"Item": item} if item else {}
        with mock.patch.object(store, "ddb", lambda: table):
            return store.registry_lookup("abc")

    current = {"schema_key": "k", "schema_version": config.SCHEMA_VERSION}
    assert lookup(current)["schema_key"] == "k", "a current entry is still a hit"

    assert lookup({"schema_key": "k", "schema_version": config.SCHEMA_VERSION - 1}) is None
    # Entries written before the field existed are generation 1, not "current".
    assert lookup({"schema_key": "k"}) is None
    assert lookup(None) is None


def test_a_stale_catalog_entry_is_flagged_but_never_rebuilt_behind_your_back():
    """A catalog entry can carry boxes a person placed by hand and a guide a
    person reviewed. Re-running ingest over that to pick up a better default is
    not a trade the system gets to make on its own."""
    from common import catalog as cat, config

    stale = cat.with_staleness({"catalog_id": "form-101", "schema_version": 1})
    assert stale["schema_stale"] is True
    assert "Re-upload" in stale["schema_stale_note"]

    current = cat.with_staleness(
        {"catalog_id": "form-101", "schema_version": config.SCHEMA_VERSION})
    assert "schema_stale" not in current


def test_cells_are_scoped_to_their_row_not_the_whole_page():
    """Two tables side by side at different heights. Pairing grid lines globally
    lets the lower table's column boundaries slice the upper table's rows, so the
    real cells are never even candidates — which is what reconstructed exactly
    one cell for the whole of page 1 of the 101."""
    # Upper table: two columns, x 0.1..0.5, y 0.10..0.16
    # Lower table: three columns at completely different x, y 0.40..0.46
    hor = [[0.10, 0.10, 0.50, 0.10], [0.10, 0.16, 0.50, 0.16],
           [0.60, 0.40, 0.95, 0.40], [0.60, 0.46, 0.95, 0.46]]
    ver = [[0.10, 0.10, 0.10, 0.16], [0.30, 0.10, 0.30, 0.16], [0.50, 0.10, 0.50, 0.16],
           [0.60, 0.40, 0.60, 0.46], [0.75, 0.40, 0.75, 0.46], [0.95, 0.40, 0.95, 0.46]]

    got = geo.cells(hor, ver)
    assert len(got) == 4, got
    for cell in ([0.10, 0.10, 0.30, 0.16], [0.30, 0.10, 0.50, 0.16],
                 [0.60, 0.40, 0.75, 0.46], [0.75, 0.40, 0.95, 0.46]):
        assert any(all(abs(a - b) < 1e-3 for a, b in zip(cell, g)) for g in got), cell


def test_a_border_drawn_in_segments_still_bounds_a_cell():
    """Table borders are routinely drawn one segment per cell. Demanding a single
    rule that spans the whole edge rejects the row."""
    hor = [[0.10, 0.10, 0.30, 0.10], [0.30, 0.10, 0.50, 0.10],   # top, in two pieces
           [0.10, 0.16, 0.50, 0.16]]
    ver = [[0.10, 0.10, 0.10, 0.16], [0.50, 0.10, 0.50, 0.16]]
    assert len(geo.cells(hor, ver)) == 1


def test_near_identical_rules_are_one_grid_line():
    """A stroke plus a fill edge 0.2pt apart is one border, not two lines with a
    hairline cell between them."""
    hor = [[0.1, 0.100, 0.5, 0.100], [0.1, 0.1002, 0.5, 0.1002], [0.1, 0.16, 0.5, 0.16]]
    ver = [[0.1, 0.10, 0.1, 0.16], [0.5, 0.10, 0.5, 0.16]]
    got = geo.cells(hor, ver)
    assert len(got) == 1, got
    assert got[0][3] - got[0][1] > 0.05, "the real row, not a sliver"


def test_merge_keeps_collinear_rules_that_do_not_touch():
    """Two segments of the same column, far apart down the page, are two rules.

    This is the bug at unit level. `_merge` used to sort the whole list by
    `(axis, span)`, so two rules on the same column whose x differed by a
    ten-thousandth ordered by that hair instead of by position — putting the
    lower one first. The "touching" test then passed trivially and `max()`
    absorbed the upper one into it. On page one of the 101 that deleted 45 of 96
    vertical rules, including the separators that divide section ב into its five
    columns, and the employee's ID number was stamped under `שם פרטי`.
    """
    lower = [0.35225, 0.8126, 0.35305, 0.8446]
    upper = [0.35234, 0.2497, 0.35314, 0.2804]   # a hair to the right, far above

    got = geo._merge([lower, upper], axis=0)
    assert len(got) == 2, got
    assert sorted(round(g[1], 4) for g in got) == [0.2497, 0.8126], got

    # ...and the ones that genuinely do touch are still collapsed into one.
    joined = geo._merge([[0.35, 0.10, 0.3508, 0.16], [0.3501, 0.16, 0.3509, 0.22]], axis=0)
    assert len(joined) == 1, joined
    assert round(joined[0][1], 4) == 0.10 and round(joined[0][3], 4) == 0.22

    # Horizontal rules take the same path with the axes swapped.
    rows = geo._merge([[0.60, 0.4001, 0.95, 0.4009], [0.10, 0.4002, 0.50, 0.4010]], axis=1)
    assert len(rows) == 2, "two borders on the same row, with a gap between them"


def test_the_employee_row_reconstructs_five_columns():
    """Section ב of the real 101: מספר זהות, שם משפחה, שם פרטי, תאריך לידה, תאריך עליה.

    The row that started this. It used to come back as two boxes — the first
    spanning `מספר זהות` and `שם משפחה` together, which is where the ID number
    went, and the second spanning `שם פרטי` and `תאריך לידה`. With no region of
    their own, the two name fields fell through to the model's estimate escape
    hatch and landed a whole row down.
    """
    src = Path(__file__).resolve().parents[2] / "Service_Pages_Income_tax_annual-report-2024_itc101.pdf"
    if not src.exists():
        print("     (skipped: the 101 PDF is not in the repo root)")
        return

    page = pdfium.PdfDocument(src.read_bytes())[0]
    band = [r for r in geo.candidate_regions(page) if r["bbox"][3] > 0.25 and r["bbox"][1] < 0.29]

    assert len(band) == 5, [r["bbox"] for r in band]
    # Right to left, one per printed column heading.
    edges = [round(r["bbox"][2], 2) for r in band]
    assert edges == [0.91, 0.74, 0.53, 0.35, 0.20], edges
    # None of them may span two headings: the widest is well under a third of
    # the page, where the merged box used to be 0.39 wide.
    for r in band:
        assert r["bbox"][2] - r["bbox"][0] < 0.25, r["bbox"]


def test_a_long_rule_does_not_swallow_a_short_one_a_point_away():
    """`שנת המס` had no region at all — not a wrong one, none.

    Its underline (y=0.1177, 0.110 wide) sits 3.1pt above the instructions frame
    below it (y=0.1214, 0.859 wide). `_collinear` grouped by `_MERGE_TOL`, 3.4pt,
    so the two counted as one line, `_merge` extended the frame across the
    underline and deleted it, and with no bottom rule `_underlines` never
    produced a box. The model was offered nothing and estimated one instead.

    `_MERGE_TOL` is right for what it does elsewhere — clustering grid positions.
    Deciding whether two drawn lines are the same line is a different question.
    """
    src = Path(__file__).resolve().parents[2] / "Service_Pages_Income_tax_annual-report-2024_itc101.pdf"
    if not src.exists():
        print("     (skipped: the 101 PDF is not in the repo root)")
        return

    page = pdfium.PdfDocument(src.read_bytes())[0]
    regions = geo.candidate_regions(page)

    tax_year = [r for r in regions if 0.09 < r["bbox"][1] < 0.13]
    assert len(tax_year) == 1, [r["bbox"] for r in tax_year]
    assert tax_year[0]["found_by"] == "rule"
    # Four cells, because a tax year is four digits.
    assert tax_year[0]["comb"]["cells"] == 4, tax_year[0].get("comb")

    # The tolerance is tight enough to keep that rule and still loose enough for
    # the case merging exists for: a stroke plus a fill edge a fifth of a point
    # apart is one border, not two lines with a hairline cell between them.
    assert geo._COLLINEAR_TOL < 0.0037, "the gap that swallowed the tax-year box"
    one = geo._merge([[0.1, 0.1000, 0.5, 0.1000], [0.1, 0.1002, 0.5, 0.1002]], axis=1)
    assert len(one) == 1, one


def test_a_box_printed_under_a_caption_carries_that_caption():
    """`own_label` — what the document calls this box, not what sits near it.

    Section ב's five columns are found correctly and still received the wrong
    values, because `nearby_text` is all the enrich prompt had to identify them
    with. For `שם משפחה` it reads `תאריך לידה` then `תאריך עליה` — two *other*
    columns, reported as "left" at a distance of 0.001 — before the box's own
    caption above it. The model concluded the region was not the surname box and
    took the prompt's estimated-bbox escape hatch.

    `_under_label` already had the answer: the text it measures to find where the
    writing space starts is the caption. It is read off the page, so it is not a
    guess, and it outranks anything merely printed nearby.
    """
    src = Path(__file__).resolve().parents[2] / "Service_Pages_Income_tax_annual-report-2024_itc101.pdf"
    if not src.exists():
        print("     (skipped: the 101 PDF is not in the repo root)")
        return

    page = pdfium.PdfDocument(src.read_bytes())[0]
    regions = geo.candidate_regions(page)

    band = [r for r in regions if r["bbox"][3] > 0.25 and r["bbox"][1] < 0.29]
    assert [r["own_label"] for r in band] == [
        "מספר זהות ( 9 ספרות)", "שם משפחה", "שם פרטי", "תאריך לידה", "תאריך עליה"]

    # Only a box printed under its caption can have one; a bare ruled cell has
    # nothing to read, and must not inherit the previous region's label.
    for r in regions:
        assert bool(r.get("own_label")) == (r["found_by"] == "under_label"), r

    # A caption is a caption, not a paragraph: `_under_label` already refuses a
    # cell whose text runs deep, so nothing here is anywhere near the 80-char cap.
    assert max(len(r["own_label"]) for r in band) < 80


def test_checkbox_glyphs_are_writing_areas():
    """The 61 tick squares on the real 101 are ZapfDingbats glyphs, so every test
    in this module used to read them as the form's own printing and throw them
    away. One EnrichFn run logged `33 of 47 fields could not be placed`, almost
    all of them checkboxes reported as 98-100% covered by text."""
    src = Path(__file__).resolve().parents[2] / "Service_Pages_Income_tax_annual-report-2024_itc101.pdf"
    if not src.exists():
        print("     (skipped: the 101 PDF is not in the repo root)")
        return

    page = pdfium.PdfDocument(src.read_bytes())[0]
    boxes = [r for r in geo.candidate_regions(page) if r.get("is_checkbox")]
    assert len(boxes) == 36, len(boxes)

    # The font is the discriminator, so nothing that is merely small and square
    # comes through: the page carries plenty of digits at the same size.
    for r in boxes:
        w, h = r["bbox"][2] - r["bbox"][0], r["bbox"][3] - r["bbox"][1]
        assert 0.004 <= w <= 0.025 and 0.004 <= h <= 0.025, r["bbox"]
        assert r["found_by"] == "checkbox"

    # Each one carries the choice it stands for. On this RTL form the label sits
    # to the box's left.
    labels = {n["text"] for r in boxes for n in r["nearby_text"]}
    for choice in ("זכר", "רווק/ה", "נשוי/אה", "גרוש/ה"):
        assert choice in labels, choice


def test_comb_cells_are_detected_and_only_where_they_are_real():
    """`מספר זהות` is printed as nine separate squares, one per digit, and a date
    as eight. Drawn as one string the number starts at an edge and drifts out of
    step with the cells immediately."""
    src = Path(__file__).resolve().parents[2] / "Service_Pages_Income_tax_annual-report-2024_itc101.pdf"
    if not src.exists():
        print("     (skipped: the 101 PDF is not in the repo root)")
        return

    page = pdfium.PdfDocument(src.read_bytes())[0]
    regions = geo.candidate_regions(page)
    combed = [r for r in regions if r.get("comb")]
    assert combed, "the form is full of character-cell boxes"

    # The identity column of the employee row: nine cells for nine digits.
    row = [r for r in regions if r["bbox"][3] > 0.25 and r["bbox"][1] < 0.29]
    ident = max(row, key=lambda r: r["bbox"][2])
    assert ident["comb"]["cells"] == 9, ident.get("comb")
    assert len(ident["comb"]["xs"]) == 10, "one boundary more than there are cells"
    assert ident["comb"]["xs"] == sorted(ident["comb"]["xs"])

    for r in combed:
        assert r["comb"]["cells"] >= geo._COMB_MIN_TICKS + 1
        assert not r.get("is_checkbox"), "a tick square is not divided into cells"

    # Evenness is the test that keeps this off boxes that merely have strokes
    # crossing them.
    ticks = [[0.10, 0.10, 0.101, 0.16], [0.30, 0.10, 0.301, 0.16]]
    assert geo._comb_for([0.05, 0.10, 0.90, 0.16], ticks) is None, "two ticks is not a comb"
    ragged = [[0.11, 0.10, 0.111, 0.16], [0.20, 0.10, 0.201, 0.16],
              [0.70, 0.10, 0.701, 0.16]]
    assert geo._comb_for([0.10, 0.10, 0.90, 0.16], ragged) is None, "gaps nothing like equal"


def test_sanity_check_does_not_reject_a_checkbox_for_being_a_checkbox():
    """A tick goes on top of the form's own square — that is what the square is
    for — and the square is smaller than anything a person writes a word in.
    The two tests that measure those things reject every checkbox on the page."""
    glyph = [0.888, 0.334, 0.901, 0.343]
    printed = [{"bbox": glyph, "text": "o"}]

    assert geo.sanity_check(glyph, printed) is not None, "rejected as ordinary text"
    assert geo.sanity_check(glyph, printed, is_checkbox=True) is None

    # It may also sit inside a wider cell without that counting as a collision.
    row = [0.60, 0.33, 0.92, 0.345]
    assert geo.sanity_check(glyph, printed, others=[row], is_checkbox=True) is None
    # A degenerate box is still refused, checkbox or not.
    assert geo.sanity_check([0.5, 0.5, 0.5, 0.5], [], is_checkbox=True) is not None


def test_a_label_inside_its_own_cell_still_yields_a_writing_area():
    """Sections א, ב and ו of the 101 print the label in the top corner of the
    box you write in. Rejecting every cell that contains text loses all of them.

    The label itself comes back with the box. It is the text the function already
    had to measure to find where the writing space starts, and it is the document
    stating what this box is called — see `own_label` below.
    """
    cell = [0.70, 0.10, 0.90, 0.16]
    label = [{"bbox": [0.71, 0.105, 0.78, 0.118], "text": "שם"}]
    assert not geo._is_blank(cell, label)

    got = geo._under_label(cell, label)
    assert got is not None
    below, caption = got
    assert below[1] > 0.118, "starts under the label, not on it"
    assert below[3] == 0.16 and below[0] == 0.70
    assert caption == "שם", caption

    # Right-to-left, so a caption split into fragments reassembles rightmost
    # first. Left to its own order it reads backwards.
    split = [{"bbox": [0.80, 0.105, 0.88, 0.118], "text": "מספר"},
             {"bbox": [0.71, 0.105, 0.79, 0.118], "text": "זהות"}]
    assert geo._under_label(cell, split)[1] == "מספר זהות"

    # Text running deep into the cell is a paragraph or a filled-in value.
    deep = [{"bbox": [0.71, 0.105, 0.88, 0.155], "text": "a whole paragraph"}]
    assert geo._under_label(cell, deep) is None


def test_a_label_row_with_its_own_blank_row_beneath_is_not_a_second_box():
    """The other layout: a header row of labels and empty cells under it. Taking
    the header's leftover space too would offer two boxes for one field."""
    label_cell = [0.10, 0.10, 0.30, 0.14]
    blank_below = [0.10, 0.14, 0.30, 0.20]
    assert geo._has_box_beneath(label_cell, [blank_below])
    # ...but not when the empty cell is in a different column.
    assert not geo._has_box_beneath(label_cell, [[0.60, 0.14, 0.80, 0.20]])


def test_snap_takes_a_region_it_clearly_matches_and_nothing_else():
    """Snapping edges to the nearest rule was measured to make boxes *worse* on
    a dense form — rules sit closer together than a model's error. Matching a
    whole region by overlap either finds the right one or leaves the box alone."""
    regions = [{"bbox": [0.70, 0.10, 0.90, 0.16]}, {"bbox": [0.40, 0.10, 0.60, 0.16]}]

    near = [0.705, 0.104, 0.895, 0.158]
    out, moved = geo.snap(near, regions)
    assert moved and out == [0.70, 0.10, 0.90, 0.16]

    # Nothing close enough: returned untouched rather than dragged somewhere.
    far = [0.10, 0.70, 0.20, 0.74]
    out, moved = geo.snap(far, regions)
    assert not moved and out == geo.clamp(far)
    assert geo.snap([0.1, 0.2, 0.3, 0.4], []) == ([0.1, 0.2, 0.3, 0.4], False)


def test_merge_regions_prefers_the_pdf_and_renumbers():
    """The PDF's geometry is exact where it works; Textract fills the gaps. A
    tie goes to the PDF, and ids stay contiguous so the numbers drawn on the
    page image still address every region."""
    pdf = [{"bbox": [0.70, 0.10, 0.90, 0.16], "found_by": "cell"}]
    ocr_regions = [
        {"bbox": [0.705, 0.101, 0.898, 0.159], "found_by": "textract_value"},  # same box
        {"bbox": [0.10, 0.50, 0.30, 0.56], "found_by": "textract_value",
         "textract_label": "מספר זהות"},                                       # new
    ]
    merged = geo.merge_regions(pdf, ocr_regions)
    assert len(merged) == 2
    assert [r["region_id"] for r in merged] == [1, 2]
    kept = [r for r in merged if r["bbox"] == [0.70, 0.10, 0.90, 0.16]]
    assert kept and kept[0]["found_by"] == "cell", "the PDF wins the overlap"
    assert any(r.get("textract_label") == "מספר זהות" for r in merged)


def test_textract_may_subdivide_a_region_the_pdf_read_too_wide():
    """Scoring the overlap against the smaller area means a finer box inside a
    coarser one always scores 1.0, so Textract could only ever fill a gap and
    never correct a row whose dividing rules went unread — which is the one case
    where the PDF is wrong and Textract can see it."""
    wide = [{"bbox": [0.50, 0.26, 0.91, 0.28], "found_by": "cell"}]
    columns = [
        {"bbox": [0.74, 0.26, 0.91, 0.28], "found_by": "textract_value",
         "textract_label": "מספר זהות"},
        {"bbox": [0.50, 0.26, 0.73, 0.28], "found_by": "textract_value",
         "textract_label": "שם משפחה"},
    ]
    merged = geo.merge_regions(wide, columns)

    assert len(merged) == 2, merged
    assert not any(r["bbox"] == [0.50, 0.26, 0.91, 0.28] for r in merged), \
        "the over-wide region is superseded by the columns inside it"
    assert {r["textract_label"] for r in merged} == {"מספר זהות", "שם משפחה"}
    assert [r["region_id"] for r in merged] == [1, 2]

    # One child on its own is not evidence of anything: the coarse region stays,
    # and the finer box joins it rather than replacing it.
    merged = geo.merge_regions(wide, columns[:1])
    assert len(merged) == 2, merged
    assert any(r["bbox"] == [0.50, 0.26, 0.91, 0.28] for r in merged)

    # A checkbox is never treated as a parent — everything overlaps a tick
    # square without that saying anything about how the page is divided.
    tick = [{"bbox": [0.888, 0.334, 0.901, 0.343], "found_by": "checkbox",
             "is_checkbox": True}]
    kept = geo.merge_regions(tick, [{"bbox": [0.60, 0.33, 0.92, 0.345],
                                     "found_by": "textract_value"}])
    assert len(kept) == 2 and any(r.get("is_checkbox") for r in kept)


def test_a_select_gets_one_box_per_printed_choice():
    """`מצב משפחתי` is five tick squares, but a field carries one bbox. Without a
    box per choice, picking any option but the one the model anchored on stamps
    the answer onto the wrong square — as text, because the type is "select"
    rather than "checkbox", into a box nine thousandths of a page wide."""
    from functions.ingest_enrich import _option_boxes, _place

    def tick(x, label):
        return {"bbox": [x, 0.332, x + 0.013, 0.341], "found_by": "checkbox",
                "is_checkbox": True, "region_id": int(x * 1000),
                "nearby_text": [{"side": "left", "text": label}]}

    regions = [tick(0.723, "נשוי/אה"), tick(0.610, "גרוש/ה"), tick(0.823, "רווק/ה"),
               tick(0.530, "אלמן/ה"), tick(0.450, "פרוד/ה")]
    field = {"field_id": "employee_marital_status", "type": "select",
             "options": ["רווק/ה", "נשוי/אה", "גרוש/ה", "אלמן/ה", "פרוד/ה"]}

    boxes = _option_boxes(field, regions)
    assert [b["value"] for b in boxes] == field["options"]
    assert boxes[0]["bbox"][0] == 0.823 and boxes[1]["bbox"][0] == 0.723
    assert len({tuple(b["bbox"]) for b in boxes}) == 5, "no square used twice"

    # All or nothing: a partial map would stamp some choices correctly and drop
    # the rest onto whatever square the field anchored on.
    assert _option_boxes(field, regions[:3]) == []

    # Two choices sharing a short prefix must not collapse onto one square.
    kibbutz = {"field_id": "employee_kibbutz_member", "type": "select",
               "options": ["לא", "כן, הכנסותיי ממעסיק זה מועברות לקיבוץ",
                           "כן, הכנסותיי ממעסיק זה אינן מועברות לקיבוץ"]}
    got = _option_boxes(kibbutz, [
        tick(0.30, "לא"),
        tick(0.50, "כן, הכנסותיי ממעסיק זה מועברות לקיבוץ"),
        tick(0.70, "כן, הכנסותיי ממעסיק זה אינן מועברות לקיבוץ"),
    ])
    assert len({tuple(b["bbox"]) for b in got}) == 3, got

    # And a select whose squares cannot all be found is refused, not left to
    # stamp a choice as text into a tick box.
    pages = [{"page": 1, "text": [], "regions": regions[:2]}]
    placed = _place([{**field, "page": 1, "region_id": regions[0]["region_id"]}], pages)
    assert placed[0]["bbox_confidence"] == "low"
    assert "choices" in placed[0]["bbox_note"], placed[0]["bbox_note"]


def _tick(x, label, y=0.332):
    return {"bbox": [x, y, x + 0.013, y + 0.009], "found_by": "checkbox",
            "is_checkbox": True, "region_id": int(x * 1000) + int(y * 10),
            "nearby_text": [{"side": "left", "text": label}]}


def test_a_tick_square_the_model_called_text_is_still_a_tick_square():
    """`is_checkbox` comes off the page's own content stream; `type` is the ingest
    model's opinion. When the two disagree the page wins — otherwise the renderer
    has no signal and writes the value as a string into a box a few points wide,
    which is the whole failure."""
    regions = [_tick(0.72, "בן/בת זוג עובד/ת")]
    pages = [{"page": 1, "text": [], "regions": regions}]

    field = {"field_id": "spouse_works", "label": "בן/בת זוג עובד/ת",
             "type": "text", "page": 1, "region_id": regions[0]["region_id"]}
    placed = _place([field], pages)[0]

    assert placed["type"] == "checkbox", "a printed square is not a text field"
    assert placed["backend"]["mark"] == "checkbox"
    assert placed["bbox_confidence"] == "ok"


def test_a_mistyped_tick_square_carrying_choices_is_read_as_a_choice():
    """Same coercion, but the model also returned options — so this is a printed
    choice, and it needs one box per option rather than a single tick."""
    regions = [_tick(0.72, "נשוי/אה"), _tick(0.61, "רווק/ה")]
    pages = [{"page": 1, "text": [], "regions": regions}]

    field = {"field_id": "marital", "label": "מצב משפחתי", "type": "date",
             "page": 1, "region_id": regions[0]["region_id"],
             "options": ["רווק/ה", "נשוי/אה"]}
    placed = _place([field], pages)[0]

    assert placed["type"] == "select"
    boxes = placed["backend"]["option_boxes"]
    assert [b["value"] for b in boxes] == ["רווק/ה", "נשוי/אה"]
    assert boxes[0]["bbox"][0] == 0.61 and boxes[1]["bbox"][0] == 0.72


def test_a_select_anchored_beside_the_tick_row_still_finds_its_squares():
    """The enrich prompt steers mutually exclusive checkboxes toward one `select`,
    and the model often anchors that select on the wide cell beside the row rather
    than on a square. `is_checkbox` is then false, so before this every guard was
    unarmed and the choice was written as text into that cell."""
    ticks = [_tick(0.72, "נשוי/אה"), _tick(0.61, "גרוש/ה"), _tick(0.83, "רווק/ה")]
    cell = {"bbox": [0.40, 0.325, 0.90, 0.350], "region_id": 900,
            "found_by": "cell", "nearby_text": [{"side": "right", "text": "מצב משפחתי"}]}
    pages = [{"page": 1, "text": [], "regions": ticks + [cell]}]

    field = {"field_id": "marital", "label": "מצב משפחתי", "type": "select",
             "page": 1, "region_id": 900,
             "options": ["רווק/ה", "נשוי/אה", "גרוש/ה"]}
    placed = _place([field], pages)[0]

    boxes = placed["backend"].get("option_boxes")
    assert boxes, "a select beside the tick row must still map onto the squares"
    assert [b["value"] for b in boxes] == field["options"]
    assert placed["backend"]["mark"] == "checkbox"


def test_a_select_with_no_tick_squares_is_left_alone():
    """The widened lookup must not claim an ordinary write-on-a-line select. It is
    all or nothing, so a page with no squares to match simply yields nothing —
    and the field keeps its own box and stays a text write."""
    cell = {"bbox": [0.40, 0.325, 0.90, 0.350], "region_id": 900, "found_by": "cell",
            "nearby_text": [{"side": "right", "text": "מקצוע"}]}
    pages = [{"page": 1, "text": [], "regions": [cell]}]

    field = {"field_id": "profession", "label": "מקצוע", "type": "select",
             "page": 1, "region_id": 900, "options": ["שכיר", "עצמאי"]}
    placed = _place([field], pages)[0]

    assert "option_boxes" not in placed["backend"]
    assert placed["backend"].get("mark") != "checkbox"
    assert placed["bbox_confidence"] == "ok", "an ordinary select must still render"


def test_the_real_form_reconstructs_its_boxes():
    """The bar that matters. Everything above is a synthetic page; this is the
    document that was being filled wrongly.

    Skipped when the PDF is not present, so the suite still runs anywhere.
    """
    src = Path(__file__).resolve().parents[2] / "Service_Pages_Income_tax_annual-report-2024_itc101.pdf"
    if not src.exists():
        print("     (skipped: the 101 PDF is not in the repo root)")
        return

    pdf = pdfium.PdfDocument(src.read_bytes())
    page_one = geo.candidate_regions(pdf[0])
    # Before this change the same page produced 2. The form has roughly 70
    # writing areas; anything near the old number means the grid is not being
    # reconstructed and the model is back to guessing coordinates.
    assert len(page_one) >= 45, len(page_one)

    kinds = {r["found_by"] for r in page_one}
    assert "cell" in kinds and "under_label" in kinds, kinds

    # Section א's employer row: four columns near the top, and the name cell is
    # the one that used to receive the deduction-file number.
    band = [r for r in page_one if 0.18 < r["bbox"][1] < 0.22]
    assert len(band) >= 3, band
    assert any(r["bbox"][2] > 0.90 for r in band), "the rightmost (שם) cell"

    for r in page_one:
        assert geo.area(r["bbox"]) > 0
        assert all(0.0 <= v <= 1.0 for v in r["bbox"])


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} geometry tests passed")


if __name__ == "__main__":
    run_all()
