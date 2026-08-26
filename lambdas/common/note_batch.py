"""Field notes for a whole form, in chunks, in parallel.

The authoring agent used to write these one tool call per field. For the
ITC-101 form that is 97 tool calls: 97 JSON blocks streamed token by token,
97 tool-result round trips through a transcript that grows with each one, and
97 full rewrites of guide.md to S3. It ran out of turns at 70 and the entry was
published anyway, because nothing counted.

The seven guide *sections* are still written by the agent, one at a time, with
a person reading each one — they are few, they overlap, and they need judgment.
Field notes are the opposite: 97 independent items with a uniform shape. That
is a batch, not a conversation, and this is the batch.

Every chunk sends the same prefix — system prompt, page images, the sections
already written — and differs only in its slice of the field list. Identical
prefixes are what make the `cachePoint` in `llm_json` worth having: the page
images are paid for once, not once per chunk.
"""
from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor

from . import catalog as cat, config, guide as gd, guide_checks as gc
from .aws import bedrock, s3
from .llm_json import invoke_json

log = logging.getLogger()

SYSTEM = """You write the per-field notes for a government form's guide.

A note is what someone sees when they click a box on the form and ask "what goes in here?". Write for a person who has never filled this form before.

Return ONLY a JSON array. No prose, no markdown fences.

Each element:
{"field_id": "exactly as given", "markdown": "the note", "basis": "form_itself|source_doc|author_said", "citation": "where this comes from"}

Rules:
- Write in the language of the form. Do not translate the form's own terms.
- Say something the label does not already say. If the label is "Name" the note must say *whose* name and where to find it — a note that only restates the label is worse than no note, because it looks answered.
- Where the guide's rules below bear on a field, say so. A field about a second employer should mention the tax-coordination rule.
- Two or three sentences. These are read in a tooltip, not a manual.
- Never write the same note for several fields. If two fields genuinely need the same guidance, say what distinguishes them.
- Do not invent thresholds, amounts, dates or legal references. If you cannot ground a fact, leave it out and describe the box instead.
- Return one element for every field_id given to you, and no field_id that was not given."""


def write_notes(ctx, field_ids=None, section=None, progress=None) -> dict:
    """Write a note for every requested field. Returns the guide_checks report
    plus what this run did.

    `progress(done, total)` is called after each chunk lands, so the page can
    show a real count rather than a spinner. `ctx` is an
    `author_tools.AuthorContext`.
    """
    targets = _targets(ctx, field_ids, section)
    if not targets:
        return {"ok": False, "error": "no fields matched — call get_field_list for the real ids",
                "written": 0}

    prefix = _prefix(ctx)
    # Build the Bedrock client before the pool starts. boto3 is thread-safe to
    # *use* but not to construct, and `aws.bedrock()` is a lazy singleton — four
    # threads racing on its `is None` check is the one place that bites.
    bedrock()

    chunks = [targets[i:i + config.NOTE_CHUNK_SIZE]
              for i in range(0, len(targets), config.NOTE_CHUNK_SIZE)]
    log.info("writing %d notes in %d chunks", len(targets), len(chunks))

    written, failed, rejected, done = 0, [], [], 0

    # The model calls run in parallel; applying their answers does not. Every
    # chunk mutates the same guide dict and flushes it whole, so that has to
    # happen on one thread — as_completed hands them back one at a time.
    with ThreadPoolExecutor(max_workers=config.NOTE_CONCURRENCY) as pool:
        futures = [pool.submit(_run_chunk, prefix, chunk) for chunk in chunks]
        for i, future in enumerate(futures):
            try:
                notes = future.result()
            except Exception as e:
                # One chunk failing costs its 15 fields, not the run. They come
                # back in `missing`, and asking again re-writes only those.
                log.exception("note chunk %d failed", i)
                failed.append(f"{type(e).__name__}: {e}")
                notes = []

            for note in notes:
                fid = note.get("field_id")
                body = (note.get("markdown") or "").strip()
                # Same guard as the singular tool: a note keyed to an invented
                # field_id would look written and be permanently unreachable.
                if fid not in ctx.by_id or not body:
                    continue
                # And the same basis rule. Writing in bulk is not a licence to
                # skip the check the singular tool applies — a note with no
                # stated origin is exactly what this system exists to refuse,
                # and it should not become admissible by arriving in a batch.
                if note.get("basis") not in gd.VALID_BASIS or not (note.get("citation") or "").strip():
                    rejected.append(fid)
                    continue
                gd.set_field_note(ctx.guide, fid, body)
                written += 1

            done += len(chunks[i])
            # Once per chunk, not once per note: 7 writes of guide.md instead
            # of 97. A crash costs the chunk in flight, nothing already landed.
            _flush(ctx)
            if progress:
                progress(min(done, len(targets)), len(targets))

    report = gc.check(ctx.guide, ctx.fields, ctx.entry.get("language", ""))
    out = {
        "ok": True,
        "written": written,
        "requested": len(targets),
        "chunks_failed": failed,
        "report": report,
        "summary": gc.summary(report),
        "awaiting_human_review": True,
    }
    if rejected:
        out["rejected_no_basis"] = sorted(rejected)[:20]
        out["rejected_count"] = len(rejected)
        out["rejected_note"] = ("These came back without a valid basis and citation, "
                                "so they were not written. Say where the guidance comes "
                                "from and write them again.")
    return out


