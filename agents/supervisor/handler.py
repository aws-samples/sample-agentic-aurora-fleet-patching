# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Supervisor Agent — Orchestrates the progressive rollout across the fleet.

PHILOSOPHY: This agent ADVISES — it does not unilaterally act.
It receives events, uses Bedrock for reasoning at key decision points,
and surfaces recommendations to the user. The user approves or rejects.

The only exception: read-only operations (analysis, health checks) proceed
without approval since they don't modify infrastructure.
"""
import json
import logging
from datetime import datetime, timezone

from shared.aws_clients import bedrock_client, sns_client, dynamodb_resource, extract_bedrock_text
from shared.config import DYNAMODB_TABLE, BEDROCK_MODEL_ID, PROGRESSIVE_ROLLOUT, SNS_APPROVAL_TOPIC
from shared.models import Environment, UpgradeStatus, RiskLevel
from shared.recommendations import (
    recommend,
    recommend_plan,
    require_approval,
    get_pending_recommendations,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event, context):
    """Main decision handler — called by Step Functions at gate points."""
    action = event["action"]

    if action == "plan_rollout":
        return plan_rollout(event["assessments"])
    elif action == "gate_check":
        return gate_check(event)
    elif action == "request_approval":
        return request_approval(event)
    elif action == "handle_failure":
        return handle_failure(event)
    elif action == "summarize":
        return summarize_run(event)
    else:
        raise ValueError(f"Unknown supervisor action: {action}")


def plan_rollout(assessments: list[dict]) -> dict:
    """Given assessed clusters, produce an ordered rollout plan.

    Order: dev (lowest risk first) → staging → prod canary → prod fleet.
    Within each environment, order by risk score ascending.
    """
    env_order = {
        Environment.DEV.value: 0,
        Environment.STAGING.value: 1,
        Environment.PRODUCTION.value: 2,
    }

    sorted_clusters = sorted(
        assessments,
        key=lambda a: (env_order.get(a["environment"], 99), a["risk_score"]),
    )

    phases = []
    dev_clusters = [c for c in sorted_clusters if c["environment"] == "dev"]
    staging_clusters = [c for c in sorted_clusters if c["environment"] == "staging"]
    prod_clusters = [c for c in sorted_clusters if c["environment"] == "production"]

    if dev_clusters:
        phases.append({
            "phase": "dev",
            "clusters": dev_clusters,
            "auto_approve": True,
            "bake_time_hours": 0,
        })

    if staging_clusters:
        phases.append({
            "phase": "staging",
            "clusters": staging_clusters,
            "auto_approve": True,
            "bake_time_hours": PROGRESSIVE_ROLLOUT["staging"]["bake_time_hours"],
        })

    if prod_clusters:
        # First prod cluster is canary
        phases.append({
            "phase": "production_canary",
            "clusters": prod_clusters[:1],
            "auto_approve": True,
            "bake_time_hours": PROGRESSIVE_ROLLOUT["production_canary"]["bake_time_hours"],
        })
        if len(prod_clusters) > 1:
            phases.append({
                "phase": "production_fleet",
                "clusters": prod_clusters[1:],
                "auto_approve": False,
                "bake_time_hours": 0,
            })

    plan = {
        "plan_time": datetime.now(timezone.utc).isoformat(),
        "total_clusters": len(sorted_clusters),
        "total_phases": len(phases),
        "phases": phases,
        "blocked": [c for c in sorted_clusters if c.get("status") == UpgradeStatus.AWAITING_APPROVAL.value],
    }

    _persist_plan(plan)
    return plan


def gate_check(event: dict) -> dict:
    """Evaluate whether the next phase should proceed — surfaces recommendation.

    Checks:
    - All clusters in current phase completed successfully
    - Bake time has elapsed
    - No rollbacks occurred

    Returns analysis + recommendation. Does NOT auto-proceed.
    """
    phase = event["current_phase"]
    phase_results = event["phase_results"]
    bake_start = event.get("bake_start")
    bake_hours = event.get("bake_time_hours", 0)

    all_passed = all(r.get("passed", False) for r in phase_results)
    any_rolled_back = any(r.get("action_taken") == "rollback" for r in phase_results)

    if any_rolled_back:
        rec = recommend(
            action="halt_rollout",
            cluster_id="fleet",
            reasoning=(
                f"Phase '{phase}' had one or more rollbacks. "
                "This indicates the upgrade introduced a problem. "
                "Recommend halting the rollout until the root cause is investigated."
            ),
            risk_level=RiskLevel.HIGH,
            confidence=95.0,
            alternatives=[
                "Continue anyway (risk: same issue in later phases)",
                "Retry only the failed clusters",
                "Skip failed clusters, proceed with others",
            ],
            context={"phase": phase, "phase_results": phase_results},
        )
        return {
            "proceed": False,
            "reason": "One or more clusters rolled back in this phase",
            "recommendation_id": rec.id,
            "action": "awaiting_user_decision",
        }

    if not all_passed:
        failed = [r["cluster_id"] for r in phase_results if not r.get("passed")]
        analysis = _ask_bedrock_for_decision(phase, phase_results, failed)

        # Surface the AI analysis as a recommendation
        rec = recommend(
            action=analysis.get("action", "halt"),
            cluster_id="fleet",
            reasoning=(
                f"Phase '{phase}' completed with partial failures: {failed}. "
                f"AI analysis: {analysis.get('reason', 'No reasoning available')}"
            ),
            risk_level=RiskLevel.MEDIUM,
            confidence=float(analysis.get("confidence", 70)),
            alternatives=[
                "Proceed to next phase (accept partial failure)",
                "Retry failed clusters only",
                "Halt all remaining waves",
            ],
            context={"phase": phase, "failed_clusters": failed, "ai_analysis": analysis},
        )
        return {
            "proceed": False,
            "reason": f"Partial failures in phase {phase}: {failed}",
            "recommendation_id": rec.id,
            "action": "awaiting_user_decision",
            "ai_analysis": analysis,
        }

    if bake_hours > 0 and bake_start:
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(bake_start)).total_seconds() / 3600
        if elapsed < bake_hours:
            return {
                "proceed": False,
                "reason": f"Bake time: {elapsed:.1f}h / {bake_hours}h elapsed",
                "action": "wait",
                "remaining_hours": bake_hours - elapsed,
            }

    # All checks pass — recommend proceeding (still needs user approval)
    rec = recommend(
        action="proceed_to_next_phase",
        cluster_id="fleet",
        reasoning=(
            f"Phase '{phase}' completed successfully. All clusters passed validation. "
            f"Bake time requirements met. Ready to proceed to the next phase."
        ),
        risk_level=RiskLevel.LOW,
        confidence=95.0,
        alternatives=[
            "Wait longer before proceeding (extra bake time)",
            "Re-run validation before moving on",
            "Halt here — don't proceed to next phase",
        ],
        context={"phase": phase, "phase_results": phase_results},
    )
    return {
        "proceed": False,  # Still needs approval
        "reason": "All checks passed — awaiting user approval to proceed",
        "recommendation_id": rec.id,
        "action": "awaiting_user_decision",
    }


def request_approval(event: dict) -> dict:
    """Send approval request for production fleet upgrade."""
    phase = event["phase"]
    clusters = event["clusters"]
    canary_result = event.get("canary_result", {})

    message = {
        "type": "APPROVAL_REQUIRED",
        "phase": phase,
        "clusters": [c["cluster_id"] for c in clusters],
        "cluster_count": len(clusters),
        "canary_result": canary_result,
        "risk_summary": {c["cluster_id"]: c.get("risk_level", "unknown") for c in clusters},
        "action_url": event.get("approval_url", ""),
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }

    if SNS_APPROVAL_TOPIC:
        sns_client().publish(
            TopicArn=SNS_APPROVAL_TOPIC,
            Subject=f"[Aurora Patch Agent] Approval needed: {len(clusters)} production clusters",
            Message=json.dumps(message, indent=2),
        )

    return {
        "status": UpgradeStatus.AWAITING_APPROVAL.value,
        "approval_requested": True,
        "cluster_count": len(clusters),
    }


def handle_failure(event: dict) -> dict:
    """Analyze a failure and RECOMMEND next steps — user decides.

    The agent uses Bedrock to reason about the failure, but surfaces the
    recommendation to the user rather than auto-executing.
    """
    cluster_id = event["cluster_id"]
    error = event.get("error", "Unknown error")
    phase = event.get("phase", "unknown")
    attempt = event.get("attempt", 1)

    prompt = f"""You are an Aurora PostgreSQL upgrade agent supervisor.

