# Form Registry — pipeline למילוי טפסים ממשלתיים בדיוק גבוה

עיקרון אחד: **ה-LLM לעולם לא מייצר קואורדינטות.** הוא בוחר `field_id` מתוך enum
סגור; הקוד עושה את כל השאר.

```
                    ┌─ offline, פעם אחת לכל גרסת טופס ─────────────────┐
PDF ──► extract_form_schema.py ──► schema.draft.json + page_N.annotated.png
                                          │
                                          ├──► label_with_bedrock.py ──► labels.bedrock.json
                                          │        (Claude Vision — תוויות בלבד)
                                          ▼
                                    calibrate.html  (אישור אנושי)
                                          │
                                          ▼
                                    schema.json  ◄── קפוא + pdf_sha256
                    └──────────────────────────────────────────────────┘
                                          │
                    ┌─ runtime ────────────┼───────────────────────────┐
צ'אט ──► Claude + agent_tools.py ──► set_field(field_id, value)
                                          ▼
                                    validators.py   ──(שגיאה)──► שאלת הבהרה למשתמש
                                          ▼
                                    render_fill.py  ──► filled.pdf + crops/
                                          ▼
                                    verify_fill.py  (Claude Vision קורא בחזרה)
                                          ▼
                                   אישור המשתמש ──► flatten ──► הגשה
                    └──────────────────────────────────────────────────┘
```

## למה לא Textract

Textract לא תומך בעברית — חילוץ טפסים וטבלאות מוגבל לאנגלית, ספרדית, גרמנית,
איטלקית, צרפתית ופורטוגזית. לטופס 101 זה לא משנה ממילא: הוא PDF וקטורי מ-Illustrator
עם שכבת טקסט מלאה, אז PyMuPDF נותן bbox מדויק לכל גליף בלי שום OCR.

## התקנה

```bash
pip install pymupdf python-bidi boto3
# פונט עברי לרינדור:
# /usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf
# בפרודקשן: הטמע Noto Sans Hebrew / Rubik ב-Lambda layer או container image
```

## הרצה מקצה לקצה

```bash
# A1 — חילוץ גיאומטרי דטרמיניסטי
python extract_form_schema.py itc101.pdf -o registry/itc101 --form-id itc101 --rtl

# A2 — תיוג סמנטי (אופציונלי אך מומלץ; דורש Bedrock)
export AWS_REGION=eu-west-1
python label_with_bedrock.py registry/itc101

# A3 — כיול אנושי: פתח calibrate.html, טען schema.draft.json + page_N.png
#      (+ labels.bedrock.json), אשר/תקן, ייצא schema.json
#      לצורך הדגמה בלבד, promote_demo.py מייצר schema.json חלקי בקוד:
python promote_demo.py

# B — מילוי
python agent_tools.py registry/itc101/schema.json > tools.json
python render_fill.py itc101.pdf registry/itc101/schema.json values.json \
    -o out/filled.pdf --crops out/crops --verify-manifest out/crops/manifest.json

# אימות ויזואלי
python verify_fill.py out/crops/manifest.json
```

## מה קרה בפועל על טופס 101

```
page 0:  36 checkbox   27 comb   17 underline   12 groups   60 auto-labelled
page 1:  31 checkbox    0 comb   24 underline    8 groups   50 auto-labelled
```

67 תיבות סימון נמצאו **בוודאות מוחלטת** — הן גליפי ZapfDingbats (`o`, `q`) בשכבת
הטקסט, כלומר ה-bbox שלהן מגיע ישירות מה-content stream של ה-PDF. אין כאן זיהוי,
אין ניחוש, אין סף.

שיוך התוויות בעברית עובד כי בטופס RTL קצה ה-**ימני** של הכיתוב נוגע בקצה ה-שמאלי
של הסימון בדיוק (`label.x1 == mark.x0`, נבדק). 110 מתוך 135 מועמדים קיבלו תווית
אוטומטית נכונה.

### שתי המחשות למה שלב A3 קיים

1. **`מספר זהות` — הגיאומטריה מצאה 8 תאים במקום 9.** הקצה הימני של השדה הוא גבול
   הסקציה, לא קו מפריד מצויר, אז אין וקטור לזהות. `LABEL_LEXICON` תופס את זה
   ומסמן `MISMATCH` עם confidence 0.3 במקום להעביר את זה בשקט הלאה. התיקון
   בכיול: `left=438.10, pitch=11.35, cells=9` → הקצה יוצא בדיוק על 540.25, שהוא
   גבול הסקציה. זה מאמת את התיקון.

2. **`מצב משפחתי` פוצל לשתי קבוצות.** חמש האפשרויות מסודרות בגריד 2×3, אז הקיבוץ
   האוטומטי יצר עמודה אחת (`רווק/ה` + `אלמן/ה`) ושורה אחת (`גרוש/ה` + `נשוי/אה`),
   ו-`פרוד/ה` נשאר בודד. אף היוריסטיקה גיאומטרית לא תפתור את זה נכון — צריך אדם
   או Vision. ב-`promote_demo.py` הן מאוחדות ידנית לקבוצת radio אחת.

