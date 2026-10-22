# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Advisor Agent — Bedrock-powered upgrade strategy recommendation engine.

Uses Claude on Bedrock to analyze cluster context, upgrade history, and errors
to recommend the best upgrade approach and troubleshoot failures.

Strategies it can recommend:
- Blue/Green Deployment (zero-downtime, rollback-safe, best for production)
- In-Place Upgrade (faster, simpler, but requires brief downtime)
- Backup & Restore (safest for major version jumps, most conservative)
"""
import json
import logging
from datetime import datetime, timezone

from shared.aws_clients import bedrock_client, rds_client, cloudwatch_client, extract_bedrock_text
from shared.config import BEDROCK_MODEL_ID

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

KNOWLEDGE_BASE = """
# Aurora PostgreSQL Upgrade Knowledge Base

## Upgrade Strategies

### 1. Blue/Green Deployment
- **Best for**: Production clusters, zero-downtime requirements
- **How**: Creates a staging copy, applies upgrade, then switches traffic via DNS
- **Downtime**: ~30 seconds during switchover
- **Rollback**: Instant (switch back to blue)
- **Prerequisites**: rds.logical_replication=1, no Secrets Manager passwords, cluster must be in 'available' state
- **Risks**: Logical replication lag during sync; large databases may take longer to sync
- **Not suitable when**: Cluster uses unsupported extensions (pg_logical with custom slots), cluster > 64TB, more than 100 databases

### 2. In-Place Minor Version Upgrade
- **Best for**: Dev/test clusters, minor patch versions (e.g., 15.4 → 15.7)
- **How**: Applies engine update directly, restarts instances
- **Downtime**: 2-10 minutes depending on instance class
- **Rollback**: Restore from pre-upgrade snapshot (10-20 min)
- **Prerequisites**: Compatible parameter group, no blocking connections
- **Risks**: Longer downtime if custom extensions need re-compilation
- **Not suitable when**: Zero-downtime required, cluster is in production with SLA

### 3. Backup & Restore (Clone + Upgrade)
- **Best for**: Major version jumps (e.g., 13.x → 15.x), clusters with complex extension stacks
- **How**: Take snapshot, restore to new cluster with target version, validate, switch DNS
- **Downtime**: Extended (minutes to hours depending on data size)
- **Rollback**: Keep original cluster running until validation complete
- **Prerequisites**: Application can handle connection string change
- **Risks**: Data drift during migration window, DNS propagation
- **Not suitable when**: Can't tolerate any data loss between snapshot and cutover

## Common Failure Patterns

### SourceClusterNotSupportedFault
- **Cause**: Parameter group not in-sync (pending reboot) or logical replication not enabled
- **Fix**: Reboot writer instance, verify DBClusterParameterGroupStatus = 'in-sync'

### InvalidDBClusterStateFault
- **Cause**: Cluster is modifying, rebooting, or has pending operations
- **Fix**: Wait for cluster to return to 'available' state

### Extension Compatibility
- **postgis**: Check target version supports installed postgis version
- **pg_partman**: Verify pg_partman version compatibility matrix
- **pglogical**: May conflict with Blue/Green logical replication
- **pg_cron**: Requires shared_preload_libraries update after upgrade

### Replication Lag During B/G Sync
- **Cause**: High write throughput exceeding replication capacity
- **Fix**: Reduce write load during sync phase, or schedule during low-traffic window

### Switchover Timeout
- **Cause**: Long-running transactions blocking final switchover
- **Fix**: Cancel blocking queries, set statement_timeout before switchover

## Decision Matrix

| Factor | Blue/Green | In-Place | Backup/Restore |
|--------|-----------|----------|----------------|
| Zero downtime | ✓ | ✗ | ✗ |
| Instant rollback | ✓ | ✗ | ✓ (keep old) |
| Major version jump | ✗ | ✗ | ✓ |
| Simple setup | ✗ | ✓ | ✗ |
| Large databases | Slower sync | Fast | Slow restore |
| Cost | 2x during sync | No extra | 2x during validate |
"""

MYSQL_KNOWLEDGE_BASE = """
# Aurora MySQL Upgrade Knowledge Base

## Key Differences from PostgreSQL Upgrades

### Blue/Green Deployment for MySQL
- **Prerequisite**: Binary logging (binlog_format) must be enabled
- **Replication**: Uses binlog-based replication (not logical like PG)
- **Switchover**: Same ~30s downtime window as PostgreSQL
- **Version support**: Available for Aurora MySQL v2 (5.7) → v3 (8.0) upgrades

