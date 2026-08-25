"""Guide parse/render tests with no AWS.

The guide is the one artifact in the system a non-engineer edits by hand, so
the round-trip is the property that matters: a person opens guide.md, fixes a
deadline, saves, and nothing else in the file moves. These tests pin that,
plus the prompt budget that keeps a long guide from crowding the field list
out of the agent's context.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import guide as gd

SAMPLE = """---
catalog_id: form-106
name: טופס 106
agency: רשות המסים
language: he
---

## Overview

אישור שנתי על הכנסה ועל ניכוי מס.

## Eligibility

כל שכיר שקיבל משכורת בשנת המס.

## Required attachments

- צילום תעודת זהות
- אישור ניכוי מס במקור

## Key rules

יש להגיש עד 30 באפריל.

## Field notes

Notes are keyed by the schema's field_id.

### national_id

תשע ספרות, כולל ספרת ביקורת.

### employer_name

שם המעסיק כפי שהוא מופיע ברישום החברות.

## Common mistakes

## Where to submit

בסניף פקיד השומה.
"""


def test_parses_frontmatter_sections_and_field_notes():
    g = gd.parse(SAMPLE)
    assert g["meta"]["catalog_id"] == "form-106"
    assert g["meta"]["name"] == "טופס 106"
    assert g["meta"]["agency"] == "רשות המסים"
    assert "אישור שנתי" in g["sections"]["Overview"]
    assert "30 באפריל" in g["sections"]["Key rules"]
    assert set(g["field_notes"]) == {"national_id", "employer_name"}
    assert "תשע ספרות" in g["field_notes"]["national_id"]
    # Intro text above the first ### stays with the section, not with a note.
    assert g["sections"]["Field notes"].startswith("Notes are keyed")
    assert g["sections"]["Common mistakes"] == ""


def test_round_trip_is_stable():
    once = gd.render(gd.parse(SAMPLE))
    twice = gd.render(gd.parse(once))
    assert once == twice, "render(parse(x)) must be a fixed point"

    g = gd.parse(once)
    assert g["meta"]["catalog_id"] == "form-106"
    assert set(g["field_notes"]) == {"national_id", "employer_name"}
    assert "30 באפריל" in g["sections"]["Key rules"]


def test_render_keeps_every_section_and_unknown_headings():
    g = gd.parse(SAMPLE)
    g["sections"]["Notes from the clerk"] = "hand-added by a person"
    out = gd.render(g)
    for name in gd.SECTIONS:
        assert f"## {name}" in out
    # A heading a human invented must survive a machine round-trip.
    assert "## Notes from the clerk" in out
    assert "hand-added by a person" in gd.parse(out)["sections"]["Notes from the clerk"]


def test_empty_skeleton_round_trips_and_reads_as_unfilled():
    g = gd.empty({"catalog_id": "blank"})
    assert gd.is_filled(g) is False
    out = gd.render(g)
    assert set(gd.parse(out)["sections"]) >= set(gd.SECTIONS)
    assert gd.is_filled(gd.parse(out)) is False

    gd.set_section(g, "Overview", "now it says something")
    assert gd.is_filled(g) is True


def test_set_field_note_adds_and_clears():
    g = gd.empty()
    gd.set_field_note(g, "national_id", "nine digits")
    assert gd.parse(gd.render(g))["field_notes"]["national_id"] == "nine digits"
    gd.set_field_note(g, "national_id", "   ")
    assert "national_id" not in g["field_notes"]


def test_prompt_block_inlines_only_the_always_needed_sections():
    block = gd.prompt_block(gd.parse(SAMPLE))
    assert "30 באפריל" in block                      # Key rules: inline
    assert "צילום תעודת זהות" in block                # Required attachments: inline
    assert "בסניף פקיד השומה" not in block            # Where to submit: tool-only
    assert "תשע ספרות" not in block                   # field notes: explain_field
    assert "read_guide" in block                      # the agent is told how to get the rest
    assert "טופס 106" in block


def test_prompt_block_respects_its_budget():
    g = gd.empty({"name": "Long form"})
    gd.set_section(g, "Overview", "x" * 5000)
    gd.set_section(g, "Key rules", "y" * 5000)
    block = gd.prompt_block(g, budget=1200)
    # Header text is outside the per-section share; what matters is that a
    # runaway section cannot swallow the whole budget.
    assert block.count("x") <= 700
    assert block.count("y") <= 700
    assert "[...]" in block


def test_prompt_block_is_empty_without_a_guide():
    assert gd.prompt_block(None) == ""
    assert gd.prompt_block(gd.empty({"name": "n"})) == ""


def test_parse_tolerates_a_file_with_no_frontmatter():
    g = gd.parse("## Overview\n\njust prose\n")
    assert g["meta"] == {}
    assert g["sections"]["Overview"] == "just prose"


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} guide tests passed")


if __name__ == "__main__":
    run_all()
