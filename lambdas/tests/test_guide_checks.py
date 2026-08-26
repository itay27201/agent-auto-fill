"""Tests for the deterministic guide checks.

These exist because of one live failure: a guide covering 70 of 97 fields was
published as finished. `is_filled` said yes, because one section had text. The
check that would have caught it is a set difference, and the point of this file
is that it stays a set difference — every assertion here is about a number
being right, not about a model's opinion of its own work.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import guide as gd
from common import guide_checks as gc

FIELDS = [
    {"field_id": "national_id", "label": "National ID", "type": "text", "section": "Personal"},
    {"field_id": "child1_name", "label": "Name", "type": "text", "section": "Children"},
    {"field_id": "child2_name", "label": "Name", "type": "text", "section": "Children"},
    {"field_id": "employer_name", "label": "Employer", "type": "text", "section": "Employment"},
]


def _guide(notes=None, sections=None):
    g = gd.empty({"catalog_id": "form-101"})
    for name, body in (sections or {}).items():
        gd.set_section(g, name, body)
    for fid, body in (notes or {}).items():
        gd.set_field_note(g, fid, body)
    return g


def test_missing_names_every_unnoted_field():
    g = _guide({"national_id": "Nine digits including the check digit."})
    r = gc.check(g, FIELDS)
    assert r["noted"] == 1
    assert r["total"] == 4
    assert r["counts"]["missing"] == 3
    assert set(r["missing"]) == {"child1_name", "child2_name", "employer_name"}
    assert r["complete"] is False


def test_complete_needs_both_full_coverage_and_written_sections():
    notes = {f["field_id"]: f"A useful sentence about {f['field_id']} and where to find it."
             for f in FIELDS}
    covered = gc.check(_guide(notes), FIELDS)
    assert covered["counts"]["missing"] == 0
    # Coverage alone is not completeness: every section is still blank.
    assert covered["complete"] is False

    full = _guide(notes, {s: "written" for s in gd.SECTIONS})
    assert gc.check(full, FIELDS)["complete"] is True


def test_the_field_notes_heading_is_never_counted_as_an_empty_section():
    """"Field notes" is in SECTIONS, but `parse` routes its body into
    `field_notes` and leaves the section blank. Counting it reported one empty
    section on every guide forever — including complete ones."""
    notes = {f["field_id"]: f"A useful sentence about {f['field_id']} and where to find it."
             for f in FIELDS}
    prose = {s: "written" for s in gd.SECTIONS if s != gd.FIELD_NOTES}
    r = gc.check(_guide(notes, prose), FIELDS)
    assert r["empty_sections"] == []
    assert r["complete"] is True


def test_weak_flags_a_note_that_only_restates_the_label():
    g = _guide({
        # Echoes "Name" and stops — the reader learns nothing.
        "child1_name": "Name of the child.",
        # Echoes "Name" and then says which child, and where it is written.
        "child2_name": "Name of the second child - given and family name exactly "
                       "as they appear on the identity card appendix you attach.",
    })
    r = gc.check(g, FIELDS)
    assert "child1_name" in r["weak"]
    assert "child2_name" not in r["weak"]


def test_duplicates_catch_boilerplate_pasted_across_fields():
    body = "Usually completed by the payroll department."
    g = _guide({"national_id": body, "child1_name": body, "child2_name": body,
                "employer_name": "Something else entirely, written for this box."})
    r = gc.check(g, FIELDS)
    assert r["counts"]["duplicates"] == 3
    assert sorted(r["duplicates"][0]) == ["child1_name", "child2_name", "national_id"]


def test_duplicates_tolerate_two_fields_sharing_a_note():
    body = "Usually completed by the payroll department."
    r = gc.check(_guide({"national_id": body, "child1_name": body}), FIELDS)
    assert r["counts"]["duplicates"] == 0, "two is a coincidence; DUPLICATE_AT is three"


def test_duplicates_ignore_whitespace_and_case():
    g = _guide({"national_id": "Fill this in.", "child1_name": "fill this in.",
                "child2_name": "Fill  this\nin."})
    assert gc.check(g, FIELDS)["counts"]["duplicates"] == 3


def test_wrong_language_flags_english_in_a_hebrew_guide():
    g = _guide({
        "national_id": "תשע ספרות כולל ספרת ביקורת, כפי שמופיע בתעודת הזהות.",
        "child1_name": "Enter the child's full name as written on the ID appendix.",
    })
    r = gc.check(g, FIELDS, language="he")
    assert r["wrong_language"] == ["child1_name"]


def test_wrong_language_is_silent_without_a_testable_language():
    g = _guide({"national_id": "Enter your national ID number here please."})
    assert gc.check(g, FIELDS, language="")["counts"]["wrong_language"] == 0
    assert gc.check(g, FIELDS, language="zz")["counts"]["wrong_language"] == 0


def test_wrong_language_ignores_a_note_with_too_little_text_to_judge():
    """A bare number carries no script. Flagging it would be noise."""
    g = _guide({"national_id": "9 (123456782)"})
    assert gc.check(g, FIELDS, language="he")["counts"]["wrong_language"] == 0


def test_counts_survive_the_display_cap():
    """The lists are truncated for display; the counts must not be."""
    many = [{"field_id": f"f{i}", "label": f"L{i}", "type": "text"} for i in range(90)]
    r = gc.check(_guide(), many)
    assert len(r["missing"]) == 40, "list is capped"
    assert r["counts"]["missing"] == 90, "count is the real number"


def test_summary_reports_the_true_count_not_the_capped_list():
    many = [{"field_id": f"f{i}", "label": f"L{i}", "type": "text"} for i in range(90)]
    line = gc.summary(gc.check(_guide(), many))
    assert "0 of 90 fields noted" in line
    assert "90 with no note" in line


def test_check_survives_a_guide_that_does_not_exist_yet():
    r = gc.check(None, FIELDS)
    assert r["noted"] == 0 and r["counts"]["missing"] == 4
    assert r["complete"] is False


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} guide-check tests passed")


if __name__ == "__main__":
    run_all()
