"""Tools for the authoring agent — the one that writes a form's guide.

Disjoint from `tools.py` by design. That agent writes values into a person's
form; this one writes prose into a catalog entry. Neither can reach the
other's tools, because the toolConfig is chosen per turn, so there is no way
for a guide-writing turn to touch someone's national ID field.

The rule here mirrors `set_field`'s: everything written must trace to
something that actually exists — text in a source document the author
uploaded, or something the author said in this conversation. `write_section`
demands a `basis` for the same reason `set_field` demands `evidence`. An
invented filing deadline sitting in a government catalog is worse than a blank
section, because a blank section is visibly blank.

Nothing here publishes. Publishing is `PATCH /catalog/{cid}` — a button a
person presses after reading the draft.
"""
from __future__ import annotations

import io

from . import catalog as cat, config, guide as gd, note_batch as nb
from .aws import s3

# Defined in guide.py, because note_batch enforces the same rule on the bulk
# path and one tuple in two modules is how the two drift apart. Re-exported
# here: this is where callers and tests have always looked for it.
VALID_BASIS = gd.VALID_BASIS

_BASIS_PROP = {
    "type": "string",
    "enum": list(VALID_BASIS),
    "description": (
        "Where this text comes from. source_doc: you read it in an uploaded "
        "reference document. author_said: the person told you in this "
        "conversation. form_itself: it is printed on the form. If none apply, "
        "you do not know it — ask, or leave the section empty."
    ),
}

MAX_SOURCE_CHARS = 60_000

TOOL_SPECS = [
    {
        "toolSpec": {
            "name": "list_sources",
            "description": (
                "Reference documents the author uploaded for this form — "
                "instruction booklets, appendices, agency notices."
            ),
            "inputSchema": {"json": {"type": "object", "properties": {}}},
        }
    },
    {
        "toolSpec": {
            "name": "read_source",
            "description": (
                "Read a reference document. Long documents come back in pages; "
                "read the ones that look relevant rather than all of them."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "from_page": {"type": "integer", "default": 1},
                    "to_page": {"type": "integer"},
                },
                "required": ["source_id"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "get_field_list",
            "description": (
                "The form's fields with their ids and labels. Field notes must "
                "be keyed by these exact field_ids, so call this before writing "
                "any of them."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {"section": {"type": "string"}},
            }},
        }
    },
    {
        "toolSpec": {
            "name": "read_guide",
            "description": "Read what the guide currently says, to extend it rather than replace it.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {"section": {"type": "string", "enum": list(gd.SECTIONS)}},
            }},
        }
    },
    {
        "toolSpec": {
            "name": "write_section",
            "description": (
                "Replace one section of the guide. This overwrites the section, "
                "so include everything that should remain in it. Write in the "
                "language the form is in. Markdown; short paragraphs and lists."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "enum": list(gd.SECTIONS)},
                    "markdown": {"type": "string"},
                    "basis": _BASIS_PROP,
                    "citation": {
                        "type": "string",
                        "description": "Which source and roughly where — or what the author said.",
                    },
                },
                "required": ["section", "markdown", "basis", "citation"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "write_field_notes",
            "description": (
                "Write notes for many fields at once — this is how you do the "
                "field notes. With no arguments it covers every field that has "
                "no note yet, so calling it again after a partial run repairs "
                "the gap instead of rewriting what is already there. It reads "
                "the form's pages and the sections you have written, and "
                "returns a report naming any field it missed. Do not loop over "
                "write_field_note to do this; a hundred single calls is what "
                "runs you out of turns partway through."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "description": "Only fields in this section of the form. "
                                       "Rewrites their notes even if they have one.",
                    },
                    "field_ids": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Only these fields. Rewrites their notes.",
                    },
                },
            }},
        }
    },
    {
        "toolSpec": {
            "name": "write_field_note",
            "description": (
                "Attach guidance to ONE field — for fixing a single note the "
                "person objected to. Use write_field_notes for bulk work. Pass "
                "an empty string to delete a note."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "field_id": {"type": "string"},
                    "markdown": {"type": "string"},
                    "basis": _BASIS_PROP,
                    "citation": {"type": "string"},
                },
                "required": ["field_id", "markdown", "basis", "citation"],
            }},
        }
    },
]

TOOL_CONFIG = {"tools": TOOL_SPECS}


class AuthorContext:
    """Per-turn state. `guide` is mutated in place by the write tools and
    flushed to S3 after each one — the authoring page renders it live, and a
    dropped connection should not lose an hour of work."""

    def __init__(self, cid: str, entry: dict, fields: list[dict], guide: dict, emit=None):
        self.cid = cid
        self.entry = entry
        self.fields = fields
        self.by_id = {f.get("field_id"): f for f in fields}
        self.guide = guide
        self.emit = emit or (lambda *_a, **_k: None)


def run_tool(name: str, args: dict, ctx: AuthorContext) -> dict:
    fn = _DISPATCH.get(name)
    if not fn:
        return {"error": f"unknown tool: {name}"}
    try:
        return fn(args, ctx)
    except Exception as e:  # a tool failure is a result, not a crash
        return {"error": f"{type(e).__name__}: {e}"}


# ------------------------------------------------------------------ handlers

def _t_list_sources(_args: dict, ctx: AuthorContext) -> dict:
    sources = cat.list_sources(ctx.cid)
    if not sources:
        return {"sources": [], "note": "The author has not uploaded any reference "
                                       "documents. Ask them for the instruction booklet, "
                                       "or work from what they tell you."}
    return {"sources": [{"source_id": s["source_id"], "size": s["size"]} for s in sources]}


