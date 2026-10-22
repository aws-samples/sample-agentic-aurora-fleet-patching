# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Recommendation Engine — The agent suggests, the user decides.

This module is the central mechanism that enforces the "recommend, don't execute"
pattern. All agents call into this module to:

1. Create recommendations (proposed actions with reasoning)
2. Persist them for the UI to display
3. Check approval status before executing anything

DESIGN PRINCIPLE:
    The agent is an advisor. It does the hard work of analysis, correlates data
    across CVE feeds / CloudWatch / cluster state / past outcomes, and produces
    clear recommendations with confidence levels and alternatives.

    But it NEVER touches infrastructure without the user's explicit go-ahead.
    This keeps the human in the loop for all destructive or state-changing operations.

WHAT DOES NOT REQUIRE APPROVAL (read-only):
    - Fleet discovery (describe clusters)
    - CVE scanning (fetch + analyze)
    - Health checks (read CloudWatch metrics)
    - Analysis (read parameter groups, extensions)
    - Plan generation (purely computational)

WHAT REQUIRES APPROVAL (state-changing):
    - Create Blue/Green deployment
    - Execute switchover
    - Trigger rollback
    - Modify cluster (in-place upgrade)
    - Delete Blue/Green deployment
    - Any parameter group changes
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from shared.aws_clients import dynamodb_resource
from shared.config import DYNAMODB_TABLE, ACTION_MODE
from shared.models import Recommendation, RiskLevel

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Create Recommendations
# ─────────────────────────────────────────────────────────────────────────────


def recommend(
    action: str,
    cluster_id: str,
    reasoning: str,
    risk_level: RiskLevel = RiskLevel.LOW,
    confidence: float = 80.0,
    alternatives: list[str] = None,
    context: dict = None,
) -> Recommendation:
    """Create and persist a recommendation for user review.

    This is the primary interface all agents use to propose actions.

    Args:
        action: What the agent proposes (e.g., "upgrade_blue_green", "rollback",
                "switchover", "hold", "skip")
        cluster_id: Which cluster this affects
        reasoning: Plain-English explanation of WHY (shown to user)
        risk_level: How risky the proposed action is
        confidence: 0-100, how confident the agent is in this recommendation
        alternatives: Other valid choices the user could make
        context: Supporting data (metrics, versions, CVE IDs, etc.)

    Returns:
        The created Recommendation object (persisted to DynamoDB)
    """
    rec = Recommendation(
        id=f"rec-{uuid.uuid4().hex[:12]}",
        action=action,
        cluster_id=cluster_id,
        reasoning=reasoning,
        risk_level=risk_level,
        confidence=confidence,
        alternatives=alternatives or [],
        context=context or {},
        created_at=datetime.now(timezone.utc).isoformat(),
        status="pending",
    )

    _persist_recommendation(rec)
    logger.info(
        f"Recommendation created: [{rec.id}] {action} for {cluster_id} "
        f"(risk={risk_level.value}, confidence={confidence}%)"
    )
    return rec


def recommend_upgrade(
    cluster_id: str,
    target_version: str,
    strategy: str,
    reasoning: str,
    risk_level: RiskLevel = RiskLevel.LOW,
    confidence: float = 85.0,
    estimated_duration: str = "",
    pre_steps: list[str] = None,
    context: dict = None,
) -> Recommendation:
    """Recommend an upgrade for a specific cluster.

    Provides structured context for the UI to display a clear upgrade proposal.
    """
    full_context = {
        "target_version": target_version,
        "strategy": strategy,
        "estimated_duration": estimated_duration,
        "pre_upgrade_steps": pre_steps or [],
        **(context or {}),
    }

    alternatives = []
    if strategy == "blue_green":
        alternatives.append("Use in-place upgrade instead (faster but causes downtime)")
    elif strategy == "in_place":
        alternatives.append("Use Blue/Green deployment (zero-downtime but slower)")
    alternatives.append("Defer this upgrade to the next maintenance window")
    alternatives.append("Skip this cluster entirely")

    return recommend(
        action=f"upgrade_{strategy}",
        cluster_id=cluster_id,
        reasoning=reasoning,
        risk_level=risk_level,
        confidence=confidence,
        alternatives=alternatives,
        context=full_context,
    )


def recommend_rollback(
    cluster_id: str,
    deployment_id: str,
    reasoning: str,
    health_metrics: dict = None,
    risk_level: RiskLevel = RiskLevel.HIGH,
    confidence: float = 90.0,
) -> Recommendation:
    """Recommend a rollback — agent detected health degradation but awaits user decision.

    Even in critical situations, the user makes the call. The agent provides
    urgency context so the user can act quickly.
    """
    return recommend(
        action="rollback",
        cluster_id=cluster_id,
        reasoning=reasoning,
        risk_level=risk_level,
        confidence=confidence,
        alternatives=[
            "Wait and continue monitoring (risk: further degradation)",
            "Investigate manually before deciding",
            "Proceed despite health issues (override agent recommendation)",
        ],
        context={
            "deployment_id": deployment_id,
            "health_metrics": health_metrics or {},
            "urgency": "high" if confidence > 85 else "medium",
        },
    )


def recommend_switchover(
    cluster_id: str,
    deployment_id: str,
    reasoning: str,
    risk_level: RiskLevel = RiskLevel.MEDIUM,
    confidence: float = 90.0,
    context: dict = None,
) -> Recommendation:
    """Recommend executing a switchover — green is ready, agent suggests proceeding."""
    return recommend(
        action="switchover",
        cluster_id=cluster_id,
        reasoning=reasoning,
        risk_level=risk_level,
        confidence=confidence,
        alternatives=[
            "Delay switchover — run more validation first",
            "Cancel the Blue/Green deployment entirely",
            "Wait for a lower-traffic period before switching",
        ],
        context={
            "deployment_id": deployment_id,
            **(context or {}),
        },
    )


