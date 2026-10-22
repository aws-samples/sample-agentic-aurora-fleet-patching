# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Persistent CVE cache backed by DynamoDB.

Solves the problem of Lambda cold starts losing the in-memory NVD cache.
If the live NVD API is unavailable, the agent falls back to the last-known-good
cached data from DynamoDB, ensuring CVE scanning always has data.

Cache hierarchy:
1. In-memory (hot path, ~0ms)
2. DynamoDB (warm path, ~10ms, survives cold starts)
3. Live NVD API (cold path, ~2s, refreshes both caches)
4. Static KNOWN_CVES (last resort, always available)
"""
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from shared.aws_clients import dynamodb_resource
from shared.config import DYNAMODB_TABLE

logger = logging.getLogger(__name__)

_CACHE_PK = "CVE_CACHE"
_CACHE_SK = "nvd_postgresql"
_CACHE_TTL_HOURS = 24  # DynamoDB cache valid for 24 hours


def get_cached_cves() -> Optional[dict]:
    """Retrieve CVE data from DynamoDB cache.

    Returns the cached CVE dict if it exists and is not expired,
    otherwise returns None.
    """
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        response = table.get_item(
            Key={"pk": _CACHE_PK, "sk": _CACHE_SK},
        )
        item = response.get("Item")
        if not item:
            return None

        # Check if cache is still valid
        cached_at = item.get("cached_at", 0)
        age_hours = (time.time() - float(cached_at)) / 3600
        if age_hours > _CACHE_TTL_HOURS:
            logger.info(f"DynamoDB CVE cache expired ({age_hours:.1f}h old)")
            return None

        cve_data = json.loads(item.get("data", "{}"))
        logger.info(f"Using DynamoDB CVE cache ({len(cve_data)} CVEs, {age_hours:.1f}h old)")
        return cve_data

    except Exception as e:
        logger.warning(f"Failed to read CVE cache from DynamoDB: {e}")
        return None


def persist_cves(cve_data: dict):
    """Write CVE data to DynamoDB cache.

    Called after a successful NVD API fetch to ensure the data survives
    Lambda cold starts and NVD outages.
    """
    if not cve_data:
        return

    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        table.put_item(Item={
            "pk": _CACHE_PK,
            "sk": _CACHE_SK,
            "data": json.dumps(cve_data, default=str),
            "cached_at": str(time.time()),
            "cve_count": len(cve_data),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            # Keep cache for 7 days in DynamoDB (even if stale, better than nothing)
            "ttl": int(time.time()) + 86400 * 7,
        })
        logger.info(f"Persisted {len(cve_data)} CVEs to DynamoDB cache")

    except Exception as e:
        logger.warning(f"Failed to persist CVE cache to DynamoDB: {e}")


def get_cves_with_fallback(fetch_live_fn, static_cves: dict) -> dict:
    """Get CVE data with full fallback chain.

    Args:
        fetch_live_fn: Callable that fetches live CVEs from NVD (returns dict)
        static_cves: Static KNOWN_CVES dict (last resort)

    Returns:
        Merged CVE dict (live > dynamodb > static)
    """
    # 1. Try live fetch
    live_cves = fetch_live_fn()
    if live_cves:
        # Persist to DynamoDB for future cold starts
        persist_cves(live_cves)
        # Merge: live overrides static
        merged = dict(static_cves)
        merged.update(live_cves)
        return merged

    # 2. Try DynamoDB cache
    cached_cves = get_cached_cves()
    if cached_cves:
        merged = dict(static_cves)
        merged.update(cached_cves)
        return merged

    # 3. Fall back to static data
    logger.warning("Using static CVE data only — NVD API and DynamoDB cache both unavailable")
    return dict(static_cves)
