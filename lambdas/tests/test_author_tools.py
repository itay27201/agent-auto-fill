"""Authoring-agent tool tests with no AWS.

The authoring agent writes prose that later gets shown to members of the
public as official guidance, so the interesting cases are all refusals: a
sentence with no stated basis, a note attached to a field that does not exist,
a section name nobody defined. Each of those, accepted quietly, produces a
guide that looks written and is wrong.
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import author_tools as at
from common import guide as gd

FIELDS = [
    {"field_id": "national_id", "label": "National ID", "type": "text",
     "section": "Personal", "required": True},
    {"field_id": "employer_name", "label": "Employer", "type": "text",
     "section": "Employment", "required": False},
]


class FakeCatalog:
    """Stands in for common.catalog: the write tools flush after every call,
    and what matters here is *what* they tried to persist."""

    def __init__(self):
        self.guides = {}
        self.updates = []

    def put_guide(self, cid, guide):
        self.guides[cid] = gd.render(guide)
        return f"catalog/{cid}/guide.md"

    def update(self, cid, **attrs):
        self.updates.append((cid, attrs))
        return {}

    def list_sources(self, _cid):
        return []

    def sources_prefix(self, cid):
        return f"catalog/{cid}/sources/"


def _ctx(emitted=None, guide=None):
    return at.AuthorContext(
        cid="form-106",
        entry={"catalog_id": "form-106", "name": "Form 106"},
        fields=FIELDS,
        guide=guide if guide is not None else gd.empty({"catalog_id": "form-106"}),
        emit=lambda kind, data: (emitted if emitted is not None else []).append((kind, data)),
    )


def test_write_section_requires_a_basis():
    fake, ctx = FakeCatalog(), _ctx()
    with mock.patch.object(at, "cat", fake):
        out = at.run_tool("write_section", {
            "section": "Eligibility",
            "markdown": "Anyone over 18 may file.",
            "basis": "general_knowledge",
            "citation": "I know this form",
        }, ctx)
    assert out["ok"] is False
    assert "basis" in out["error"]
    assert fake.guides == {}, "nothing may be persisted when the basis is rejected"


def test_write_section_requires_a_citation():
    fake, ctx = FakeCatalog(), _ctx()
    with mock.patch.object(at, "cat", fake):
        out = at.run_tool("write_section", {
            "section": "Key rules", "markdown": "Due 30 April.",
            "basis": "source_doc", "citation": "   ",
        }, ctx)
    assert out["ok"] is False
    assert "citation" in out["error"]
    assert fake.guides == {}


def test_write_section_persists_and_stays_a_draft():
    fake, emitted, = FakeCatalog(), []
    ctx = _ctx(emitted)
    with mock.patch.object(at, "cat", fake):
        out = at.run_tool("write_section", {
            "section": "Required attachments",
            "markdown": "- Photo ID\n- Withholding certificate",
            "basis": "source_doc",
            "citation": "instructions.pdf, page 2",
        }, ctx)

    assert out == {"ok": True, "section": "Required attachments", "awaiting_human_review": True}
    assert "Photo ID" in fake.guides["form-106"]
    # The tool marks the entry as having content, but never as published.
    assert fake.updates == [("form-106", {"has_guide": True})]
    assert emitted[0][0] == "guide_updated"


def test_write_field_note_rejects_a_field_that_does_not_exist():
    """A note on an invented field_id is dead text: explain_field looks notes
    up by the schema's ids, so it would never be shown to anyone."""
    fake, ctx = FakeCatalog(), _ctx()
    with mock.patch.object(at, "cat", fake):
        out = at.run_tool("write_field_note", {
            "field_id": "spouse_id", "markdown": "Enter your spouse's ID.",
            "basis": "source_doc", "citation": "instructions.pdf p4",
        }, ctx)
    assert out["ok"] is False
    assert "get_field_list" in out["error"]
    assert fake.guides == {}


def test_write_field_note_round_trips_to_the_field_the_filling_agent_reads():
    fake, ctx = FakeCatalog(), _ctx()
    with mock.patch.object(at, "cat", fake):
        at.run_tool("write_field_note", {
            "field_id": "national_id", "markdown": "Nine digits including the check digit.",
            "basis": "form_itself", "citation": "printed under the box",
        }, ctx)

    reparsed = gd.parse(fake.guides["form-106"])
    assert reparsed["field_notes"]["national_id"] == "Nine digits including the check digit."


def test_deleting_a_field_note_needs_no_basis():
    fake = FakeCatalog()
    ctx = _ctx(guide=gd.parse("## Field notes\n\n### national_id\n\nold text\n"))
    with mock.patch.object(at, "cat", fake):
        out = at.run_tool("write_field_note", {
            "field_id": "national_id", "markdown": "", "basis": "", "citation": "",
        }, ctx)
    assert out["ok"] is True
    assert "national_id" not in gd.parse(fake.guides["form-106"])["field_notes"]


def test_write_section_rejects_an_unknown_section():
    fake, ctx = FakeCatalog(), _ctx()
    with mock.patch.object(at, "cat", fake):
        out = at.run_tool("write_section", {
            "section": "Fees", "markdown": "x",
            "basis": "source_doc", "citation": "p1",
        }, ctx)
    assert out["ok"] is False
    assert fake.guides == {}


def test_get_field_list_reports_which_fields_still_need_a_note():
    ctx = _ctx(guide=gd.parse("## Field notes\n\n### national_id\n\nnine digits\n"))
    out = at.run_tool("get_field_list", {}, ctx)
    by_id = {f["field_id"]: f for f in out["fields"]}
    assert by_id["national_id"]["has_note"] is True
    assert by_id["employer_name"]["has_note"] is False

    scoped = at.run_tool("get_field_list", {"section": "employment"}, ctx)
    assert [f["field_id"] for f in scoped["fields"]] == ["employer_name"]


def test_read_guide_names_the_sections_still_empty():
    ctx = _ctx(guide=gd.parse("## Overview\n\nAn annual income statement.\n"))
    out = at.run_tool("read_guide", {}, ctx)
    assert out["sections"] == {"Overview": "An annual income statement."}
    assert "Eligibility" in out["empty_sections"]
    assert "Overview" not in out["empty_sections"]


def test_a_tool_failure_is_a_result_not_a_crash():
    ctx = _ctx()
    assert "error" in at.run_tool("no_such_tool", {}, ctx)


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} authoring tool tests passed")


if __name__ == "__main__":
    run_all()