def recommend_plan(
    plan: dict,
    reasoning: str,
    confidence: float = 80.0,
) -> Recommendation:
    """Recommend a fleet-wide upgrade plan for user review.

    The plan itself is the recommendation — user can approve, modify, or reject.
    """
    return recommend(
        action="execute_plan",
        cluster_id="fleet",
        reasoning=reasoning,
        risk_level=RiskLevel.MEDIUM if plan.get("waves") else RiskLevel.LOW,
        confidence=confidence,
        alternatives=[
            "Modify the plan (re-sequence, add/remove clusters)",
            "Reject and re-generate with different constraints",
            "Approve only dev/staging waves — hold production for now",
        ],
        context={"plan": plan},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Approval Management
# ─────────────────────────────────────────────────────────────────────────────


def is_approved(recommendation_id: str) -> bool:
    """Check if a recommendation has been approved by the user."""
    rec = get_recommendation(recommendation_id)
    if not rec:
        return False
    return rec.get("status") == "approved"


def approve_recommendation(recommendation_id: str, approved_by: str = "user") -> dict:
    """Mark a recommendation as approved — allows execution to proceed."""
    return _update_recommendation_status(
        recommendation_id, "approved", approved_by=approved_by
    )


def reject_recommendation(recommendation_id: str, reason: str = "") -> dict:
    """Mark a recommendation as rejected — agent will not execute this action."""
    return _update_recommendation_status(
        recommendation_id, "rejected", reason=reason
    )


def get_pending_recommendations(cluster_id: Optional[str] = None) -> list[dict]:
    """Get all pending recommendations awaiting user decision.

    Args:
        cluster_id: Filter to a specific cluster (None = all clusters)
    """
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)

        # Query all pending recommendations
        response = table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": "RECOMMENDATIONS"},
            ScanIndexForward=False,  # Most recent first
        )

        items = response.get("Items", [])
        pending = [
            json.loads(item["data"]) for item in items
            if json.loads(item["data"]).get("status") == "pending"
        ]

        if cluster_id:
            pending = [r for r in pending if r.get("cluster_id") == cluster_id]

        return pending

    except Exception as e:
        logger.warning(f"Failed to fetch pending recommendations: {e}")
        return []


def get_recommendation(recommendation_id: str) -> Optional[dict]:
    """Retrieve a specific recommendation by ID."""
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        response = table.get_item(
            Key={"pk": "RECOMMENDATIONS", "sk": recommendation_id}
        )
        item = response.get("Item")
        if item:
            return json.loads(item["data"])
        return None
    except Exception as e:
        logger.warning(f"Failed to fetch recommendation {recommendation_id}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Guard: Check Mode Before Executing
# ─────────────────────────────────────────────────────────────────────────────


def require_approval(action: str, cluster_id: str) -> bool:
    """Check whether the current action mode requires user approval.

    Returns True if the agent should STOP and create a recommendation
    instead of executing directly.

    Read-only actions never require approval.
    """
    # Read-only actions are always allowed
    read_only_actions = {
        "discover", "scan", "analyze", "assess", "plan",
        "health_check", "describe", "list", "summarize",
    }
    if action in read_only_actions:
        return False

    # In recommend mode, all state-changing actions require approval
    if ACTION_MODE == "recommend":
        return True

    # In execute mode, only production actions require approval
    # (this mode is not the default — user must explicitly opt in)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Persistence
# ─────────────────────────────────────────────────────────────────────────────


def _persist_recommendation(rec: Recommendation):
    """Write recommendation to DynamoDB."""
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        table.put_item(Item={
            "pk": "RECOMMENDATIONS",
            "sk": rec.id,
            "data": json.dumps({
                "id": rec.id,
                "action": rec.action,
                "cluster_id": rec.cluster_id,
                "reasoning": rec.reasoning,
                "risk_level": rec.risk_level.value,
                "confidence": rec.confidence,
                "alternatives": rec.alternatives,
                "context": rec.context,
                "created_at": rec.created_at,
                "status": rec.status,
            }, default=str),
            "cluster_id": rec.cluster_id,
            "action": rec.action,
            "status": rec.status,
            "created_at": rec.created_at,
            "ttl": int(datetime.now(timezone.utc).timestamp()) + 86400 * 7,  # 7 day TTL
        })
    except Exception as e:
        logger.warning(f"Failed to persist recommendation {rec.id}: {e}")


def _update_recommendation_status(
    recommendation_id: str,
    status: str,
    approved_by: str = "",
    reason: str = "",
) -> dict:
    """Update the status of a recommendation."""
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)

        # Get current item
        response = table.get_item(
            Key={"pk": "RECOMMENDATIONS", "sk": recommendation_id}
        )
        item = response.get("Item")
        if not item:
            return {"error": f"Recommendation {recommendation_id} not found"}

        rec_data = json.loads(item["data"])
        rec_data["status"] = status
        rec_data["decided_at"] = datetime.now(timezone.utc).isoformat()
        if approved_by:
            rec_data["approved_by"] = approved_by
        if reason:
            rec_data["rejection_reason"] = reason

        table.put_item(Item={
            **item,
            "data": json.dumps(rec_data, default=str),
            "status": status,
        })

        logger.info(f"Recommendation {recommendation_id} → {status}")
        return rec_data

    except Exception as e:
        logger.error(f"Failed to update recommendation {recommendation_id}: {e}")
        return {"error": str(e)}
