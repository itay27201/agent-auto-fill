"""Lazy boto3 clients, cached per warm Lambda execution environment so a
container reuse doesn't pay client-construction cost on every invocation."""
from __future__ import annotations

import os

import boto3
from botocore.client import Config as BotoConfig

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
    """Explicit region + SigV4 + virtual-hosted addressing. Without this,
    presigned URLs came out signed for the legacy global s3.amazonaws.com
    endpoint (SigV2-style: AWSAccessKeyId/Signature/Expires query params) —
    S3 then 307-redirects PUTs to the bucket's real regional endpoint, and
    the browser reports that redirect as a CORS failure (S3's redirect
    response doesn't carry the CORS headers the follow-up request needs)."""
    global _s3
    if _s3 is None:
        _s3 = boto3.client(
            "s3",
            region_name=os.environ.get("AWS_REGION"),
            config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
    return _s3


def bedrock():
    """The *runtime* client — .converse/.converse_stream only exist there,
    not on the control-plane `bedrock` client.

    Explicit timeouts, because botocore's defaults are wrong for model calls.
    The default read timeout is 60s and the default retry mode is `legacy`
    (5 attempts): a Converse call that runs longer than a minute — which any
    multi-page vision prompt does — gets killed mid-generation and then
    retried four more times, each one paying for a full inference nobody
    reads. Ingest failed exactly this way, burning 311s before surfacing
    ReadTimeoutError. Both agents stream, so this timeout has to cover the
    gap between chunks rather than the whole generation; 300s is far more
    than that and still under the shortest caller's Lambda timeout.
    """
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client(
            "bedrock-runtime",
            region_name=config.BEDROCK_REGION,
            config=BotoConfig(
                connect_timeout=10,
                read_timeout=config.BEDROCK_READ_TIMEOUT,
                # One retry, not five (botocore counts this as retries, so
                # two attempts total). A stalled model call is expensive to
                # repeat, and the ingest state machine already retries the
                # step for the errors that are actually worth retrying.
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )
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
