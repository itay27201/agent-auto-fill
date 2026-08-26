"""Step 3 of ingest: turn raw extraction into a usable schema.

This is the only expensive step, and it runs once per distinct document because
of the registry cache — which is exactly why it runs on the *ingest* model tier
rather than the chat one. Deciding which ruled cell a Hebrew label belongs to is
a judgement made once and inherited by every session that form ever has.

Two prompts, one output shape:

  pdf_acroform  we already have field names, types and geometry, but names like
                "Text14" are useless to the agent. The model looks at the page
                images and supplies human labels, sections, help text and
                validation. It must not invent or drop fields.

  pdf_flat      no field structure — but `ingest_extract` has read the page's
                ruled cells and text out of the PDF and numbered every place a
                person could write. The model is shown those numbers drawn onto
                the page and picks one per field. It does not return
                coordinates, so it cannot return wrong ones.

That last change is the point of this module. Asking a vision model to estimate
four normalized floats per field is asking it to do the one thing it is worst
at, and on the Israeli 101 form it produced boxes that were confidently wrong —
the employer's name stamped over the instructions paragraph. Picking a numbered
region off an image is character recognition, which it is good at, and the
answer is exact by construction because the coordinates come from the PDF.

Flat documents are also processed one page per model call rather than the whole
document in one. That is what retires the `ENRICH_MAX_TOKENS` truncation the old
single call kept hitting, and it means a failed page costs a page.
"""
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from common import config, form_map, geometry as geo
from common.aws import bedrock, s3
from common.llm_json import invoke_json
from common.store import update_session

log = logging.getLogger()
log.setLevel(logging.INFO)

SYSTEM = """You analyze government and administrative forms and describe every input a person must complete.

Return ONLY a JSON array. No prose, no markdown fences.

Each element:
{
  "field_id": "stable_snake_case_id",
  "label": "the visible label, in the document's own language",
  "type": "text|textarea|number|date|select|multiselect|checkbox|signature",
  "page": 1,
  "region_id": 12,
  "section": "the heading this field sits under",
  "required": true,
  "options": ["..."],
  "validation": "regex, or empty string",
  "help": "plain-language explanation of what this field wants",
  "max_length": null
}

Rules:
- Keep labels in the source language. Do not translate them.
- field_id must be unique across the whole form and must say which field it is.
  A form that labels three different boxes "שם" needs employer_name,
  employee_first_name and health_fund_name — never name_2 and name_3. The
  people using these ids cannot see the page.
- "help" is for someone who has never filled this form. Explain what the field
  wants and where to find the information. Write it in the same language as the
  form.
- Set "required" true only where the form marks it or clearly implies it.
- Use "validation" for well-defined formats (national ID, postal code, phone).
  Leave it empty if unsure — a wrong regex blocks a correct answer.
- Checkboxes that are mutually exclusive should be one "select" field with the
  choices in "options", not several checkboxes.
- Never invent a field that is not visible in the document."""

ACRO_TASK = """This PDF has real form fields. Their ids, types and positions are already known and are AUTHORITATIVE — do not change field_id, type, page or bbox, and do not add or remove entries.

Your job is to look at the page images and fill in the human-readable parts: label, section, required, help, validation, and options where the extracted list has none.

Return one element per entry in the extracted list below, in the same order, preserving field_id, type, page and bbox exactly. Return "bbox" as given rather than "region_id".

Extracted fields:
"""

