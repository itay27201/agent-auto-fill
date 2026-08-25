"""DynamoDB single-table access.

Layout
------
PK = SESSION#<sid>
    SK = META                  session status, doc pointers, schema key
    SK = FIELD#<field_id>      one item per field  <- per-field so the user
                               typing in box 12 never clobbers the agent
                               writing box 7
    SK = EVENT#<ts>#<uuid>     append-only change log
    SK = MSG#<ts>#<uuid>       chat transcript

PK = FORM#<sha256>
    SK = SCHEMA                form registry: hash -> cached schema key

The event log matters more than it looks. When the user manually edits three
fields between agent turns, the next turn has to see that or the agent
re-asks for something the user just typed.
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import uuid
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from . import config
from .aws import ddb, s3


class VersionConflict(Exception):
    """Someone else wrote this field since you read it."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _ttl() -> int:
    exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=config.SESSION_TTL_DAYS)
    return int(exp.timestamp())


def _clean(obj):
    """DynamoDB hands back Decimal; JSON does not want it."""
    if isinstance(obj, list):
        return [_clean(o) for o in obj]
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, decimal.Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


# ------------------------------------------------------------------- session

def create_session(sid: str, doc_key: str, filename: str, owner: str) -> dict:
    item = {
        "PK": f"SESSION#{sid}",
        "SK": "META",
        "session_id": sid,
        "status": "awaiting_upload",
        "doc_key": doc_key,
        "filename": filename,
        "owner": owner,
        "created_at": _now(),
        "updated_at": _now(),
        "ttl": _ttl(),
    }
    ddb().put_item(Item=item)
    return _clean(item)


def get_session(sid: str) -> dict | None:
    r = ddb().get_item(Key={"PK": f"SESSION#{sid}", "SK": "META"})
    return _clean(r["Item"]) if "Item" in r else None


def update_session(sid: str, **attrs) -> dict:
    attrs["updated_at"] = _now()
    names = {f"#k{i}": k for i, k in enumerate(attrs)}
    vals = {f":v{i}": v for i, v in enumerate(attrs.values())}
    expr = "SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(attrs)))
    r = ddb().update_item(
        Key={"PK": f"SESSION#{sid}", "SK": "META"},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=vals,
        ReturnValues="ALL_NEW",
    )
    return _clean(r["Attributes"])


# -------------------------------------------------------------------- schema
# Schemas live in S3, not DynamoDB. A 60-field schema with bboxes and backend
# payloads runs well past the 400KB item limit on a long form.

