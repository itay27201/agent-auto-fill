"""The form schema — the single contract shared by ingest, the viewer, the
agent, and the renderer.

Everything downstream keys off this object. The frontend draws boxes from
`bbox`, the agent reads `label`/`help` and writes values, the renderer
stamps values back using `backend`.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import asdict, dataclass, field as dc_field
from typing import Any

log = logging.getLogger(__name__)

FIELD_TYPES = {"text", "textarea", "number", "date", "select", "multiselect", "checkbox", "signature"}

# The ingest model is given the enum above and mostly returns one of it, but it
# reaches for these instead often enough to matter. Falling back to "text" for them
# is how a tick square silently becomes a text field: `validate_value` then accepts
# "כן" where it would have demanded a boolean, and the renderer writes that word
# into a box a few points across.
_TYPE_ALIASES = {
    "radio": "checkbox", "radio_group": "checkbox", "radio_button": "checkbox",
    "boolean": "checkbox", "bool": "checkbox", "yes_no": "checkbox",
    "check": "checkbox", "tick": "checkbox", "checkbox_group": "multiselect",
    "dropdown": "select", "choice": "select", "single_select": "select",
    "multi_select": "multiselect", "multiple_select": "multiselect",
}


@dataclass
class FormField:
    field_id: str
    label: str
    type: str = "text"
    page: int = 1
    # Normalized [x0, y0, x1, y1], origin top-left, 0..1 of page width/height.
    # Normalized so the frontend can overlay at any zoom without rescaling.
    bbox: list[float] = dc_field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    # How much to trust `bbox`, and why. "ok" came from the document's own
    # geometry; "estimated" was a model's guess about a page with no text layer;
    # "low" failed `geometry.sanity_check` and has no usable box at all.
    #
    # This exists because the alternative to admitting a box is wrong is
    # stamping a value somewhere wrong and calling the form filled. A field
    # marked "low" is listed in the panel, drawn nowhere on the page, skipped by
    # the renderer, and waiting for a person to place it.
    bbox_confidence: str = "ok"
    bbox_source: str = ""         # region | estimated | none | user
    bbox_note: str = ""           # why it was rejected, for the person fixing it
    # The form's own printing nearest this box, from the geometry pass. What
    # tells three fields labelled "שם" apart when `label` cannot.
    nearby_text: list[dict] = dc_field(default_factory=list)
    section: str = ""
    required: bool = False
    options: list[str] = dc_field(default_factory=list)
    validation: str = ""          # regex, anchored at both ends when applied
    help: str = ""                # plain-language explanation for the agent
    max_length: int | None = None
    # Renderer-specific payload carried through from extraction so the render
    # Lambda never has to re-derive it. For AcroForm PDFs:
    #   {"kind": "acroform", "name": "...", "checked_value": "/Yes",
    #    "unchecked_value": "/Off", "rect": [...], "widget_type": "checkbox"}
    # For flat PDFs: {"kind": "overlay", "font_size": 10, "align": "auto"}
    backend: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FormField":
        known = {k: v for k, v in d.items() if k in cls.__annotations__}
        known.setdefault("field_id", d.get("id", ""))
        known["type"] = _canonical_type(known.get("type"))
        return cls(**known)


def _canonical_type(raw: Any) -> str:
    """One of `FIELD_TYPES`, mapping the near-misses rather than flattening them.

    Anything still unrecognized falls back to "text", but says so: a type nobody
    noticed being rewritten is a field that renders as a string wherever it lands.
    """
    name = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if name in FIELD_TYPES:
        return name
    if name in _TYPE_ALIASES:
        return _TYPE_ALIASES[name]
    if name:
        log.warning("unknown field type %r — treating it as text", raw)
    return "text"


def schema_from_list(items: list[dict]) -> list[FormField]:
    return [FormField.from_dict(i) for i in items]


def schema_to_list(fields: list[FormField]) -> list[dict]:
    return [f.to_dict() for f in fields]


def agent_view(fields: list[FormField], values: dict[str, dict], only: list[str] | None = None) -> list[dict]:
    """Trimmed projection for the model. Drops bbox and backend — the agent
    never needs pixel geometry, and every token spent on it is a token not
    spent on the form's actual content."""
    out = []
    for f in fields:
        if only and f.field_id not in only:
            continue
        v = values.get(f.field_id) or {}
        item = {
            "field_id": f.field_id,
            "label": f.label,
            "type": f.type,
            "section": f.section,
            "required": f.required,
            "value": v.get("value"),
            "source": v.get("source"),
        }
        if f.options:
            item["options"] = f.options
        if f.help:
            item["help"] = f.help
        out.append(item)
    return out


# ------------------------------------------------------------------ validation

