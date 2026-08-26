"""Roundtrip test with no AWS: build a fillable PDF, run the real extraction
code against it, then run the real fill code and read the values back.

This is the part most likely to break silently in production — checkbox
on-states and radio groups are per-document, and a wrong guess produces a
PDF that looks filled but exports blank.
"""
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def build_form(path: str):
    c = canvas.Canvas(path, pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Bold", 14)
    c.drawString(60, h - 60, "Application for Residence Permit")

    c.setFont("Helvetica", 10)
    c.drawString(60, h - 110, "Family name")
    c.acroForm.textfield(name="Text14", x=180, y=h - 118, width=250, height=18,
                         borderStyle="underlined", forceBorder=True)

    c.drawString(60, h - 145, "National ID number")
    c.acroForm.textfield(name="Text15", x=180, y=h - 153, width=180, height=18,
                         maxlen=9, borderStyle="underlined", forceBorder=True)

    c.drawString(60, h - 185, "Marital status")
    c.acroForm.choice(name="Choice3", value="Single",
                      options=["Single", "Married", "Divorced", "Widowed"],
                      x=180, y=h - 195, width=150, height=20, forceBorder=True)

    c.drawString(60, h - 230, "I consent to data processing")
    c.acroForm.checkbox(name="CheckBox7", x=300, y=h - 234, size=14,
                        checked=False, buttonStyle="check", forceBorder=True)

    c.drawString(60, h - 270, "Preferred contact")
    c.acroForm.radio(name="Radio9", value="Email", selected=False,
                     x=180, y=h - 274, size=14, buttonStyle="circle",
                     shape="circle", forceBorder=True)
    c.drawString(200, h - 270, "Email")
    c.acroForm.radio(name="Radio9", value="Phone", selected=False,
                     x=280, y=h - 274, size=14, buttonStyle="circle",
                     shape="circle", forceBorder=True)
    c.drawString(300, h - 270, "Phone")

    c.save()


def main():
    out = Path(tempfile.gettempdir()) / "formtest"
    out.mkdir(exist_ok=True)
    src_path = out / "blank.pdf"
    build_form(str(src_path))
    src = src_path.read_bytes()

    # ---- classification -------------------------------------------------
    from functions.ingest_classify import _detect
    doc_type, pages = _detect(src, "blank.pdf")
    print(f"classify   -> {doc_type}, {pages} page(s)")
    assert doc_type == "pdf_acroform", doc_type

    # ---- extraction -----------------------------------------------------
    from functions.ingest_extract import _acroform_fields
    cands = _acroform_fields(src)
    print(f"extract    -> {len(cands)} candidates")
    for c in cands:
        bits = [f"{c['name']!r}", c.get("type", "?"), f"p{c.get('page')}"]
        if c.get("checked_value"):
            bits.append(f"on={c['checked_value']}")
        if c.get("options"):
            bits.append(f"options={c['options']}")
        if c.get("radio_options"):
            bits.append(f"radio={[o['value'] for o in c['radio_options']]}")
        bb = c.get("bbox") or []
        bits.append("bbox=[" + ", ".join(f"{v:.3f}" for v in bb) + "]")
        print("             ", " ".join(bits))

    by_name = {c["name"]: c for c in cands}
    assert set(by_name) >= {"Text14", "Text15", "Choice3", "CheckBox7"}, list(by_name)
    assert by_name["CheckBox7"].get("checked_value"), "checkbox on-state not captured"
    assert "Radio9" in by_name and by_name["Radio9"]["type"] == "radio_group", "radio group missed"
    for c in cands:
        bb = c.get("bbox")
        assert bb and all(0.0 <= v <= 1.0 for v in bb), f"bbox out of range: {c['name']} {bb}"

    # ---- simulate the enrich reconcile ---------------------------------
    from functions.ingest_enrich import _reconcile
    enriched = [
        {"field_id": "Text14", "label": "Family name", "section": "Personal", "required": True,
         "help": "As printed on your passport."},
        {"field_id": "Text15", "label": "National ID number", "section": "Personal",
         "required": True, "validation": r"\d{9}", "help": "Nine digits, no dashes."},
        {"field_id": "Choice3", "label": "Marital status", "section": "Personal"},
        {"field_id": "CheckBox7", "label": "Consent to data processing", "section": "Declarations",
         "required": True},
        {"field_id": "Radio9", "label": "Preferred contact method", "section": "Contact"},
    ]
    fields_raw = _reconcile(cands, enriched)
    print(f"reconcile  -> {len(fields_raw)} fields")

    from common import schema as sch
    fields = sch.schema_from_list(fields_raw)
    by_id = {f.field_id: f for f in fields}
    assert by_id["Radio9"].type == "select", by_id["Radio9"].type
    assert by_id["Radio9"].options, "radio options not promoted to select options"
    print(f"             Radio9 -> select {by_id['Radio9'].options}")

    # ---- validation ------------------------------------------------------
    err = sch.validate_value(by_id["Text15"], "12345")
    assert err, "short ID should fail the regex"
    print(f"validate   -> rejects '12345': {err}")
    assert sch.validate_value(by_id["Text15"], "123456789") is None
    assert sch.validate_value(by_id["Choice3"], "Martian"), "bad option should fail"
    assert sch.validate_value(by_id["Choice3"], "Married") is None

    # ---- fill -------------------------------------------------------------
    values = {
        "Text14": {"value": "Cohen", "source": "user"},
        "Text15": {"value": "123456789", "source": "user"},
        "Choice3": {"value": "Married", "source": "agent"},
        "CheckBox7": {"value": True, "source": "user"},
        "Radio9": {"value": by_id["Radio9"].options[0], "source": "user"},
    }
    from functions.api_render import _fill_acroform
    filled = _fill_acroform(src, fields, values, flatten=False)
    (out / "filled.pdf").write_bytes(filled)

    from pypdf import PdfReader
    got = {k: v.get("/V") for k, v in (PdfReader(io.BytesIO(filled)).get_fields() or {}).items()}
    print(f"fill       -> wrote {len(filled)} bytes")
    for k, v in got.items():
        print(f"             {k} = {v!r}")

    assert str(got.get("Text14")) == "Cohen", got.get("Text14")
    assert str(got.get("Text15")) == "123456789"
    assert str(got.get("Choice3")) == "Married"
    assert str(got.get("CheckBox7")) == by_name["CheckBox7"]["checked_value"], got.get("CheckBox7")

    # ---- overlay path on the same doc ------------------------------------
    from functions.api_render import _stamp_overlay
    stamped, unplaced = _stamp_overlay(src, fields, values)
    (out / "stamped.pdf").write_bytes(stamped)
    assert len(stamped) > 1000
    assert unplaced == [], unplaced
    print(f"overlay    -> wrote {len(stamped)} bytes")

    # A field whose box was rejected at ingest must be reported, not stamped at
    # the origin on top of whatever the form prints there.
    lost = [sch.FormField(field_id="ghost", label="Nowhere", page=1,
                          bbox=[0, 0, 0, 0], bbox_confidence="low")]
    _, missing = _stamp_overlay(src, fields + lost,
                                {**values, "ghost": {"value": "Bleader"}})
    assert missing == ["ghost"], missing
    print("overlay    -> unplaced field reported, not drawn")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
