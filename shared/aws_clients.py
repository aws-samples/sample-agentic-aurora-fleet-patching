# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared AWS client initialization.

Supports both single-account (default credentials) and multi-account (AssumeRole)
operation. Clients are cached per region for performance.

For cross-account operations, use shared.multi_account.get_client_for_account()
instead of these cached clients.
"""
import os
import boto3
from functools import lru_cache

_DEFAULT_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))


@lru_cache
def rds_client(region: str = _DEFAULT_REGION):
    return boto3.client("rds", region_name=region)


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
    return boto3.client("bedrock-runtime", region_name=bedrock_region)


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
