"""Tool dispatcher tests with no AWS: the source/evidence enforcement in
common/tools.py is the whole trust model for what the agent is allowed to
write, so it gets exercised directly rather than only through the roundtrip
test's document-handling path.
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import guide as gd
from common import schema as sch
from common import tools


def _field(**over) -> sch.FormField:
    base = dict(field_id="f1", label="Family name", type="text", required=True)
    base.update(over)
    return sch.FormField(**base)


class FakeStore:
    """In-memory stand-in for common.store, keyed the same way tools.py
    calls it: get_values/set_value/append_event/confirm_value."""

    def __init__(self):
        self.values: dict[str, dict] = {}
        self.events: list[dict] = []

    def get_values(self, _sid):
        return self.values

    def set_value(self, _sid, field_id, value, source, actor, expected_version=None, confirmed=None):
        self.values[field_id] = {
            "value": value, "source": source,
            "confirmed": bool(confirmed) if confirmed is not None else (source != "agent"),
        }
        return self.values[field_id]

    def append_event(self, _sid, kind, actor, **payload):
        self.events.append({"kind": kind, "actor": actor, **payload})


def _ctx(store: FakeStore, emitted: list):
    fields = [_field()]
    return tools.ToolContext(sid="s1", fields=fields, actor="tester",
                              emit=lambda kind, data: emitted.append((kind, data)))


def test_set_field_rejects_bad_source():
    store, emitted = FakeStore(), []
    with mock.patch.object(tools, "store", store):
        out = tools.run_tool("set_field", {
            "field_id": "f1", "value": "Cohen", "source": "inferred", "evidence": "guessed it",
        }, _ctx(store, emitted))
    assert out["ok"] is False
    assert "source" in out["error"]
    assert "f1" not in store.values


def test_set_field_rejects_missing_evidence():
    store, emitted = FakeStore(), []
    with mock.patch.object(tools, "store", store):
        out = tools.run_tool("set_field", {
            "field_id": "f1", "value": "Cohen", "source": "user_said", "evidence": "  ",
        }, _ctx(store, emitted))
    assert out["ok"] is False
    assert "evidence" in out["error"]


def test_set_field_writes_as_unconfirmed_agent_draft():
    store, emitted = FakeStore(), []
    with mock.patch.object(tools, "store", store):
        out = tools.run_tool("set_field", {
            "field_id": "f1", "value": "Cohen", "source": "user_said",
            "evidence": "user said 'my family name is Cohen'",
        }, _ctx(store, emitted))

    assert out == {"field_id": "f1", "ok": True, "awaiting_user_confirmation": True}
    assert store.values["f1"] == {"value": "Cohen", "source": "agent", "confirmed": False}
    assert emitted == [("field_updated", {"field_id": "f1", "value": "Cohen",
                                          "source": "agent", "confirmed": False})]
    assert store.events[0]["kind"] == "agent_fill"


def test_validate_flags_unconfirmed_until_confirmed():
    store, emitted = FakeStore(), []
    ctx = _ctx(store, emitted)
    with mock.patch.object(tools, "store", store):
        tools.run_tool("set_field", {
            "field_id": "f1", "value": "Cohen", "source": "user_said", "evidence": "said it",
        }, ctx)
        result = tools.run_tool("validate", {}, ctx)
    assert result["awaiting_confirmation"] == [{"field_id": "f1", "label": "Family name"}]

    store.values["f1"]["confirmed"] = True
    with mock.patch.object(tools, "store", store):
        result = tools.run_tool("validate", {}, ctx)
    assert result["awaiting_confirmation"] == []
    assert result["ok"] is True


def test_explain_field_keeps_the_human_note_separate_from_the_model_help():
    """`help` came from a model reading a page image; `official_note` came from
    a person who knows the form. Merging them into one string would leave the
    agent unable to tell which to trust when they disagree."""
    store, emitted = FakeStore(), []
    fields = [_field(help="model guessed this from the image")]
    guide = gd.parse("## Field notes\n\n### f1\n\nNine digits, including the check digit.\n")
    ctx = tools.ToolContext(sid="s1", fields=fields, actor="t", guide=guide)

    out = tools.run_tool("explain_field", {"field_id": "f1"}, ctx)
    assert out["guidance"] == "model guessed this from the image"
    assert out["official_note"] == "Nine digits, including the check digit."
    assert "Prefer it" in out["note_source"]

    # No guide -> no note, and nothing invented in its place.
    plain = tools.ToolContext(sid="s1", fields=fields, actor="t")
    assert "official_note" not in tools.run_tool("explain_field", {"field_id": "f1"}, plain)


def test_read_guide_returns_a_section_and_refuses_to_paper_over_a_blank_one():
    guide = gd.parse("## Where to submit\n\nAt the tax office.\n")
    ctx = tools.ToolContext(sid="s1", fields=[_field()], actor="t", guide=guide)

    assert tools.run_tool("read_guide", {"section": "Where to submit"}, ctx)["content"] \
        == "At the tax office."

    blank = tools.run_tool("read_guide", {"section": "Eligibility"}, ctx)
    assert blank["empty"] is True
    assert "Do not fill the gap yourself" in blank["note"]

    assert "error" in tools.run_tool("read_guide", {"section": "Nonsense"}, ctx)


def test_read_guide_is_not_offered_when_the_form_has_no_guide():
    names = lambda cfg: {t["toolSpec"]["name"] for t in cfg["tools"]}
    assert "read_guide" not in names(tools.config_for(None))
    assert "read_guide" in names(tools.config_for(gd.parse("## Overview\n\nx\n")))


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} tool tests passed")


if __name__ == "__main__":
    run_all()
