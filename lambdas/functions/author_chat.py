"""The authoring agent: writes the guide for a form being added to the catalog.

The second of the system's two agents, and entirely separate from the first.
`agent_chat` helps a person fill a document and can write form values;
this one helps a person describe a document and can write markdown. Different
prompt, different tools, different storage. The only shared code is the
streaming loop in common/agent_loop.py.

WebSocket route: `author` (the same API as the filling agent — its
RouteSelectionExpression is `$request.body.action`, so routing is by the
`action` field the browser sends).

The transcript is kept in memory for the duration of a turn only. Defining a
document is a sitting, not a resumable session like filling one, and the guide
itself — flushed to S3 after every write — is the durable artifact.
"""
import json
import logging

from common import (agent_loop as loop, author_tools as at, catalog as cat, config,
                    guide as gd, guide_checks as gchk)
from common.aws import apigw_ws
from common.store import load_schema

log = logging.getLogger()
log.setLevel(logging.INFO)

SYSTEM = """You help a person write the official guide for a government form, so that later, someone filling that form in gets real answers instead of guesses.

What the guide is for. The form itself says what the boxes are. It almost never says who may file it, what has to be attached, when it is due, or why an office sends it back. That is what you are writing down.

Where your sentences may come from:
- a reference document the person uploaded — read it with read_source
- something the person told you in this conversation
- what is printed on the form itself

Nothing else. You may not use what you happen to know about this form, this agency, or forms like it. If a section is not covered by any of those three, leave it empty and say so. An empty section is honest; an invented eligibility rule, threshold or deadline is a person being turned away at a counter. Every write asks you for a basis and a citation — if you cannot fill those truthfully, do not make the call.

How to work:
- Start by asking what the form is and who files it, then read whatever they uploaded before writing anything.
- Write section by section. Show the person what you wrote and ask before moving on.
- Field notes are the highest-value part: they are what the filling agent shows someone who asks "what goes in this box?". Write them with write_field_notes, in one call, after the sections are settled — it covers every field that has no note yet and tells you what it missed. Never loop write_field_note over the whole form; that is what leaves a guide two-thirds written. Use the singular tool only to fix one note the person objected to.
- write_field_notes returns a report. If it names missing fields, call it again — it will only fill the gap. Tell the person the real count, and never describe the notes as done while fields are still missing.
- Write in the language of the form. Keep it plain — the reader is a member of the public, not an official.
- Quantities, dates and legal references must be exact. If a source is ambiguous, say it is ambiguous rather than resolving it.
- You never publish. The person reviews the draft and publishes it themselves. Say so once, when the guide is taking shape."""


def lambda_handler(event, _context):
    ctx = event.get("requestContext", {})
    route = ctx.get("routeKey")
    if route in ("$connect", "$disconnect"):
        return {"statusCode": 200}

    conn = ctx["connectionId"]
    endpoint = config.WS_ENDPOINT or f"https://{ctx['domainName']}/{ctx['stage']}"
    send = loop.sender(apigw_ws(endpoint), conn)

    try:
        _turn(json.loads(event.get("body") or "{}"), send)
    except Exception as e:
        log.exception("authoring turn failed")
        send("error", {"message": f"{type(e).__name__}: {e}"})
    return {"statusCode": 200}


def _turn(body: dict, send) -> None:
    cid = body.get("catalog_id")
    if not cid:
        send("error", {"message": "catalog_id is required"})
        return

    try:
        entry = cat.get(cid)
    except cat.NotFound:
        send("error", {"message": "catalog entry not found"})
        return

    user_text = (body.get("message") or "").strip()
    if not user_text:
        send("error", {"message": "message is empty"})
        return

    fields = load_schema(entry["schema_key"])
    guide = cat.load_guide(entry.get("guide_key")) or gd.empty({"catalog_id": cid})
    actx = at.AuthorContext(cid=cid, entry=entry, fields=fields, guide=guide, emit=send)

    system = [
        {"text": SYSTEM},
        {"text": _entry_context(entry, fields, guide)},
        # The form's identity and field list do not change across a sitting.
        {"cachePoint": {"type": "default"}},
    ]

    # The browser holds the transcript: without a session record to key one
    # off, replaying it is what keeps a multi-turn sitting coherent.
    messages = _history(body.get("history") or [])
    messages.append({"role": "user", "content": [{"text": user_text}]})

    send("turn_start", {"catalog_id": cid})
    loop.run_turn(
        messages,
        system,
        at.TOOL_CONFIG,
        dispatch=lambda name, args: at.run_tool(name, args, actx),
        send=send,
    )
    # One report rather than the ad-hoc pair this used to send: the page and
    # the publish gate now read the same numbers the agent was given.
    report = gchk.check(actx.guide, fields, entry.get("language", ""))
    send("turn_end", {
        "catalog_id": cid,
        "markdown": gd.render(actx.guide),
        "guide": actx.guide,
        "report": report,
        "summary": gchk.summary(report),
        # Kept for the fields the page already reads off this frame.
        "empty_sections": report["empty_sections"],
        "field_notes": report["noted"],
        "field_count": report["total"],
    })


def _entry_context(entry: dict, fields: list[dict], guide: dict) -> str:
    sections = sorted({(f.get("section") or "").strip() for f in fields} - {""})
    written = [s for s in gd.SECTIONS if ((guide.get("sections") or {}).get(s) or "").strip()]
    notes = guide.get("field_notes") or {}
    lines = [
        f"You are writing the guide for: {entry.get('name') or entry['catalog_id']}",
        f"Agency: {entry.get('agency') or 'not stated yet'}",
        f"The form has {len(fields)} fields"
        + (f" across these sections: {', '.join(sections)}." if sections else "."),
        f"Sections already written: {', '.join(written) or 'none yet'}.",
        f"Fields with a note: {len(notes)} of {len(fields)}.",
    ]
    return "\n".join(lines)


def _history(raw: list) -> list[dict]:
    """Accept only plain text turns from the client. Tool-use blocks are
    reconstructed by the model each turn; echoing client-supplied ones back
    into the prompt would let the page fabricate tool results."""
    out = []
    for m in raw[-20:]:
        role = m.get("role")
        text = (m.get("text") or "").strip()
        if role in ("user", "assistant") and text:
            out.append({"role": role, "content": [{"text": text}]})
    # Bedrock rejects a conversation that does not alternate; collapse runs.
    merged: list[dict] = []
    for m in out:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"][0]["text"] += "\n\n" + m["content"][0]["text"]
        else:
            merged.append(m)
    return merged if not merged or merged[0]["role"] == "user" else merged[1:]
