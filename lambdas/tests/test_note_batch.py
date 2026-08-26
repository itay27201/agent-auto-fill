"""Batch field-note tests, with no AWS and no model.

The thing being tested is not prose quality — it is that the batch is honest
about what it did. The failure this replaces reported nothing and stopped at 70
of 97, so every test here is about the accounting: which fields a run targets,
what happens to the rest when one chunk dies, and whether the report tells the
truth afterwards.
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import author_tools as at
from common import guide as gd
from common import note_batch as nb

FIELDS = [{"field_id": f"f{i:02d}", "label": f"Label {i}", "type": "text",
           "section": "Personal" if i < 10 else "Employment"} for i in range(20)]


class FakeCatalog:
    def __init__(self):
        self.guides, self.updates, self.flushes = {}, [], 0

    def put_guide(self, cid, guide):
        self.guides[cid] = gd.render(guide)
        self.flushes += 1
        return f"catalog/{cid}/guide.md"

    def update(self, cid, **attrs):
        self.updates.append((cid, attrs))
        return {}


def _ctx(guide=None, emitted=None):
    return at.AuthorContext(
        cid="form-101",
        entry={"catalog_id": "form-101", "name": "Form 101", "language": "en",
               # No page_keys: _prefix skips the images, which is what we want
               # in a test that must not touch S3.
               "page_keys": []},
        fields=FIELDS,
        guide=guide if guide is not None else gd.empty({"catalog_id": "form-101"}),
        emit=lambda kind, data: (emitted if emitted is not None else []).append((kind, data)),
    )


def _answers(chunk_fields):
    """What a well-behaved model returns for one chunk."""
    return [{"field_id": f["field_id"],
             "markdown": f"What to write in {f['field_id']}, and where to find it.",
             "basis": "form_itself", "citation": "printed beside the box"}
            for f in chunk_fields]


def _fake_invoke(calls=None, fail_on=None):
    """Stands in for llm_json.invoke_json. Replays the field_ids it was asked
    about, so a test can assert the batch sent the right ones."""
    def invoke(_system, content, _max_tokens):
        asked = content[-1]["text"]
        chunk = [f for f in FIELDS if f'"{f["field_id"]}"' in asked]
        if calls is not None:
            calls.append([f["field_id"] for f in chunk])
        if fail_on is not None and len(calls) == fail_on:
            raise RuntimeError("throttled")
        return _answers(chunk)
    return invoke


def test_one_call_covers_every_field_and_reports_completion():
    fake, calls, ctx = FakeCatalog(), [], _ctx()
    with mock.patch.object(nb, "cat", fake), \
         mock.patch.object(nb, "invoke_json", _fake_invoke(calls)):
        out = nb.write_notes(ctx)

    assert out["ok"] is True
    assert out["written"] == 20
    assert out["report"]["counts"]["missing"] == 0
    assert out["report"]["noted"] == 20
    reparsed = gd.parse(fake.guides["form-101"])
    assert len(reparsed["field_notes"]) == 20


def test_it_chunks_rather_than_asking_for_everything_at_once():
    """The whole point: bounded output per call. One call for 97 fields is what
    truncates; one call per field is what runs out of turns."""
    fake, calls, ctx = FakeCatalog(), [], _ctx()
    with mock.patch.object(nb, "cat", fake), \
         mock.patch.object(nb.config, "NOTE_CHUNK_SIZE", 6), \
         mock.patch.object(nb, "invoke_json", _fake_invoke(calls)):
        nb.write_notes(ctx)

    assert [len(c) for c in calls] == [6, 6, 6, 2]
    # Every field asked about exactly once, none invented, none dropped.
    assert sorted(f for c in calls for f in c) == [f["field_id"] for f in FIELDS]


def test_it_flushes_once_per_chunk_not_once_per_note():
    fake, ctx = FakeCatalog(), _ctx()
    with mock.patch.object(nb, "cat", fake), \
         mock.patch.object(nb.config, "NOTE_CHUNK_SIZE", 5), \
         mock.patch.object(nb, "invoke_json", _fake_invoke([])):
        nb.write_notes(ctx)
    assert fake.flushes == 4, "20 fields in chunks of 5 is four writes of guide.md, not twenty"


def test_a_failed_chunk_costs_its_own_fields_and_nothing_else():
    fake, calls, ctx = FakeCatalog(), [], _ctx()
    with mock.patch.object(nb, "cat", fake), \
         mock.patch.object(nb.config, "NOTE_CHUNK_SIZE", 5), \
         mock.patch.object(nb.config, "NOTE_CONCURRENCY", 1), \
         mock.patch.object(nb, "invoke_json", _fake_invoke(calls, fail_on=2)):
        out = nb.write_notes(ctx)

    assert out["ok"] is True, "a dead chunk is a gap in the report, not a dead run"
    assert out["written"] == 15
    assert out["chunks_failed"] and "throttled" in out["chunks_failed"][0]
    assert out["report"]["counts"]["missing"] == 5
    assert set(out["report"]["missing"]) == {f"f{i:02d}" for i in range(5, 10)}


def test_calling_again_repairs_the_gap_instead_of_rewriting_everything():
    """This is what makes a partial run recoverable: the default target is
    fields with no note, so the agent's retry is cheap and additive."""
    fake, ctx = FakeCatalog(), _ctx()
    with mock.patch.object(nb, "cat", fake), \
         mock.patch.object(nb.config, "NOTE_CHUNK_SIZE", 5), \
         mock.patch.object(nb.config, "NOTE_CONCURRENCY", 1), \
         mock.patch.object(nb, "invoke_json", _fake_invoke([], fail_on=2)):
        nb.write_notes(ctx)

    calls = []
    with mock.patch.object(nb, "cat", fake), \
         mock.patch.object(nb, "invoke_json", _fake_invoke(calls)):
        out = nb.write_notes(ctx)

    assert sorted(f for c in calls for f in c) == [f"f{i:02d}" for i in range(5, 10)]
    assert out["report"]["counts"]["missing"] == 0


