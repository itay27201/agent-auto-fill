"""The document catalog: the closed list of forms an agency actually issues.

A government does not have arbitrary documents, it has twenty. Before this,
every user uploaded the same twenty PDFs over and over and each one paid for a
full ingest — classify, rasterize, a Bedrock vision pass — to rediscover a
schema the system had already built. `store.registry_lookup` cached that by
SHA-256, but the cache was invisible: no name, nothing to browse, and keyed by
bytes, so an agency reissuing the form with a new revision date invalidated it.

The catalog is that registry made into a product surface. An entry is defined
once, reviewed by a person, and published; after that, starting a session is a
schema copy — no upload, no state machine, no model call.

Layout
------
PK = CATALOG#<cid>
    SK = META           the full entry
PK = CATALOG
    SK = ITEM#<cid>     thin listing row  <- one Query lists the catalog,
                        no Scan and no GSI

S3, under ArtifactsBucket:
    catalog/<cid>/source.pdf         the blank master
    catalog/<cid>/schema.json        field schema; JSON stays the contract
    catalog/<cid>/pages/*.png        pre-rasterized viewer pages
    catalog/<cid>/guide.md           the knowledge file (see guide.py)
    catalog/<cid>/sources/<name>     booklets the author fed the agent

The `catalog/` prefix is not incidental. ArtifactsBucket's lifecycle rules are
scoped to `derived/` and `outputs/`, so `catalog/` never expires — the same
reason `schemas/` doesn't. DocsBucket was not an option: its purge rule has no
prefix filter and would delete the master after seven days.
"""
from __future__ import annotations

import datetime as dt
import json
import re

from boto3.dynamodb.conditions import Key

from . import config, guide as gd
from .aws import ddb, s3
# DynamoDB hands numbers back as Decimal, which json.dumps refuses. store.py
# already has the converter and there is no reason for a second one.
from .store import _clean as _decimals_to_numbers

INDEX_PK = "CATALOG"
DRAFT, PUBLISHED = "draft", "published"

# Listing rows carry only what the card grid draws. The full entry is a second
# get_item away, and browsing shouldn't pay for page_keys on every row.
_LISTING = ("catalog_id", "name", "agency", "description", "language",
            "doc_type", "field_count", "status", "has_guide", "updated_at")

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