FLAT_TASK = """This document has no form fields. The page has been scanned for the places a person could write — ruled cells and underlines with no printed text of their own — and each one is outlined in red with its **region id** printed in the corner. The list of regions below gives, for each id, the text printed nearest to it and on which side.

Identify every input a person must complete on THIS PAGE, and give each one the `region_id` of the box it should be written into.

- Read the region id off the image, and check it against the list below.
- A region with an `own_label` is a box printed directly underneath that caption, read out of the document itself. It is not a guess and it outranks `nearby_text` and the image: if you are looking for the field captioned X, the region whose `own_label` is X is that field, full stop.
- `nearby_text` is only what happens to be printed around a box. On a table row it frequently names the column *beside* this one, so use it to break a tie, never to overrule an `own_label`.
- Some regions carry a `textract_label`: a form-reader's guess at which printed label that box belongs to. Treat it as a strong hint, not as fact — it is often right and is occasionally attached to the wrong box, so confirm it against the image before you trust it, and ignore it when the image disagrees.
- On a right-to-left form the label is usually the text to the RIGHT of, or directly ABOVE, the box that belongs to it.
- A region marked `"kind": "checkbox"` is one of the form's printed tick squares. It takes an X, never text, so the field that claims it must be "checkbox" — or, where several of them are the mutually exclusive answers to one question, one "select" field whose options are those choices. Its `nearby_text` is the choice it stands for.
- A region with `character_cells` is printed as that many separate boxes, one character each. Use it to type the field and to set `max_length`: nine cells beside a label about identity is a 9-digit id, eight is usually a date.
- Do NOT return coordinates. `region_id` is the only way to place a field.
- `region_id` is mandatory while this page has any regions at all. If none of them looks right, pick the closest and say why in `help`, or omit the field — do NOT invent a bbox. An estimated box on a page that has regions is rejected downstream and the field arrives unplaced, so guessing buys nothing and costs the person a correct answer in the wrong cell.
- Only when the region list below is empty may you give an approximate `"bbox": [x0, y0, x1, y1]` normalized 0..1 from the top-left.
- Every field must be on this page. Set "page" to the page number given below.

Regions on this page:
"""


def lambda_handler(event, _context):
    if event.get("cache_hit"):
        return event

    sid = event["session_id"]
    doc_type = event["doc_type"]
    candidates = event.get("candidates") or []
    page_keys = event.get("page_keys") or []

    if doc_type == "pdf_acroform" and candidates:
        fields = _enrich_acroform(candidates, page_keys)
        fields = _reconcile(candidates, fields)
        pages = []
    else:
        pages = _load_regions(event.get("regions_key"))
        fields = _enrich_flat(pages, event.get("annotated_keys") or page_keys)

    fields = _dedupe(fields)
    fields = _place(fields, pages)

    update_session(sid, progress="finalizing")
    placed = sum(1 for f in fields if f.get("bbox_confidence") != "low")
    log.info("enriched to %d fields, %d placed", len(fields), placed)
    return {**event, "fields": fields, "form_map": form_map.render(fields)}


# -------------------------------------------------------------------- acroform

def _enrich_acroform(candidates: list[dict], page_keys: list[str]) -> list[dict]:
    content = _page_content(page_keys)
    content.append({"text": ACRO_TASK + json.dumps(_slim(candidates), ensure_ascii=False, indent=1)})
    return invoke_json(SYSTEM, content, config.ENRICH_MAX_TOKENS)


def _slim(candidates: list[dict]) -> list[dict]:
    """Send only what the model needs to align its answer — dropping rects
    and on-state values keeps the prompt small and stops the model
    hallucinating changes to them."""
    return [
        {"field_id": c["field_id"], "type": c.get("type", "text"),
         "page": c.get("page", 1), "bbox": c.get("bbox"),
         "options": c.get("options", [])}
        for c in candidates
    ]


def _reconcile(candidates: list[dict], enriched: list[dict]) -> list[dict]:
    """Structure from the PDF wins; language from the model wins.

    A model that renamed, dropped or moved a field silently would produce a
    schema the renderer cannot write back, so extraction is authoritative
    for everything mechanical.
    """
    by_id = {e.get("field_id"): e for e in enriched}
    out = []
    for c in candidates:
        fid = c.get("field_id") or c.get("name") or f"field_{len(out) + 1}"
        e = by_id.get(fid, {})
        backend = {
            "kind": "acroform",
            "name": c["name"],
            "page": c.get("page", 1),
            "rect": c.get("rect"),
        }
        if "checked_value" in c:
            backend["checked_value"] = c["checked_value"]
            backend["unchecked_value"] = c.get("unchecked_value", "/Off")

        ftype = c.get("type", "text")
        options = c.get("options") or e.get("options") or []
        if c.get("type") == "radio_group":
            ftype = "select"
            options = [o["value"] for o in c.get("radio_options", [])]
            backend["radio_options"] = c.get("radio_options", [])

        out.append({
            "field_id": fid,
            "label": e.get("label") or c["name"],
            "type": ftype,
            "page": c.get("page", 1),
            "bbox": c.get("bbox") or [0, 0, 0, 0],
            "section": e.get("section", ""),
            "required": bool(e.get("required", False)),
            "options": options,
            "validation": e.get("validation", "") or "",
            "help": e.get("help", ""),
            "max_length": c.get("max_length") or e.get("max_length"),
            "backend": backend,
        })
    return out


# ------------------------------------------------------------------ flat pages

