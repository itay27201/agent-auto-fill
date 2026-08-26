#!/usr/bin/env python3
"""
agent_tools.py — turns a confirmed schema into a Bedrock tool definition.

This is the airlock. The model receives field IDS AND LABELS ONLY — never a
coordinate, never a bbox, never a page offset. `field_id` is a closed enum, so a
hallucinated field is a schema violation the runtime rejects before any drawing
happens.

    python agent_tools.py registry/itc101/schema.json > tools.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SYSTEM_PROMPT_HE = """\
אתה עוזר שממלא טפסים ממשלתיים בעברית. אתה מדבר עם המשתמש בעברית.

כללי ברזל:
1. אתה קורא ל-set_field רק כשהמשתמש מסר ערך מפורש. אתה לעולם לא מנחש, לא משלים
   ולא ממציא ערכים — גם לא ערכים "סבירים".
2. אם לא ברור לך לאיזה שדה המשתמש מתכוון, או שהערך חלקי/דו-משמעי, אתה קורא ל-
   ask_clarification ומציין את השדות המועמדים. עדיף לשאול מאשר לטעות.
3. שדות מסוג radio הם בלעדיים. סימון אפשרות אחת מבטל אוטומטית את האחרות —
   אל תנסה לסמן שתיים.
4. אם המערכת מחזירה לך שגיאת ולידציה, מסור למשתמש את ההודעה כפי שהיא ובקש תיקון.
   אל תעקוף ולידציה ואל תשנה את הערך בעצמך.
5. אתה לא רואה את הטופס ולא יודע היכן שדה נמצא בדף. זה לא תפקידך.
"""


def build(schema: dict) -> dict:
    fillable, enum, descriptions = [], [], []
    groups = {g["id"]: g for g in schema.get("groups", [])}

    for f in schema["fields"]:
        fid = f["id"]
        if f["type"] == "radio_option":
            g = groups.get(f.get("group"), {})
            desc = (f"אפשרות '{f.get('label_he','')}' בקבוצה "
                    f"'{g.get('label_he', f.get('group',''))}' (בחירה יחידה)")
        else:
            desc = f.get("label_he", "") or fid
            if f.get("validator"):
                desc += f" [{f['validator']}]"
            if f["type"] == "comb":
                desc += f" ({f['cells']} תאים)"
        enum.append(fid)
        descriptions.append(f"{fid} — {desc}")
        fillable.append({"id": fid, "type": f["type"], "label_he": f.get("label_he", "")})

    set_field = {
        "name": "set_field",
        "description": (
            "כותב ערך לשדה בטופס. יש לקרוא לכלי פעם אחת לכל שדה. "
            "המערכת מבצעת ולידציה ומחזירה שגיאה אם הערך אינו תקין.\n\n"
            "שדות זמינים:\n" + "\n".join(descriptions)
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "field_id": {"type": "string", "enum": enum,
                             "description": "מזהה השדה. חייב להיות מהרשימה."},
                "value": {"type": ["string", "boolean"],
                          "description": "הערך. עבור radio_option/checkbox: true."},
                "source_quote": {"type": "string",
                                 "description": "הציטוט המדויק מדברי המשתמש שממנו נלקח הערך. "
                                                "חובה — משמש לביקורת ולמניעת המצאות."},
            },
            "required": ["field_id", "value", "source_quote"],
        },
    }

    ask = {
        "name": "ask_clarification",
        "description": "שואל את המשתמש שאלת הבהרה כשלא ברור לאיזה שדה או לאיזה ערך הוא מתכוון.",
        "input_schema": {
            "type": "object",
            "properties": {
                "question_he": {"type": "string"},
                "candidate_fields": {"type": "array", "items": {"type": "string", "enum": enum}},
            },
            "required": ["question_he"],
        },
    }

    review = {
        "name": "list_filled",
        "description": "מחזיר את כל השדות שמולאו עד כה, לצורך סיכום למשתמש לפני הפקת הטופס.",
        "input_schema": {"type": "object", "properties": {}},
    }

    return {
        "form_id": schema["form_id"],
        "schema_version": schema["schema_version"],
        "system_prompt_he": SYSTEM_PROMPT_HE,
        "tools": [set_field, ask, review],
        "field_index": fillable,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("schema")
    a = ap.parse_args()
    schema = json.loads(Path(a.schema).read_text(encoding="utf-8"))
    print(json.dumps(build(schema), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