A cluster upgrade has failed. Analyze and recommend the best next action.

Cluster: {cluster_id}
Phase: {phase}
Attempt: {attempt}
Error: {error}

Options:
1. RETRY — transient error, try again (max 3 attempts)
2. SKIP — skip this cluster, continue with fleet
3. HALT — stop the entire rollout
4. ROLLBACK — revert this cluster and continue

Respond with JSON: {{
  "recommended_action": "RETRY|SKIP|HALT|ROLLBACK",
  "reason": "why this is the best option",
  "confidence": 0-100,
  "risk_of_inaction": "what happens if user does nothing",
  "alternatives": ["other valid choices with tradeoffs"]
}}"""

    analysis = _invoke_bedrock(prompt)
    logger.info(f"Supervisor analysis for {cluster_id} failure: {analysis}")

    # Surface recommendation — do NOT auto-execute
    recommended_action = analysis.get("recommended_action", "HALT")
    confidence = analysis.get("confidence", 70)
    reason = analysis.get("reason", "Agent analysis unavailable — defaulting to safest option")

    risk_map = {
        "RETRY": RiskLevel.LOW,
        "SKIP": RiskLevel.MEDIUM,
        "ROLLBACK": RiskLevel.MEDIUM,
        "HALT": RiskLevel.LOW,
    }

    rec = recommend(
        action=f"failure_response_{recommended_action.lower()}",
        cluster_id=cluster_id,
        reasoning=(
            f"Upgrade failed in {phase} phase (attempt {attempt}). "
            f"Error: {error}\n\n"
            f"Agent recommendation: {recommended_action} — {reason}\n\n"
            f"Risk of inaction: {analysis.get('risk_of_inaction', 'Continued failure state')}"
        ),
        risk_level=risk_map.get(recommended_action, RiskLevel.MEDIUM),
        confidence=float(confidence),
        alternatives=analysis.get("alternatives", [
            "RETRY — try again if error seems transient",
            "SKIP — move on, address this cluster later",
            "HALT — stop all upgrades until investigated",
            "ROLLBACK — revert this cluster to previous state",
        ]),
        context={
            "error": error,
            "phase": phase,
            "attempt": attempt,
            "ai_analysis": analysis,
        },
    )

    return {
        "cluster_id": cluster_id,
        "recommendation_id": rec.id,
        "recommended_action": recommended_action,
        "status": "awaiting_user_decision",
        "reasoning": reason,
        "confidence": confidence,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }


def summarize_run(event: dict) -> dict:
    """Generate a summary of the entire upgrade run."""
    results = event.get("all_results", [])
    total = len(results)
    succeeded = sum(1 for r in results if r.get("status") == "complete")
    failed = sum(1 for r in results if r.get("status") == "failed")
    rolled_back = sum(1 for r in results if r.get("status") == "rolled_back")

    return {
        "summary_time": datetime.now(timezone.utc).isoformat(),
        "total_clusters": total,
        "succeeded": succeeded,
        "failed": failed,
        "rolled_back": rolled_back,
        "success_rate": f"{(succeeded / total * 100):.1f}%" if total else "N/A",
    }


def _ask_bedrock_for_decision(phase: str, results: list, failed: list) -> dict:
    """Ask Bedrock to reason about partial failures."""
    prompt = f"""You are supervising an Aurora PostgreSQL fleet upgrade.