def put_schema(sid: str, fields: list[dict]) -> str:
    key = f"schemas/{sid}/schema.json"
    s3().put_object(
        Bucket=config.ARTIFACTS_BUCKET,
        Key=key,
        Body=json.dumps(fields, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )
    return key


def load_schema(key: str) -> list[dict]:
    obj = s3().get_object(Bucket=config.ARTIFACTS_BUCKET, Key=key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def copy_schema(src_key: str, sid: str) -> str:
    """Registry cache hit: reuse a schema built for an identical document."""
    dst = f"schemas/{sid}/schema.json"
    s3().copy_object(
        Bucket=config.ARTIFACTS_BUCKET,
        CopySource={"Bucket": config.ARTIFACTS_BUCKET, "Key": src_key},
        Key=dst,
    )
    return dst


# ------------------------------------------------------------------ registry

def registry_lookup(doc_hash: str) -> dict | None:
    r = ddb().get_item(Key={"PK": f"FORM#{doc_hash}", "SK": "SCHEMA"})
    return _clean(r["Item"]) if "Item" in r else None


def registry_store(doc_hash: str, schema_key: str, doc_type: str, form_name: str = "") -> None:
    """Government forms repeat. The second user to upload the same form gets
    the schema instantly and never pays for the vision pass."""
    ddb().put_item(
        Item={
            "PK": f"FORM#{doc_hash}",
            "SK": "SCHEMA",
            "schema_key": schema_key,
            "doc_type": doc_type,
            "form_name": form_name,
            "created_at": _now(),
            # No TTL: the registry is the asset that makes this cheap over time.
        }
    )


# -------------------------------------------------------------------- fields

def seed_fields(sid: str, fields: list[dict]) -> None:
    with ddb().batch_writer() as batch:
        for f in fields:
            batch.put_item(
                Item={
                    "PK": f"SESSION#{sid}",
                    "SK": f"FIELD#{f['field_id']}",
                    "field_id": f["field_id"],
                    "value": None,
                    "source": None,
                    "confirmed": False,
                    "version": 0,
                    "ttl": _ttl(),
                }
            )


def get_values(sid: str) -> dict[str, dict]:
    out, kwargs = {}, {
        "KeyConditionExpression": Key("PK").eq(f"SESSION#{sid}") & Key("SK").begins_with("FIELD#")
    }
    while True:
        r = ddb().query(**kwargs)
        for it in r.get("Items", []):
            out[it["field_id"]] = _clean(it)
        if "LastEvaluatedKey" not in r:
            return out
        kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def set_value(
    sid: str,
    field_id: str,
    value: Any,
    source: str,
    actor: str,
    expected_version: int | None = None,
    confirmed: bool | None = None,
) -> dict:
    """Conditional write. `expected_version` from the client makes concurrent
    edits fail loudly instead of silently overwriting."""
    cond = "attribute_exists(SK)"
    names = {"#v": "value", "#s": "source", "#ver": "version", "#c": "confirmed"}
    vals = {
        ":v": value,
        ":s": source,
        ":one": 1,
        ":zero": 0,
        ":c": bool(confirmed) if confirmed is not None else (source != "agent"),
        ":t": _now(),
        ":a": actor,
    }
    if expected_version is not None:
        cond += " AND #ver = :expected"
        vals[":expected"] = expected_version

    try:
        r = ddb().update_item(
            Key={"PK": f"SESSION#{sid}", "SK": f"FIELD#{field_id}"},
            UpdateExpression=(
                "SET #v = :v, #s = :s, #c = :c, updated_at = :t, updated_by = :a, "
                "#ver = if_not_exists(#ver, :zero) + :one"
            ),
            ConditionExpression=cond,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=vals,
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise VersionConflict(field_id) from e
        raise

    append_event(sid, "field_set", actor=actor, field_id=field_id, source=source)
    return _clean(r["Attributes"])


def confirm_value(
    sid: str,
    field_id: str,
    actor: str,
    expected_version: int | None = None,
) -> dict:
    """Mark an agent-drafted value as attested by the human, without
    touching `value`/`source` — the point is to preserve who actually wrote
    the value (and the evidence behind it) while recording that a person
    signed off on it."""
    cond = "attribute_exists(SK)"
    names = {"#c": "confirmed", "#ver": "version"}
    vals = {":c": True, ":one": 1, ":zero": 0, ":t": _now(), ":a": actor}
    if expected_version is not None:
        cond += " AND #ver = :expected"
        vals[":expected"] = expected_version

    try:
        r = ddb().update_item(
            Key={"PK": f"SESSION#{sid}", "SK": f"FIELD#{field_id}"},
            UpdateExpression=(
                "SET #c = :c, updated_at = :t, updated_by = :a, "
                "#ver = if_not_exists(#ver, :zero) + :one"
            ),
            ConditionExpression=cond,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=vals,
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise VersionConflict(field_id) from e
        raise

    append_event(sid, "user_confirmed", actor=actor, field_id=field_id)
    return _clean(r["Attributes"])


# -------------------------------------------------------------- events / chat

def append_event(sid: str, kind: str, actor: str, **payload) -> None:
    ddb().put_item(
        Item={
            "PK": f"SESSION#{sid}",
            "SK": f"EVENT#{_now()}#{uuid.uuid4().hex[:8]}",
            "kind": kind,
            "actor": actor,
            "at": _now(),
            "ttl": _ttl(),
            **{k: v for k, v in payload.items() if v is not None},
        }
    )


def recent_events(sid: str, limit: int = 25) -> list[dict]:
    r = ddb().query(
        KeyConditionExpression=Key("PK").eq(f"SESSION#{sid}") & Key("SK").begins_with("EVENT#"),
        ScanIndexForward=False,
        Limit=limit,
    )
    return list(reversed([_clean(i) for i in r.get("Items", [])]))


def append_message(sid: str, role: str, content: Any) -> None:
    ddb().put_item(
        Item={
            "PK": f"SESSION#{sid}",
            "SK": f"MSG#{_now()}#{uuid.uuid4().hex[:8]}",
            "role": role,
            "content": json.dumps(content, ensure_ascii=False),
            "ttl": _ttl(),
        }
    )


def recent_messages(sid: str, limit: int = 20) -> list[dict]:
    r = ddb().query(
        KeyConditionExpression=Key("PK").eq(f"SESSION#{sid}") & Key("SK").begins_with("MSG#"),
        ScanIndexForward=False,
        Limit=limit,
    )
    msgs = []
    for it in reversed(r.get("Items", [])):
        msgs.append({"role": it["role"], "content": json.loads(it["content"])})
    return msgs
