"""The agent turn: Bedrock Converse + tools, streamed back over a WebSocket.

Why WebSocket and not a streaming function URL: Lambda response streaming
is native to the Node.js managed runtime only. In Python it needs the Lambda
Web Adapter layer fronting an ASGI app. A WebSocket API is plain boto3, and
it also lets the agent push UI events mid-turn — `highlight_field` scrolls
the user's viewer to a box while the model is still talking, which a
request/response endpoint cannot do.

Routes: $connect, $disconnect, and `message`.
"""
import json
import logging

from common import config, schema as sch, tools
from common.aws import apigw_ws, bedrock
from common.store import (
    append_message,
    get_session,
    get_values,
    load_schema,
    recent_events,
    recent_messages,
)

log = logging.getLogger()
log.setLevel(logging.INFO)

SYSTEM = """You help a person fill out an official form. You can see the form's fields and write values into them, but you never submit anything.

How you work:
- The person is the source of truth. Fill a field only from something they told you, something in their saved profile, or something you read in a document they uploaded. If you do not have a value, ask for it. Never guess, never infer from context, never fill a plausible placeholder. A blank box is always better than a wrong one on an official form.
- Everything you write is a draft. The person confirms each value in the interface before it can be exported. Say so when it matters, but do not repeat it every turn.
- When you ask about a specific field, call highlight_field first so they can see which box you mean.
- Work through the form in the order the person wants. If they selected a section, stay in it unless they ask to move on.
- Before answering a question about what a field wants, call explain_field. The official guidance is often more specific than the label suggests.
- Reply in the language the person writes in. Field labels stay in the form's own language.
- Be brief. This is a form, not a conversation — short questions, several at a time when they are related."""


def lambda_handler(event, _context):
    ctx = event.get("requestContext", {})
    route = ctx.get("routeKey")
    if route == "$connect":
        return {"statusCode": 200}
    if route == "$disconnect":
        return {"statusCode": 200}

    conn = ctx["connectionId"]
    endpoint = config.WS_ENDPOINT or f"https://{ctx['domainName']}/{ctx['stage']}"
    send = _sender(endpoint, conn)

    try:
        body = json.loads(event.get("body") or "{}")
        _turn(body, send)
    except Exception as e:
        log.exception("agent turn failed")
        send("error", {"message": f"{type(e).__name__}: {e}"})
    return {"statusCode": 200}


def _sender(endpoint: str, conn: str):
    client = apigw_ws(endpoint)

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


def _turn(body: dict, send) -> None:
    sid = body["session_id"]
    user_text = (body.get("message") or "").strip()
    # Fields the user selected in the viewer. Scoping the turn shrinks the
    # prompt from the whole form to one section and constrains where the
    # agent is allowed to write.
    scope = body.get("scope_field_ids") or None

    sess = get_session(sid)
    if not sess or sess.get("status") != "ready":
        send("error", {"message": "session is not ready"})
        return

    fields = sch.schema_from_list(load_schema(sess["schema_key"]))
    values = get_values(sid)
    actor = body.get("actor") or sess.get("owner", "anonymous")

    ctx = tools.ToolContext(sid=sid, fields=fields, actor=actor, emit=send, scope=scope)

    system = [
        {"text": SYSTEM},
        {"text": _form_context(fields, values, scope)},
        # The schema block is identical across every turn in a session and
        # is the bulk of the prompt. Caching it makes the marginal turn
        # cost almost nothing.
        {"cachePoint": {"type": "default"}},
        {"text": _recent_changes(sid)},
    ]

    messages = recent_messages(sid, limit=20)
    messages.append({"role": "user", "content": [{"text": user_text}]})
    append_message(sid, "user", [{"text": user_text}])

    send("turn_start", {"session_id": sid})

    for _ in range(config.MAX_AGENT_TURNS):
        reply, stop = _stream_once(messages, system, send)
        messages.append(reply)
        append_message(sid, "assistant", reply["content"])

        if stop != "tool_use":
            break

        results = []
        for block in reply["content"]:
            if "toolUse" not in block:
                continue
            tu = block["toolUse"]
            send("tool_start", {"name": tu["name"]})
            out = tools.run_tool(tu["name"], tu.get("input") or {}, ctx)
            results.append({"toolResult": {
                "toolUseId": tu["toolUseId"],
                "content": [{"json": out}],
                "status": "error" if isinstance(out, dict) and out.get("error") else "success",
            }})

        tool_msg = {"role": "user", "content": results}
        messages.append(tool_msg)
        append_message(sid, "user", results)
    else:
        send("warning", {"message": "stopped after the maximum number of tool steps"})

    send("turn_end", {"summary": sch.validate_all(fields, get_values(sid))})


def _stream_once(messages, system, send) -> tuple[dict, str]:
    """One Converse stream. Returns the assembled assistant message and the
    stop reason."""
    resp = bedrock().converse_stream(
        modelId=config.BEDROCK_MODEL_ID,
        system=system,
        messages=messages,
        toolConfig=tools.TOOL_CONFIG,
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


def _form_context(fields, values, scope) -> str:
    """The stable part of the prompt: what the form is and where things stand."""
    view = sch.agent_view(fields, values, only=scope)
    sections = sorted({f.section for f in fields if f.section})
    header = (
        f"The form has {len(fields)} fields"
        + (f" across these sections: {', '.join(sections)}." if sections else ".")
    )
    if scope:
        header += (
            f"\n\nThe person has selected {len(scope)} field(s) in the document viewer. "
            "Work on these unless they ask otherwise."
        )
    return f"{header}\n\nFields:\n{json.dumps(view, ensure_ascii=False)}"


def _recent_changes(sid: str) -> str:
    """Manual edits the user made between turns.

    Without this the agent re-asks for values the user just typed in
    themselves, which is the fastest way to make it feel broken.
    """
    events = [e for e in recent_events(sid, limit=25)
              if e.get("kind") in ("field_set", "user_confirmed") and e.get("actor") != "system"]
    if not events:
        return "No recent manual changes."
    lines = [f"- {e.get('kind')}: {e.get('field_id')}" for e in events[-12:]]
    return "Recent changes in the interface (may include edits the person made themselves):\n" + "\n".join(lines)