def _load_regions(key: str | None) -> list[dict]:
    if not key:
        return []
    try:
        body = s3().get_object(Bucket=config.ARTIFACTS_BUCKET, Key=key)["Body"].read()
        return json.loads(body.decode("utf-8"))
    except Exception:
        log.exception("could not read the region table; falling back to estimation")
        return []


def _enrich_flat(pages: list[dict], image_keys: list[str]) -> list[dict]:
    """One model call per page, in parallel.

    The old code sent every page in one call and asked for the whole form back.
    That is what kept hitting the output budget — the 101 form truncated
    mid-array on page two — and a truncated array is a failed ingest, not a
    partial one. Per page, a budget overrun is impossible at these sizes and a
    failed page costs its own fields rather than the document's.
    """
    if not pages:
        # No text layer: a scan. Nothing to pick from, so the model is asked for
        # estimates the way it always was, and `_place` marks them low
        # confidence so nothing is stamped on the strength of a guess.
        log.warning("no candidate regions — falling back to estimated boxes")
        content = _page_content(image_keys)
        content.append({"text": "This document has no machine-readable geometry. "
                                "Identify every place a person must write and give "
                                "each an approximate \"bbox\": [x0, y0, x1, y1], "
                                "normalized 0..1 from the top-left."})
        return invoke_json(SYSTEM, content, config.ENRICH_MAX_TOKENS)

    # Same reason note_batch does this: boto3 clients are thread-safe to use but
    # not to construct, and `aws.bedrock()` is a lazy singleton.
    bedrock()

    by_page = {p["page"]: p for p in pages}
    jobs = [(p, image_keys[p - 1]) for p in sorted(by_page) if p <= len(image_keys)]

    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=config.NOTE_CONCURRENCY) as pool:
        futures = [pool.submit(_run_page, by_page[p], key) for p, key in jobs]
        for (page_no, _), future in zip(jobs, futures):
            try:
                out.extend(future.result())
            except Exception as e:
                # One page failing costs that page's fields. They come back as
                # nothing rather than as garbage, and the person can still fill
                # everything the other pages found.
                log.exception("enrich failed on page %d: %s", page_no, e)
    return out


def _run_page(page: dict, image_key: str) -> list[dict]:
    regions = page.get("regions") or []
    img = s3().get_object(Bucket=config.ARTIFACTS_BUCKET, Key=image_key)["Body"].read()
    content = [
        {"text": f"--- page {page['page']} ---"},
        {"image": {"format": "png", "source": {"bytes": img}}},
        {"text": FLAT_TASK + json.dumps(_slim_regions(regions, page["page"]),
                                        ensure_ascii=False, indent=1)},
    ]
    fields = invoke_json(SYSTEM, content, config.ENRICH_MAX_TOKENS)
    for f in fields:
        f["page"] = page["page"]
    return fields


def _slim_regions(regions: list[dict], page_no: int) -> dict:
    """What the model needs to choose a region: the id and what is printed
    around it. Not the coordinates — it has no use for them, they are most of
    the tokens, and a model that can see numbers is a model that can copy one
    into a bbox field it was told not to fill."""
    out = []
    for r in regions:
        item = {"region_id": r["region_id"], "nearby_text": r.get("nearby_text") or []}
        if r.get("own_label"):
            # The caption printed directly above this box, read off the page. The
            # one identifier here that is not a guess — `nearby_text` is whatever
            # happens to be near, and on a table row that is usually the column
            # beside this one.
            item["own_label"] = r["own_label"]
        if r.get("is_checkbox"):
            # The one thing about a region the model cannot reliably read off the
            # image: at this size a tick box and a full stop look alike, and the
            # difference decides whether the field takes an X or a sentence.
            item["kind"] = "checkbox"
        if r.get("comb"):
            # A box printed as nine character cells is telling you what goes in
            # it. The model does not place anything with this — it types the
            # field with it.
            item["character_cells"] = r["comb"]["cells"]
        if r.get("textract_label"):
            item["textract_label"] = r["textract_label"]
        if r.get("already_contains"):
            item["already_contains"] = r["already_contains"]
        out.append(item)
    return {"page": page_no, "regions": out}


def _page_content(page_keys: list[str]) -> list[dict]:
    content = []
    for i, key in enumerate(page_keys, start=1):
        img = s3().get_object(Bucket=config.ARTIFACTS_BUCKET, Key=key)["Body"].read()
        content.append({"text": f"--- page {i} ---"})
        content.append({"image": {"format": "png", "source": {"bytes": img}}})
    return content


