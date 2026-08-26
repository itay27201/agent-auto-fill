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
        # Mirrors the real store's `if_not_exists(version, 0) + 1` and its
        # ALL_NEW return: callers propagate the version out of what comes back,
        # so a stand-in that omitted it would hide the thing being tested.
        prior = (self.values.get(field_id) or {}).get("version", 0)
        self.values[field_id] = {
            "value": value, "source": source,
            "confirmed": bool(confirmed) if confirmed is not None else (source != "agent"),
            "version": prior + 1,
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
            "field_id": "f1", "value": "Cohen", "field_label": "Family name",
            "source": "inferred", "evidence": "guessed it",
        }, _ctx(store, emitted))
    assert out["ok"] is False
    assert "source" in out["error"]
    assert "f1" not in store.values


def test_set_field_rejects_missing_evidence():
    store, emitted = FakeStore(), []
    with mock.patch.object(tools, "store", store):
        out = tools.run_tool("set_field", {
            "field_id": "f1", "value": "Cohen", "field_label": "Family name",
            "source": "user_said", "evidence": "  ",
        }, _ctx(store, emitted))
    assert out["ok"] is False
    assert "evidence" in out["error"]


def test_set_field_writes_as_unconfirmed_agent_draft():
    store, emitted = FakeStore(), []
    with mock.patch.object(tools, "store", store):
        out = tools.run_tool("set_field", {
            "field_id": "f1", "value": "Cohen", "field_label": "Family name",
            "source": "user_said", "evidence": "user said 'my family name is Cohen'",
        }, _ctx(store, emitted))

    # `value` is echoed because the stored value is not always the one sent: a
    # date is normalized on the way in, and a model told only "ok" would carry on
    # believing its own notation was what landed.
    assert out == {"field_id": "f1", "ok": True, "value": "Cohen",
                   "awaiting_user_confirmation": True}
    assert store.values["f1"] == {"value": "Cohen", "source": "agent",
                                  "confirmed": False, "version": 1}
    assert emitted == [("field_updated", {"field_id": "f1", "value": "Cohen",
                                          "source": "agent", "confirmed": False,
                                          "version": 1})]
    assert store.events[0]["kind"] == "agent_fill"


def test_field_updated_carries_the_version_the_store_landed_on():
    """The browser confirms a draft with `expected_version`. If the write it is
    confirming never told it the version, it sends none — and the conditional
    write that exists to catch a lost race silently stops being conditional."""
    store, emitted = FakeStore(), []
    ctx = _ctx(store, emitted)
    with mock.patch.object(tools, "store", store):
        for value in ("Cohen", "Levi"):
            tools.run_tool("set_field", {
                "field_id": "f1", "value": value, "field_label": "Family name",
                "source": "user_said", "evidence": f"user said '{value}'",
            }, ctx)

    # Second write onto the same field: the version has moved on, and the event
    # says so rather than leaving the client to assume it is still at 1.
    assert [data["version"] for _, data in emitted] == [1, 2]
    assert emitted[-1][1]["version"] == store.values["f1"]["version"]


