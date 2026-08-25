"""Step 2 of ingest: raw extraction.

Produces field *candidates* plus page rasters. It deliberately does not try
to be smart about labels — that's the enrich step's job. Here we only pull
out what the file structurally tells us.

  pdf_acroform  field names, types, checkbox on-states, choice options and
                widget rects, straight from the PDF
  pdf_flat      nothing but page images; detection happens in enrich
  docx          convert to PDF for display, collect placeholder tokens
"""
import io
import logging
import subprocess
import tempfile
from pathlib import Path

from common import config
from common.aws import s3
from common.store import update_session

log = logging.getLogger()
log.setLevel(logging.INFO)


def lambda_handler(event, _context):
    sid = event["session_id"]
    doc_type = event["doc_type"]
    body = s3().get_object(Bucket=config.DOCS_BUCKET, Key=event["key"])["Body"].read()

    if doc_type == "docx":
        body = _docx_to_pdf(body)
        key = f"derived/{sid}/source.pdf"
        s3().put_object(Bucket=config.ARTIFACTS_BUCKET, Key=key, Body=body,
                        ContentType="application/pdf", ServerSideEncryption="aws:kms")
        event["render_pdf_key"] = key

    # Page images are session-scoped (derived/{sid}/...), not part of the
    # schema-registry cache — a cache hit on the schema still needs its own
    # rasters for the viewer, so only the acroform candidate scan (which
    # feeds enrich's reconciliation, itself skipped on a cache hit) is
    # skippable here.
    candidates = [] if event.get("cache_hit") else (
        _acroform_fields(body) if doc_type == "pdf_acroform" else []
    )
    page_keys = _rasterize(sid, body)

    update_session(sid, page_keys=page_keys, page_count=len(page_keys), progress="enriching")
    log.info("extracted %d candidates over %d pages", len(candidates), len(page_keys))
    return {**event, "candidates": candidates, "page_keys": page_keys}


# ------------------------------------------------------------------ acroform

def _acroform_fields(body: bytes) -> list[dict]:
    """Walk the AcroForm and its widget annotations.

    Radio groups are the awkward case: the group itself has /Kids and no
    rect, so each option's rect comes from the individual widget's /AP/N
    on-state.
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(body))
    fields = reader.get_fields() or {}

    simple: dict[str, dict] = {}
    radio_names: set[str] = set()

    for name, f in fields.items():
        if f.get("/Kids"):
            if f.get("/FT") == "/Btn":
                radio_names.add(name)
            continue
        simple[name] = _describe(name, f)

    radios: dict[str, dict] = {}

    for page_index, page in enumerate(reader.pages):
        pw = float(page.mediabox.width) or 1.0
        ph = float(page.mediabox.height) or 1.0
        for ann in page.get("/Annots", []) or []:
            try:
                obj = ann.get_object()
            except Exception:
                continue
            name = _full_name(obj)
            rect = obj.get("/Rect")

            if name in simple:
                simple[name]["page"] = page_index + 1
                simple[name]["rect"] = [float(x) for x in rect] if rect else None
                simple[name]["bbox"] = _norm(rect, pw, ph)
            elif name in radio_names:
                try:
                    on = [v for v in obj["/AP"]["/N"] if v != "/Off"]
                except (KeyError, TypeError):
                    continue
                if len(on) != 1:
                    continue
                grp = radios.setdefault(name, {
                    "name": name, "field_id": _slug(name), "type": "radio_group",
                    "page": page_index + 1, "radio_options": [], "bbox": None,
                })
                grp["radio_options"].append({"value": on[0], "bbox": _norm(rect, pw, ph)})
                if grp["bbox"] is None:
                    grp["bbox"] = _norm(rect, pw, ph)

    out = [f for f in simple.values() if f.get("page")]
    out.extend(radios.values())
    out.sort(key=lambda f: (f.get("page", 0), f.get("bbox") or [0, 0, 0, 0])[0:2])
    return out


def _describe(name: str, f: dict) -> dict:
    ft = f.get("/FT")
    d = {"name": name, "field_id": _slug(name)}
    if ft == "/Tx":
        d["type"] = "text"
        if f.get("/MaxLen"):
            d["max_length"] = int(f["/MaxLen"])
    elif ft == "/Btn":
        d["type"] = "checkbox"
        states = f.get("/_States_", []) or []
        if len(states) == 2:
            if "/Off" in states:
                d["checked_value"] = states[0] if states[0] != "/Off" else states[1]
                d["unchecked_value"] = "/Off"
            else:
                d["checked_value"], d["unchecked_value"] = states[0], states[1]
    elif ft == "/Ch":
        d["type"] = "select"
        states = f.get("/_States_", []) or []
        d["options"] = [s[0] if isinstance(s, (list, tuple)) else s for s in states]
    else:
        d["type"] = "text"
    return d


def _full_name(ann) -> str | None:
    parts, node = [], ann
    while node is not None:
        t = node.get("/T")
        if t:
            parts.append(str(t))
        node = node.get("/Parent")
        if node is not None:
            try:
                node = node.get_object()
            except Exception:
                break
    return ".".join(reversed(parts)) if parts else None


def _norm(rect, pw: float, ph: float):
    """PDF rect [left, bottom, right, top] with a bottom-left origin, to
    normalized [x0, y0, x1, y1] with a top-left origin."""
    if not rect:
        return [0.0, 0.0, 0.0, 0.0]
    left, bottom, right, top = (float(x) for x in rect)
    return [
        round(min(left, right) / pw, 5),
        round(1.0 - max(top, bottom) / ph, 5),
        round(max(left, right) / pw, 5),
        round(1.0 - min(top, bottom) / ph, 5),
    ]


def _slug(name: str) -> str:
    keep = [c if (c.isalnum() or c in "._-") else "_" for c in str(name)]
    return "".join(keep)[:80] or "field"


# ----------------------------------------------------------------- rasterize

def _rasterize(sid: str, body: bytes) -> list[str]:
    """Page images for the vision pass and for the viewer.

    pypdfium2 ships as a self-contained wheel — no poppler binary, no
    ImageMagick, so it works in a plain Lambda zip.
    """
    import pypdfium2 as pdfium

    keys = []
    pdf = pdfium.PdfDocument(body)
    scale = config.RASTER_DPI / 72.0
    for i in range(min(len(pdf), config.MAX_INGEST_PAGES)):
        bitmap = pdf[i].render(scale=scale)
        buf = io.BytesIO()
        bitmap.to_pil().save(buf, format="PNG", optimize=True)
        key = f"derived/{sid}/page-{i + 1:03d}.png"
        s3().put_object(Bucket=config.ARTIFACTS_BUCKET, Key=key, Body=buf.getvalue(),
                        ContentType="image/png", ServerSideEncryption="aws:kms")
        keys.append(key)
    return keys


# ---------------------------------------------------------------- docx->pdf

def _docx_to_pdf(body: bytes) -> bytes:
    """LibreOffice headless. Needs a container image, not a zip — the binary
    is ~400MB, well past the 250MB unzipped layer limit.
    """
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.docx"
        src.write_bytes(body)
        subprocess.run(
            ["soffice", "--headless", "--norestore", "--convert-to", "pdf",
             "--outdir", tmp, str(src)],
            check=True, capture_output=True, timeout=180,
            env={"HOME": "/tmp", "PATH": "/usr/bin:/usr/local/bin"},
        )
        return (Path(tmp) / "in.pdf").read_bytes()