def test_a_section_filter_rewrites_that_section_even_where_notes_exist():
    fake, calls = FakeCatalog(), []
    ctx = _ctx(guide=gd.parse("## Field notes\n\n### f15\n\nstale text\n"))
    with mock.patch.object(nb, "cat", fake), \
         mock.patch.object(nb, "invoke_json", _fake_invoke(calls)):
        nb.write_notes(ctx, section="employment")

    assert sorted(f for c in calls for f in c) == [f"f{i:02d}" for i in range(10, 20)]
    assert "stale text" not in fake.guides["form-101"]


def test_a_note_on_an_invented_field_id_is_dropped():
    """Same guard as the singular tool. A note keyed to a field that does not
    exist would never be shown to anyone, but would still count as written."""
    fake, ctx = FakeCatalog(), _ctx()

    def invoke(_s, _c, _m):
        return [{"field_id": "not_a_field", "markdown": "text",
                 "basis": "form_itself", "citation": "x"},
                {"field_id": "f00", "markdown": "A real note about a real box.",
                 "basis": "form_itself", "citation": "x"}]

    with mock.patch.object(nb, "cat", fake), \
         mock.patch.object(nb.config, "NOTE_CHUNK_SIZE", 20), \
         mock.patch.object(nb, "invoke_json", invoke):
        out = nb.write_notes(ctx)

    assert out["written"] == 1
    assert "not_a_field" not in gd.parse(fake.guides["form-101"])["field_notes"]


def test_a_note_with_no_stated_basis_is_refused_the_same_as_a_single_write():
    """The bulk path enforces the rule the singular tool enforces. A guide
    sentence with no stated origin is the thing this system exists to refuse;
    arriving in a batch of a hundred must not make it admissible."""
    fake, ctx = FakeCatalog(), _ctx()

    def invoke(_s, _c, _m):
        return [
            {"field_id": "f00", "markdown": "Grounded.", "basis": "form_itself",
             "citation": "printed beside the box"},
            {"field_id": "f01", "markdown": "Invented.", "basis": "general_knowledge",
             "citation": "I know this form"},
            {"field_id": "f02", "markdown": "Uncited.", "basis": "source_doc",
             "citation": "   "},
        ]

    with mock.patch.object(nb, "cat", fake), \
         mock.patch.object(nb.config, "NOTE_CHUNK_SIZE", 20), \
         mock.patch.object(nb, "invoke_json", invoke):
        out = nb.write_notes(ctx, field_ids=["f00", "f01", "f02"])

    assert out["written"] == 1
    assert out["rejected_count"] == 2
    assert out["rejected_no_basis"] == ["f01", "f02"]
    notes = gd.parse(fake.guides["form-101"])["field_notes"]
    assert set(notes) == {"f00"}


def test_progress_is_reported_against_the_real_total():
    fake, seen, ctx = FakeCatalog(), [], _ctx()
    with mock.patch.object(nb, "cat", fake), \
         mock.patch.object(nb.config, "NOTE_CHUNK_SIZE", 6), \
         mock.patch.object(nb, "invoke_json", _fake_invoke([])):
        nb.write_notes(ctx, progress=lambda done, total: seen.append((done, total)))

    assert seen == [(6, 20), (12, 20), (18, 20), (20, 20)]
    assert seen[-1][0] == seen[-1][1], "the last tick must land exactly on the total"


def test_nothing_to_do_is_an_error_the_agent_can_act_on():
    fake, ctx = FakeCatalog(), _ctx()
    with mock.patch.object(nb, "cat", fake):
        out = nb.write_notes(ctx, field_ids=["nope", "also_nope"])
    assert out["ok"] is False
    assert "get_field_list" in out["error"]
    assert fake.flushes == 0


def test_the_tool_emits_progress_and_a_live_guide_to_the_page():
    fake, emitted, ctx = FakeCatalog(), [], _ctx(emitted=[])
    ctx.emit = lambda kind, data: emitted.append((kind, data))
    with mock.patch.object(nb, "cat", fake), \
         mock.patch.object(nb.config, "NOTE_CHUNK_SIZE", 10), \
         mock.patch.object(nb, "invoke_json", _fake_invoke([])):
        out = at.run_tool("write_field_notes", {}, ctx)

    kinds = [k for k, _ in emitted]
    assert kinds.count("note_progress") == 2
    assert "guide_updated" in kinds
    assert dict(emitted)["note_progress"] == {"done": 20, "total": 20}
    assert out["summary"].startswith("20 of 20 fields noted")


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} note-batch tests passed")


if __name__ == "__main__":
    run_all()
