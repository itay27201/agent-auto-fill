"""Streaming-loop tests with no AWS.

These exist because of a specific outage: the loop used to return
`content or [{"text": ""}]` when a stream produced nothing, `run_turn`
persisted that to DynamoDB, and every later turn of the session replayed it
into Converse and got

    ValidationException: messages: text content blocks must be non-empty

The transcript is durable, so the session never recovered — it had to be
abandoned. The rule the tests below pin down is therefore two-sided: never
write a block Bedrock would reject, and never send one either, because
transcripts written by the old code are still out there.
"""
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import agent_loop as loop


# ------------------------------------------------------------------- fakes

def text_events(*chunks, stop="end_turn"):
    """The event sequence Converse emits for one text block."""
    evs = [{"contentBlockStart": {"start": {}}}]
    evs += [{"contentBlockDelta": {"delta": {"text": c}}} for c in chunks]
    evs += [{"contentBlockStop": {}}, {"messageStop": {"stopReason": stop}}]
    return evs


def tool_events(tool_use_id, name, input_json, stop="tool_use"):
    return [
        {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tool_use_id, "name": name}}}},
        {"contentBlockDelta": {"delta": {"toolUse": {"input": input_json}}}},
        {"contentBlockStop": {}},
        {"messageStop": {"stopReason": stop}},
    ]


class FakeBedrock:
    """Returns one canned stream per call and records the kwargs it was
    given, which is where the regression actually shows up."""

    def __init__(self, *streams):
        self.streams = list(streams)
        self.calls: list[dict] = []

    def converse_stream(self, **kwargs):
        self.calls.append(kwargs)
        evs = self.streams.pop(0) if self.streams else []
        return {"stream": evs}


class Recorder:
    """Collects the frames sent to the client and the messages persisted."""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.persisted: list[tuple[str, list]] = []

    def send(self, kind, data):
        self.sent.append((kind, data))

    def persist(self, role, content):
        self.persisted.append((role, content))

    def kinds(self):
        return [k for k, _ in self.sent]


def _run(bedrock, messages, dispatch=None, rec=None):
    rec = rec or Recorder()
    with mock.patch.object(loop, "bedrock", lambda: bedrock):
        added = loop.run_turn(
            messages,
            system=[{"text": "sys"}],
            tool_config={"tools": []},
            dispatch=dispatch or (lambda _n, _a: {"ok": True}),
            send=rec.send,
            persist=rec.persist,
        )
    return added, rec


def _all_blocks(messages):
    return [b for m in messages for b in m.get("content") or []]


# ------------------------------------------------------------------- tests

def test_a_poisoned_transcript_still_works():
    """The regression test for the outage. A stored `{"text": ""}` must never
    reach Converse, or the session stays dead forever."""
    history = [
        {"role": "user", "content": [{"text": "שנת המס 2026"}]},
        {"role": "assistant", "content": [{"text": ""}]},   # <- written by the old loop
        {"role": "user", "content": [{"text": "23432"}]},
    ]
    fake = FakeBedrock(text_events("בסדר"))
    _run(fake, history)

    sent = fake.calls[0]["messages"]
    assert all((b.get("text") or "").strip() for b in _all_blocks(sent)), sent
    # Dropping the assistant turn would have left two adjacent user turns,
    # which Converse rejects just as hard.
    assert [m["role"] for m in sent] == ["user"], sent
    assert sent[0]["content"] == [{"text": "שנת המס 2026"}, {"text": "23432"}]


def test_an_empty_stream_is_retried_once_and_never_written_down():
    fake = FakeBedrock([], [])  # two empty streams: the call and its retry
    added, rec = _run(fake, [{"role": "user", "content": [{"text": "hi"}]}])

    assert len(fake.calls) == 2, "an empty turn should be retried exactly once"
    assert rec.persisted == [], "nothing empty may reach the transcript"
    assert added == []
    assert "warning" in rec.kinds()


def test_an_empty_stream_that_recovers_on_the_retry_is_kept():
    fake = FakeBedrock([], text_events("שלום"))
    added, rec = _run(fake, [{"role": "user", "content": [{"text": "hi"}]}])

    assert len(fake.calls) == 2
    assert added == [{"role": "assistant", "content": [{"text": "שלום"}]}]
    assert rec.persisted == [("assistant", [{"text": "שלום"}])]
    assert "warning" not in rec.kinds()