### Aurora MySQL Version Mapping
- Aurora MySQL v2 = MySQL 5.7 compatibility (EOL standard support: Oct 2024, Extended: Jun 2029)
- Aurora MySQL v3 = MySQL 8.0 compatibility (standard support through May 2027)

### Common MySQL 5.7 → 8.0 Breaking Changes
1. Reserved keywords added: RANK, GROUPS, SYSTEM, CUBE, etc.
2. utf8mb3 charset (old 'utf8') deprecated in favor of utf8mb4
3. sql_mode defaults changed (NO_AUTO_CREATE_USER removed)
4. mysql_native_password deprecated — default auth plugin is caching_sha2_password
5. Query cache removed entirely — SQL_CACHE/SQL_NO_CACHE hints cause errors
6. GROUP BY implicit sorting removed — add explicit ORDER BY
7. Information schema changes (uppercase column names in some views)

### Aurora MySQL B/G Prerequisites
- binlog_format must be ROW, MIXED, or STATEMENT in cluster parameter group
- Cannot use Aurora MySQL Serverless v1 (v2 is fine)
- Database must not use MyISAM tables (InnoDB only)
- No more than 100 databases per cluster for B/G

### In-Place Upgrade for MySQL
- Supported for both minor and major version upgrades
- Downtime: 5-30 minutes depending on schema complexity
- Pre-upgrade checks run automatically (reserved keywords, data dictionary)
- Creates a pre-upgrade snapshot automatically
"""


def get_advisor_recommendation(cluster_context: dict, question: str = None) -> dict:
    """Get Bedrock-powered recommendation for upgrade strategy or error diagnosis."""
    cluster_id = cluster_context.get("cluster_id", "unknown")
    region = cluster_context.get("region", "")

    # Gather live cluster info
    cluster_info = _gather_cluster_info(cluster_id, region)
    history = cluster_context.get("upgrade_history", [])
    error = cluster_context.get("error")

    # Carry a caller-supplied target version into the cluster info so the
    # strategy scorer can weigh the size of the version jump.
    if cluster_context.get("target_version"):
        cluster_info["target_version"] = cluster_context["target_version"]

    if error:
        return _diagnose_error(cluster_info, error, history)
    elif question:
        return _answer_question(cluster_info, question, history)
    else:
        return _recommend_strategy(cluster_info, history, region)


def _recommend_strategy(cluster_info: dict, history: list, region: str = "") -> dict:
    """Recommend the best upgrade strategy, grounded in real traffic patterns.

    The strategy is chosen DETERMINISTICALLY by score_strategy() from persisted
    traffic-generator stats + RDS CloudWatch metrics. Bedrock is used only to
    explain the decision and enumerate risks/prerequisites — it does not pick
    the strategy, so the recommendation is reproducible and auditable.
    """
    cluster_id = cluster_info.get("cluster_id")

    # Build the traffic profile from persisted generator stats + CloudWatch.
    traffic = {}
    scoring = None
    try:
        from shared.traffic import (
            get_recent_traffic, get_cloudwatch_traffic_metrics,
            summarize_traffic, score_strategy,
        )
        samples = get_recent_traffic(cluster_id) if cluster_id else []
        cw = get_cloudwatch_traffic_metrics(cluster_id, region) if cluster_id else {}
        traffic = summarize_traffic(samples, cw)
        scoring = score_strategy(traffic, cluster_info)
    except Exception as e:
        logger.warning(f"Traffic-based scoring unavailable for {cluster_id}: {e}")

    # Enrich with historical outcome data (makes recommendations smarter over time)
    historical_context = {}
    try:
        from shared.outcome_learning import get_strategy_success_rates, get_duration_estimate
        current_version = cluster_info.get("engine_version", "")
        target_version = cluster_info.get("target_version", "")
        if current_version and target_version:
            success_rates = get_strategy_success_rates(current_version, target_version)
            if success_rates:
                historical_context["strategy_success_rates"] = success_rates
            for strat in ["blue_green", "in_place", "backup_restore"]:
                est = get_duration_estimate(current_version, target_version, strat,
                                           cluster_info.get("storage_gb", 0))
                if est:
                    historical_context.setdefault("duration_estimates", {})[strat] = est
    except Exception as e:
        logger.warning(f"Historical outcome lookup failed: {e}")

    prompt = f"""{KNOWLEDGE_BASE}

{MYSQL_KNOWLEDGE_BASE if cluster_info.get("engine_type") == "aurora-mysql" else ""}