Phase: {phase}
Total clusters: {len(results)}
Failed clusters: {failed}

The following clusters failed validation after upgrade:
{json.dumps([r for r in results if r['cluster_id'] in failed], indent=2)}

Should the rollout proceed to the next phase, or halt?
Consider: if only 1 of 10 dev clusters failed, it's likely safe to continue.
If staging fails, production should not proceed.

Respond with JSON: {{"proceed": true/false, "reason": "...", "action": "proceed|halt|retry_failed"}}"""

    return _invoke_bedrock(prompt)


def _invoke_bedrock(prompt: str) -> dict:
    """Call Bedrock Claude for reasoning."""
    try:
        client = bedrock_client()
        response = client.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        body = json.loads(response["body"].read())
        text = extract_bedrock_text(body)

        # Extract JSON from response
        if text and "{" in text:
            json_str = text[text.index("{"):text.rindex("}") + 1]
            return json.loads(json_str)
        return {"action": "HALT", "reason": "Could not parse LLM response"}
    except Exception as e:
        logger.error(f"Bedrock invocation failed: {e}")
        return {"action": "HALT", "reason": f"LLM unavailable: {e}"}


def _persist_plan(plan: dict):
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        table.put_item(Item={
            "pk": "ROLLOUT_PLAN",
            "sk": plan["plan_time"],
            "data": json.dumps(plan, default=str),
            "ttl": int(datetime.now(timezone.utc).timestamp()) + 86400 * 30,
        })
    except Exception as e:
        logger.warning(f"Failed to persist plan: {e}")
