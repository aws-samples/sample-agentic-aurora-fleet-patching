# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Configuration constants for the patching agent.

All configuration is environment-driven for portability. The agent can be deployed
to any AWS account/region without code changes — just set the right env vars or
provide a config file.
"""
import json
import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Agent Behavior Mode
# ─────────────────────────────────────────────────────────────────────────────

# ACTION_MODE controls whether agents execute actions directly or recommend them.
#   "recommend" (default): Agent analyzes and proposes — user must approve each action.
#   "execute":             Agent carries out actions that were previously approved.
#
# The recommend mode is the safe default. The agent will NEVER make changes to
# your infrastructure without explicit user approval. It surfaces what to do,
# why, and what the alternatives are. The user decides.
ACTION_MODE = os.environ.get("ACTION_MODE", "recommend")

# ─────────────────────────────────────────────────────────────────────────────
# Core Infrastructure Config
# ─────────────────────────────────────────────────────────────────────────────

# Prefer STATE_TABLE_NAME (set by the UI/API stack) and fall back to the legacy
# PATCH_STATE_TABLE var; default to the table that the stack actually creates.
DYNAMODB_TABLE = (
    os.environ.get("STATE_TABLE_NAME")
    or os.environ.get("PATCH_STATE_TABLE")
    or "aurora-patch-agent-ui-state"
)

# Default to Sonnet 4.5: unlike Sonnet 5 it does not force extended thinking, so
# it returns usable text at modest max_tokens and responds ~5x faster (~6s vs
# ~30s), keeping synchronous endpoints under API Gateway's 29s limit.
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
SNS_APPROVAL_TOPIC = os.environ.get("SNS_APPROVAL_TOPIC", "")

# ─────────────────────────────────────────────────────────────────────────────
# Multi-Account & Multi-Region Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Role name to assume in target accounts (used with Organizations discovery)
CROSS_ACCOUNT_ROLE_NAME = os.environ.get(
    "CROSS_ACCOUNT_ROLE_NAME", "AuroraPatchAgentRole"
)

# Explicit target accounts JSON (overrides Organizations discovery)
# Format: [{"account_id": "123456789012", "role_arn": "arn:aws:iam::123:role/X",
#            "regions": ["us-east-1"], "alias": "prod-account", "environment": "production"}]
TARGET_ACCOUNTS_JSON = os.environ.get("TARGET_ACCOUNTS", "")

# Regions to scan when no explicit account config exists (local-account mode)
DEFAULT_SCAN_REGIONS = [
    r.strip() for r in
    os.environ.get("SCAN_REGIONS", os.environ.get("AWS_REGION", "us-east-1")).split(",")
    if r.strip()
]

# Enable Organizations-based auto-discovery of accounts
ENABLE_ORG_DISCOVERY = os.environ.get("ENABLE_ORG_DISCOVERY", "false").lower() == "true"

# ─────────────────────────────────────────────────────────────────────────────
# Database Connectivity (for Analyzer agent)
# ─────────────────────────────────────────────────────────────────────────────

# IAM database auth (preferred — portable, no secrets to manage)
DB_IAM_AUTH_ENABLED = os.environ.get("DB_IAM_AUTH", "true").lower() == "true"
DB_IAM_USER = os.environ.get("DB_IAM_USER", "aurora_patch_agent")

# Secrets Manager fallback
DB_SECRET_ARN = os.environ.get("DB_SECRET_ARN", "")

# Static credential fallback (not recommended for production)
DB_USER = os.environ.get("DB_USER", "")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

# ─────────────────────────────────────────────────────────────────────────────
# Bedrock Model Configuration
# ─────────────────────────────────────────────────────────────────────────────

AVAILABLE_MODELS = [
    {"id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0", "name": "Claude Sonnet 4.5", "tier": "balanced"},
    {"id": "us.anthropic.claude-sonnet-5", "name": "Claude Sonnet 5", "tier": "advanced"},
    {"id": "us.anthropic.claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "tier": "balanced"},
    {"id": "us.anthropic.claude-haiku-4-5-20251001-v1:0", "name": "Claude Haiku 4.5", "tier": "fast"},
    {"id": "us.anthropic.claude-opus-4-8", "name": "Claude Opus 4.8", "tier": "advanced"},
]

# Bedrock region override (useful when the agent runs in a region without Bedrock)
BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "")


def get_model_id() -> str:
    """Get the current active model ID."""
    return BEDROCK_MODEL_ID


def set_model_id(model_id: str):
    """Set the active model ID at runtime."""
    global BEDROCK_MODEL_ID
    BEDROCK_MODEL_ID = model_id


# ─────────────────────────────────────────────────────────────────────────────
# Progressive Rollout Configuration
# ─────────────────────────────────────────────────────────────────────────────

PROGRESSIVE_ROLLOUT = {
    "dev": {"auto_approve": True, "bake_time_hours": 0},
    "staging": {"auto_approve": True, "bake_time_hours": 24},
    "production_canary": {"auto_approve": True, "bake_time_hours": 4},
    "production_fleet": {"auto_approve": False, "bake_time_hours": 0},
}

HEALTH_THRESHOLDS = {
    "p99_latency_regression_pct": 20,
    "error_rate_max_pct": 1.0,
    "replication_lag_max_ms": 500,
    "connection_success_min_pct": 99.0,
}

ROLLBACK_TRIGGERS = {
    "p99_regression_pct": 20,
    "error_spike_pct": 5.0,
    "connection_failure_pct": 2.0,
}

# ─────────────────────────────────────────────────────────────────────────────
# Notification & Approval Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Slack webhook for approval requests (alternative to SNS email)
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Approval timeout in hours (auto-deny after this period)
APPROVAL_TIMEOUT_HOURS = int(os.environ.get("APPROVAL_TIMEOUT_HOURS", "48"))

# ─────────────────────────────────────────────────────────────────────────────
# Deployment Metadata (set by CDK/CloudFormation at deploy time)
# ─────────────────────────────────────────────────────────────────────────────

DEPLOYMENT_ACCOUNT = os.environ.get("DEPLOYMENT_ACCOUNT", "")
DEPLOYMENT_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
STACK_NAME = os.environ.get("STACK_NAME", "aurora-patch-agent")
AGENT_VERSION = os.environ.get("AGENT_VERSION", "1.0.0")


# ─────────────────────────────────────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────────────────────────────────────

def get_target_accounts() -> list:
    """Parse target accounts from environment configuration.

    Returns list of TargetAccount-compatible dicts. Empty list means
    operate on local account only.
    """
    if not TARGET_ACCOUNTS_JSON:
        return []

    try:
        accounts_raw = json.loads(TARGET_ACCOUNTS_JSON)
        if not isinstance(accounts_raw, list):
            logger.warning("TARGET_ACCOUNTS must be a JSON array")
            return []

        from shared.multi_account import TargetAccount
        accounts = []
        for acct in accounts_raw:
            accounts.append(TargetAccount(
                account_id=acct["account_id"],
                role_arn=acct["role_arn"],
                regions=acct.get("regions", DEFAULT_SCAN_REGIONS),
                alias=acct.get("alias", ""),
                environment=acct.get("environment", ""),
                external_id=acct.get("external_id", ""),
            ))
        return accounts

    except (json.JSONDecodeError, KeyError) as e:
        logger.error(f"Failed to parse TARGET_ACCOUNTS: {e}")
        return []


def get_scan_regions() -> list[str]:
    """Get the list of regions to scan for the local account."""
    return DEFAULT_SCAN_REGIONS


def get_bedrock_region() -> str:
    """Get the region for Bedrock API calls.

    Returns BEDROCK_REGION if set, otherwise falls back to deployment region.
    Useful when deploying the agent in a region without Bedrock availability.
    """
    return BEDROCK_REGION or DEPLOYMENT_REGION
