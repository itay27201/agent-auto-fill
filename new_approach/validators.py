#!/usr/bin/env python3
"""
validators.py — deterministic value validation.

Runs BETWEEN the LLM's set_field() call and the renderer. Nothing reaches the
PDF without passing through here. A ValidationError is not a failure: it is the
signal that the agent should go back and ask the user a clarifying question.
"""

from __future__ import annotations

import re
from datetime import datetime


class ValidationError(ValueError):
    """Carries a Hebrew message the agent can relay to the user verbatim."""

    def __init__(self, message_he: str, code: str = "invalid"):
        super().__init__(message_he)
        self.message_he = message_he
        self.code = code


# --------------------------------------------------------------------------
def israeli_id(value: str) -> str:
    """
    9 digits, zero-padded, with the standard Luhn-style check digit.
    Returns the normalised value or raises.
    """
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        raise ValidationError("לא זוהו ספרות במספר תעודת הזהות.", "empty")
    if len(digits) > 9:
        raise ValidationError(
            f"מספר תעודת זהות בן {len(digits)} ספרות — חייב להיות 9 ספרות.", "too_long")
    digits = digits.zfill(9)

    total = 0
    for i, ch in enumerate(digits):
        n = int(ch) * (1 if i % 2 == 0 else 2)
        total += n if n < 10 else n - 9
    if total % 10 != 0:
        raise ValidationError(
            f"ספרת הביקורת של מספר הזהות {digits} אינה תקינה. "
            "אפשר לוודא את המספר?", "checksum")
    return digits


def tax_file_number(value: str) -> str:
    digits = re.sub(r"\D", "", str(value))
    if len(digits) > 9:
        raise ValidationError("מספר תיק ניכויים ארוך מ-9 ספרות.", "too_long")
    return digits.zfill(9)


def date_ddmmyy(value: str) -> str:
    """Accepts 01/02/1990, 1.2.90, 010290 … → '010290' (6 comb cells)."""
    return _date(value, "%d%m%y", 6)


def date_ddmmyyyy(value: str) -> str:
    return _date(value, "%d%m%Y", 8)


def _date(value: str, fmt: str, cells: int) -> str:
    raw = re.sub(r"\D", "", str(value))
    for parse in ("%d%m%Y", "%d%m%y", "%Y%m%d"):
        try:
            dt = datetime.strptime(raw, parse)
            break
        except ValueError:
            continue
    else:
        raise ValidationError(
            f"לא הצלחתי לפענח את התאריך '{value}'. אפשר בפורמט dd/mm/yyyy?", "format")
    if not (1900 <= dt.year <= datetime.now().year + 1):
        raise ValidationError(f"שנה לא סבירה בתאריך: {dt.year}.", "range")
    out = dt.strftime(fmt)
    if len(out) != cells:
        raise ValidationError("אורך התאריך אינו תואם את מספר התאים בטופס.", "length")
    return out


def mobile(value: str) -> str:
    d = re.sub(r"\D", "", str(value))
    if d.startswith("972"):
        d = "0" + d[3:]
    if len(d) != 10 or not d.startswith("05"):
        raise ValidationError("מספר נייד ישראלי חייב להיות 10 ספרות ולהתחיל ב-05.", "format")
    return d


def phone(value: str) -> str:
    d = re.sub(r"\D", "", str(value))
    if not 9 <= len(d) <= 10:
        raise ValidationError("מספר טלפון חייב להיות 9 או 10 ספרות.", "format")
    return d


def postal_code(value: str) -> str:
    d = re.sub(r"\D", "", str(value))
    if len(d) not in (5, 7):
        raise ValidationError("מיקוד ישראלי הוא 5 או 7 ספרות.", "format")
    return d.zfill(7)


def tax_year(value: str) -> str:
    d = re.sub(r"\D", "", str(value))
    if len(d) == 2:
        d = "20" + d
    if len(d) != 4:
        raise ValidationError("שנת מס חייבת להיות 4 ספרות.", "format")
    return d


def hebrew_text(value: str) -> str:
    v = " ".join(str(value).split())
    if not v:
        raise ValidationError("הערך ריק.", "empty")
    if len(v) > 60:
        raise ValidationError("הטקסט ארוך מדי לשדה בטופס (מקסימום 60 תווים).", "too_long")
    return v


def email(value: str) -> str:
    v = str(value).strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", v):
        raise ValidationError(f"כתובת הדוא\"ל '{v}' אינה תקינה.", "format")
    return v


REGISTRY = {
    "israeli_id": israeli_id,
    "tax_file_number": tax_file_number,
    "date_ddmmyy": date_ddmmyy,
    "date_ddmmyyyy": date_ddmmyyyy,
    "mobile": mobile,
    "phone": phone,
    "postal_code": postal_code,
    "tax_year": tax_year,
    "text": hebrew_text,
    "email": email,
}


def validate(kind: str, value):
    """Dispatch by validator name; unknown kinds pass through untouched."""
    fn = REGISTRY.get(kind)
    return fn(value) if fn else value


# --------------------------------------------------------------------------
# cross-field rules — form 101 specific
# --------------------------------------------------------------------------
def check_dependencies(schema: dict, values: dict) -> list[str]:
    """Returns a list of Hebrew problems. Empty list = the form is consistent."""
    problems = []
    fields = {f["id"]: f for f in schema["fields"]}

    # radio exclusivity is structural, not advisory
    for grp in schema.get("groups", []):
        if grp.get("kind") != "radio":
            continue
        chosen = [m for m in grp["members"] if values.get(m) in (True, "X", "x")]
        if len(chosen) > 1:
            problems.append(
                f"בקבוצה '{grp.get('label_he', grp['id'])}' סומנו {len(chosen)} אפשרויות — "
                "מותרת אחת בלבד.")

    for fid, f in fields.items():
        if not f.get("required_when"):
            continue
        cond_field, cond_value = f["required_when"]
        if values.get(cond_field) == cond_value and not values.get(fid):
            problems.append(f"השדה '{f.get('label_he', fid)}' נדרש בהתאם לבחירה קודמת.")

    return problems