class NotFound(Exception):
    """No catalog entry with that id."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def slug(name: str) -> str:
    """A stable, human id — `form-106`, not a hash.

    Hash-keying is what made the old registry brittle: a ministry reissuing
    the same form with a new revision date produced different bytes and lost
    the entry. The id is the form's identity; the hash is only a hint used to
    match an uploaded copy back to its entry.
    """
    s = _SLUG_STRIP.sub("-", (name or "").strip().lower()).strip("-")
    return s[:60] or "document"


# --------------------------------------------------------------------- keys

def source_key(cid: str, ext: str = "pdf") -> str:
    """The master keeps its original extension: `api_render` dispatches on
    `doc_type`, and a .docx entry must still render through the docx path."""
    return f"catalog/{cid}/source.{ext.lstrip('.') or 'pdf'}"


def schema_key(cid: str) -> str:
    return f"catalog/{cid}/schema.json"


def guide_key(cid: str) -> str:
    return f"catalog/{cid}/guide.md"


def pages_prefix(cid: str) -> str:
    return f"catalog/{cid}/pages/"


def sources_prefix(cid: str) -> str:
    return f"catalog/{cid}/sources/"


# ------------------------------------------------------------------- entries

def exists(cid: str) -> bool:
    r = ddb().get_item(Key={"PK": f"CATALOG#{cid}", "SK": "META"},
                       ProjectionExpression="PK")
    return "Item" in r


def unique_id(name: str) -> str:
    """`form-106`, then `form-106-2` if taken. Two people defining the same
    form should collide visibly in the listing, not silently overwrite."""
    base = slug(name)
    cid, n = base, 2
    while exists(cid):
        cid, n = f"{base}-{n}", n + 1
    return cid


def put(entry: dict) -> dict:
    """Write the entry and its listing row. Two put_items rather than a
    transaction: the listing row is a derived view, and a torn write leaves a
    stale card, not a corrupt entry."""
    entry["updated_at"] = _now()
    ddb().put_item(Item={"PK": f"CATALOG#{entry['catalog_id']}", "SK": "META", **entry})
    _index(entry)
    return _decimals_to_numbers(entry)


def _index(entry: dict) -> None:
    ddb().put_item(Item={
        "PK": INDEX_PK,
        "SK": f"ITEM#{entry['catalog_id']}",
        **{k: entry.get(k) for k in _LISTING if entry.get(k) is not None},
    })


def get(cid: str) -> dict:
    r = ddb().get_item(Key={"PK": f"CATALOG#{cid}", "SK": "META"})
    if "Item" not in r:
        raise NotFound(cid)
    return _decimals_to_numbers(r["Item"])


def update(cid: str, **attrs) -> dict:
    """Read-modify-write through `put` so the listing row never drifts from
    the entry. Catalog writes are rare — this is not a hot path."""
    entry = get(cid)
    entry.update({k: v for k, v in attrs.items() if v is not None})
    return put(entry)


def delete(cid: str) -> None:
    ddb().delete_item(Key={"PK": f"CATALOG#{cid}", "SK": "META"})
    ddb().delete_item(Key={"PK": INDEX_PK, "SK": f"ITEM#{cid}"})


def listing(status: str | None = PUBLISHED) -> list[dict]:
    """One Query over the index partition. `status=None` includes drafts,
    which the authoring page needs and the public catalog tab does not."""
    out, kwargs = [], {"KeyConditionExpression": Key("PK").eq(INDEX_PK)}
    while True:
        r = ddb().query(**kwargs)
        for it in r.get("Items", []):
            row = _decimals_to_numbers(it)
            if status and row.get("status") != status:
                continue
            out.append({k: row.get(k) for k in _LISTING if k in row})
        if "LastEvaluatedKey" not in r:
            break
        kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    out.sort(key=lambda e: (e.get("agency") or "", e.get("name") or ""))
    return out


# ---------------------------------------------------------------------- s3

def put_guide(cid: str, guide: dict) -> str:
    key = guide_key(cid)
    s3().put_object(
        Bucket=config.ARTIFACTS_BUCKET,
        Key=key,
        Body=gd.render(guide).encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
        ServerSideEncryption="aws:kms",
    )
    return key


def load_guide_markdown(key: str | None) -> str:
    """Raw text. Missing guide is not an error: a catalog entry is usable the
    moment its schema exists, and the guide is written afterwards — a session
    that starts before it lands should still work, just without the context."""
    if not key:
        return ""
    try:
        return s3().get_object(Bucket=config.ARTIFACTS_BUCKET, Key=key)["Body"].read().decode("utf-8")
    except s3().exceptions.NoSuchKey:
        return ""


def load_guide(key: str | None) -> dict | None:
    md = load_guide_markdown(key)
    return gd.parse(md) if md else None


def parse_guide(markdown: str) -> dict | None:
    return gd.parse(markdown) if markdown else None


def copy_object(src_key: str, dst_key: str) -> str:
    s3().copy_object(
        Bucket=config.ARTIFACTS_BUCKET,
        CopySource={"Bucket": config.ARTIFACTS_BUCKET, "Key": src_key},
        Key=dst_key,
    )
    return dst_key


def copy_from_docs(src_key: str, dst_key: str) -> str:
    """The uploaded master moves out of DocsBucket, which expires everything
    after seven days, into the catalog prefix, which expires nothing."""
    s3().copy_object(
        Bucket=config.ARTIFACTS_BUCKET,
        CopySource={"Bucket": config.DOCS_BUCKET, "Key": src_key},
        Key=dst_key,
    )
    return dst_key


def copy_pages(cid: str, page_keys: list[str]) -> list[str]:
    """Session rasters live under `derived/` and expire; the catalog needs its
    own copies so the viewer still has pages a month from now."""
    out = []
    for i, key in enumerate(page_keys, start=1):
        out.append(copy_object(key, f"{pages_prefix(cid)}page-{i:03d}.png"))
    return out


def put_schema(cid: str, fields: list[dict]) -> str:
    key = schema_key(cid)
    s3().put_object(
        Bucket=config.ARTIFACTS_BUCKET,
        Key=key,
        Body=json.dumps(fields, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
        ServerSideEncryption="aws:kms",
    )
    return key


# ------------------------------------------------------------------- sources

def list_sources(cid: str) -> list[dict]:
    """Instruction booklets and appendices the author uploaded for the
    authoring agent to read."""
    r = s3().list_objects_v2(Bucket=config.ARTIFACTS_BUCKET, Prefix=sources_prefix(cid))
    return [
        {"source_id": o["Key"].rsplit("/", 1)[-1], "key": o["Key"], "size": o["Size"]}
        for o in r.get("Contents", [])
        if not o["Key"].endswith("/")
    ]