# ------------------------------------------------------------------ internals

def _targets(ctx, field_ids, section) -> list[dict]:
    """Which fields this run covers. Explicit ids win; then a section filter;
    otherwise every field that has no note yet — so a second call after a
    partial run is a repair, not a rewrite."""
    if field_ids:
        return [ctx.by_id[f] for f in field_ids if f in ctx.by_id]
    fields = ctx.fields
    if section:
        want = section.strip().lower()
        fields = [f for f in fields if (f.get("section") or "").strip().lower() == want]
        return fields
    noted = ctx.guide.get("field_notes") or {}
    return [f for f in fields if f.get("field_id") not in noted]


def _prefix(ctx) -> list[dict]:
    """The content every chunk shares: the form's pages, then whatever the
    guide already says. The sections matter — a note that contradicts Key rules
    is worse than a vague one, and the model can only avoid that if it has read
    them."""
    content: list[dict] = []
    for i, key in enumerate(ctx.entry.get("page_keys") or [], start=1):
        try:
            img = s3().get_object(Bucket=config.ARTIFACTS_BUCKET, Key=key)["Body"].read()
        except Exception:
            log.warning("page image missing: %s", key)
            continue
        content.append({"text": f"--- page {i} ---"})
        content.append({"image": {"format": "png", "source": {"bytes": img}}})

    sections = ctx.guide.get("sections") or {}
    written = [f"## {name}\n{body.strip()}"
               for name, body in sections.items() if (body or "").strip()]
    if written:
        content.append({"text": "The guide for this form so far:\n\n" + "\n\n".join(written)})
    else:
        content.append({"text": "No sections of the guide have been written yet — "
                                "describe each box from the form itself."})
    return content


def _run_chunk(prefix: list[dict], chunk: list[dict]) -> list[dict]:
    ask = {"text": "Write a note for each of these fields:\n"
                   + json.dumps(_slim(chunk), ensure_ascii=False, indent=1)}
    return invoke_json(SYSTEM, prefix + [ask], config.NOTE_MAX_TOKENS)


def _slim(chunk: list[dict]) -> list[dict]:
    """Only what the model needs to write about a box. Geometry is what the
    viewer uses; sending bboxes here would just cost tokens."""
    out = []
    for f in chunk:
        d = {"field_id": f.get("field_id"), "label": f.get("label"),
             "type": f.get("type"), "section": f.get("section")}
        if f.get("required"):
            d["required"] = True
        if f.get("options"):
            d["options"] = f["options"]
        if f.get("help"):
            d["help_from_ingest"] = f["help"]
        out.append({k: v for k, v in d.items() if v not in (None, "")})
    return out


def _flush(ctx) -> None:
    cat.put_guide(ctx.cid, ctx.guide)
    cat.update(ctx.cid, has_guide=gd.is_filled(ctx.guide))