def test_whitespace_only_text_counts_as_empty():
    fake = FakeBedrock(text_events("  ", "\n"), text_events("  "))
    _, rec = _run(fake, [{"role": "user", "content": [{"text": "hi"}]}])

    assert rec.persisted == []
    assert "warning" in rec.kinds()


def test_a_truncated_reply_says_so():
    fake = FakeBedrock(text_events("a long answer", stop="max_tokens"))
    _, rec = _run(fake, [{"role": "user", "content": [{"text": "hi"}]}])

    warnings = [d["message"] for k, d in rec.sent if k == "warning"]
    assert any("cut off" in w for w in warnings), rec.sent
    assert rec.persisted == [("assistant", [{"text": "a long answer"}])]


def test_an_orphaned_tool_result_is_dropped():
    """`recent_messages` slices by count, so the window can open in the middle
    of a tool cycle, leaving a result whose call is gone."""
    history = [
        {"role": "user", "content": [{"toolResult": {"toolUseId": "gone",
                                                     "content": [{"json": {"ok": True}}],
                                                     "status": "success"}}]},
        {"role": "assistant", "content": [{"text": "done"}]},
        {"role": "user", "content": [{"text": "next"}]},
    ]
    fake = FakeBedrock(text_events("ok"))
    _run(fake, history)

    sent = fake.calls[0]["messages"]
    assert not any("toolResult" in b for b in _all_blocks(sent)), sent
    assert sent[0]["role"] == "user", "Converse requires the first turn be the user's"
    assert sent[0]["content"] == [{"text": "next"}]


def test_a_tool_call_nothing_answered_is_dropped_from_the_tail():
    history = [
        {"role": "user", "content": [{"text": "fill it"}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "set_field",
                                                       "input": {}}}]},
    ]
    fake = FakeBedrock(text_events("ok"))
    _run(fake, history)

    sent = fake.calls[0]["messages"]
    assert [m["role"] for m in sent] == ["user"], sent
    assert not any("toolUse" in b for b in _all_blocks(sent))


def test_a_paired_tool_cycle_survives_untouched():
    """The guard against a sanitizer that eats valid traffic."""
    history = [
        {"role": "user", "content": [{"text": "fill it"}]},
        {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "set_field",
                                                       "input": {"field_id": "f1"}}}]},
        {"role": "user", "content": [{"toolResult": {"toolUseId": "t1",
                                                     "content": [{"json": {"ok": True}}],
                                                     "status": "success"}}]},
        {"role": "assistant", "content": [{"text": "done"}]},
        {"role": "user", "content": [{"text": "next"}]},
    ]
    fake = FakeBedrock(text_events("ok"))
    _run(fake, list(history))

    assert fake.calls[0]["messages"] == history


def test_a_normal_tool_turn_still_runs_the_tool_and_feeds_the_result_back():
    fake = FakeBedrock(
        tool_events("t1", "highlight_field", '{"field_id": "employer_name"}'),
        text_events("מה מספר תיק הניכויים?"),
    )
    seen = []

    def dispatch(name, args):
        seen.append((name, args))
        return {"ok": True}

    added, rec = _run(fake, [{"role": "user", "content": [{"text": "פרטי המעסיק"}]}],
                      dispatch=dispatch)

    assert seen == [("highlight_field", {"field_id": "employer_name"})]
    assert added[0]["content"][0]["toolUse"]["input"] == {"field_id": "employer_name"}
    assert added[1]["content"][0]["toolResult"]["toolUseId"] == "t1"
    assert added[2]["content"] == [{"text": "מה מספר תיק הניכויים?"}]
    # The second call carries the whole cycle: nothing was pruned as orphaned.
    assert len(fake.calls[1]["messages"]) == 3


def test_sanitize_leaves_a_clean_history_alone():
    clean = [
        {"role": "user", "content": [{"text": "a"}]},
        {"role": "assistant", "content": [{"text": "b"}]},
        {"role": "user", "content": [{"text": "c"}]},
    ]
    assert loop.sanitize(clean) == clean


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} agent-loop tests passed")


if __name__ == "__main__":
    run_all()
