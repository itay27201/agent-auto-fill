"""The document guide — the knowledge a form PDF cannot contain.

`schema.json` says where the boxes are and what type they hold. It cannot say
who is eligible, which certificates must be attached, what the filing deadline
is, or why an office rejects the form. That lives here, as markdown in S3, one
`guide.md` per catalog entry.

Markdown rather than JSON because a person has to read and correct it. An
official whose job is knowing this form should be able to open the guide, spot
that the deadline is wrong, and fix it — without touching a field schema the
renderer depends on.

Format: flat `key: value` frontmatter, then fixed `##` sections. Field-specific
notes are `###` blocks under "Field notes", keyed by the schema's `field_id` so
`explain_field` can merge them in.

Parsing is hand-rolled on purpose. The frontmatter is flat, the structure is
two heading levels deep, and the layer is already close enough to the Lambda
size limit that a YAML dependency for this would be a bad trade.
"""
from __future__ import annotations

import re

# Order matters: `render` writes sections in this order, and the authoring
# agent is offered exactly these names. A fixed list is what makes the guide
# sliceable — a free-form heading set would leave `prompt_block` guessing.
# What may be cited as the origin of a sentence written into a guide. No
# "general knowledge" and no "inferred" — if it is neither in a source nor
# stated by the author, the agent does not have it.
#
# It lives here rather than beside the tool schema because two writers enforce
# it now: `author_tools.write_field_note` one note at a time, and
# `note_batch` a hundred at once. Writing in bulk is not a licence to skip it,
# and a second copy of the tuple is how the two quietly drift apart.
VALID_BASIS = ("source_doc", "author_said", "form_itself")

SECTIONS = (
    "Overview",
    "Eligibility",
    "Required attachments",
    "Key rules",
    "Field notes",
    "Common mistakes",
    "Where to submit",
)

FIELD_NOTES = "Field notes"

# Sections that ride along in every agent turn. Everything else is pulled on
# demand by the read_guide tool — see prompt_block().
INLINE_SECTIONS = ("Overview", "Key rules", "Required attachments")

_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def empty(meta: dict | None = None) -> dict:
    """A guide with every section present and blank. A new catalog entry gets
    one of these so the authoring agent has a skeleton to fill rather than a
    blank file to invent structure for."""
    return {"meta": dict(meta or {}), "sections": {s: "" for s in SECTIONS}, "field_notes": {}}


# ------------------------------------------------------------------- parsing

def parse(md: str) -> dict:
    """-> {"meta": {...}, "sections": {name: body}, "field_notes": {fid: body}}

    Unknown `##` headings are kept in `sections` so a hand-edited guide never
    loses text on a round-trip; they just don't appear in the offered list.
    """
    text = (md or "").replace("\r\n", "\n")
    meta, body = _split_frontmatter(text)

    sections: dict[str, str] = {s: "" for s in SECTIONS}
    field_notes: dict[str, str] = {}

    for name, chunk in _split_headings(body, level=2):
        if name == FIELD_NOTES:
            intro, notes = _parse_field_notes(chunk)
            sections[FIELD_NOTES] = intro
            field_notes.update(notes)
        else:
            sections[name] = chunk

    return {"meta": meta, "sections": sections, "field_notes": field_notes}


def _split_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"')
    return meta, text[m.end():]


def _split_headings(body: str, level: int) -> list[tuple[str, str]]:
    """Split on `##` (or `###`) headings into (name, body) pairs, preserving
    order. Text before the first heading is dropped by callers that don't
    want it, so it is returned under the empty name."""
    marker = "#" * level + " "
    out: list[tuple[str, str]] = []
    name, buf = "", []
    for line in body.split("\n"):
        stripped = line.strip()
        # A deeper heading (### under ##) is content, not a split point.
        if stripped.startswith(marker) and not stripped.startswith(marker + "#"):
            out.append((name, "\n".join(buf).strip()))
            name, buf = stripped[len(marker):].strip(), []
        else:
            buf.append(line)
    out.append((name, "\n".join(buf).strip()))
    return [(n, b) for n, b in out if n]


