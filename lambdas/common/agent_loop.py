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


def stream_once(messages: list[dict], system: list[dict], tool_config: dict, send) -> tuple[dict, str]:
    """One Converse stream. Returns the assembled assistant message and the
    stop reason. Text deltas are pushed to the client as they arrive."""
    resp = bedrock().converse_stream(
        modelId=config.BEDROCK_MODEL_ID,
        system=system,
        messages=messages,
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
            if current:
                content.append(current)
            current, tool_json = None, ""

        elif "messageStop" in ev:
            stop = ev["messageStop"].get("stopReason", "end_turn")

        elif "metadata" in ev:
            usage = ev["metadata"].get("usage", {})
            log.info("usage: %s", json.dumps(usage))

    return {"role": "assistant", "content": content or [{"text": ""}]}, stop


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
        messages.append(reply)
        added.append(reply)
        persist("assistant", reply["content"])

        if stop != "tool_use":
            return added

        results = []
        for block in reply["content"]:
            if "toolUse" not in block:
                continue
            tu = block["toolUse"]
            send("tool_start", {"name": tu["name"]})
            out = dispatch(tu["name"], tu.get("input") or {})
            results.append({"toolResult": {
                "toolUseId": tu["toolUseId"],
                "content": [{"json": out}],
                "status": "error" if isinstance(out, dict) and out.get("error") else "success",
            }})

        tool_msg = {"role": "user", "content": results}
        messages.append(tool_msg)
        added.append(tool_msg)
        persist("user", results)

    send("warning", {"message": "stopped after the maximum number of tool steps"})
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