def test_set_field_refuses_a_write_whose_label_belongs_to_another_box():
    """The screenshot failure: a correct, correctly-sourced value written into
    the wrong cell because three fields on form 101 are labelled "שם"."""
    store, emitted = FakeStore(), []
    fields = [
        _field(field_id="employer_name", label="שם", section="א. פרטי המעסיק"),
        _field(field_id="employee_first_name", label="שם", section="ב. פרטי העובד"),
        _field(field_id="employer_dednum", label="מספר תיק ניכויים", section="א. פרטי המעסיק"),
    ]
    ctx = tools.ToolContext(sid="s1", fields=fields, actor="t",
                            emit=lambda k, d: emitted.append((k, d)))

    with mock.patch.object(tools, "store", store):
        out = tools.run_tool("set_field", {
            "field_id": "employer_dednum", "value": "Bleader", "field_label": "שם",
            "source": "user_said", "evidence": "the company is Bleader",
        }, ctx)

    assert out["ok"] is False
    assert out["actual_label"] == "מספר תיק ניכויים"
    assert store.values == {}, "nothing may be written on a mismatch"
    # The rejection has to be recoverable inside the same turn, so it names the
    # fields that really do carry the label the model asked for.
    assert [f["field_id"] for f in out["fields_with_that_label"]] == [
        "employer_name", "employee_first_name"]

    # And the right box goes through, with the twin flagged rather than guessed.
    with mock.patch.object(tools, "store", store):
        ok = tools.run_tool("set_field", {
            "field_id": "employer_name", "value": "Bleader", "field_label": " שם :",
            "source": "user_said", "evidence": "the company is Bleader",
        }, ctx)
    assert ok["ok"] is True, "punctuation and spacing must not fail a correct write"
    assert store.values["employer_name"]["value"] == "Bleader"


def test_set_field_refuses_a_label_no_field_carries():
    store, emitted = FakeStore(), []
    with mock.patch.object(tools, "store", store):
        out = tools.run_tool("set_field", {
            "field_id": "f1", "value": "Cohen", "field_label": "Passport number",
            "source": "user_said", "evidence": "said it",
        }, _ctx(store, emitted))
    assert out["ok"] is False
    assert "No field on this form carries that label" in out["hint"]
    assert store.values == {}


def test_explain_field_says_which_box_and_names_its_twins():
    fields = [
        _field(field_id="employer_name", label="שם", section="א",
               nearby_text=[{"side": "right", "text": "כתובת"}]),
        _field(field_id="employee_first_name", label="שם", section="ב"),
    ]
    out = tools.run_tool("explain_field", {"field_id": "employer_name"},
                         tools.ToolContext(sid="s1", fields=fields, actor="t"))
    assert out["other_fields_with_the_same_label"] == ["employee_first_name"]
    assert out["printed_around_this_box"] == [{"side": "right", "text": "כתובת"}]
    assert "different boxes" in out["disambiguation"]


def test_explain_field_admits_when_a_box_has_no_known_place():
    """A value in an unplaced field never reaches the exported document, so the
    agent has to be able to say so rather than reporting it filled."""
    fields = [_field(bbox_confidence="low", bbox_note="box lies on top of the form's own text")]
    out = tools.run_tool("explain_field", {"field_id": "f1"},
                         tools.ToolContext(sid="s1", fields=fields, actor="t"))
    assert "will not appear in the exported document" in out["not_placed"]