# ---------------------------------------------------------------- placement

def _place(fields: list[dict], pages: list[dict]) -> list[dict]:
    """Resolve each field's box, and refuse the ones that cannot be trusted.

    A field whose box fails the check keeps everything else — label, type,
    section, help — and loses only its geometry. The viewer lists it and asks
    someone to place it; the renderer skips it. This is the same rule the
    filling agent works under: a blank box is always better than a wrong one on
    an official form, and an unplaced field is visibly unplaced where a
    misplaced one looks finished.
    """
    regions = {(p["page"], r["region_id"]): r for p in pages for r in p.get("regions") or []}
    text = {p["page"]: p.get("text") or [] for p in pages}
    page_regions = {p["page"]: p.get("regions") or [] for p in pages}
    placed: dict[int, list] = {}
    rejected = 0

    for f in fields:
        page = int(f.get("page") or 1)
        rid = f.get("region_id")
        region = regions.get((page, rid)) if rid is not None else None

        is_checkbox = bool(region and region.get("is_checkbox"))

        if region:
            f["bbox"] = region["bbox"]
            f["bbox_source"] = "region"
            f["nearby_text"] = region.get("nearby_text") or []
        elif f.get("bbox"):
            # No region id, so this is the model's own estimate. If it lands
            # squarely on a region we did find, it almost certainly meant that
            # one — take the region's exact geometry instead of the guess. When
            # nothing matches well the estimate is kept untouched, so this can
            # only improve a box, never move it somewhere new.
            snapped, moved = geo.snap(geo.clamp(f["bbox"]), page_regions.get(page, []))
            f["bbox"] = snapped
            f["bbox_source"] = "snapped" if moved else "estimated"
        else:
            f["bbox"], f["bbox_source"] = None, "none"

        f.pop("region_id", None)

        f.setdefault("backend", {"kind": "overlay"})
        option_boxes = []
        if is_checkbox:
            f["backend"]["mark"] = "checkbox"
            # The same rule `_reconcile` applies to AcroForm: structure from the
            # document wins, language from the model wins. The page's own content
            # stream says this box is a printed tick square, so it takes a mark —
            # the model does not get a vote on that. Left alone, a square the model
            # called "text" is stamped with a string a few points wide.
            if f.get("type") not in ("checkbox", "select", "multiselect"):
                was = f.get("type")
                f["type"] = "select" if len(f.get("options") or []) >= 2 else "checkbox"
                log.info("%s (%s): typed %r on a printed tick square — reading it as %r",
                         f.get("field_id"), f.get("label"), was, f["type"])

        # A select printed as a row of tick squares needs one box per choice. A
        # field carries a single bbox, so without this, picking any option but the
        # one the model anchored on stamps the answer onto the wrong square — and
        # because the type is "select" rather than "checkbox", the renderer writes
        # the choice as *text* into a box nine thousandths of a page wide. The
        # AcroForm path has always had this as `backend.radio_options`; a printed
        # radio group needs the same thing.
        #
        # Attempted whether or not the field anchored on a square: the model often
        # anchors such a select on the wide cell beside the row instead, which
        # leaves `is_checkbox` false and every one of these guards unarmed. What
        # keeps that from claiming an unrelated write-on-a-line select is
        # `_option_boxes` being all or nothing — every option must find its own
        # square by label before any of them is used.
        if f.get("type") in ("select", "multiselect"):
            option_boxes = _option_boxes(f, page_regions.get(page, []))
            if option_boxes:
                f["backend"]["option_boxes"] = option_boxes
                f["backend"]["mark"] = "checkbox"

        page_text = text.get(page, [])
        reason = geo.sanity_check(f["bbox"], page_text,
                                  others=placed.get(page, []),
                                  label=f.get("label", ""),
                                  is_checkbox=is_checkbox) if f.get("bbox") else "no box"
        # A choice whose squares could not all be located would be stamped as
        # text into one tick box. Refusing it is the honest outcome.
        if not reason and is_checkbox and f.get("type") in ("select", "multiselect") \
                and not option_boxes:
            reason = "its printed choices could not be told apart"
        # A box the model invented rather than chose. `bbox_confidence` grades it
        # "estimated", and nothing anywhere reads that value — every consumer, the
        # renderer included, tests only for "low" — so without this it is stamped,
        # counted as placed and drawn exactly like a box read out of the PDF.
        #
        # On a page that produced regions, an estimate means the model failed to
        # identify the box, which is not a licence to place it approximately. On a
        # page with no text and no rules there is nothing better to be had, but
        # `sanity_check` also has nothing to measure it against, so it would pass
        # by default. A box nobody can verify is what the rest of this refuses to
        # draw. Either way the field keeps everything but its geometry and waits
        # for a person.
        if not reason and f["bbox_source"] == "estimated":
            available = len(page_regions.get(page, []))
            if available:
                reason = ("the model estimated this box instead of choosing one "
                          f"of the {available} regions found on this page")
            elif not page_text and not regions:
                reason = "estimated on a page with no geometry to check it against"

        if reason:
            rejected += 1
            log.info("unplaced %s (%s): %s", f.get("field_id"), f.get("label"), reason)
            f["bbox"] = [0.0, 0.0, 0.0, 0.0]
            f["bbox_confidence"] = "low"
            f["bbox_note"] = reason
        else:
            f["bbox_confidence"] = "ok" if f["bbox_source"] in ("region", "snapped") else "estimated"
            # Checkboxes are kept out of the collision list for the same reason
            # they are exempt from the collision test: a tick box inside a wider
            # cell is the normal layout, and letting one claim its row would
            # reject whatever field legitimately owns that cell.
            if not is_checkbox:
                placed.setdefault(page, []).append(f["bbox"])

        # How the box is divided into character cells, where it is. Only useful
        # on a box we actually took from a region — a snapped or estimated one
        # has no claim to a comb it never matched.
        if region and region.get("comb") and f["bbox_source"] == "region":
            f["backend"]["comb"] = region["comb"]

    if rejected:
        log.warning("%d of %d fields could not be placed and need a person", rejected, len(fields))
    return fields