## שכבות ההגנה

| שכבה | תופס |
|---|---|
| `field_id` enum בכלי | שדה שהמודל המציא |
| `pdf_sha256` | הטופס עודכן ברשות המסים והקואורדינטות כבר לא תקפות |
| `validators.py` | ספרת ביקורת ת"ז, פורמט תאריך, אורך, טווח |
| `check_dependencies` | שתי אפשרויות מסומנות ב-radio, שדה חובה מותנה שלא מולא |
| `draw_comb` overflow | ערך ארוך מכמות התאים |
| `verify_fill.py` | הערך נכון, עבר ולידציה, ונחת בתיבה הלא נכונה |

נבדק בפועל:

```
GUARD 1 (radio): בקבוצה 'מצב משפחתי' סומנו 2 אפשרויות — מותרת אחת בלבד.
GUARD 2 (hash):  PDF hash does not match the schema. The form was revised…
GUARD 3 (enum):  unknown field ids: ['employee_shoe_size']
```

## מבנה שדה ב-schema.json

```jsonc
{
  "id": "employee_id",
  "label_he": "מספר זהות של העובד/ת",
  "page": 0,
  "type": "comb",                    // comb | text | radio_option | checkbox
  "validator": "israeli_id",
  "cells": 9,
  "fill_direction": "ltr",           // ספרות תמיד LTR גם בטופס RTL
  "cell_boxes": [[438.1,220,449.45,236], …],
  "bbox": [438.1, 220.0, 540.25, 236.0]
}
```

```jsonc
{
  "id": "marital_status__married",
  "type": "radio_option",
  "group": "marital_status",
  "value": "married",
  "mark_bbox": [430.19, 277.91, 438.57, 288.91],
  "mark_style": "X",
  "group_bbox": [358.9, 273.9, 501.7, 305.9]   // מה שנשלח לאימות ויזואלי
}
```

`group_bbox` הוא הפרט הקטן שמשנה: מאמתים את **הקבוצה כולה**, לא את התיבה הבודדת,
כי כך תופסים גם סימון שדלף לתיבה הסמוכה וגם שתי תיבות מסומנות.

## מדידה

בנה golden set של 40–60 תרחישים שמכסים כל שדה וכל ענף תלות (רווק בלי ילדים,
נשוי+3, הורה יחיד, תושב יישוב מזכה, חייל משוחרר). שלוש מטריקות **נפרדות**,
פר-שדה ולא ממוצע:

- **field exact match** — הערך נכון אחרי ולידציה
- **placement accuracy** — מרכז הערך שנכתב נמצא בתוך ה-bbox היעד
- **group integrity** — אין שתי תיבות ב-radio, אין שדה חובה ריק כשהתלות פעילה

`verify_fill.py` מחזיר exit code 1 בכשלון, אז אפשר להריץ אותו כשלב ב-CI על כל
שינוי של סכמה, prompt או מודל.

## פריסה ב-AWS

| רכיב | היכן |
|---|---|
| `extract_form_schema.py`, `label_with_bedrock.py` | job חד-פעמי (CodeBuild / Fargate) |
| `calibrate.html` | סטטי על S3 + CloudFront |
| `schema.json` + PDF מקור | S3, immutable, גרסה לפי `pdf_sha256` |
| `agent_tools.py` + orchestrator | Lambda, קורא ל-Bedrock |
| `validators.py` | אותו Lambda |
| `render_fill.py` | Lambda **נפרד** כ-container image (PyMuPDF + פונטים עבריים לא נכנסים ל-zip) |
| `verify_fill.py` | Step Functions: fill → render → verify → retry |
| מצב שיחה | DynamoDB, `{session_id → {field_id: value}}` |

## המהלך שכדאי לשקול אחרי הכיול

אחרי ש-`schema.json` מאושר, המר את הטופס פעם אחת ל-AcroForm אמיתי
(`page.add_widget()` עם `field_name = f["id"]`). מאותו רגע המילוי הוא לפי **שם
שדה** ולא לפי קואורדינטה, בלעדיות radio נאכפת ע"י ה-PDF עצמו, והמשתמש יכול לערוך
ידנית בכל viewer — בדיוק הדרישה על עריכה ידנית לצד הצ'אט. הקואורדינטות נצרבות
פעם אחת ואף פעם לא נוגעים בהן שוב בזמן ריצה.

## סדר עבודה

כייל את 101 בלבד → המר ל-AcroForm → golden set של 20 תרחישים → orchestrator עם
5 שדות → מדוד → הרחב. אל תיגע בטופס שני עד ש-101 עומד על 98%+ placement accuracy.
