"""The Bedrock Converse streaming loop, shared by both agents.

There are two agents in this system and they have nothing in common except
this. The filling agent (`agent_chat`) writes form values for a person filling
a document. The authoring agent (`author_chat`) writes the markdown guide for
a person defining one. Different system prompts, different tools, different
storage, different WebSocket routes — neither can call the other's tools,
because the toolConfig is passed in per turn.

What they do share is plumbing: reassembling streamed content blocks into a
message, parsing tool-use input that arrives as JSON fragments, running the
tools, feeding results back, and stopping. That is fiddly, easy to get subtly
wrong, and has no opinion about what either agent is for — so it lives here
once instead of being copied and then diverging.
"""
from __future__ import annotations

import json
import logging
from typing import Callable

from . import config
from .aws import bedrock

log = logging.getLogger()
log.setLevel(logging.INFO)


def _usable(block: dict) -> bool:
    """Whether Bedrock will accept this content block. An empty or
    whitespace-only text block is rejected outright — and because the filling
    agent's transcript is durable, one written to DynamoDB poisons every
    later turn of that session, not just the turn that produced it."""
    if "text" in block:
        return bool((block.get("text") or "").strip())
    return True


def sanitize(messages: list[dict]) -> list[dict]:
    """Drop anything Converse would reject, without changing what was said.

    Applied on the way *in* rather than only at the point of writing, because
    a transcript that already contains a bad block has to keep working: the
    session is stored, the damage outlives the bug, and there is no migration
    path to a DynamoDB item a user cannot see.

    Removes empty text blocks, tool results whose call is no longer in the
    window (`recent_messages` slices by count, not by tool cycle), and a
    trailing tool call nothing answered. Runs of the same role are then
    merged, since dropping a message whole would otherwise leave two user
    turns adjacent, which Converse also rejects.
    """
    called: set[str] = set()
    kept: list[dict] = []

    for m in messages:
        content = []
        for b in m.get("content") or []:
            if "toolResult" in b:
                if b["toolResult"].get("toolUseId") not in called:
                    continue
            elif "toolUse" in b:
                called.add(b["toolUse"].get("toolUseId"))
            elif not _usable(b):
                continue
            content.append(b)
        if content:
            kept.append({**m, "content": content})

    merged: list[dict] = []
    for m in kept:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1] = {**merged[-1], "content": merged[-1]["content"] + m["content"]}
        else:
            merged.append(m)

    answered = {b["toolResult"].get("toolUseId")
                for m in merged for b in m["content"] if "toolResult" in b}
    while merged and any("toolUse" in b and b["toolUse"].get("toolUseId") not in answered
                         for b in merged[-1]["content"]):
        merged.pop()

    while merged and merged[0]["role"] != "user":
        merged.pop(0)

    if len(merged) != len(messages):
        log.info("sanitize: %d message(s) dropped from history", len(messages) - len(merged))
    return merged


def stream_once(messages: list[dict], system: list[dict], tool_config: dict, send) -> tuple[dict, str]:
    """One Converse stream. Returns the assembled assistant message and the
    stop reason. Text deltas are pushed to the client as they arrive.

    The returned message may have empty `content`: a stream can end without
    producing a usable block, and inventing one to fill the gap is what broke
    sessions before. The caller decides what to do about it."""
    resp = bedrock().converse_stream(
        modelId=config.BEDROCK_MODEL_ID,
        system=system,
        messages=sanitize(messages),
        toolConfig=tool_config,
        inferenceConfig={"maxTokens": config.MAX_TOKENS, "temperature": 0.2},
    )

    content, stop = [], "end_turn"
    current, tool_json = None, ""

    for ev in resp["stream"]:
        if "contentBlockStart" in ev:
            start = ev["contentBlockStart"]["start"]
            if "toolUse" in start:
                current = {"toolUse": {**start["toolUse"], "input": {}}}
                tool_json = ""

        elif "contentBlockDelta" in ev:
            delta = ev["contentBlockDelta"]["delta"]
            if "text" in delta:
                if current is None:
                    current = {"text": ""}
                current["text"] = current.get("text", "") + delta["text"]
                send("text", {"delta": delta["text"]})
            elif "toolUse" in delta:
                tool_json += delta["toolUse"].get("input", "")

        elif "contentBlockStop" in ev:
            if current and "toolUse" in current:
                try:
                    current["toolUse"]["input"] = json.loads(tool_json) if tool_json else {}
                except json.JSONDecodeError:
                    current["toolUse"]["input"] = {}
            if current and _usable(current):
                content.append(current)
            current, tool_json = None, ""

        elif "messageStop" in ev:
            stop = ev["messageStop"].get("stopReason", "end_turn")

        elif "metadata" in ev:
            usage = ev["metadata"].get("usage", {})
            log.info("usage: %s", json.dumps(usage))

    return {"role": "assistant", "content": content}, stop


