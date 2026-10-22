# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Outcome Learning — The agent gets smarter over time.

This module tracks the results of upgrades, error diagnoses, and fixes,
building a knowledge base that improves future recommendations.

HOW IT WORKS:
1. After each upgrade: record version pair, strategy, duration, success/failure,
   performance delta. Future risk scores use this history.

2. After each successful error diagnosis + fix: persist the error pattern → fix
   mapping. Next time the same error appears → instant resolution, no Bedrock call.

3. After each rollback: record what went wrong and which version/extension combo
   caused it. Future assessments flag these combos as higher risk.

WHAT GETS LEARNED:
- Upgrade duration estimates (per version pair × cluster size × strategy)
- Error → fix mappings (novel errors that Bedrock resolved successfully)
- Risk amplifiers (version + extension combos that have historically failed)
- Performance baselines (expected P99 per workload type post-upgrade)

STORAGE: DynamoDB with pk=LEARNING#{category}, sk=<specific key>
"""
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from shared.aws_clients import dynamodb_resource
from shared.config import DYNAMODB_TABLE

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Outcome Recording
# ─────────────────────────────────────────────────────────────────────────────


def record_upgrade_outcome(
    cluster_id: str,
    from_version: str,
    to_version: str,
    strategy: str,
    success: bool,
    duration_minutes: float,
    instance_class: str = "",
    storage_gb: float = 0,
    extensions: list[str] = None,
    performance_delta: dict = None,
    failure_reason: str = "",
    rollback: bool = False,
):
    """Record the outcome of an upgrade for future learning.

    This data feeds into:
    - Duration estimation for future upgrades of the same version pair
    - Risk scoring (failure rate per version + extension combo)
    - Strategy selection confidence (which strategy works best for similar clusters)
    """
    outcome = {
        "cluster_id": cluster_id,
        "from_version": from_version,
        "to_version": to_version,
        "strategy": strategy,
        "success": success,
        "duration_minutes": duration_minutes,
        "instance_class": instance_class,
        "storage_gb": storage_gb,
        "extensions": extensions or [],
        "performance_delta": performance_delta or {},
        "failure_reason": failure_reason,
        "rollback": rollback,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }

    # Primary key: version pair for fast lookup
    version_key = f"{from_version}#to#{to_version}"

    _persist_learning(
        category="UPGRADE_OUTCOME",
        key=f"{version_key}#{cluster_id}#{int(time.time())}",
        data=outcome,
        ttl_days=365,  # Keep upgrade history for 1 year
    )

    logger.info(
        f"Recorded upgrade outcome: {cluster_id} {from_version}→{to_version} "
        f"strategy={strategy} success={success} duration={duration_minutes:.1f}min"
    )

    # Update aggregated stats for this version pair
    _update_version_pair_stats(version_key, outcome)


def record_error_resolution(
    error_pattern: str,
    diagnosis: str,
    fix_applied: str,
    fix_succeeded: bool,
    cluster_id: str = "",
    context: dict = None,
):
    """Record a successful error diagnosis + fix for future instant resolution.

    When Bedrock diagnoses a novel error and the fix works, we persist it.
    Next time the same error pattern appears → use the learned fix directly
    (faster, cheaper, no Bedrock call needed).
    """
    resolution = {
        "error_pattern": error_pattern,
        "diagnosis": diagnosis,
        "fix_applied": fix_applied,
        "fix_succeeded": fix_succeeded,
        "cluster_id": cluster_id,
        "context": context or {},
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "use_count": 1,
        "last_used": datetime.now(timezone.utc).isoformat(),
    }

    # Normalize the error pattern for matching
    normalized_pattern = _normalize_error_pattern(error_pattern)

    if fix_succeeded:
        _persist_learning(
            category="ERROR_RESOLUTION",
            key=normalized_pattern,
            data=resolution,
            ttl_days=180,  # Keep learned fixes for 6 months
        )
        logger.info(f"Learned new error resolution: '{normalized_pattern[:60]}...' → fix succeeded")
    else:
        # Record failure too — so we don't recommend the same bad fix again
        _persist_learning(
            category="ERROR_RESOLUTION_FAILED",
            key=f"{normalized_pattern}#{int(time.time())}",
            data=resolution,
            ttl_days=90,
        )
        logger.info(f"Recorded failed fix attempt for: '{normalized_pattern[:60]}...'")


def record_rollback_cause(
    cluster_id: str,
    version: str,
    extensions: list[str],
    reason: str,
    health_metrics: dict = None,
):
    """Record what caused a rollback — used to flag risky combos in future assessments.

    If the same version + extension combination causes rollbacks multiple times,
    future assessments will amplify the risk score for similar clusters.
    """
    cause = {
        "cluster_id": cluster_id,
        "version": version,
        "extensions": extensions,
        "reason": reason,
        "health_metrics": health_metrics or {},
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }

    # Key by extension combo (sorted) + version for pattern matching
    ext_key = ",".join(sorted(extensions)) if extensions else "none"
    key = f"{version}#{ext_key}#{int(time.time())}"

    _persist_learning(
        category="ROLLBACK_CAUSE",
        key=key,
        data=cause,
        ttl_days=365,
    )

    logger.info(f"Recorded rollback cause: {cluster_id} v{version} extensions={ext_key}")


# ─────────────────────────────────────────────────────────────────────────────
# Knowledge Retrieval — Used by agents to make smarter decisions
# ─────────────────────────────────────────────────────────────────────────────


def lookup_learned_fix(error_message: str) -> Optional[dict]:
    """Check if we have a previously successful fix for this error pattern.

    Returns the fix details if found (diagnosis, fix_applied, confidence),
    or None if this is a novel error requiring Bedrock.

    This is the main optimization: skip the ~6s Bedrock call for known errors.
    """
    normalized = _normalize_error_pattern(error_message)

    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        response = table.get_item(
            Key={"pk": "LEARNING#ERROR_RESOLUTION", "sk": normalized}
        )
        item = response.get("Item")
        if not item:
            return None

        learned = json.loads(item["data"])

        # Only return fixes that have actually succeeded
        if not learned.get("fix_succeeded"):
            return None

        # Bump usage counter
        _increment_use_count(normalized)

        logger.info(
            f"Using learned fix for '{normalized[:50]}...' "
            f"(used {learned.get('use_count', 1)} times before)"
        )

        return {
            "diagnosis": learned["diagnosis"],
            "remediation": learned["fix_applied"],
            "auto_fixable": True,
            "source": "learned",
            "confidence": _calculate_fix_confidence(learned),
            "times_used": learned.get("use_count", 1),
            "last_used": learned.get("last_used", ""),
        }

    except Exception as e:
        logger.warning(f"Failed to lookup learned fix: {e}")
        return None


def get_upgrade_stats(from_version: str, to_version: str) -> Optional[dict]:
    """Get historical stats for a version pair.

    Returns aggregated data: success rate, average duration, common failure reasons.
    Used by the Assessment and Planner agents to make better estimates.
    """
    version_key = f"{from_version}#to#{to_version}"

    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        response = table.get_item(
            Key={"pk": "LEARNING#VERSION_STATS", "sk": version_key}
        )
        item = response.get("Item")
        if not item:
            return None

        stats = json.loads(item["data"])
        return stats

    except Exception as e:
        logger.warning(f"Failed to get upgrade stats: {e}")
        return None


def get_risk_amplifiers(version: str, extensions: list[str]) -> list[dict]:
    """Check if this version + extension combo has historically caused rollbacks.

    Returns a list of risk factors based on past outcomes. Used by the
    Assessment agent to amplify risk scores for known-bad combinations.
    """
    ext_key = ",".join(sorted(extensions)) if extensions else "none"
    amplifiers = []

    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)

        # Query rollback causes for this version
        response = table.query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": "LEARNING#ROLLBACK_CAUSE",
                ":prefix": f"{version}#",
            },
        )

        for item in response.get("Items", []):
            cause = json.loads(item["data"])
            cause_extensions = set(cause.get("extensions", []))
            current_extensions = set(extensions)

            # Check for overlap — if the cluster has extensions that previously caused rollbacks
            overlap = cause_extensions & current_extensions
            if overlap:
                amplifiers.append({
                    "type": "historical_rollback",
                    "extensions_involved": list(overlap),
                    "previous_reason": cause.get("reason", "Unknown"),
                    "previous_cluster": cause.get("cluster_id", ""),
                    "risk_increase": 15,  # Add 15 points to risk score
                })

    except Exception as e:
        logger.warning(f"Failed to get risk amplifiers: {e}")

    return amplifiers


def get_duration_estimate(
    from_version: str,
    to_version: str,
    strategy: str,
    storage_gb: float = 0,
) -> Optional[dict]:
    """Estimate upgrade duration based on historical outcomes.

    Returns estimated minutes with confidence interval, or None if no history.
    """
    stats = get_upgrade_stats(from_version, to_version)
    if not stats:
        return None

    strategy_stats = stats.get("by_strategy", {}).get(strategy, {})
    if not strategy_stats or strategy_stats.get("count", 0) == 0:
        return None

    avg_duration = strategy_stats.get("avg_duration_minutes", 0)
    max_duration = strategy_stats.get("max_duration_minutes", 0)
    count = strategy_stats.get("count", 0)

    # Adjust estimate based on storage size (larger = longer sync time for B/G)
    size_multiplier = 1.0
    if storage_gb > 500 and strategy == "blue_green":
        size_multiplier = 1.5
    elif storage_gb > 1000 and strategy == "blue_green":
        size_multiplier = 2.0

    return {
        "estimated_minutes": round(avg_duration * size_multiplier, 1),
        "worst_case_minutes": round(max_duration * size_multiplier, 1),
        "based_on_samples": count,
        "confidence": "high" if count >= 5 else "medium" if count >= 2 else "low",
    }


def get_strategy_success_rates(from_version: str, to_version: str) -> dict:
    """Get success rates per strategy for a version pair.

    Helps the Advisor agent recommend the strategy with the best track record.
    """
    stats = get_upgrade_stats(from_version, to_version)
    if not stats:
        return {}

    return {
        strategy: {
            "success_rate": s.get("success_rate", 0),
            "total_attempts": s.get("count", 0),
            "avg_duration": s.get("avg_duration_minutes", 0),
        }
        for strategy, s in stats.get("by_strategy", {}).items()
    }


# ─────────────────────────────────────────────────────────────────────────────
# Internal Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _normalize_error_pattern(error: str) -> str:
    """Normalize an error message for consistent matching.

    Strips variable parts (timestamps, IDs, ARNs) to match on the pattern itself.
    """
    import re

    # Remove timestamps
    pattern = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.\d]*Z?', '<TIMESTAMP>', error)
    # Remove ARNs
    pattern = re.sub(r'arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:[^\s,)]+', '<ARN>', pattern)
    # Remove UUIDs
    pattern = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '<UUID>', pattern)
    # Remove specific resource IDs
    pattern = re.sub(r'(cluster|instance|deployment|snapshot)-[a-z0-9-]+', r'\1-<ID>', pattern)
    # Remove IP addresses
    pattern = re.sub(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', '<IP>', pattern)
    # Lowercase for consistent matching
    pattern = pattern.lower().strip()
    # Truncate to reasonable length for DynamoDB key
    return pattern[:200]


def _calculate_fix_confidence(learned: dict) -> float:
    """Calculate confidence in a learned fix based on usage history."""
    use_count = learned.get("use_count", 1)
    if use_count >= 10:
        return 98.0
    elif use_count >= 5:
        return 95.0
    elif use_count >= 3:
        return 90.0
    elif use_count >= 2:
        return 85.0
    return 80.0


def _update_version_pair_stats(version_key: str, outcome: dict):
    """Update aggregated statistics for a version pair."""
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)

        # Get existing stats
        response = table.get_item(
            Key={"pk": "LEARNING#VERSION_STATS", "sk": version_key}
        )
        item = response.get("Item")
        stats = json.loads(item["data"]) if item else {
            "version_key": version_key,
            "total_attempts": 0,
            "total_successes": 0,
            "total_failures": 0,
            "by_strategy": {},
            "common_failure_reasons": [],
        }

        # Update totals
        stats["total_attempts"] += 1
        if outcome["success"]:
            stats["total_successes"] += 1
        else:
            stats["total_failures"] += 1
            if outcome.get("failure_reason"):
                reasons = stats.get("common_failure_reasons", [])
                reasons.append(outcome["failure_reason"])
                stats["common_failure_reasons"] = reasons[-20:]  # Keep last 20

        # Update per-strategy stats
        strategy = outcome["strategy"]
        strat_stats = stats.get("by_strategy", {}).get(strategy, {
            "count": 0, "successes": 0, "total_duration": 0,
            "max_duration_minutes": 0, "success_rate": 0,
        })
        strat_stats["count"] += 1
        if outcome["success"]:
            strat_stats["successes"] += 1
        strat_stats["total_duration"] += outcome.get("duration_minutes", 0)
        strat_stats["avg_duration_minutes"] = (
            strat_stats["total_duration"] / strat_stats["count"]
        )
        if outcome.get("duration_minutes", 0) > strat_stats.get("max_duration_minutes", 0):
            strat_stats["max_duration_minutes"] = outcome["duration_minutes"]
        strat_stats["success_rate"] = (
            (strat_stats["successes"] / strat_stats["count"]) * 100
        )
        stats.setdefault("by_strategy", {})[strategy] = strat_stats

        stats["last_updated"] = datetime.now(timezone.utc).isoformat()

        # Persist updated stats
        table.put_item(Item={
            "pk": "LEARNING#VERSION_STATS",
            "sk": version_key,
            "data": json.dumps(stats, default=str),
            "ttl": int(time.time()) + 86400 * 365,
        })

    except Exception as e:
        logger.warning(f"Failed to update version pair stats: {e}")


def _increment_use_count(normalized_pattern: str):
    """Increment the use counter for a learned fix."""
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        response = table.get_item(
            Key={"pk": "LEARNING#ERROR_RESOLUTION", "sk": normalized_pattern}
        )
        item = response.get("Item")
        if item:
            data = json.loads(item["data"])
            data["use_count"] = data.get("use_count", 0) + 1
            data["last_used"] = datetime.now(timezone.utc).isoformat()
            table.put_item(Item={
                **item,
                "data": json.dumps(data, default=str),
            })
    except Exception:
        pass  # Non-critical — don't fail on counter increment


def _persist_learning(category: str, key: str, data: dict, ttl_days: int = 365):
    """Write learning data to DynamoDB."""
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        table.put_item(Item={
            "pk": f"LEARNING#{category}",
            "sk": key,
            "data": json.dumps(data, default=str),
            "category": category,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "ttl": int(time.time()) + 86400 * ttl_days,
        })
    except Exception as e:
        logger.warning(f"Failed to persist learning data [{category}]: {e}")
