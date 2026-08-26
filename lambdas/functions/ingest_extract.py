"""Step 2 of ingest: raw extraction.

Produces field *candidates* plus page rasters. It deliberately does not try
to be smart about labels — that's the enrich step's job. Here we only pull
out what the file structurally tells us.

  pdf_acroform  field names, types, checkbox on-states, choice options and
                widget rects, straight from the PDF
  pdf_flat      page images, plus the page's ruled cells and text geometry —
                the *candidate regions* a person could write in. Which field
                goes in which region is enrich's job; where the regions are is
                answered here, from the PDF, and never guessed.
  docx          convert to PDF for display, collect placeholder tokens
"""
import io
import json
import logging
import subprocess
import tempfile
from pathlib import Path

from common import config, geometry as geo, ocr
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

    # Region scanning only earns its keep on the flat path. An AcroForm already
    # carries exact widget rects, and a cache hit is reusing a schema whose
    # geometry was settled the first time round.
    scan = not event.get("cache_hit") and doc_type != "pdf_acroform"
    # Set when a form is being defined rather than merely filled — a catalog
    # rebuild, or an explicit re-ingest. It buys a Textract pass per page, which
    # is worth paying once for a document every later session inherits and not
    # worth paying on an ordinary upload.
    define_time = bool(event.get("define_time"))
    page_keys, annotated_keys, pages = _rasterize(sid, body, scan=scan,
                                                  define_time=define_time)

    out = {**event, "candidates": candidates, "page_keys": page_keys}
    scanned = {}
    if scan:
        # Written to S3 rather than carried on the event: Step Functions caps
        # state at 256KB and a dense form's region table goes straight past it.
        out["regions_key"] = _put_json(f"derived/{sid}/regions.json", pages)
        out["annotated_keys"] = annotated_keys
        # Recorded on the session too, not just passed down the pipeline. When a
        # form comes out wrong the first question is "what did ingest actually
        # see", and answering it used to mean having the PDF and running the
        # geometry by hand. These make it one GET.
        scanned = {"annotated_keys": annotated_keys,
                   "region_count": sum(len(p["regions"]) for p in pages)}
        log.info("scanned %d candidate regions over %d pages",
                 scanned["region_count"], len(pages))

    update_session(sid, page_keys=page_keys, page_count=len(page_keys),
                   progress="enriching", **scanned)
    log.info("extracted %d candidates over %d pages", len(candidates), len(page_keys))
    return out


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
                simple[name]["bbox"] = geo.norm(rect, pw, ph)
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
                grp["radio_options"].append({"value": on[0], "bbox": geo.norm(rect, pw, ph)})
                if grp["bbox"] is None:
                    grp["bbox"] = geo.norm(rect, pw, ph)

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


def _slug(name: str) -> str:
    keep = [c if (c.isalnum() or c in "._-") else "_" for c in str(name)]
    return "".join(keep)[:80] or "field"


# ----------------------------------------------------------------- rasterize

def _rasterize(sid: str, body: bytes, scan: bool = False,
               define_time: bool = False) -> tuple[list[str], list[str], list[dict]]:
    """Page images for the viewer, and — when `scan` — the page geometry and a
    numbered copy of each image for the vision pass.

    pypdfium2 ships as a self-contained wheel — no poppler binary, no
    ImageMagick, so it works in a plain Lambda zip. It is already open here for
    rendering, so reading the text and path geometry off the same page objects
    costs one extra pass over a document we have in memory anyway.

    Two images per page, deliberately. The clean one is what the person sees in
    the viewer; the annotated one, with a red number in every candidate region,
    goes only to the model. Drawing region ids into the image the user looks at
    would be answering a question nobody asked.
    """
    import pypdfium2 as pdfium

    keys, annotated, pages = [], [], []
    pdf = pdfium.PdfDocument(body)
    scale = config.RASTER_DPI / 72.0

    for i in range(min(len(pdf), config.MAX_INGEST_PAGES)):
        page = pdf[i]
        buf = io.BytesIO()
        page.render(scale=scale).to_pil().save(buf, format="PNG", optimize=True)
        png = buf.getvalue()
        keys.append(_put_png(f"derived/{sid}/page-{i + 1:03d}.png", png))

        if not scan:
            continue

        # A failure here must not take the upload down with it: no regions
        # means enrich falls back to estimating, which is where it started.
        try:
            regions = geo.candidate_regions(page)
            text = geo.text_boxes(page)
        except Exception:
            log.exception("geometry scan failed on page %d", i + 1)
            regions, text = [], []

        # Textract runs in two situations, and neither is "every upload".
        #
        # A page with no text and no rules is a scan: there is nothing in the
        # file to read, so OCR is the only source of geometry there is.
        #
        # Otherwise it runs only while a form is being *defined* — the tier that
        # happens once per document and is inherited by every session after it.
        # Reconstructing cells from rules covers most of a form but not all of
        # it, and Textract's key/value pairs reach the ones it misses; paying a
        # few cents once to place a box correctly for every future filling of
        # that form is the trade this whole pipeline is built around.
        scanned = not text and not regions
        if scanned or define_time:
            log.info("page %d: running Textract (%s)", i + 1,
                     "no text layer" if scanned else "define-time pass")
            regions = geo.merge_regions(regions, ocr.regions_from_image(png))

        pages.append({"page": i + 1, "regions": regions, "text": text})
        annotated.append(_put_png(f"derived/{sid}/page-{i + 1:03d}-regions.png",
                                  geo.annotate(png, regions)))

    return keys, annotated, pages


def _put_png(key: str, body: bytes) -> str:
    s3().put_object(Bucket=config.ARTIFACTS_BUCKET, Key=key, Body=body,
                    ContentType="image/png", ServerSideEncryption="aws:kms")
    return key


def _put_json(key: str, payload) -> str:
    s3().put_object(
        Bucket=config.ARTIFACTS_BUCKET, Key=key,
        Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8", ServerSideEncryption="aws:kms",
    )
    return key


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