def _parse_field_notes(chunk: str) -> tuple[str, dict[str, str]]:
    """The "Field notes" section is `###  <field_id>` blocks. Anything before
    the first one is section intro text."""
    notes = dict(_split_headings(chunk, level=3))
    first = chunk.find("### ")
    intro = (chunk if first == -1 else chunk[:first]).strip()
    return intro, notes


# ------------------------------------------------------------------ rendering

def render(guide: dict) -> str:
    """Round-trips `parse`. Sections are written in SECTIONS order first, then
    any unknown heading a human added, so hand edits survive."""
    meta = guide.get("meta") or {}
    sections = guide.get("sections") or {}
    field_notes = guide.get("field_notes") or {}

    parts = []
    if meta:
        parts.append("---")
        parts.extend(f"{k}: {v}" for k, v in meta.items())
        parts.append("---\n")

    extra = [k for k in sections if k not in SECTIONS]
    for name in list(SECTIONS) + extra:
        if name == FIELD_NOTES and not (sections.get(name) or field_notes):
            parts.append(f"## {name}\n")
            continue
        parts.append(f"## {name}\n")
        body = (sections.get(name) or "").strip()
        if body:
            parts.append(body + "\n")
        if name == FIELD_NOTES:
            for fid, note in field_notes.items():
                parts.append(f"### {fid}\n")
                parts.append((note or "").strip() + "\n")

    return "\n".join(parts).rstrip() + "\n"


# --------------------------------------------------------------- prompt slice

def prompt_block(guide: dict | None, budget: int = 4000) -> str:
    """The part of the guide that rides in every agent turn.

    Only Overview / Key rules / Required attachments — the things the agent
    needs whether or not it thinks to ask. The rest is behind the read_guide
    tool, because a long guide inlined in full would crowd out the field list
    it has to reason over.

    `budget` is a character cap on the whole block. Truncation is per-section
    so one runaway section cannot starve the others.
    """
    if not guide:
        return ""
    sections = guide.get("sections") or {}
    present = [(n, (sections.get(n) or "").strip()) for n in INLINE_SECTIONS]
    present = [(n, b) for n, b in present if b]
    if not present:
        return ""

    share = max(200, budget // len(present))
    meta = guide.get("meta") or {}
    head = "Official guidance for this form"
    if meta.get("name"):
        head += f" ({meta['name']}"
        head += f", {meta['agency']})" if meta.get("agency") else ")"
    head += (
        ". It was written by a person for this specific form — prefer it over "
        "your own knowledge, and quote it when the person asks what is required. "
        f"Further sections are available through the read_guide tool: "
        f"{', '.join(s for s in SECTIONS if s not in INLINE_SECTIONS)}."
    )

    out = [head]
    for name, body in present:
        out.append(f"\n## {name}\n{_truncate(body, share)}")
    return "\n".join(out)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    return text[: cut if cut > limit // 2 else limit].rstrip() + "\n[...]"


# ------------------------------------------------------------------- editing

def set_section(guide: dict, name: str, markdown: str) -> dict:
    """Replace one `##` section. Used by the authoring agent's write_section."""
    guide.setdefault("sections", {})[name] = (markdown or "").strip()
    return guide


def set_field_note(guide: dict, field_id: str, markdown: str) -> dict:
    notes = guide.setdefault("field_notes", {})
    body = (markdown or "").strip()
    if body:
        notes[field_id] = body
    else:
        notes.pop(field_id, None)
    return guide


def is_filled(guide: dict | None) -> bool:
    """Whether anything has actually been written. `POST /catalog` seeds an
    empty skeleton, and an all-blank guide should not be advertised as one."""
    if not guide:
        return False
    if any((v or "").strip() for v in (guide.get("sections") or {}).values()):
        return True
    return bool(guide.get("field_notes"))
