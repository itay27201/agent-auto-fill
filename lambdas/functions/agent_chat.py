"""The filling agent: helps a person complete a document they are looking at.

Why WebSocket and not a streaming function URL: Lambda response streaming
is native to the Node.js managed runtime only. In Python it needs the Lambda
Web Adapter layer fronting an ASGI app. A WebSocket API is plain boto3, and
it also lets the agent push UI events mid-turn — `highlight_field` scrolls
the user's viewer to a box while the model is still talking, which a
request/response endpoint cannot do.

Routes: $connect, $disconnect, and `message`.

Not to be confused with `author_chat`, the other agent, which writes the
guide for a form being *defined*. They share only the streaming loop in
common/agent_loop.py; the tools and prompts are disjoint, so this one cannot
edit a guide and that one cannot write a form value.
"""
import json
import logging

from common import agent_loop as loop, catalog as cat, config, guide as gd, schema as sch, tools
from common.aws import apigw_ws
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
    send = loop.sender(apigw_ws(endpoint), conn)

    try:
        body = json.loads(event.get("body") or "{}")
        _turn(body, send)
    except Exception as e:
        log.exception("agent turn failed")
        send("error", {"message": f"{type(e).__name__}: {e}"})
    return {"statusCode": 200}


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

    # The page will not send a blank message, but the socket is reachable
    # without it. Refusing here keeps an empty text block out of the stored
    # transcript, where Bedrock would reject it on every turn that followed.
    if not user_text:
        send("error", {"message": "message is empty"})
        return

    fields = sch.schema_from_list(load_schema(sess["schema_key"]))
    values = get_values(sid)
    actor = body.get("actor") or sess.get("owner", "anonymous")
    # Present when the form came from the catalog, or when an upload
    # hash-matched a published one. Most sessions have no guide.
    guide = cat.load_guide(sess.get("guide_key"))

    ctx = tools.ToolContext(sid=sid, fields=fields, actor=actor, emit=send,
                            scope=scope, guide=guide)

    system = [
        {"text": SYSTEM},
        {"text": _form_context(fields, values, scope)},
    ]
    block = gd.prompt_block(guide)
    if block:
        # Inside the cache point: the guide is byte-identical on every turn of
        # a session, so after the first turn it costs almost nothing.
        system.append({"text": block})
    system += [
        # The schema block is identical across every turn in a session and
        # is the bulk of the prompt. Caching it makes the marginal turn
        # cost almost nothing.
        {"cachePoint": {"type": "default"}},
        {"text": _recent_changes(sid)},
    ]

    # A few more than we need: the window is sliced by message count, so it
    # can open in the middle of a tool cycle. `sanitize` in the loop prunes
    # the orphaned blocks at the edge, and the spare messages mean that trim
    # costs context we were not going to keep anyway.
    messages = recent_messages(sid, limit=24)
    messages.append({"role": "user", "content": [{"text": user_text}]})
    append_message(sid, "user", [{"text": user_text}])

    send("turn_start", {"session_id": sid})

    loop.run_turn(
        messages,
        system,
        tools.config_for(guide),
        dispatch=lambda name, args: tools.run_tool(name, args, ctx),
        send=send,
        persist=lambda role, content: append_message(sid, role, content),
    )

    send("turn_end", {"summary": sch.validate_all(fields, get_values(sid))})


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
