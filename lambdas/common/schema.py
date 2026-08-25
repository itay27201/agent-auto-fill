"""The form schema — the single contract shared by ingest, the viewer, the
agent, and the renderer.

Everything downstream keys off this object. The frontend draws boxes from
`bbox`, the agent reads `label`/`help` and writes values, the renderer
stamps values back using `backend`.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, field as dc_field
from typing import Any

FIELD_TYPES = {"text", "textarea", "number", "date", "select", "multiselect", "checkbox", "signature"}


@dataclass
class FormField:
    field_id: str
    label: str
    type: str = "text"
    page: int = 1
    # Normalized [x0, y0, x1, y1], origin top-left, 0..1 of page width/height.
    # Normalized so the frontend can overlay at any zoom without rescaling.
    bbox: list[float] = dc_field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
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
        if known.get("type") not in FIELD_TYPES:
            known["type"] = "text"
        return cls(**known)


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

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%m/%d/%Y")


def validate_value(f: FormField, raw: Any) -> str | None:
    """Return an error message, or None if the value is acceptable."""
    empty = raw is None or (isinstance(raw, str) and not raw.strip())

    if empty:
        return f"{f.label} is required" if f.required else None

    if f.type == "checkbox":
        if not isinstance(raw, bool):
            return f"{f.label} must be true or false"
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
        return f"{f.label} is not a recognizable date"

    if f.max_length and len(value) > f.max_length:
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
