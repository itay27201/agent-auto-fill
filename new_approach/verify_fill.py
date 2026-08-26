#!/usr/bin/env python3
"""
verify_fill.py — the accuracy loop. Run after every render, before the user sees anything.

Crops each filled field out of the GENERATED pdf and asks Claude what it sees.
This is the only check that catches the failure mode nothing else catches:
the value was correct, the validator passed, and it landed in the wrong box.

    python verify_fill.py out/crops/manifest.json --region eu-west-1
"""

from __future__ import annotations

import argparse
import base64
import json
import re
from pathlib import Path

import boto3

TEXT_PROMPT = """\
בתמונה מוצג קטע מטופס ממשלתי בעברית עם ערך שמולא בו.
התווית של השדה היא: "{label}"

החזר JSON בלבד:
{{"value": "<הערך שמולא, בדיוק כפי שהוא נראה>",
  "inside_field": true|false,
  "legible": true|false,
  "confidence": 0.0-1.0}}

inside_field=false אם הערך חורג מגבולות השדה, נכתב על גבי טקסט מודפס,
או נראה שהוא שייך לשדה סמוך.
"""

MARK_PROMPT = """\
בתמונה מוצגת קבוצת תיבות סימון מטופס בעברית. התווית של הקבוצה: "{label}"

החזר JSON בלבד:
{{"marked_labels": ["<התווית של כל תיבה מסומנת>"],
  "marked_count": <מספר>,
  "any_mark_outside_box": true|false,
  "confidence": 0.0-1.0}}
"""


def ask(client, model, png: Path, prompt: str) -> dict:
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 400, "temperature": 0,
        "system": "You return JSON only.",
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(png.read_bytes()).decode()}},
            {"type": "text", "text": prompt},
        ]}],
    }
    r = client.invoke_model(modelId=model, body=json.dumps(body))
    txt = "".join(b.get("text", "") for b in json.loads(r["body"].read())["content"])
    txt = txt.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    return json.loads(txt)


def norm(s) -> str:
    return re.sub(r"[\s\-/.,]", "", str(s))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--model", default="eu.anthropic.claude-sonnet-4-5-20250929-v1:0")
    ap.add_argument("--region", default=None)
    ap.add_argument("--report", default="out/verification.json")
    a = ap.parse_args()

    items = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    client = boto3.client("bedrock-runtime", region_name=a.region)

    results, failures = [], 0
    for it in items:
        is_mark = it["type"] in ("checkbox", "radio_option")
        prompt = (MARK_PROMPT if is_mark else TEXT_PROMPT).format(label=it["label_he"])
        seen = ask(client, a.model, Path(it["crop"]), prompt)

        if is_mark:
            ok = (seen.get("marked_count") == 1
                  and not seen.get("any_mark_outside_box")
                  and any(norm(it["label_he"]) == norm(m) for m in seen.get("marked_labels", [])))
        else:
            ok = (norm(seen.get("value")) == norm(it["expected"])
                  and seen.get("inside_field") and seen.get("legible"))

        failures += (not ok)
        results.append({"field_id": it["field_id"], "expected": it["expected"],
                        "observed": seen, "pass": ok})
        print(f"{'PASS' if ok else 'FAIL'}  {it['field_id']:<28} "
              f"expected={it['expected']!r} observed={seen}")

    Path(a.report).parent.mkdir(parents=True, exist_ok=True)
    Path(a.report).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    n = len(results)
    print(f"\n{n - failures}/{n} passed  ({(n - failures) / max(n,1):.1%})")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