---

You are an Aurora {'MySQL' if cluster_info.get("engine_type") == "aurora-mysql" else 'PostgreSQL'} upgrade advisor. A deterministic, rule-based scorer
has ALREADY selected the recommended upgrade strategy from this cluster's real
traffic patterns. Your job is to EXPLAIN that decision clearly — do NOT change
the recommended strategy.

## Cluster Context
{json.dumps(cluster_info, indent=2, default=str)}

## Observed Traffic Profile
{json.dumps(traffic, indent=2, default=str)}

## Rule-Based Scoring Result (authoritative — use this strategy)
{json.dumps(scoring, indent=2, default=str)}

## Historical Outcomes (from past upgrades of similar clusters)
{json.dumps(historical_context, indent=2, default=str) if historical_context else "No historical data available yet — this version pair has not been upgraded before."}

## Previous Upgrade History (this cluster)
{json.dumps(history[-5:] if history else [], indent=2, default=str)}

## Your Task
1. Use "recommended_strategy" from the scoring result as-is.
2. Translate the scorer's "reasons" for that strategy into clear reasoning that
   references the actual traffic numbers (write ratio, connections, p99, troughs).
3. List risks to watch for and prerequisites that must be met.
4. Note the runner-up strategy (next in "ranked") as the fallback.

Respond with JSON:
{{
  "recommended_strategy": "blue_green|in_place|backup_restore",
  "confidence": "high|medium|low",
  "reasoning": "...",
  "risks": ["risk1", "risk2"],
  "prerequisites": ["prereq1", "prereq2"],
  "estimated_downtime": "...",
  "estimated_duration": "...",
  "fallback_strategy": "...",
  "steps": ["step1", "step2", "step3"]
}}"""

    result = _invoke_bedrock(prompt)

    # The scorer is authoritative — never let the LLM override the choice.
    if scoring:
        result["recommended_strategy"] = scoring["recommended_strategy"]
        result.setdefault("confidence", scoring["confidence"])
        result["strategy_scores"] = scoring["scores"]
        result["decision_factors"] = scoring["reasons"].get(scoring["recommended_strategy"], [])
        if not result.get("fallback_strategy") and len(scoring["ranked"]) > 1:
            result["fallback_strategy"] = scoring["ranked"][1]
    result["traffic_profile"] = traffic
    result["cluster_id"] = cluster_id
    result["advisor_type"] = "strategy_recommendation"
    result["generated_at"] = datetime.now(timezone.utc).isoformat()

    # Attach historical context for the UI
    if historical_context:
        result["historical_context"] = historical_context

    return result


def _diagnose_error(cluster_info: dict, error: str, history: list) -> dict:
    """Diagnose an upgrade error and provide actionable fix steps."""
    prompt = f"""{KNOWLEDGE_BASE}

---

You are an Aurora PostgreSQL upgrade troubleshooting expert. An upgrade has failed.

## Cluster Context
{json.dumps(cluster_info, indent=2, default=str)}

## Error
{error}

## Previous Attempts
{json.dumps(history[-5:] if history else [], indent=2, default=str)}

## Your Task
1. Identify the ROOT CAUSE of this error
2. Determine if it's auto-fixable or requires manual intervention
3. Provide SPECIFIC CLI commands or steps to fix it
4. If this is a recurring issue (check history), suggest a different strategy
5. Estimate time to fix

Respond with JSON:
{{
  "root_cause": "...",
  "category": "configuration|state|capacity|extension|network|permissions",
  "auto_fixable": true/false,
  "fix_steps": [
    {{"step": 1, "action": "...", "command": "aws rds ...", "expected_result": "..."}},
    ...
  ],
  "alternative_strategy": "blue_green|in_place|backup_restore|null",
  "estimated_fix_time": "...",
  "prevention": "How to prevent this in the future",
  "retry_safe": true/false
}}"""

    result = _invoke_bedrock(prompt)
    result["cluster_id"] = cluster_info.get("cluster_id")
    result["advisor_type"] = "error_diagnosis"
    result["error_input"] = error[:500]
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    return result


def _answer_question(cluster_info: dict, question: str, history: list) -> dict:
    """Answer a freeform question about the cluster or upgrade process."""
    prompt = f"""{KNOWLEDGE_BASE}

---

You are an Aurora PostgreSQL upgrade advisor answering a user question.

## Cluster Context
{json.dumps(cluster_info, indent=2, default=str)}