def _t_read_source(args: dict, ctx: AuthorContext) -> dict:
    sid = args.get("source_id") or ""
    key = f"{cat.sources_prefix(ctx.cid)}{sid}"
    try:
        body = s3().get_object(Bucket=config.ARTIFACTS_BUCKET, Key=key)["Body"].read()
    except Exception:
        return {"error": f"no such source: {sid}. Call list_sources first."}

    if sid.lower().endswith(".txt") or sid.lower().endswith(".md"):
        return {"source_id": sid, "text": body.decode("utf-8", "replace")[:MAX_SOURCE_CHARS]}

    pages, total = _pdf_text(body, args.get("from_page") or 1, args.get("to_page"))
    if not any(p.strip() for p in pages):
        # A scanned booklet has no text layer. Say so rather than returning
        # empty pages the model will read as "the booklet says nothing".
        return {"source_id": sid, "page_count": total, "text": "",
                "error": "This document has no extractable text — it is probably a "
                         "scan. Ask the author to summarize the relevant parts, or to "
                         "upload a text-based version."}

    out, used = [], 0
    for i, text in pages:
        chunk = text.strip()
        if used + len(chunk) > MAX_SOURCE_CHARS:
            out.append({"page": i, "truncated": True})
            break
        used += len(chunk)
        out.append({"page": i, "text": chunk})
    return {"source_id": sid, "page_count": total, "pages": out}


def _pdf_text(body: bytes, first: int, last: int | None):
    """Text extraction, not vision. Instruction booklets are typeset documents
    with a real text layer; rasterizing forty pages and paying for a vision
    pass to read text that is already there would be absurd."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(body))
    total = len(reader.pages)
    first = max(1, int(first or 1))
    last = min(total, int(last) if last else first + config.MAX_INGEST_PAGES - 1)
    return [(i, reader.pages[i - 1].extract_text() or "") for i in range(first, last + 1)], total


def _t_get_field_list(args: dict, ctx: AuthorContext) -> dict:
    fields = ctx.fields
    if args.get("section"):
        want = args["section"].strip().lower()
        fields = [f for f in fields if (f.get("section") or "").strip().lower() == want]
    noted = set((ctx.guide.get("field_notes") or {}))
    return {
        "fields": [
            {"field_id": f.get("field_id"), "label": f.get("label"),
             "type": f.get("type"), "section": f.get("section"),
             "required": f.get("required"), "has_note": f.get("field_id") in noted}
            for f in fields
        ],
        "count": len(fields),
    }


def _t_read_guide(args: dict, ctx: AuthorContext) -> dict:
    sections = ctx.guide.get("sections") or {}
    name = args.get("section")
    if name:
        return {"section": name, "content": sections.get(name, "")}
    return {
        "sections": {k: v for k, v in sections.items() if (v or "").strip()},
        "empty_sections": [k for k in gd.SECTIONS if not (sections.get(k) or "").strip()],
        "field_notes": sorted(ctx.guide.get("field_notes") or {}),
    }


def _check_basis(args: dict) -> str | None:
    if args.get("basis") not in VALID_BASIS:
        return f"basis must be one of {VALID_BASIS}; if it is none of these, ask the author"
    if not (args.get("citation") or "").strip():
        return "citation is required — name the source, or quote what the author said"
    return None


def _flush(ctx: AuthorContext) -> None:
    cat.put_guide(ctx.cid, ctx.guide)
    cat.update(ctx.cid, has_guide=gd.is_filled(ctx.guide))


def _t_write_section(args: dict, ctx: AuthorContext) -> dict:
    name = args.get("section")
    if name not in gd.SECTIONS:
        return {"ok": False, "error": f"section must be one of {gd.SECTIONS}"}
    err = _check_basis(args)
    if err:
        return {"ok": False, "error": err}

    gd.set_section(ctx.guide, name, args.get("markdown") or "")
    _flush(ctx)
    ctx.emit("guide_updated", {"section": name,
                               "markdown": gd.render(ctx.guide),
                               "guide": ctx.guide})
    return {"ok": True, "section": name, "awaiting_human_review": True}


def _t_write_field_note(args: dict, ctx: AuthorContext) -> dict:
    fid = args.get("field_id")
    if fid not in ctx.by_id:
        # Silently accepting an invented field_id would produce a note that
        # explain_field can never surface, so it would look written and be dead.
        return {"ok": False, "error": f"no field {fid!r} on this form — "
                                      "call get_field_list for the real ids"}
    body = args.get("markdown") or ""
    if body.strip():
        err = _check_basis(args)
        if err:
            return {"ok": False, "error": err}

    gd.set_field_note(ctx.guide, fid, body)
    _flush(ctx)
    ctx.emit("guide_updated", {"field_id": fid,
                               "markdown": gd.render(ctx.guide),
                               "guide": ctx.guide})
    return {"ok": True, "field_id": fid, "awaiting_human_review": True}


def _t_write_field_notes(args: dict, ctx: AuthorContext) -> dict:
    """The bulk path. No `basis` argument: the batch writes from the form's own
    pages and the sections already agreed with the author, and each note comes
    back carrying its own basis rather than one blanket claim for ninety-seven
    of them."""
    def progress(done: int, total: int) -> None:
        ctx.emit("note_progress", {"done": done, "total": total})
        # The guide panel redraws per chunk too — watching the notes appear is
        # most of what tells the author this is working.
        ctx.emit("guide_updated", {"markdown": gd.render(ctx.guide), "guide": ctx.guide})

    return nb.write_notes(
        ctx,
        field_ids=args.get("field_ids"),
        section=args.get("section"),
        progress=progress,
    )


_DISPATCH = {
    "list_sources": _t_list_sources,
    "read_source": _t_read_source,
    "get_field_list": _t_get_field_list,
    "read_guide": _t_read_guide,
    "write_section": _t_write_section,
    "write_field_note": _t_write_field_note,
    "write_field_notes": _t_write_field_notes,
}