def run_turn(
    messages: list[dict],
    system: list[dict],
    tool_config: dict,
    dispatch: Callable[[str, dict], dict],
    send,
    persist: Callable[[str, list], None] | None = None,
    max_turns: int | None = None,
) -> list[dict]:
    """Stream, run whatever tools the model asked for, repeat until it stops.

    `dispatch(name, args) -> dict` is the agent's tool implementation.
    `persist(role, content)` records each message; both agents keep a
    transcript, but in different places, so it is a callback rather than a
    hardcoded store call.

    Returns the messages appended this turn.
    """
    persist = persist or (lambda *_a: None)
    limit = max_turns or config.MAX_AGENT_TURNS
    added: list[dict] = []

    for _ in range(limit):
        reply, stop = stream_once(messages, system, tool_config, send)
        if not reply["content"]:
            # An empty turn is nearly always transient, so it is worth one
            # more try before bothering the person. What it must never do is
            # get written down: a placeholder block in a stored transcript
            # fails every subsequent turn of the session, not just this one.
            log.info("empty assistant turn (stop=%s); retrying once", stop)
            reply, stop = stream_once(messages, system, tool_config, send)
        if not reply["content"]:
            send("warning", {"message": "the assistant did not reply — please send that again"})
            return added

        messages.append(reply)
        added.append(reply)
        persist("assistant", reply["content"])

        if stop == "max_tokens":
            # Otherwise a truncated reply is indistinguishable from the agent
            # simply having finished talking.
            send("warning", {"message": "the reply was cut off at the length limit"})

        if stop != "tool_use":
            return added

        results = []
        for block in reply["content"]:
            if "toolUse" not in block:
                continue
            tu = block["toolUse"]
            send("tool_start", {"name": tu["name"]})
            out = dispatch(tu["name"], tu.get("input") or {})
            # `set_fields` reports each write's outcome under `results` with its
            # own `rejected` tally, and never a top-level `error` — so a batch in
            # which every single write was refused used to arrive here as a
            # success, and the activity row showed no failure at all.
            failed = isinstance(out, dict) and bool(
                out.get("error") or (out.get("rejected") and not out.get("written")))
            # Paired with tool_start so the client counts calls that actually
            # finished. A model mid-batch has already emitted every tool_start
            # for that turn, so counting those would run the progress display
            # ahead of the work.
            send("tool_end", {"name": tu["name"], "ok": not failed})
            results.append({"toolResult": {
                "toolUseId": tu["toolUseId"],
                "content": [{"json": out}],
                "status": "error" if failed else "success",
            }})

        tool_msg = {"role": "user", "content": results}
        messages.append(tool_msg)
        added.append(tool_msg)
        persist("user", results)

    # Everything the tools wrote is already persisted — they flush as they go —
    # so this is a pause, not a loss. Saying which makes the difference between
    # a person retrying from scratch and a person typing "continue".
    send("warning", {"message": f"Paused after {limit} tool steps. Everything written so "
                                f"far is saved — say \"continue\" to pick up where this "
                                f"left off."})
    return added


def sender(client, conn: str):
    """A `send(kind, data)` that posts one JSON frame to a WebSocket
    connection and treats a disconnected client as normal, not as an error —
    people close tabs mid-turn."""
    def send(kind: str, data):
        try:
            client.post_to_connection(
                ConnectionId=conn,
                Data=json.dumps({"type": kind, **(data if isinstance(data, dict) else {"data": data})},
                                ensure_ascii=False).encode("utf-8"),
            )
        except client.exceptions.GoneException:
            log.info("connection %s gone", conn)
        except Exception:
            log.exception("ws send failed")

    return send
