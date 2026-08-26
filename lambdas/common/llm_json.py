"""One Bedrock call that returns a JSON array.

Extracted from `ingest_enrich` when the authoring agent grew a second caller
with the same needs: read a form's page images, return one structured record
per field. The two differ only in the prompt and the size of the answer.

Streamed rather than a single `converse()`. Reading a multi-page form takes
minutes, and a non-streaming call holds the connection silent for all of it —
long past botocore's read timeout, which then kills the request mid-generation
and retries the whole thing. That is what left ingest sessions stuck on
"enriching" until the state machine gave up 5 minutes later with
ReadTimeoutError. Streaming means the timeout only has to cover the gap between
chunks, which is small no matter how long the answer is.

Every caller of this is on the define-once path — reading a document once and
returning structured records that the registry and the catalog then hand to
every later session — so it defaults to the ingest model tier rather than the
chat one. See `config.INGEST_MODEL_ID` and `_converse` below for what that
changes about the request.

Only `text` deltas are collected. With adaptive thinking on, the stream also
carries `reasoningContent` deltas; ignoring them is what we want — the reasoning
is not part of the JSON answer — but note that thinking tokens do count against
`maxTokens`, which is a further reason enrich now sends one page per call
instead of a whole document.
"""
from __future__ import annotations

import json
import logging

from botocore.exceptions import ClientError

from . import config
from .aws import bedrock

log = logging.getLogger()


def invoke_json(system_text: str, content: list[dict], max_tokens: int,
                model_id: str | None = None) -> list[dict]:
    """Converse with `content` as the single user message; parse a JSON array
    out of the reply.

    The system block is followed by a cachePoint: callers that fan out over
    chunks of one document send an identical prefix every time, so the page
    images are paid for once rather than once per chunk.

    `model_id` defaults to the ingest tier, because every caller of this
    function is on the define-once path — reading a document and returning
    structured records. The chat agents go through `agent_loop`, not here.
    """
    model_id = model_id or config.INGEST_MODEL_ID
    resp = _converse(model_id, system_text, content, max_tokens)

    parts, stop = [], ""
    for ev in resp["stream"]:
        if "contentBlockDelta" in ev:
            delta = ev["contentBlockDelta"]["delta"]
            if "text" in delta:
                parts.append(delta["text"])
        elif "messageStop" in ev:
            stop = ev["messageStop"].get("stopReason", "")

    if stop == "max_tokens":
        # The JSON array is cut off mid-element, so parse_array would fail with
        # a decode error that says nothing about why. Long forms hit this: the
        # ITC-101 income-tax form truncated at the old 8192-token budget on
        # page two. Raise the caller's budget rather than guessing at the tail.
        raise ValueError(
            f"model hit the {max_tokens}-token output budget after "
            f"{len(''.join(parts))} chars — this call needs a larger budget"
        )
    return parse_array("".join(parts))


def _converse(model_id: str, system_text: str, content: list[dict], max_tokens: int):
    """One streaming Converse call, with the request shaped for the model.

    Two things differ across the tiers and both are hard failures, not degraded
    answers:

    `temperature` — removed on Opus 4.7+ and Sonnet 5, which reject it with a
    ValidationException. Sonnet 4.6 still accepts it.

    `thinking` — on Opus 4.8 an omitted `thinking` block means the model runs
    *without* thinking, which is most of what the tier was chosen for. It has to
    be asked for explicitly, and `budget_tokens` is gone: adaptive only.

    The extra fields ride in `additionalModelRequestFields`, which is Converse's
    passthrough for native Anthropic body fields it does not model itself. If a
    Bedrock version does not recognise one, the call fails validation and takes
    ingest down with it — so a rejected passthrough is retried once without it.
    Losing the thinking block costs quality; failing the call costs the upload.
    """
    kwargs = {
        "modelId": model_id,
        "system": [{"text": system_text}, {"cachePoint": {"type": "default"}}],
        "messages": [{"role": "user", "content": content}],
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    if config.accepts_sampling(model_id):
        kwargs["inferenceConfig"]["temperature"] = 0
        return bedrock().converse_stream(**kwargs)

    extra = {"thinking": {"type": "adaptive"},
             "output_config": {"effort": config.INGEST_EFFORT}}
    try:
        return bedrock().converse_stream(**kwargs, additionalModelRequestFields=extra)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ValidationException":
            raise
        log.warning("model %s rejected %s (%s) — retrying without it",
                    model_id, sorted(extra), e)
        return bedrock().converse_stream(**kwargs)


def parse_array(text: str) -> list[dict]:
    """Tolerant of a markdown fence and of prose either side of the array —
    both of which models emit despite being told not to."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        t = t[4:] if t.lower().startswith("json") else t
    start, end = t.find("["), t.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON array in model output: {text[:300]}")
    return json.loads(t[start:end + 1])
