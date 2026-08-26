"""The form map: where each box sits, and what tells apart two that share a label.

`schema.json` is the machine contract and `guide.md` is the knowledge a PDF
cannot hold. This is the third artifact, and it answers the one question neither
of those can: *which box is this?*

It exists because of a specific failure. Form 101 labels at least three
different fields `שם` — the employer's name, the employee's first name, and the
health fund's. The filling agent sees a field list of ids, labels and sections
and nothing else, so it had no way to tell them apart, and a correct value went
into the wrong cell. A table saying which row and which side of the row each
field is, with the repeated labels called out by name, is what closes that.

Two readers, which is why it is markdown rather than more JSON:

  a person   defining a form opens it to check the boxes were understood. That
             is not a question `schema.json` can be asked.
  the agent  gets it in the system prompt behind the cache point, so the layout
             costs tokens once per session rather than being re-reasoned every
             turn.

Derived in code, not asked of a model. It is a projection of the schema, so
generating it mechanically means it cannot drift from `schema.json` — and it is
free. The model contributed the parts that need judgement (label, section,
help); position comes from the geometry, which is exact.
"""
from __future__ import annotations

from . import geometry as geo


def render(fields: list[dict]) -> str:
    """Build the map from a schema. Pure function of `fields`, so it can be
    regenerated after someone moves a box without re-reading the document."""
    by_page: dict[int, list[dict]] = {}
    for f in fields:
        by_page.setdefault(int(f.get("page") or 1), []).append(f)

    labels: dict[str, int] = {}
    for f in fields:
        key = (f.get("label") or "").strip()
        if key:
            labels[key] = labels.get(key, 0) + 1

    lines = ["# Form map", "",
             "Where each box sits on the page, and what distinguishes boxes that "
             "share a label. Generated from the document's own geometry.", ""]

    for page in sorted(by_page):
        lines.append(f"## Page {page}")
        siblings = [f["bbox"] for f in by_page[page]
                    if f.get("bbox") and f.get("bbox_confidence") != "low"]

        # Grouped by section, then down the page within each. Sorting the whole
        # page by position first would interleave the sections wherever a field
        # is unplaced (no box sorts as the top-left corner), and a section that
        # appears twice under two headings reads as two different sections.
        for section, group in _by_section(by_page[page]).items():
            lines += ["", f"### {section or 'No section'}", "",
                      "| field_id | label | where | wants |",
                      "|---|---|---|---|"]
            for f in group:
                where = ("**not placed**" if f.get("bbox_confidence") == "low"
                         else geo.describe_position(f.get("bbox"), siblings) or "on the page")
                near = " · ".join(n["text"] for n in (f.get("nearby_text") or [])[:3])
                wants = (f.get("help") or "").replace("\n", " ").replace("|", "/")[:110]
                lines.append(f"| `{f['field_id']}` | {f.get('label', '')} | "
                             f"{where}{' — near ' + near if near else ''} | {wants} |")

        dupes = sorted({(f.get("label") or "").strip() for f in by_page[page]
                        if labels.get((f.get("label") or "").strip(), 0) > 1} - {""})
        if dupes:
            lines += ["", "**Repeated labels on this page — these are different boxes:**", ""]
            for label in dupes:
                same = [f"`{f['field_id']}`" for f in fields
                        if (f.get("label") or "").strip() == label]
                lines.append(f"- {label}: {', '.join(same)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _by_section(fields: list[dict]) -> dict[str, list[dict]]:
    """Sections in the order they first appear down the page; fields within a
    section in reading order, with the unplaced ones last.

    Unplaced fields go last rather than first because they have no box, and a
    missing box sorts as [0, 0, 0, 0] — the top-left corner of the page, which
    is the one place they certainly are not.
    """
    order = sorted(fields, key=lambda f: _sort_key(f))
    out: dict[str, list[dict]] = {}
    for f in order:
        out.setdefault(f.get("section") or "", []).append(f)
    return out


def _sort_key(f: dict):
    if f.get("bbox_confidence") == "low" or not f.get("bbox") or not any(f["bbox"]):
        return (1, 0.0, 0.0)
    # Down the page, then right to left across it — these forms are RTL.
    return (0, round(f["bbox"][1], 3), -f["bbox"][2])
