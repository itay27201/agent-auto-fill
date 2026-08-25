"""Lazy boto3 clients, cached per warm Lambda execution environment so a
container reuse doesn't pay client-construction cost on every invocation."""
from __future__ import annotations

import boto3

from . import config

_ddb_table = None
_s3 = None
_bedrock = None
_sfn = None
_apigw_ws: dict[str, object] = {}


def ddb():
    """A bound Table resource, not a raw client — every call site in
    common/store.py omits TableName."""
    global _ddb_table
    if _ddb_table is None:
        _ddb_table = boto3.resource("dynamodb").Table(config.TABLE_NAME)
    return _ddb_table


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def bedrock():
    """The *runtime* client — .converse/.converse_stream only exist there,
    not on the control-plane `bedrock` client."""
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=config.BEDROCK_REGION)
    return _bedrock


def sfn():
    global _sfn
    if _sfn is None:
        _sfn = boto3.client("stepfunctions")
    return _sfn


def apigw_ws(endpoint: str):
    """Cached per endpoint: the WebSocket management endpoint varies by
    stage/domain, so one global singleton would silently misroute."""
    client = _apigw_ws.get(endpoint)
    if client is None:
        client = boto3.client("apigatewaymanagementapi", endpoint_url=endpoint)
        _apigw_ws[endpoint] = client
    return client
