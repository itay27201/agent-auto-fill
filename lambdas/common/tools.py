"""Agent tools: Bedrock Converse `toolConfig` specs plus the dispatcher.

The load-bearing rule lives in `set_field`: every write carries a `source`,
and the only accepted sources are things that actually happened — the user
said it, it came from their saved profile, or it was read out of a document
they uploaded. There is no `inferred`. On a government form the agent
drafts and the human attests; a plausible-looking ID number invented by a
model is worse than an empty box.
"""
from __future__ import annotations

from typing import Any

from . import guide as gd
from . import schema as sch
from . import store

VALID_SOURCES = ("user_said", "profile", "source_doc")

_LABEL_PROP = {
    "type": "string",
    "description": (
        "The label printed on the box you are writing into, copied exactly from "
        "the field list. This is checked against the schema and the write is "
        "refused if it does not match — forms repeat labels, and a value in the "
        "wrong box is worse than no value."
    ),
}

_SOURCE_PROP = {
    "type": "string",
    "enum": list(VALID_SOURCES),
    "description": (
        "Where this value came from. user_said: the user stated it in this "
        "conversation. profile: it came from their saved profile. source_doc: "
        "you read it from a document they uploaded. If none of these apply you "
        "do not have the value — ask the user instead of guessing."
    ),
}

_VALUE_PROP = {
    "type": ["string", "number", "boolean", "array"],
    "description": (
        "Match the field's type. A checkbox takes true or false — the booleans, "
        "never the words \"כן\", \"yes\" or \"X\"; the form's own tick mark is "
        "drawn for you. A select takes one of its options copied exactly; a "
        "multiselect takes an array of them. Everything else takes a string or "
        "number."
    ),
}

