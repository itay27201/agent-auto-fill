"""What is wrong with a guide, decided in code rather than by a model.

`is_filled` answers "has anything been written at all", which is the right
question for the publish floor and the wrong one for everything else. It is
what let a guide covering 70 of 97 fields reach the catalog as finished: one
non-empty section passes it.

Every check here is a set difference or a string comparison. That matters —
the failures these catch are exactly the ones a model is worst at reporting on
its own work. Asking the writer "did you cover every field?" gets you a
confident yes; `set(a) - set(b)` gets you the 27 it missed.

Model-graded questions — is this note *true*, does it contradict Key rules —
are a separate tier and are not here.
"""
from __future__ import annotations

import re
import unicodedata

from . import guide as gd

# A note that adds fewer than this many characters to the label it echoes is
# restating the box rather than explaining it. Tuned against the real ITC-101
# guide: it flags a note reading "the house number on the street" under a label
# reading "number", and leaves alone one that expands "name" into "the third
# child's name -- given and family name as they appear on the ID appendix".
WEAK_MARGIN = 70

# At or above this, the same sentence pasted onto many fields is boilerplate
# rather than coincidence. Two fields sharing a note is plausible; five is a
# model filling space.
DUPLICATE_AT = 3

# Written as escapes rather than literal glyphs. The ranges are equally
# unreadable either way, but a mangled paste of one Hebrew character would
# silently narrow the range instead of failing loudly.
_HEBREW = re.compile("[֐-׿יִ-ﭏ]")
_ARABIC = re.compile("[؀-ۿݐ-ݿ]")
_LATIN = re.compile("[A-Za-z]")

_SCRIPTS = {"he": _HEBREW, "ar": _ARABIC, "en": _LATIN}


def check(guide: dict | None, fields: list[dict], language: str = "") -> dict:
    """-> {"total", "noted", "missing", "weak", "duplicates",
           "wrong_language", "empty_sections", "counts", "complete"}

    The field_id lists are capped for display; `counts` carries the true sizes
    so a caller never reports a truncated number as the real one.
    """
    notes = (guide or {}).get("field_notes") or {}
    sections = (guide or {}).get("sections") or {}
    by_id = {f.get("field_id"): f for f in fields if f.get("field_id")}

    missing = [fid for fid in by_id if fid not in notes]
    weak = [fid for fid, body in notes.items() if _is_weak(body, by_id.get(fid))]
    wrong_language = [fid for fid, body in notes.items()
                      if _wrong_script(body, language)]

    seen: dict[str, list[str]] = {}
    for fid, body in notes.items():
        seen.setdefault(_normalize(body), []).append(fid)
    duplicates = [sorted(fids) for fids in seen.values() if len(fids) >= DUPLICATE_AT]

    # "Field notes" is a heading in SECTIONS, but nothing is ever written under
    # it: `parse` routes that block into `field_notes` and leaves the section
    # body empty. Counting it here reported one empty section on every guide
    # forever, including finished ones — the old turn_end had the same bug.
    # Coverage above is what actually measures whether the notes were written.
    empty_sections = [s for s in gd.SECTIONS
                      if s != gd.FIELD_NOTES and not (sections.get(s) or "").strip()]

    return {
        "total": len(by_id),
        "noted": len([fid for fid in by_id if fid in notes]),
        "missing": sorted(missing)[:40],
        "weak": sorted(weak)[:20],
        "duplicates": duplicates[:5],
        "wrong_language": sorted(wrong_language)[:20],
        "empty_sections": empty_sections,
        "counts": {
            "missing": len(missing),
            "weak": len(weak),
            "duplicates": sum(len(f) for f in duplicates),
            "wrong_language": len(wrong_language),
        },
        "complete": not missing and not empty_sections,
    }


def summary(report: dict) -> str:
    """One line for a person. The agent gets the whole dict; this is what the
    page shows and what the publish confirmation asks about."""
    parts = [f"{report['noted']} of {report['total']} fields noted"]
    c = report.get("counts") or {}
    if c.get("missing"):
        parts.append(f"{c['missing']} with no note")
    if report.get("empty_sections"):
        parts.append(f"{len(report['empty_sections'])} sections empty")
    if c.get("weak"):
        parts.append(f"{c['weak']} that only restate the label")
    if c.get("duplicates"):
        parts.append(f"{c['duplicates']} sharing a copied note")
    if c.get("wrong_language"):
        parts.append(f"{c['wrong_language']} in the wrong language")
    return " · ".join(parts)


# ------------------------------------------------------------------ internals

def _is_weak(body: str, field: dict | None) -> bool:
    """The note opens by repeating the field's own label and stops soon after.

    Anchored on the opening rather than on similarity anywhere in the string:
    a good note often *starts* from the label and then says something useful —
    the defect is starting from it and then stopping.
    """
    label = ((field or {}).get("label") or "").strip()
    body = (body or "").strip()
    if not label or not body:
        return False
    return body.startswith(label) and len(body) < len(label) + WEAK_MARGIN


def _normalize(body: str) -> str:
    """Fold whitespace, case and Unicode form, so a note pasted with a
    different dash or a trailing space still counts as the same note."""
    text = unicodedata.normalize("NFKC", (body or "").strip().lower())
    return re.sub(r"\s+", " ", text)


def _wrong_script(body: str, language: str) -> bool:
    """The guide is written in the form's language. A note carrying none of
    that language's script is either untranslated boilerplate or a stray
    English sentence in a Hebrew guide.

    Only runs for languages this can actually test, and only where there is
    enough text to judge — a note that is a bare number or date has no script
    either way and is not a defect.
    """
    script = _SCRIPTS.get((language or "").strip().lower()[:2])
    if not script:
        return False
    letters = [c for c in (body or "") if c.isalpha()]
    if len(letters) < 8:
        return False
    return not script.search(body)