## Upgrade History
{json.dumps(history[-5:] if history else [], indent=2, default=str)}

## User Question
{question}

## Your Task
Provide a clear, actionable answer. If the question is about strategy choice, compare options with pros/cons specific to THIS cluster. If it's about an error, diagnose and provide fix steps.

Respond with JSON:
{{
  "answer": "...",
  "recommendations": ["rec1", "rec2"],
  "relevant_commands": ["cmd1", "cmd2"],
  "confidence": "high|medium|low"
}}"""

    result = _invoke_bedrock(prompt)
    result["cluster_id"] = cluster_info.get("cluster_id")
    result["advisor_type"] = "question_answer"
    result["question"] = question
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    return result


def _gather_cluster_info(cluster_id: str, region: str) -> dict:
    """Gather live cluster metadata for context."""
    info = {
        "cluster_id": cluster_id,
        "region": region,
    }
    try:
        client = rds_client(region)
        resp = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = resp["DBClusters"][0]

        info.update({
            "engine_version": cluster.get("EngineVersion"),
            "status": cluster.get("Status"),
            "instance_count": len(cluster.get("DBClusterMembers", [])),
            "storage_encrypted": cluster.get("StorageEncrypted"),
            "deletion_protection": cluster.get("DeletionProtection"),
            "parameter_group": cluster.get("DBClusterParameterGroup"),
            "multi_az": cluster.get("MultiAZ"),
            "backup_retention": cluster.get("BackupRetentionPeriod"),
            "allocated_storage": cluster.get("AllocatedStorage"),
        })

        # Get instance classes
        instances = []
        for member in cluster.get("DBClusterMembers", []):
            try:
                inst_resp = client.describe_db_instances(DBInstanceIdentifier=member["DBInstanceIdentifier"])
                inst = inst_resp["DBInstances"][0]
                instances.append({
                    "id": inst["DBInstanceIdentifier"],
                    "class": inst.get("DBInstanceClass"),
                    "is_writer": member.get("IsClusterWriter"),
                    "status": inst.get("DBInstanceStatus"),
                    "pg_status": member.get("DBClusterParameterGroupStatus"),
                })
            except Exception:
                pass
        info["instances"] = instances

        # Get available upgrade targets
        try:
            versions_resp = client.describe_db_engine_versions(
                Engine="aurora-postgresql",
                EngineVersion=cluster["EngineVersion"],
            )
            if versions_resp.get("DBEngineVersions"):
                targets = versions_resp["DBEngineVersions"][0].get("ValidUpgradeTarget", [])
                info["available_upgrades"] = [
                    {"version": t["EngineVersion"], "is_major": t.get("IsMajorVersionUpgrade", False)}
                    for t in targets[:5]
                ]
        except Exception:
            pass

        # Check extensions via parameter group
        try:
            pg_resp = client.describe_db_cluster_parameters(
                DBClusterParameterGroupName=cluster["DBClusterParameterGroup"],
                Source="user",
            )
            shared_preload = next(
                (p["ParameterValue"] for p in pg_resp.get("Parameters", [])
                 if p["ParameterName"] == "shared_preload_libraries" and p.get("ParameterValue")),
                None
            )
            if shared_preload:
                info["shared_preload_libraries"] = shared_preload
        except Exception:
            pass

    except Exception as e:
        info["error_gathering_info"] = str(e)

    return info


def _invoke_bedrock(prompt: str) -> dict:
    """Call Bedrock Claude for reasoning."""
    try:
        from shared.config import get_model_id
        client = bedrock_client(region="")
        response = client.invoke_model(
            modelId=get_model_id(),
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                # Sonnet 5 thinks by default; too small a budget is spent entirely
                # on thinking, leaving no text output. Give room for both.
                "max_tokens": 8192,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        body = json.loads(response["body"].read())
        text = extract_bedrock_text(body)

        if text:
            start = text.find("{")
            end = text.rfind("}")
            # Require a well-ordered {...} span. Guarding only on "{" and then
            # calling rindex("}") raises "substring not found" when the model
            # returns an opening brace but no closing one (truncated JSON).
            if 0 <= start < end:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError as pe:
                    logger.warning(f"Advisor JSON parse failed: {pe}")
                    return {"answer": text, "parse_error": f"Malformed JSON: {pe}"}
        return {"answer": text or "", "parse_error": "Could not extract JSON from response"}
    except Exception as e:
        logger.error(f"Bedrock invocation failed: {e}")
        return {"error": str(e), "fallback": True}