def test_validate_flags_unconfirmed_until_confirmed():
    store, emitted = FakeStore(), []
    ctx = _ctx(store, emitted)
    with mock.patch.object(tools, "store", store):
        tools.run_tool("set_field", {
            "field_id": "f1", "value": "Cohen", "field_label": "Family name",
            "source": "user_said", "evidence": "said it",
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


def test_a_near_miss_type_is_mapped_rather_than_flattened_to_text():
    """The ingest model is given the enum but reaches for these instead often
    enough to matter, and silently rewriting them to "text" is how a tick square
    becomes a text field: validate_value then accepts "כן" where it would have
    demanded a boolean, and the renderer writes that word into the square."""
    for raw in ("radio", "boolean", "yes_no", "Radio-Group", "tick"):
        assert sch.FormField.from_dict({"field_id": "f", "label": "l",
                                        "type": raw}).type == "checkbox", raw

    assert sch.FormField.from_dict({"field_id": "f", "label": "l",
                                    "type": "dropdown"}).type == "select"
    assert sch.FormField.from_dict({"field_id": "f", "label": "l",
                                    "type": "multi_select"}).type == "multiselect"

    # Anything genuinely unrecognized still falls back, and the enum is untouched.
    assert sch.FormField.from_dict({"field_id": "f", "label": "l",
                                    "type": "sonnet"}).type == "text"
    assert sch.FormField.from_dict({"field_id": "f", "label": "l"}).type == "text"
    assert sch.FormField.from_dict({"field_id": "f", "label": "l",
                                    "type": "signature"}).type == "signature"


def test_a_checkbox_refuses_a_word_and_says_what_to_send_instead():
    """The agent reaching for "כן" is the mistake this catches, and the message has
    to be actionable enough to correct inside the same turn."""
    box = _field(type="checkbox", label="בן/בת זוג עובד/ת", required=False)

    assert sch.validate_value(box, True) is None
    assert sch.validate_value(box, False) is None

    err = sch.validate_value(box, "כן")
    assert err and "true" in err and "כן" in err


def _date_field(**over) -> sch.FormField:
    """`תאריך לידה` as the 101 actually produces it: eight printed cells, so
    ingest reads `max_length` off the cell count."""
    return _field(field_id="employee_birth_date", label="תאריך לידה",
                  type="date", max_length=8, required=False, **over)


def test_a_date_box_of_eight_cells_accepts_a_date():
    """This field used to accept nothing at all.

    `max_length` counts printed cells; `validate_value` compared it against the
    raw string, which carries separators the cells do not hold. Every notation in
    `_DATE_FORMATS` is ten characters, so on an eight-cell box the length check
    refused all of them while the alternative — eight bare digits — failed the
    date check instead. The agent bounced between the two messages until it ran
    out of turns, and the person could not type it by hand either, because
    `api_set_fields` runs this same function.
    """
    # The contradiction, stated rather than implied: nothing that parses fits.
    import datetime as dt
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%m/%d/%Y"):
        assert len(dt.date(1996, 2, 26).strftime(fmt)) > 8, fmt

    day = _date_field()
    for written in ("26.02.1996", "26/02/1996", "1996-02-26", "26021996"):
        assert sch.validate_value(day, written) is None, written

    # The cap still means something: nine digits is nine cells' worth.
    assert sch.validate_value(day, "260219966")
    # ...and it is still counted raw for anything that is not a date, where the
    # string itself is what gets drawn.
    assert sch.validate_value(_field(max_length=8), "123456789")
    assert sch.validate_value(_field(max_length=8), "12345678") is None


def test_a_date_is_stored_in_one_shape_whatever_was_typed():
    """The guard on the fix above.

    `_comb_chars` strips separators and cannot know which end the year is on, so
    `1996-02-26` was stamped `19960226` into a box whose cells mean DDMMYYYY —
    a different date, printed on a tax form, with nothing downstream able to tell.
    It only stayed hidden because the length check happened to reject ISO first.
    Normalizing on write is what lets that check be relaxed safely.
    """
    from functions.api_render import _comb_chars

    day = _date_field()
    for written in ("26.02.1996", "26/02/1996", "1996-02-26", "26021996"):
        stored = sch.normalize_value(day, written)
        assert stored == "26/02/1996", (written, stored)
        assert _comb_chars(stored, 8) == list("26021996"), written

    # Untouched when it does not parse, so the error still comes from validation,
    # and untouched for every other type.
    assert sch.normalize_value(day, "not a date") == "not a date"
    assert sch.normalize_value(_field(), "1996-02-26") == "1996-02-26"


def test_a_date_says_which_format_to_send():
    """Same contract as the checkbox message: name the shape, echo the value."""
    err = sch.validate_value(_date_field(), "26 בפברואר 1996")
    assert err and "DD/MM/YYYY" in err and "26 בפברואר 1996" in err

    # And the shape is discoverable before a write, not only after a rejection —
    # `validation` is empty on these fields, and `max_length` was in no tool at all.
    ctx = tools.ToolContext(sid="s1", fields=[_date_field()], actor="tester",
                            emit=lambda kind, data: None)
    out = tools.run_tool("explain_field", {"field_id": "employee_birth_date"}, ctx)
    assert out["expected_format"] == "DD/MM/YYYY"
    assert out["max_length"] == 8


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} tool tests passed")


if __name__ == "__main__":
    run_all()
