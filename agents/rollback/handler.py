# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Rollback Agent — Monitors upgraded clusters and RECOMMENDS rollback on anomaly.

PHILOSOPHY: Even when health degrades, the agent does NOT auto-revert.
It surfaces the health degradation, explains the risk, and recommends
rollback — but the user makes the final call.

Why? Because:
1. Transient spikes happen — auto-rollback on a 30s spike wastes hours of work
2. The user may know about planned load tests or deployments
3. A rollback itself carries risk (DNS propagation, connection drops)
4. The user may prefer to investigate before reverting
"""
import json
import logging
from datetime import datetime, timezone

from shared.aws_clients import rds_client, cloudwatch_client, dynamodb_resource, sns_client
from shared.config import DYNAMODB_TABLE, ROLLBACK_TRIGGERS, SNS_APPROVAL_TOPIC
from shared.models import UpgradeStatus, RiskLevel
from shared.recommendations import recommend_rollback, require_approval, is_approved
from agents.validation.handler import run_health_checks

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event, context):
    """Monitor cluster health and recommend rollback if needed."""
    cluster_id = event["cluster_id"]
    region = event["region"]
    deployment_id = event["deployment_id"]
    baseline = event.get("baseline", {})
    action = event.get("action", "monitor")

    if action == "monitor":
        return monitor_and_recommend(cluster_id, region, deployment_id, baseline)
    elif action == "rollback":
        # Only execute rollback if explicitly approved
        recommendation_id = event.get("recommendation_id")
        if recommendation_id and not is_approved(recommendation_id):
            return {
                "status": "blocked",
                "message": "Rollback not approved. Approve the recommendation first.",
                "recommendation_id": recommendation_id,
            }
        return execute_rollback(cluster_id, region, deployment_id)
    else:
        raise ValueError(f"Unknown action: {action}")


def monitor_and_recommend(cluster_id: str, region: str, deployment_id: str, baseline: dict) -> dict:
    """Check health and RECOMMEND rollback if thresholds are breached.

    Does NOT auto-rollback. Surfaces the finding to the user with context.
    """
    health = run_health_checks(cluster_id, region, baseline)

    should_recommend_rollback = False
    reasons = []
    severity = "medium"

    if not health.connections_ok:
        should_recommend_rollback = True
        severity = "critical"
        reasons.append("Cluster connections are failing — database may be unreachable")

    if health.error_rate_pct > ROLLBACK_TRIGGERS["error_spike_pct"]:
        should_recommend_rollback = True
        reasons.append(
            f"Error rate is {health.error_rate_pct:.1f}% "
            f"(threshold: {ROLLBACK_TRIGGERS['error_spike_pct']}%)"
        )

    if health.baseline_p99_ms > 0 and health.p99_latency_ms > 0:
        regression = ((health.p99_latency_ms - health.baseline_p99_ms) / health.baseline_p99_ms) * 100
        if regression > ROLLBACK_TRIGGERS["p99_regression_pct"]:
            should_recommend_rollback = True
            reasons.append(
                f"P99 latency regressed {regression:.1f}% "
                f"(threshold: {ROLLBACK_TRIGGERS['p99_regression_pct']}%)"
            )

    result = {
        "cluster_id": cluster_id,
        "check_time": datetime.now(timezone.utc).isoformat(),
        "health_passed": health.passed,
        "should_recommend_rollback": should_recommend_rollback,
        "reasons": reasons,
        "action_taken": None,
        "health_metrics": {
            "connections_ok": health.connections_ok,
            "error_rate_pct": health.error_rate_pct,
            "p99_latency_ms": health.p99_latency_ms,
            "baseline_p99_ms": health.baseline_p99_ms,
            "replication_lag_ms": health.replication_lag_ms,
        },
    }

    if should_recommend_rollback:
        logger.warning(f"ROLLBACK RECOMMENDED for {cluster_id}: {reasons}")

        # Create recommendation — user must approve before execution
        risk = RiskLevel.CRITICAL if severity == "critical" else RiskLevel.HIGH
        confidence = 95.0 if severity == "critical" else 85.0

        rec = recommend_rollback(
            cluster_id=cluster_id,
            deployment_id=deployment_id,
            reasoning=(
                f"Health degradation detected after upgrade:\n"
                + "\n".join(f"  • {r}" for r in reasons)
                + f"\n\nThe agent recommends rolling back to the previous version. "
                f"This will {'delete the green deployment (original cluster untouched)' if True else 'require endpoint update'}."
            ),
            health_metrics=result["health_metrics"],
            risk_level=risk,
            confidence=confidence,
        )

        result["action_taken"] = "rollback_recommended"
        result["recommendation_id"] = rec.id

        # Notify via SNS (informational — not an action)
        _notify_rollback_recommendation(cluster_id, region, reasons, health, rec.id)
    else:
        result["action_taken"] = "continue_bake"

    _update_state(cluster_id, result)
    return result


def execute_rollback(cluster_id: str, region: str, deployment_id: str) -> dict:
    """Rollback by switching back to the blue (original) environment.

    For Blue/Green deployments, if switchover hasn't happened yet, we simply
    delete the green deployment. If switchover already happened, we need to
    use the old blue cluster that's now renamed.
    """
    client = rds_client(region)

    try:
        response = client.describe_blue_green_deployments(
            BlueGreenDeploymentIdentifier=deployment_id,
        )

        if not response["BlueGreenDeployments"]:
            return {"status": "not_found", "message": "Deployment not found — may already be cleaned up"}

        deployment = response["BlueGreenDeployments"][0]
        status = deployment["Status"]

        if status == "SWITCHOVER_COMPLETED":
            # Post-switchover: the old blue is renamed with -old suffix
            # Delete the deployment (keeps both clusters), then rename
            client.delete_blue_green_deployment(
                BlueGreenDeploymentIdentifier=deployment_id,
                DeleteTarget=False,
            )
            _update_state(cluster_id, {
                "status": UpgradeStatus.ROLLED_BACK.value,
                "reason": "Rolled back post-switchover — old cluster preserved",
            })
            return {"status": "rolled_back", "note": "Old blue cluster preserved. Manual DNS/endpoint update may be needed."}

        elif status == "AVAILABLE":
            # Pre-switchover: just delete the green environment
            client.delete_blue_green_deployment(
                BlueGreenDeploymentIdentifier=deployment_id,
                DeleteTarget=True,
            )
            _update_state(cluster_id, {
                "status": UpgradeStatus.ROLLED_BACK.value,
                "reason": "Green environment deleted — original cluster unchanged",
            })
            return {"status": "rolled_back", "note": "Green deleted. Original blue cluster is untouched."}

        else:
            return {"status": "in_progress", "message": f"Deployment in state '{status}' — cannot rollback yet"}

    except Exception as e:
        logger.error(f"Rollback failed for {cluster_id}: {e}")
        return {"status": "error", "message": str(e)}


def _notify_rollback_recommendation(
    cluster_id: str, region: str, reasons: list[str], health, recommendation_id: str
):
    """Publish rollback RECOMMENDATION to SNS — informational, not an action confirmation."""
    if not SNS_APPROVAL_TOPIC:
        logger.info("No SNS topic configured — skipping notification")
        return

    try:
        sns_client(region).publish(
            TopicArn=SNS_APPROVAL_TOPIC,
            Subject=f"[Aurora Patch Agent] ROLLBACK RECOMMENDED: {cluster_id}",
            Message=json.dumps({
                "type": "ROLLBACK_RECOMMENDATION",
                "cluster_id": cluster_id,
                "region": region,
                "reasons": reasons,
                "health_metrics": {
                    "error_rate_pct": getattr(health, "error_rate_pct", None),
                    "p99_latency_ms": getattr(health, "p99_latency_ms", None),
                    "connections_ok": getattr(health, "connections_ok", None),
                },
                "recommendation_id": recommendation_id,
                "time": datetime.now(timezone.utc).isoformat(),
                "action_required": "Approve or reject the rollback recommendation in the UI",
                "note": "No action has been taken. The cluster is still running on the upgraded version.",
            }, indent=2),
        )
        logger.info(f"Rollback recommendation notification sent for {cluster_id}")
    except Exception as e:
        logger.warning(f"Failed to send rollback recommendation notification: {e}")


def _update_state(cluster_id: str, details: dict):
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        table.put_item(Item={
            "pk": f"ROLLBACK#{cluster_id}",
            "sk": datetime.now(timezone.utc).isoformat(),
            "data": json.dumps(details, default=str),
            "ttl": int(datetime.now(timezone.utc).timestamp()) + 86400 * 30,
        })
    except Exception as e:
        logger.warning(f"State update failed: {e}")