TOOL_SPECS = [
    {
        "toolSpec": {
            "name": "get_schema",
            "description": (
                "List form fields with their current values. Call with a section "
                "name or field_ids to look at one part of the form; call with no "
                "arguments only when you need the whole form."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "section": {"type": "string"},
                    "field_ids": {"type": "array", "items": {"type": "string"}},
                },
            }},
        }
    },
    {
        "toolSpec": {
            "name": "list_unfilled",
            "description": "Fields that are still empty. Use this to answer 'what's left?'.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {"required_only": {"type": "boolean", "default": True}},
            }},
        }
    },
    {
        "toolSpec": {
            "name": "set_field",
            "description": (
                "Write one value. The value is a draft until the user confirms it "
                "in the UI. Never call this for a value you inferred or assumed."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "field_id": {"type": "string"},
                    "value": _VALUE_PROP,
                    "field_label": _LABEL_PROP,
                    "source": _SOURCE_PROP,
                    "evidence": {
                        "type": "string",
                        "description": "Quote or paraphrase the exact user statement or document text this came from.",
                    },
                },
                "required": ["field_id", "value", "field_label", "source", "evidence"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "set_fields",
            "description": "Write several values at once. Prefer this over repeated set_field calls.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "updates": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field_id": {"type": "string"},
                                "value": _VALUE_PROP,
                                "field_label": _LABEL_PROP,
                                "source": _SOURCE_PROP,
                                "evidence": {"type": "string"},
                            },
                            "required": ["field_id", "value", "field_label", "source", "evidence"],
                        },
                    }
                },
                "required": ["updates"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "clear_field",
            "description": "Empty a field, e.g. when the user corrects an earlier answer.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {"field_id": {"type": "string"}},
                "required": ["field_id"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "validate",
            "description": "Check the whole form: format errors, missing required fields, unconfirmed drafts.",
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "explain_field",
            "description": "Get the official guidance for a field before answering a question about what it wants.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {"field_id": {"type": "string"}},
                "required": ["field_id"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "read_guide",
            "description": (
                "Read a section of the official guidance a person wrote for this "
                "form. Sections: " + ", ".join(gd.SECTIONS) + ". The overview, key "
                "rules and required attachments are already in front of you — use "
                "this for the others, especially before telling someone whether "
                "they qualify, what happens if they get it wrong, or where the "
                "form goes. This guidance outranks your own knowledge of the form."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {"section": {"type": "string", "enum": list(gd.SECTIONS)}},
                "required": ["section"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "highlight_field",
            "description": (
                "Scroll the user's document view to a field and outline it. Use this "
                "whenever you ask about a specific box so they can see which one."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {"field_id": {"type": "string"}},
                "required": ["field_id"],
            }},
        }
    },
]

TOOL_CONFIG = {"tools": TOOL_SPECS}


def config_for(guide: dict | None) -> dict:
    """Most forms have no guide. Offering `read_guide` anyway teaches the model
    to call a tool that can only answer "there is no guide" — so it is dropped
    from the spec entirely rather than left to fail at runtime."""
    if guide:
        return TOOL_CONFIG
    return {"tools": [t for t in TOOL_SPECS if t["toolSpec"]["name"] != "read_guide"]}


class ToolContext:
    """Per-turn state handed to the dispatcher."""

    def __init__(self, sid: str, fields: list[sch.FormField], actor: str, emit=None,
                 scope: list[str] | None = None, guide: dict | None = None):
        self.sid = sid
        self.fields = fields
        self.by_id = {f.field_id: f for f in fields}
        self.actor = actor
        self.emit = emit or (lambda *_a, **_k: None)
        self.scope = scope  # field_ids the user selected in the viewer, if any
        self.guide = guide  # official guidance, if this form is in the catalog


def run_tool(name: str, args: dict, ctx: ToolContext) -> dict:
    fn = _DISPATCH.get(name)
    if not fn:
        return {"error": f"unknown tool: {name}"}
    try:
        return fn(args, ctx)
    except Exception as e:  # a tool failure is a result, not a crash
        return {"error": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------------ handlers

def _t_get_schema(args: dict, ctx: ToolContext) -> dict:
    values = store.get_values(ctx.sid)
    only = args.get("field_ids") or ctx.scope
    fields = ctx.fields
    if args.get("section"):
        want = args["section"].strip().lower()
        fields = [f for f in fields if f.section.strip().lower() == want]
    return {"fields": sch.agent_view(fields, values, only=only)}


def _t_list_unfilled(args: dict, ctx: ToolContext) -> dict:
    values = store.get_values(ctx.sid)
    required_only = args.get("required_only", True)
    out = [
        {"field_id": f.field_id, "label": f.label, "section": f.section, "required": f.required}
        for f in ctx.fields
        if (values.get(f.field_id) or {}).get("value") in (None, "", [])
        and (f.required or not required_only)
    ]
    return {"unfilled": out, "count": len(out)}


def _write_one(u: dict, ctx: ToolContext) -> dict:
    fid = u.get("field_id")
    f = ctx.by_id.get(fid)
    if not f:
        return {"field_id": fid, "ok": False, "error": "no such field_id"}
    if u.get("source") not in VALID_SOURCES:
        return {"field_id": fid, "ok": False,
                "error": f"source must be one of {VALID_SOURCES}; if it is none of these, ask the user"}
    if not (u.get("evidence") or "").strip():
        return {"field_id": fid, "ok": False, "error": "evidence is required"}

    mismatch = _wrong_field(u.get("field_label"), f, ctx)
    if mismatch:
        return {"field_id": fid, "ok": False, **mismatch}

    err = sch.validate_value(f, u.get("value"))
    if err:
        return {"field_id": fid, "ok": False, "error": err}

    # One stored shape per type, so the renderer is never handed a date whose
    # notation decides which end the year lands on. The value echoed back is the
    # normalized one — the model should see what was actually written down.
    value = sch.normalize_value(f, u["value"])
    stored = store.set_value(ctx.sid, fid, value, source="agent", actor=ctx.actor,
                             confirmed=False)
    store.append_event(ctx.sid, "agent_fill", actor=ctx.actor, field_id=fid,
                       origin=u["source"], evidence=u["evidence"][:400])
    # The version travels with the write. Without it the browser has to guess what
    # the store is at, and a later confirm sends no `expected_version` at all —
    # dropping the conditional write for exactly the case it exists to protect.
    ctx.emit("field_updated", {"field_id": fid, "value": value,
                               "source": "agent", "confirmed": False,
                               "version": stored.get("version")})
    return {"field_id": fid, "ok": True, "value": value,
            "awaiting_user_confirmation": True}


def _normalize_label(text: str) -> str:
    """Labels come back through a model, so they arrive with the punctuation and
    direction marks the form prints around them. Compare the words, not the
    typography."""
    stripped = "".join(ch for ch in (text or "") if ch not in "‎‏‪‫‬")
    return " ".join(stripped.split()).strip(" :*.־-").lower()


def _wrong_field(claimed: str | None, f: sch.FormField, ctx: ToolContext) -> dict | None:
    """Refuse a write whose stated label is not this field's label.

    The `source`/`evidence` rule above establishes that a value is real. It says
    nothing about whether it is going into the right box, and on a form that
    labels three different fields "שם" that is the failure that actually
    happens: a correct value, correctly sourced, written into the wrong cell and
    then stamped onto a tax form.

    Making the model state the label turns a silent misfile into a rejected
    call. The rejection carries the fields that *do* carry the claimed label, so
    it can be corrected inside the same turn rather than becoming a dead end.
    """
    want = _normalize_label(claimed)
    if not want:
        return {"error": "field_label is required — state the label printed on the box you are writing into"}
    if want == _normalize_label(f.label):
        return None

    alternatives = [
        {"field_id": o.field_id, "label": o.label, "section": o.section}
        for o in ctx.fields if _normalize_label(o.label) == want
    ]
    out = {
        "error": (f"field_label does not match: {f.field_id!r} is labelled "
                  f"{f.label!r}, not {claimed!r}. Nothing was written."),
        "actual_label": f.label,
        "actual_section": f.section,
    }
    if alternatives:
        out["fields_with_that_label"] = alternatives[:5]
        out["hint"] = "One of these is probably the field you meant."
    else:
        out["hint"] = ("No field on this form carries that label. Call get_schema "
                       "or explain_field to find the right one.")
    return out


def _t_set_field(args: dict, ctx: ToolContext) -> dict:
    return _write_one(args, ctx)


def _t_set_fields(args: dict, ctx: ToolContext) -> dict:
    results = [_write_one(u, ctx) for u in args.get("updates", [])]
    return {"results": results,
            "written": sum(1 for r in results if r.get("ok")),
            "rejected": sum(1 for r in results if not r.get("ok"))}


def _t_clear_field(args: dict, ctx: ToolContext) -> dict:
    fid = args["field_id"]
    if fid not in ctx.by_id:
        return {"ok": False, "error": "no such field_id"}
    stored = store.set_value(ctx.sid, fid, None, source=None, actor=ctx.actor,
                             confirmed=True)
    ctx.emit("field_updated", {"field_id": fid, "value": None, "source": None,
                              "version": stored.get("version")})
    return {"ok": True}


def _t_validate(_args: dict, ctx: ToolContext) -> dict:
    return sch.validate_all(ctx.fields, store.get_values(ctx.sid))


def _t_explain_field(args: dict, ctx: ToolContext) -> dict:
    f = ctx.by_id.get(args["field_id"])
    if not f:
        return {"error": "no such field_id"}

    # Two sources, kept apart on purpose. `help` came from a model reading the
    # page image during ingest; the guide note was written by a person who
    # knows the form. When they disagree the agent should trust the person, so
    # it has to be able to see which is which.
    note = ((ctx.guide or {}).get("field_notes") or {}).get(f.field_id)
    out = {
        "field_id": f.field_id,
        "label": f.label,
        "type": f.type,
        "section": f.section,
        "required": f.required,
        "options": f.options,
        "guidance": f.help or "No guidance was captured from the document itself.",
        # `validation` is empty on most fields the ingest model produces, so
        # without the date fallback a date field advertises no shape at all and
        # the model picks a notation at random. `max_length` was invisible
        # everywhere — not here, not in `agent_view` — so a write rejected for
        # length gave the model nothing it could query to find out why.
        "expected_format": f.validation or ("DD/MM/YYYY" if f.type == "date" else None),
        "max_length": f.max_length or None,
    }

    # Which box, physically. This is the on-demand answer to "is this the same
    # שם as the one in section א?", and it belongs here rather than in
    # `agent_view` — every field's neighbours in every schema listing would be a
    # lot of tokens to spend on a question that is only asked about a few.
    if f.nearby_text:
        out["printed_around_this_box"] = f.nearby_text
    twins = [o.field_id for o in ctx.fields
             if o.field_id != f.field_id and _normalize_label(o.label) == _normalize_label(f.label)]
    if twins:
        out["other_fields_with_the_same_label"] = twins
        out["disambiguation"] = (
            "This form uses this label more than once. These are different boxes; "
            "check `printed_around_this_box` and `section` before writing."
        )
    if f.bbox_confidence == "low":
        out["not_placed"] = (
            "Nobody has established where this box is on the page, so a value "
            "written here will not appear in the exported document until someone "
            "places it. Tell the person that."
        )

    if note:
        out["official_note"] = note
        out["note_source"] = "Written by a person for this form. Prefer it over `guidance`."
    return out


def _t_read_guide(args: dict, ctx: ToolContext) -> dict:
    if not ctx.guide:
        return {"error": "this form has no official guide"}
    name = args.get("section") or ""
    sections = ctx.guide.get("sections") or {}
    if name not in sections:
        return {"error": f"no such section; available: {', '.join(gd.SECTIONS)}"}
    body = (sections.get(name) or "").strip()
    if not body:
        return {"section": name, "empty": True,
                "note": "This section was left blank. Do not fill the gap yourself — "
                        "tell the person the guide does not cover it."}
    return {"section": name, "content": body}


def _t_highlight_field(args: dict, ctx: ToolContext) -> dict:
    f = ctx.by_id.get(args["field_id"])
    if not f:
        return {"ok": False, "error": "no such field_id"}
    ctx.emit("highlight", {"field_id": f.field_id, "page": f.page, "bbox": f.bbox})
    return {"ok": True}


_DISPATCH = {
    "get_schema": _t_get_schema,
    "list_unfilled": _t_list_unfilled,
    "set_field": _t_set_field,
    "set_fields": _t_set_fields,
    "clear_field": _t_clear_field,
    "validate": _t_validate,
    "explain_field": _t_explain_field,
    "read_guide": _t_read_guide,
    "highlight_field": _t_highlight_field,
}
