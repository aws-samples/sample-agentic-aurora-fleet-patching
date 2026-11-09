# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared AWS client initialization.

Supports both single-account (default credentials) and multi-account (AssumeRole)
operation. Clients are cached per region for performance.

For cross-account operations, use shared.multi_account.get_client_for_account()
instead of these cached clients.
"""
import json
import os
import re
import boto3
from botocore.config import Config
from functools import lru_cache

_DEFAULT_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

# Long Bedrock generations (e.g. the 8192-token version-drift response on
# Claude Sonnet 4.6) routinely take more than botocore's default 60 s read
# timeout, which caused a timeout + retry (~120 s) or an outright failure.
# Callers that need this run as async jobs on the 15-minute API Lambda.
# total_max_attempts=2 is one try plus one retry (botocore's "max_attempts"
# counts retries only, so max_attempts=2 would mean 3 tries = 900 s worst case,
# the whole Lambda budget). Worst case here is ~2 x 300 s.
BEDROCK_CLIENT_CONFIG = Config(
    read_timeout=300,
    connect_timeout=10,
    retries={"total_max_attempts": 2, "mode": "standard"},
)


# Fail fast when a region's RDS endpoint is unreachable (for example an opt-in
# region that isn't enabled for the account). With botocore's defaults that case
# hangs for minutes; with these settings it fails in ~12 s. Read timeout is left
# at the default because RDS API calls themselves are fast.
RDS_CLIENT_CONFIG = Config(
    connect_timeout=5,
    retries={"total_max_attempts": 3, "mode": "standard"},
)


@lru_cache
def rds_client(region: str = _DEFAULT_REGION):
    return boto3.client("rds", region_name=region, config=RDS_CLIENT_CONFIG)


@lru_cache
def cloudwatch_client(region: str = _DEFAULT_REGION):
    return boto3.client("cloudwatch", region_name=region)


@lru_cache
def dynamodb_resource(region: str = _DEFAULT_REGION):
    return boto3.resource("dynamodb", region_name=region)


@lru_cache
def bedrock_client(region: str = ""):
    """Get Bedrock runtime client.

    Uses BEDROCK_REGION env var if set (for cross-region Bedrock access),
    otherwise falls back to default region.
    """
    bedrock_region = region or os.environ.get("BEDROCK_REGION", "") or _DEFAULT_REGION
    return boto3.client("bedrock-runtime", region_name=bedrock_region, config=BEDROCK_CLIENT_CONFIG)


def parse_json_object(text: str):
    """Parse the JSON object in a model response, or return None.

    Strips markdown code fences, then takes the span from the first "{" to the
    last "}". Returns None when there is no well-ordered span; raises
    json.JSONDecodeError when the span is not valid JSON (e.g. truncated).
    """
    if not text:
        return None
    if "```" in text:
        fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    return json.loads(text[start:end + 1])


def extract_bedrock_text(body: dict):
    """Return the first text block from a Bedrock Anthropic response, or None.

    Newer models (e.g. Claude Sonnet 5) may emit reasoning/thinking blocks
    before the text block, so ``content[0]`` is not guaranteed to be text.
    This iterates blocks and also handles the Converse-style ``output`` shape.
    """
    if not isinstance(body, dict):
        return None
    blocks = body.get("content")
    if not blocks and "output" in body:
        blocks = body.get("output", {}).get("message", {}).get("content", [])
    for block in blocks or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            return block["text"]
        # Some payloads use bare {"text": "..."} blocks without a type field
        if isinstance(block, dict) and "text" in block and "type" not in block:
            return block["text"]
    return None


@lru_cache
def sns_client(region: str = _DEFAULT_REGION):
    return boto3.client("sns", region_name=region)


@lru_cache
def sfn_client(region: str = _DEFAULT_REGION):
    return boto3.client("stepfunctions", region_name=region)


@lru_cache
def ec2_client(region: str = _DEFAULT_REGION):
    return boto3.client("ec2", region_name=region)


@lru_cache
def sts_client(region: str = _DEFAULT_REGION):
    return boto3.client("sts", region_name=region)


@lru_cache
def organizations_client():
    """Organizations API is global, always us-east-1."""
    return boto3.client("organizations", region_name="us-east-1")


def get_current_account_id() -> str:
    """Get the current AWS account ID."""
    return sts_client().get_caller_identity()["Account"]
