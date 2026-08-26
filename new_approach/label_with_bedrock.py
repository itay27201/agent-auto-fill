#!/usr/bin/env python3
"""
label_with_bedrock.py — stage A2: semantic labelling of draft candidates.

Sends each annotated page to Claude on Bedrock and asks it to name the fields
and group the checkboxes. The model sees an IMAGE and returns SEMANTIC LABELS
ONLY. It is structurally incapable of moving a box: the geometry it is shown is
already fixed, and its output is merged by candidate index.

    export AWS_REGION=eu-west-1
    python label_with_bedrock.py registry/itc101 \
        --model eu.anthropic.claude-sonnet-4-5-20250929-v1:0
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

import boto3

PROMPT = """\
בתמונה מוצג טופס ממשלתי ישראלי שבו סומנו מועמדים לשדות מילוי, כל אחד עם מספר סידורי:
  אדום  = תיבת סימון (checkbox / radio)
  כחול  = שדה תאים לספרות (comb) — הקווים המקווקווים הם התאים
  ירוק  = שורת כתיבה (underline)

לכל מועמד ברשימה שאני נותן לך, החזר:
  index          — המספר הסידורי כפי שהוא בתמונה
  semantic_id    — snake_case באנגלית, ייחודי, תיאורי (employee_last_name, marital_status_divorced)
  label_he       — התווית המדויקת בעברית כפי שהיא מופיעה בטופס
  section_he     — כותרת החלק בטופס (למשל "ב. פרטי העובד/ת")
  data_type      — אחד מ: text | digits | date | email | checkbox
  validator      — אחד מ: israeli_id, tax_file_number, date_ddmmyy, date_ddmmyyyy,
                   mobile, phone, postal_code, tax_year, email, text, none
  group_key      — עבור checkbox בלבד: מפתח משותף לכל האפשרויות של אותה שאלה
                   (למשל marital_status). null אם התיבה עומדת בפני עצמה.
  group_kind     — radio | multiselect | standalone
  confidence     — 0.0 עד 1.0
  note           — הערה קצרה אם משהו נראה חשוד (למשל מספר תאים שלא מתאים לתווית)

כללים:
- אל תמציא מועמדים שאינם ברשימה ואל תשמיט אף אחד.
- אם התווית האוטומטית שאני נותן לך שגויה — תקן אותה לפי מה שאתה רואה בתמונה.
- אם מספר התאים בשדה comb לא מתאים לתווית (למשל "מספר זהות" עם 8 תאים במקום 9),
  ציין זאת ב-note ותן confidence נמוך.
- החזר JSON בלבד: {"fields": [...]}. בלי טקסט לפני או אחרי, בלי ```.
"""


def call(client, model_id, image_bytes: bytes, candidates: list[dict]) -> dict:
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 8000,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": base64.b64encode(image_bytes).decode()}},
                {"type": "text", "text": PROMPT + "\n\nהמועמדים:\n"
                 + json.dumps(candidates, ensure_ascii=False, indent=1)},
            ],
        }],
        # prefill forces the response to open as JSON — no preamble to strip
        "system": "You return JSON only.",
    }
    resp = client.invoke_model(modelId=model_id, body=json.dumps(body))
    payload = json.loads(resp["body"].read())
    text = "".join(b.get("text", "") for b in payload["content"])
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    return json.loads(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("registry_dir")
    ap.add_argument("--model", default="eu.anthropic.claude-sonnet-4-5-20250929-v1:0")
    ap.add_argument("--region", default=None)
    a = ap.parse_args()

    reg = Path(a.registry_dir)
    draft = json.loads((reg / "schema.draft.json").read_text(encoding="utf-8"))
    client = boto3.client("bedrock-runtime", region_name=a.region)

    by_page: dict[int, list] = {}
    for c in draft["fields"]:
        by_page.setdefault(c["page"], []).append(c)

    merged = {}
    for pno, cands in sorted(by_page.items()):
        img = (reg / f"page_{pno}.annotated.png").read_bytes()
        # only what the model needs to reason — no coordinates leak into the prompt
        slim = [{"index": i, "kind": c["kind"],
                 "auto_label": c["label_he"], "cells": c.get("cells"),
                 "glyph": c.get("glyph"), "hint": c.get("guessed_type")}
                for i, c in enumerate(cands)]
        print(f"page {pno}: {len(slim)} candidates → {a.model}")
        out = call(client, a.model, img, slim)
        for item in out["fields"]:
            idx = item["index"]
            if 0 <= idx < len(cands):
                merged[cands[idx]["id"]] = item

    (reg / "labels.bedrock.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    low = [k for k, v in merged.items() if v.get("confidence", 0) < 0.75 or v.get("note")]
    print(f"labelled {len(merged)} candidates → {reg}/labels.bedrock.json")
    print(f"{len(low)} need a human look in calibrate.html")


if __name__ == "__main__":
    main()
