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
"""
from __future__ import annotations

import json

from . import config
from .aws import bedrock


def invoke_json(system_text: str, content: list[dict], max_tokens: int) -> list[dict]:
    """Converse with `content` as the single user message; parse a JSON array
    out of the reply.

    The system block is followed by a cachePoint: callers that fan out over
    chunks of one document send an identical prefix every time, so the page
    images are paid for once rather than once per chunk.
    """
    resp = bedrock().converse_stream(
        modelId=config.BEDROCK_MODEL_ID,
        system=[
            {"text": system_text},
            {"cachePoint": {"type": "default"}},
        ],
        messages=[{"role": "user", "content": content}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
    )

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
