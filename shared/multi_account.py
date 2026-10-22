# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Multi-account AWS session management.

Enables the patch agent to operate across multiple AWS accounts by assuming
roles in target accounts. Follows AWS Well-Architected best practices:
- Least-privilege cross-account roles
- Session caching with automatic refresh
- Organization-level discovery support
"""
import logging
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

import boto3
from botocore.credentials import RefreshableCredentials
from botocore.session import get_session

from shared.config import get_target_accounts

logger = logging.getLogger(__name__)

# Session cache: {account_id: (session, expiry_time)}
_session_cache: dict[str, tuple[boto3.Session, float]] = {}
_SESSION_TTL_SECONDS = 3500  # Refresh 100s before 1hr expiry


@dataclass
class TargetAccount:
    """Represents an AWS account the agent can operate against."""
    account_id: str
    role_arn: str
    regions: list[str] = field(default_factory=lambda: ["us-east-1"])
    alias: str = ""
    environment: str = ""  # prod, staging, dev — applies to all clusters in this account if set
    external_id: str = ""  # Optional external ID for cross-account trust

    @property
    def role_session_name(self) -> str:
        return f"aurora-patch-agent-{self.account_id}"


def get_cross_account_session(account: TargetAccount) -> boto3.Session:
    """Get a boto3 session for a target account using AssumeRole.

    Sessions are cached and refreshed automatically before expiry.
    """
    cache_key = f"{account.account_id}:{account.role_arn}"

    # Check cache
    if cache_key in _session_cache:
        session, expiry = _session_cache[cache_key]
        if time.time() < expiry:
            return session

    # Assume role in target account
    sts = boto3.client("sts")
    assume_params = {
        "RoleArn": account.role_arn,
        "RoleSessionName": account.role_session_name,
        "DurationSeconds": 3600,
    }
    if account.external_id:
        assume_params["ExternalId"] = account.external_id

    try:
        response = sts.assume_role(**assume_params)
        credentials = response["Credentials"]

        session = boto3.Session(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )

        # Cache with TTL
        _session_cache[cache_key] = (session, time.time() + _SESSION_TTL_SECONDS)
        logger.info(f"Assumed role in account {account.account_id} ({account.alias or 'no alias'})")
        return session

    except Exception as e:
        logger.error(f"Failed to assume role {account.role_arn} in account {account.account_id}: {e}")
        raise


def get_client_for_account(
    service: str,
    account: TargetAccount,
    region: Optional[str] = None,
):
    """Get a boto3 client for a specific service in a target account.

    Args:
        service: AWS service name (e.g., 'rds', 'cloudwatch')
        account: Target account configuration
        region: Override region (defaults to first region in account config)
    """
    session = get_cross_account_session(account)
    target_region = region or account.regions[0]
    return session.client(service, region_name=target_region)


def get_resource_for_account(
    service: str,
    account: TargetAccount,
    region: Optional[str] = None,
):
    """Get a boto3 resource for a specific service in a target account."""
    session = get_cross_account_session(account)
    target_region = region or account.regions[0]
    return session.resource(service, region_name=target_region)


def discover_organization_accounts() -> list[TargetAccount]:
    """Discover accounts via AWS Organizations.

    Requires the orchestrator account to have organizations:ListAccounts permission.
    Returns accounts with a default role ARN pattern (configurable via config).
    """
    from shared.config import CROSS_ACCOUNT_ROLE_NAME

    try:
        org_client = boto3.client("organizations")
        paginator = org_client.get_paginator("list_accounts")
        accounts = []

        for page in paginator.paginate():
            for acct in page["Accounts"]:
                if acct["Status"] != "ACTIVE":
                    continue
                # Skip the orchestrator account itself
                sts = boto3.client("sts")
                caller = sts.get_caller_identity()
                if acct["Id"] == caller["Account"]:
                    continue

                accounts.append(TargetAccount(
                    account_id=acct["Id"],
                    role_arn=f"arn:aws:iam::{acct['Id']}:role/{CROSS_ACCOUNT_ROLE_NAME}",
                    alias=acct.get("Name", ""),
                ))

        logger.info(f"Discovered {len(accounts)} accounts via Organizations")
        return accounts

    except Exception as e:
        logger.warning(f"Organization discovery failed (may not have permissions): {e}")
        return []


def resolve_target_accounts() -> list[TargetAccount]:
    """Resolve target accounts from configuration or Organizations.

    Priority:
    1. Explicitly configured accounts (from config/env)
    2. Organization discovery (if enabled and no explicit accounts)
    3. Local account only (fallback)
    """
    # Check explicit configuration first
    configured = get_target_accounts()
    if configured:
        return configured

    # Try Organizations discovery
    org_accounts = discover_organization_accounts()
    if org_accounts:
        return org_accounts

    # Fallback: operate on the local account only (no cross-account)
    logger.info("No cross-account configuration found — operating on local account only")
    return []


def clear_session_cache():
    """Clear all cached sessions. Useful for testing or credential rotation."""
    global _session_cache
    _session_cache = {}
