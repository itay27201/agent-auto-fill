"""POST /sessions/{session_id}/render

Produces the filled document and returns a presigned download URL.

Three paths:
  pdf_acroform  set real form-field values with pypdf. Highest fidelity —
                the document keeps its structure and stays machine-readable.
  pdf_flat      stamp a text layer at the field bboxes and merge it in.
  docx          replace placeholder runs in the original Word file.

`flatten` matters: some agencies require a flattened PDF that can no longer
be edited, others require the fillable form intact. Offer both.
"""
import io
import json
import logging

from common import config, schema as sch
from common.api import ApiError, body_of, caller, handler, path_param
from common.aws import s3
from common.store import get_session, get_values, load_schema

log = logging.getLogger()
log.setLevel(logging.INFO)


@handler
def lambda_handler(event, _context):
    sid = path_param(event, "session_id")
    body = body_of(event)
    sess = get_session(sid)
    if not sess:
        raise ApiError("session not found", 404)
    if sess.get("owner") not in (caller(event), "anonymous"):
        raise ApiError("forbidden", 403)
    if sess.get("status") != "ready":
        raise ApiError(f"session is {sess.get('status')}, not ready", 409)

    fields = sch.schema_from_list(load_schema(sess["schema_key"]))
    values = get_values(sid)

    result = sch.validate_all(fields, values)
    if body.get("strict", True):
        if not result["ok"]:
            raise ApiError("form has validation errors", 422, **result)
        if result["awaiting_confirmation"]:
            raise ApiError(
                "some values were drafted by the assistant and not confirmed",
                422,
                awaiting_confirmation=result["awaiting_confirmation"],
            )

    # An uploaded document sits in DocsBucket; a catalog form's master sits in
    # ArtifactsBucket, because DocsBucket expires its entire contents after
    # seven days and a catalog entry has to outlive that.
    src_bucket = sess.get("doc_bucket") or config.DOCS_BUCKET
    src = s3().get_object(Bucket=src_bucket, Key=sess["doc_key"])["Body"].read()
    doc_type = sess.get("doc_type")
    flatten = bool(body.get("flatten", False))

    unplaced: list[str] = []
    if doc_type == "pdf_acroform":
        out, ext = _fill_acroform(src, fields, values, flatten), "pdf"
    elif doc_type == "pdf_flat":
        (out, unplaced), ext = _stamp_overlay(src, fields, values), "pdf"
    elif doc_type == "docx":
        out, ext = _fill_docx(src, fields, values), "docx"
    else:
        raise ApiError(f"cannot render doc_type {doc_type!r}", 500)

    # A strict render promises the exported document matches the form. A value
    # with nowhere to go breaks that promise silently, which is worse than
    # refusing: the person would file a form they believe is complete.
    if unplaced and body.get("strict", True):
        raise ApiError(
            "some filled fields have no known box on the page and would be "
            "dropped from the export — place them in the viewer first",
            422,
            unplaced=[{"field_id": fid,
                       "label": next((f.label for f in fields if f.field_id == fid), fid)}
                      for fid in unplaced],
        )

    key = f"outputs/{sid}/filled.{ext}"
    s3().put_object(
        Bucket=config.ARTIFACTS_BUCKET, Key=key, Body=out,
        ContentType="application/pdf" if ext == "pdf" else
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ServerSideEncryption="aws:kms",
    )
    url = s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": config.ARTIFACTS_BUCKET, "Key": key,
                "ResponseContentDisposition": f'attachment; filename="filled.{ext}"'},
        ExpiresIn=config.DOWNLOAD_URL_TTL,
    )
    return {"download_url": url, "key": key, "flattened": flatten,
            "summary": result, "unplaced": unplaced}


# --------------------------------------------------------------- acroform

def _patch_pypdf_opt():
    """pypdf returns /Opt for choice fields as [[value, label], ...] but
    validates against the raw list. Normalize to values."""
    from pypdf.constants import FieldDictionaryAttributes
    from pypdf.generic import DictionaryObject

    if getattr(DictionaryObject, "_opt_patched", False):
        return
    original = DictionaryObject.get_inherited

    def patched(self, key, default=None):
        result = original(self, key, default)
        if key == FieldDictionaryAttributes.Opt and isinstance(result, list) and all(
            isinstance(v, list) and len(v) == 2 for v in result
        ):
            return [r[0] for r in result]
        return result

    DictionaryObject.get_inherited = patched
    DictionaryObject._opt_patched = True