def _option_boxes(field: dict, page_regions: list[dict]) -> list[dict]:
    """Which printed tick square each choice of a select belongs to.

    Matched on the label beside each square, which `nearby_text` already
    carries, so nothing new has to be inferred and no second model call is
    needed. Same shape as the AcroForm path's `backend.radio_options`.

    All or nothing. A partial map is worse than none: the choices it did find
    would stamp correctly and the rest would land on whatever square the field
    happened to anchor on, which looks like a filled form and is not one.
    """
    options = [o for o in (field.get("options") or []) if str(o).strip()]
    if len(options) < 2:
        return []

    candidates = [r for r in page_regions if r.get("is_checkbox")]
    out, used = [], set()
    for option in options:
        match = _match_option(str(option), candidates, used)
        if match is None:
            log.info("no tick square for %r on %s", option, field.get("field_id"))
            return []
        used.add(id(match))
        out.append({"value": option, "bbox": match["bbox"]})
    return out


def _match_option(option: str, candidates: list[dict], used: set) -> dict | None:
    """The tick square whose printed label is this choice.

    Prefix matching in both directions, because the two texts are truncated at
    different points: `nearby_text` caps each string at 60 characters, and a long
    choice printed across two lines reaches here as only its first run.
    """
    want = _squash(option)
    if not want:
        return None

    best, best_score = None, 0
    for region in candidates:
        if id(region) in used:
            continue
        for near in (region.get("nearby_text") or [])[:2]:
            got = _squash(near.get("text", ""))
            if not got:
                continue
            if got == want:
                score = len(got) + 100          # exact wins outright
            elif want.startswith(got) or got.startswith(want):
                # Longer agreement is stronger evidence. Two choices on the same
                # row can share a short prefix — the kibbutz question offers two
                # that both begin "כן," — so a two-character overlap decides
                # nothing and is not accepted on its own.
                overlap = min(len(got), len(want))
                score = overlap if overlap >= 3 else 0
            else:
                score = 0
            if score > best_score:
                best, best_score = region, score
    return best


def _squash(text: str) -> str:
    """Comparable form: letters and digits only. Punctuation and spacing differ
    between what the form prints and what the model wrote into `options`."""
    return "".join(ch for ch in (text or "") if ch.isalnum())


def _dedupe(fields: list[dict]) -> list[dict]:
    seen, out = set(), []
    for f in fields:
        fid = f.get("field_id") or ""
        if not fid:
            continue
        base, n = fid, 2
        while fid in seen:
            fid, n = f"{base}_{n}", n + 1
        seen.add(fid)
        f["field_id"] = fid
        f.setdefault("backend", {"kind": "overlay"})
        out.append(f)
    return out