# Day-first before month-first, because these are Israeli forms: `03/04/1996` is
# 3 April. `%d%m%Y` is here because a box printed as eight character cells invites
# exactly that — someone reading "8 cells" types eight digits, and refusing it
# while the form asks for it is the confusion this list exists to avoid. It can
# only ever match a bare 8-digit string, since every other entry needs separators.
_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%m/%d/%Y", "%d%m%Y")

# What a date is stored as, whatever was typed. `api_render._comb_chars` says a
# date "reaches here as 26/02/1996 for eight cells" and strips the separators
# itself — so this is the shape the renderer already expects, and normalizing to
# it is what stops an ISO string being stamped into a day/month box year-first.
_DATE_CANONICAL = "%d/%m/%Y"


def validate_value(f: FormField, raw: Any) -> str | None:
    """Return an error message, or None if the value is acceptable."""
    empty = raw is None or (isinstance(raw, str) and not raw.strip())

    if empty:
        return f"{f.label} is required" if f.required else None

    if f.type == "checkbox":
        if not isinstance(raw, bool):
            # Naming the value and the fix lets the model correct itself in the
            # same turn, the way `_wrong_field` does for a mislabelled write.
            return (f"{f.label} is a tick box: send true to mark it or false to "
                    f"leave it blank, not {raw!r}.")
        return None

    if f.type == "multiselect":
        if not isinstance(raw, list):
            return f"{f.label} must be a list"
        bad = [v for v in raw if f.options and v not in f.options]
        return f"{f.label}: not valid options: {bad}" if bad else None

    value = str(raw).strip()

    if f.type == "select" and f.options and value not in f.options:
        return f"{f.label} must be one of: {', '.join(f.options)}"

    if f.type == "number":
        try:
            float(value.replace(",", ""))
        except ValueError:
            return f"{f.label} must be a number"

    if f.type == "date" and not _parse_date(value):
        # Same reasoning as the checkbox message above: name the shape and echo
        # what came in, so the model can fix it without another round trip.
        return (f"{f.label} is a date: send it as DD/MM/YYYY (for example "
                f"26/02/1996), not {raw!r}.")

    if f.max_length:
        # `max_length` on these forms is a count of printed character cells, but
        # a date's separators sit *between* the cells rather than in them —
        # `api_render._comb_chars` drops them for exactly that reason. Measuring
        # the raw string against a cell count compares two different units, and
        # on an 8-cell date box it rejected every date there is: every format
        # `_parse_date` accepts is ten characters long, so the field could not be
        # filled by the agent or by hand.
        measured = "".join(ch for ch in value if ch.isalnum()) if f.type == "date" else value
        if len(measured) > f.max_length:
            return f"{f.label} is longer than {f.max_length} characters"

    if f.validation:
        try:
            if not re.fullmatch(f.validation, value):
                return f"{f.label} does not match the expected format"
        except re.error:
            # A bad regex from the ingest LLM must never block a valid answer.
            pass

    return None


def _parse_date(value: str):
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def normalize_value(f: FormField, raw: Any) -> Any:
    """The value as it should be stored, given what the form will do with it.

    Only dates are touched, and only ones that parse. `_DATE_FORMATS` accepts
    four notations for the same day, and a box printed as character cells keeps
    whichever one it was handed — so `1996-02-26` reached `_comb_chars`, lost its
    hyphens, and was stamped `19960226` into a box whose cells mean DDMMYYYY. A
    silently wrong date on a tax form is worse than a rejected one, and it cannot
    be caught downstream: by then it is eight digits with nothing to say which
    end is the year.

    Storing one shape also means two people who type the same day the two obvious
    ways end up with the same value, and the viewer shows a date rather than a
    digit run.

    Call it after `validate_value`, never instead of it — an unparseable value is
    returned untouched so the error still comes from validation.
    """
    if f.type != "date":
        return raw
    parsed = _parse_date(str(raw).strip()) if raw is not None else None
    return parsed.strftime(_DATE_CANONICAL) if parsed else raw


def validate_all(fields: list[FormField], values: dict[str, dict]) -> dict:
    errors, missing = [], []
    for f in fields:
        raw = (values.get(f.field_id) or {}).get("value")
        err = validate_value(f, raw)
        if err:
            entry = {"field_id": f.field_id, "label": f.label, "error": err}
            (missing if (raw in (None, "") and f.required) else errors).append(entry)

    unconfirmed = [
        {"field_id": f.field_id, "label": f.label}
        for f in fields
        if (values.get(f.field_id) or {}).get("source") == "agent"
        and not (values.get(f.field_id) or {}).get("confirmed")
    ]

    filled = sum(1 for f in fields if (values.get(f.field_id) or {}).get("value") not in (None, ""))
    return {
        "ok": not errors and not missing,
        "errors": errors,
        "missing_required": missing,
        "awaiting_confirmation": unconfirmed,
        "filled": filled,
        "total": len(fields),
    }