def _fill_acroform(src: bytes, fields, values, flatten: bool) -> bytes:
    from pypdf import PdfReader, PdfWriter

    _patch_pypdf_opt()
    reader = PdfReader(io.BytesIO(src))
    writer = PdfWriter(clone_from=reader)

    by_page: dict[int, dict] = {}
    for f in fields:
        v = (values.get(f.field_id) or {}).get("value")
        if v in (None, "", []):
            continue
        b = f.backend or {}
        name = b.get("name") or f.field_id

        if f.type == "checkbox":
            # Checkbox "on" states are per-document (/Yes, /1, /On...) —
            # extraction captured the real one; never hardcode "/Yes".
            v = b.get("checked_value", "/Yes") if v else b.get("unchecked_value", "/Off")
        elif f.type == "multiselect" and isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        else:
            v = str(v)

        by_page.setdefault(int(b.get("page", f.page)), {})[name] = v

    for page_no, field_values in by_page.items():
        idx = max(0, page_no - 1)
        if idx < len(writer.pages):
            writer.update_page_form_field_values(
                writer.pages[idx], field_values, auto_regenerate=False
            )

    # Without this, most viewers render the field values as blank.
    writer.set_need_appearances_writer(True)

    if flatten:
        # Drop /AcroForm so the values become static page content.
        try:
            writer._root_object.pop("/AcroForm", None)
        except Exception:
            pass

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- overlay

def _stamp_overlay(src: bytes, fields, values) -> tuple[bytes, list[str]]:
    """Draw a transparent text layer per page and merge it onto the original.

    Returns the document and the ids of any filled field it could not place, so
    the caller can report them rather than shipping a document that is quietly
    missing values.
    """
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas

    reader = PdfReader(io.BytesIO(src))
    writer = PdfWriter()
    font = _register_font()

    per_page: dict[int, list] = {}
    skipped: list[str] = []
    for f in fields:
        v = (values.get(f.field_id) or {}).get("value")
        if v in (None, "", []):
            continue
        # No trustworthy box means no stamp. Drawing it anyway would put the
        # value at the top-left corner of page one, on top of whatever the form
        # prints there — which is the failure this whole path exists to stop.
        # It comes back in the response so the caller can say which fields were
        # dropped instead of the document quietly missing them.
        if f.bbox_confidence == "low" or not any(f.bbox):
            skipped.append(f.field_id)
            continue
        per_page.setdefault(f.page, []).append((f, v))

    if skipped:
        log.warning("not stamped, no known box: %s", ", ".join(skipped))

    for i, page in enumerate(reader.pages, start=1):
        items = per_page.get(i)
        if items:
            w = float(page.mediabox.width)
            h = float(page.mediabox.height)
            buf = io.BytesIO()
            c = canvas.Canvas(buf, pagesize=(w, h))
            for f, v in items:
                _draw_field(c, f, v, w, h, font)
            c.save()
            buf.seek(0)
            page.merge_page(PdfReader(buf).pages[0])
        writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), skipped


def _draw_field(c, f, value, page_w, page_h, font):
    x0, y0, x1, y1 = f.bbox
    # bbox is normalized top-left origin; PDF user space is bottom-left.
    left = x0 * page_w
    right = x1 * page_w
    bottom = (1.0 - y1) * page_h
    top = (1.0 - y0) * page_h
    size = float((f.backend or {}).get("font_size") or min(11.0, max(7.0, (top - bottom) * 0.62)))
    baseline = bottom + (top - bottom - size) / 2 + size * 0.18

    if f.type == "checkbox":
        if value:
            c.setFont("Helvetica-Bold", size)
            c.drawCentredString((left + right) / 2, baseline, "X")
        return

    text = ", ".join(str(x) for x in value) if isinstance(value, list) else str(value)
    rtl = _is_rtl(text)
    c.setFont(font if (rtl and font) else "Helvetica", size)

    if rtl:
        # Hebrew and Arabic need the bidi algorithm applied before drawing;
        # reportlab lays out glyphs strictly left to right, so an unprocessed
        # RTL string comes out reversed. Right-align it too.
        try:
            from bidi.algorithm import get_display
            text = get_display(text)
        except ImportError:
            pass
        c.drawRightString(right - 2, baseline, text)
    else:
        c.drawString(left + 2, baseline, text)


def _is_rtl(text: str) -> bool:
    return any("֐" <= ch <= "ࣿ" for ch in text)


def _register_font():
    """reportlab's built-in fonts have no Hebrew or Arabic glyphs — they
    render as black boxes. Ship a TTF in the layer."""
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        if "FormAgentRTL" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("FormAgentRTL", config.RTL_FONT_PATH))
        return "FormAgentRTL"
    except Exception:
        return None


# ------------------------------------------------------------------- docx

def _fill_docx(src: bytes, fields, values) -> bytes:
    """Replace {{field_id}} placeholders and matching content-control tags.

    Replacement is done run by run to preserve formatting; python-docx will
    happily drop styling if you rewrite paragraph.text wholesale.
    """
    from docx import Document

    doc = Document(io.BytesIO(src))
    repl = {}
    for f in fields:
        v = (values.get(f.field_id) or {}).get("value")
        if v in (None, [], False):
            v = ""
        elif isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        elif v is True:
            v = "X"
        repl[f"{{{{{f.field_id}}}}}"] = str(v)

    def patch(paragraph):
        for run in paragraph.runs:
            for token, val in repl.items():
                if token in run.text:
                    run.text = run.text.replace(token, val)

    for p in doc.paragraphs:
        patch(p)
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    patch(p)
    for section in doc.sections:
        for container in (section.header, section.footer):
            for p in container.paragraphs:
                patch(p)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()
