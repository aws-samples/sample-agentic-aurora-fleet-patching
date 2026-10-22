# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Planner Agent — Fleet-wide upgrade orchestration and intelligent scheduling.

This agent takes the output of Scanner + Analyzer for ALL clusters and produces
an optimal fleet-wide upgrade plan that:
1. Groups clusters by shared characteristics (same param group, same fix needed)
2. Orders by dependency (if app reads from A and B, coordinate)
3. Distributes risk (don't upgrade all prod clusters same night)
4. Respects maintenance windows per cluster
5. Promotes progressively: Dev → Staging → Prod canary → Prod fleet

WHY AI IS NEEDED:
- With 50 clusters, the combinatorial space of valid orderings is enormous
- Balancing urgency (CVE exposure) vs risk (blast radius) vs constraints
  (maintenance windows, team availability) requires multi-factor reasoning
- When dev upgrade reveals a performance regression, the planner must
  REASON about which prod clusters have similar workloads and should be held
"""
import json
import logging
from datetime import datetime, timezone, timedelta

from shared.aws_clients import bedrock_client, extract_bedrock_text
from shared.config import BEDROCK_MODEL_ID
from agents.planner.dependencies import (
    discover_dependencies,
    get_coordination_constraints,
    validate_plan_against_dependencies,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

PLANNER_MODEL = BEDROCK_MODEL_ID


def generate_fleet_plan(
    scan_results: list[dict],
    analysis_results: list[dict],
    maintenance_windows: dict,
    active_upgrades: list[str] = None,
    previous_outcomes: list[dict] = None,
    region: str = "us-east-1",
) -> dict:
    """Generate an optimal fleet-wide upgrade plan.

    This is the orchestration brain — it decides WHAT gets upgraded WHEN
    and in WHAT ORDER, considering the entire fleet as a connected system.

    Args:
        scan_results: Urgency-scored clusters from Scanner Agent
        analysis_results: Deep analysis from Analyzer Agent (per cluster)
        maintenance_windows: Configured windows {env: {start, end, days, clusters}}
        active_upgrades: Cluster IDs currently being upgraded (skip these)
        previous_outcomes: Results from prior upgrade batches (inform decisions)
        region: AWS region for dependency discovery (security group lookups)
    """
    active_upgrades = active_upgrades or []
    previous_outcomes = previous_outcomes or []

    # Filter to clusters that need action and aren't already in-flight
    candidates = [
        s for s in scan_results
        if s.get("urgency_score", 0) > 0
        and s["cluster_id"] not in active_upgrades
        and s.get("target_version")
    ]

    if not candidates:
        return {
            "plan_time": datetime.now(timezone.utc).isoformat(),
            "status": "no_action_needed",
            "message": "All clusters are up to date or already being upgraded",
            "waves": [],
        }

    # Discover cross-cluster dependencies
    dependency_graph = discover_dependencies(candidates, region)
    coordination_constraints = get_coordination_constraints(dependency_graph)

    # Enrich candidates with analysis results
    analysis_map = {a["cluster_id"]: a for a in analysis_results}
    for candidate in candidates:
        analysis = analysis_map.get(candidate["cluster_id"], {})
        candidate["ready_to_upgrade"] = analysis.get("ready_to_upgrade", False)
        candidate["blocking_issues"] = analysis.get("blocking_issues", [])
        candidate["recommended_strategy"] = analysis.get("recommended_strategy")
        candidate["pre_upgrade_steps"] = analysis.get("pre_upgrade_steps", [])
        candidate["estimated_duration"] = analysis.get("estimated_duration")

    # Separate ready vs blocked
    ready = [c for c in candidates if c.get("ready_to_upgrade")]
    blocked = [c for c in candidates if not c.get("ready_to_upgrade")]

    # Use AI to generate optimal plan (with dependency constraints)
    plan = _ai_generate_plan(
        ready, blocked, maintenance_windows, previous_outcomes, coordination_constraints
    )
    plan["plan_time"] = datetime.now(timezone.utc).isoformat()
    plan["total_candidates"] = len(candidates)
    plan["ready_count"] = len(ready)
    plan["blocked_count"] = len(blocked)
    plan["blocked_clusters"] = [
        {"cluster_id": c["cluster_id"], "issues": c["blocking_issues"]}
        for c in blocked
    ]
    plan["dependency_graph"] = dependency_graph
    plan["coordination_constraints"] = coordination_constraints

    # Validate the generated plan against dependency constraints
    if plan.get("waves"):
        violations = validate_plan_against_dependencies(plan["waves"], coordination_constraints)
        if violations:
            plan["dependency_violations"] = violations
            logger.warning(
                f"Plan has {len(violations)} dependency violation(s) — review recommended"
            )

    return plan


def _ai_generate_plan(
    ready: list[dict],
    blocked: list[dict],
    windows: dict,
    previous_outcomes: list[dict],
    coordination_constraints: list[dict] = None,
) -> dict:
    """AI reasoning: generate fleet upgrade plan with wave scheduling."""
    coordination_constraints = coordination_constraints or []

    # Check if previous upgrades had issues (influences this plan)
    outcome_context = ""
    if previous_outcomes:
        failures = [o for o in previous_outcomes if o.get("status") == "failed"]
        regressions = [o for o in previous_outcomes if o.get("performance_regression")]
        if failures or regressions:
            outcome_context = f"""
## IMPORTANT: Previous Upgrade Outcomes (inform your decisions)
Failures: {json.dumps(failures[-3:], indent=2, default=str)}
Performance regressions: {json.dumps(regressions[-3:], indent=2, default=str)}

If a dev/staging cluster had a performance regression, HOLD similar prod clusters
until the regression is investigated. Similar = same parameter group, same version jump,
or same extensions.
"""

    # Build dependency constraints section for the prompt
    constraints_context = ""
    if coordination_constraints:
        constraints_context = f"""
## MANDATORY: Cross-Cluster Dependency Constraints ({len(coordination_constraints)} constraints)
These constraints were discovered from tags, naming conventions, shared parameter groups,
and security group cross-references. You MUST respect ALL of them when assigning waves.

{json.dumps(coordination_constraints, indent=2, default=str)}

Constraint types explained:
- "must_before": The specified cluster MUST be in an earlier wave than the "before" cluster.
- "not_simultaneous": The listed clusters MUST NOT be in the same wave.
- "upgrade_together": The listed clusters should be in the same wave or adjacent waves (within 1 wave).

VIOLATION OF THESE CONSTRAINTS IS NOT ACCEPTABLE. If a constraint conflicts with another rule,
the constraint takes priority (except safety rules).
"""

    prompt = f"""You are a fleet database operations planner for 50+ Aurora PostgreSQL clusters.

## Ready for Upgrade ({len(ready)} clusters)
{json.dumps(ready, indent=2, default=str)}

## Maintenance Windows
{json.dumps(windows, indent=2, default=str)}

## Blocked Clusters ({len(blocked)} clusters — need pre-work first)
{json.dumps([{"id": c["cluster_id"], "issues": c["blocking_issues"]} for c in blocked], indent=2, default=str)}
{outcome_context}
{constraints_context}

## Planning Rules
1. NEVER upgrade all clusters in one wave — progressive rollout is mandatory
2. Dev clusters go first (validation), then staging (bake), then production
3. Within production, start with lowest-traffic cluster as canary
4. Group clusters with SAME parameter group and SAME target version into same wave
5. If a cluster has blocking_issues, put it in a "preparation" phase (fix first)
6. Respect maintenance windows — schedule waves within allowed hours
7. Maximum 5 clusters per wave (parallel upgrade limit)
8. If previous outcomes show regressions, HOLD clusters with matching profiles
9. Urgency overrides: CRITICAL CVE clusters can skip bake time (but not validation)

## Output Format
Generate a deployment plan with waves and timing:

Respond with JSON:
{{
  "status": "plan_ready",
  "strategy_summary": "one-line description of approach",
  "total_waves": N,
  "estimated_fleet_completion": "date or duration",
  "waves": [
    {{
      "wave_id": 1,
      "phase": "dev|staging|prod_canary|prod_fleet|preparation",
      "clusters": [
        {{
          "cluster_id": "...",
          "strategy": "blue_green|in_place|backup_restore",
          "target_version": "...",
          "estimated_duration": "..."
        }}
      ],
      "scheduled_window": "YYYY-MM-DD HH:MM-HH:MM UTC",
      "auto_approve": true/false,
      "bake_hours": N,
      "gate_condition": "what must be true before next wave",
      "parallel": true/false
    }}
  ],
  "preparation_needed": [
    {{
      "cluster_id": "...",
      "steps": ["step1", "step2"],
      "blocks_wave": N
    }}
  ],
  "hold_clusters": [
    {{
      "cluster_id": "...",
      "reason": "why held",
      "release_condition": "when it's safe to proceed"
    }}
  ],
  "risk_assessment": {{
    "overall_risk": "low|medium|high",
    "key_risks": ["risk1", "risk2"],
    "mitigations": ["mitigation1", "mitigation2"]
  }}
}}"""

    try:
        client = bedrock_client()
        response = client.invoke_model(
            modelId=PLANNER_MODEL,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        body = json.loads(response["body"].read())
        text = extract_bedrock_text(body)
        if text and "{" in text:
            return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except Exception as e:
        logger.error(f"Fleet plan generation failed: {e}")

    # Fallback: simple deterministic plan
    return _fallback_plan(ready, blocked)


def _fallback_plan(ready: list[dict], blocked: list[dict]) -> dict:
    """Generate a basic plan when AI is unavailable."""
    waves = []
    wave_id = 1

    # Group by environment
    dev = [c for c in ready if c.get("environment") == "dev"]
    staging = [c for c in ready if c.get("environment") == "staging"]
    prod = [c for c in ready if c.get("environment") == "production"]

    if dev:
        waves.append({
            "wave_id": wave_id,
            "phase": "dev",
            "clusters": [{"cluster_id": c["cluster_id"], "strategy": c.get("recommended_strategy", "in_place"), "target_version": c["target_version"]} for c in dev[:5]],
            "auto_approve": True,
            "bake_hours": 0,
            "parallel": True,
            "gate_condition": "All dev clusters pass validation",
        })
        wave_id += 1

    if staging:
        waves.append({
            "wave_id": wave_id,
            "phase": "staging",
            "clusters": [{"cluster_id": c["cluster_id"], "strategy": c.get("recommended_strategy", "blue_green"), "target_version": c["target_version"]} for c in staging[:5]],
            "auto_approve": True,
            "bake_hours": 24,
            "parallel": True,
            "gate_condition": "24h bake time with no alerts",
        })
        wave_id += 1

    if prod:
        # Canary
        waves.append({
            "wave_id": wave_id,
            "phase": "prod_canary",
            "clusters": [{"cluster_id": prod[0]["cluster_id"], "strategy": "blue_green", "target_version": prod[0]["target_version"]}],
            "auto_approve": True,
            "bake_hours": 4,
            "parallel": False,
            "gate_condition": "Canary passes 4h bake with no P99 regression",
        })
        wave_id += 1

        # Fleet
        if len(prod) > 1:
            for i in range(1, len(prod), 5):
                batch = prod[i:i+5]
                waves.append({
                    "wave_id": wave_id,
                    "phase": "prod_fleet",
                    "clusters": [{"cluster_id": c["cluster_id"], "strategy": "blue_green", "target_version": c["target_version"]} for c in batch],
                    "auto_approve": False,
                    "bake_hours": 0,
                    "parallel": True,
                    "gate_condition": "Human approval required",
                })
                wave_id += 1

    return {
        "status": "plan_ready",
        "strategy_summary": f"Progressive rollout: {len(dev)} dev → {len(staging)} staging → {len(prod)} prod ({len(waves)} waves)",
        "total_waves": len(waves),
        "waves": waves,
        "preparation_needed": [{"cluster_id": c["cluster_id"], "steps": c["blocking_issues"]} for c in blocked],
        "hold_clusters": [],
        "risk_assessment": {
            "overall_risk": "medium" if prod else "low",
            "key_risks": ["No AI analysis available — using deterministic fallback"],
            "mitigations": ["Manual review recommended before production waves"],
        },
    }


def adjust_plan_from_outcome(current_plan: dict, wave_outcome: dict) -> dict:
    """AGENTIC LOOP: Adjust the fleet plan based on observed outcomes.

    This is where the agent REASONS about results and modifies its approach.
    Called after each wave completes.

    Example reasoning:
    - "Wave 1 (dev) cluster-A had 15% latency increase. Cluster-B in wave 3 (prod)
       has the same parameter group and higher traffic. HOLD cluster-B,
       investigate regression first."
    """
    wave_id = wave_outcome.get("wave_id")
    results = wave_outcome.get("results", [])
    failures = [r for r in results if r.get("status") == "failed"]
    regressions = [r for r in results if r.get("latency_regression_pct", 0) > 10]

    if not failures and not regressions:
        return current_plan  # All good, continue as planned

    prompt = f"""You are a fleet upgrade supervisor. A wave just completed with issues.

## Completed Wave
{json.dumps(wave_outcome, indent=2, default=str)}

## Current Plan (remaining waves)
{json.dumps(current_plan.get("waves", []), indent=2, default=str)}

## Issues Observed
Failures: {json.dumps(failures, indent=2, default=str)}
Performance regressions: {json.dumps(regressions, indent=2, default=str)}

## Decision Needed
1. Should remaining waves proceed, be modified, or halted?
2. If a regression occurred, which upcoming clusters are at risk (same workload profile)?
3. Should any clusters be moved to a "hold" list?

Rules:
- If a staging cluster fails, HALT all production waves
- If a dev cluster has a regression, investigate but don't halt staging yet
- If multiple clusters fail with the same error, the issue is systemic → HALT ALL
- Performance regression on one cluster → hold SIMILAR clusters only

Respond with JSON:
{{
  "action": "continue|modify|halt",
  "reason": "...",
  "modified_waves": [...],
  "new_hold_clusters": [{{"cluster_id": "...", "reason": "..."}}],
  "investigation_needed": "what to check before resuming"
}}"""

    try:
        client = bedrock_client()
        response = client.invoke_model(
            modelId=PLANNER_MODEL,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 2048,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        body = json.loads(response["body"].read())
        text = extract_bedrock_text(body)
        if text and "{" in text:
            adjustment = json.loads(text[text.index("{"):text.rindex("}") + 1])

            if adjustment.get("action") == "halt":
                current_plan["status"] = "halted"
                current_plan["halt_reason"] = adjustment.get("reason")
            elif adjustment.get("action") == "modify" and adjustment.get("modified_waves"):
                current_plan["waves"] = adjustment["modified_waves"]

            if adjustment.get("new_hold_clusters"):
                current_plan.setdefault("hold_clusters", []).extend(adjustment["new_hold_clusters"])

            current_plan["last_adjustment"] = {
                "wave_id": wave_id,
                "action": adjustment.get("action"),
                "reason": adjustment.get("reason"),
                "adjusted_at": datetime.now(timezone.utc).isoformat(),
            }
            return current_plan
    except Exception as e:
        logger.error(f"Plan adjustment failed: {e}")

    # Fallback: halt on any failure
    if failures:
        current_plan["status"] = "halted"
        current_plan["halt_reason"] = f"Wave {wave_id} had {len(failures)} failure(s) — AI unavailable for analysis"
    return current_plan
