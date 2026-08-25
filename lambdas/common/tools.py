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

from . import schema as sch
from . import store

VALID_SOURCES = ("user_said", "profile", "source_doc")

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
                    "value": {
                        "description": "String, number, boolean for checkboxes, or array for multiselect."
                    },
                    "source": _SOURCE_PROP,
                    "evidence": {
                        "type": "string",
                        "description": "Quote or paraphrase the exact user statement or document text this came from.",
                    },
                },
                "required": ["field_id", "value", "source", "evidence"],
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
                                "value": {},
                                "source": _SOURCE_PROP,
                                "evidence": {"type": "string"},
                            },
                            "required": ["field_id", "value", "source", "evidence"],
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


class ToolContext:
    """Per-turn state handed to the dispatcher."""

    def __init__(self, sid: str, fields: list[sch.FormField], actor: str, emit=None, scope: list[str] | None = None):
        self.sid = sid
        self.fields = fields
        self.by_id = {f.field_id: f for f in fields}
        self.actor = actor
        self.emit = emit or (lambda *_a, **_k: None)
        self.scope = scope  # field_ids the user selected in the viewer, if any


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

    err = sch.validate_value(f, u.get("value"))
    if err:
        return {"field_id": fid, "ok": False, "error": err}

    store.set_value(ctx.sid, fid, u["value"], source="agent", actor=ctx.actor, confirmed=False)
    store.append_event(ctx.sid, "agent_fill", actor=ctx.actor, field_id=fid,
                       origin=u["source"], evidence=u["evidence"][:400])
    ctx.emit("field_updated", {"field_id": fid, "value": u["value"],
                               "source": "agent", "confirmed": False})
    return {"field_id": fid, "ok": True, "awaiting_user_confirmation": True}


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
    store.set_value(ctx.sid, fid, None, source=None, actor=ctx.actor, confirmed=True)
    ctx.emit("field_updated", {"field_id": fid, "value": None, "source": None})
    return {"ok": True}


def _t_validate(_args: dict, ctx: ToolContext) -> dict:
    return sch.validate_all(ctx.fields, store.get_values(ctx.sid))


def _t_explain_field(args: dict, ctx: ToolContext) -> dict:
    f = ctx.by_id.get(args["field_id"])
    if not f:
        return {"error": "no such field_id"}
    return {
        "field_id": f.field_id,
        "label": f.label,
        "type": f.type,
        "section": f.section,
        "required": f.required,
        "options": f.options,
        "guidance": f.help or "No official guidance was captured for this field.",
        "expected_format": f.validation or None,
    }


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
    "highlight_field": _t_highlight_field,
}
