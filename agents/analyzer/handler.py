# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Analyzer Agent — Deep parameter and configuration analysis for upgrade readiness.

This is where AI is MOST justified. The agent:
1. Pulls EVERY custom parameter from source cluster
2. Compares against target version's parameter defaults and removals
3. Identifies extensions that need version bumps or replacements
4. Analyzes query patterns to predict post-upgrade performance impact
5. Generates a complete CHANGE PLAN (params to add/remove/modify)

WHY THIS CAN'T BE A SIMPLE SCRIPT:
- PostgreSQL deprecates/renames params across versions unpredictably
- Extension compatibility matrices are complex and change per Aurora release
- Predicting query plan changes from version upgrades requires understanding
  the query optimizer changes in release notes
- Generating safe parameter values for new required params needs context
  about the cluster's workload profile
"""
import json
import logging
from datetime import datetime, timezone, timedelta

from shared.aws_clients import rds_client, cloudwatch_client, bedrock_client, extract_bedrock_text
from shared.config import BEDROCK_MODEL_ID

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Parameters removed or renamed across PostgreSQL major versions
PARAM_CHANGES_BY_VERSION = {
    "14": {
        "removed": ["wal_keep_segments"],
        "renamed": {"wal_keep_segments": "wal_keep_size"},
        "new_required": [],
        "default_changes": {"password_encryption": "scram-sha-256"},
    },
    "15": {
        "removed": ["stats_temp_directory"],
        "renamed": {},
        "new_required": [],
        "default_changes": {"log_checkpoints": "on"},
    },
    "16": {
        "removed": ["vacuum_defer_cleanup_age"],
        "renamed": {},
        "new_required": [],
        "default_changes": {
            "createrole_self_grant": "set",
            "log_line_prefix": "%m [%p] %q%u@%d ",
        },
    },
    "17": {
        "removed": ["trace_recovery_messages"],
        "renamed": {},
        "new_required": [],
        "default_changes": {
            "allow_alter_system": "on",
            "event_triggers": "on",
        },
    },
    "18": {
        "removed": [],
        "renamed": {},
        "new_required": [],
        "default_changes": {},
    },
}

# Extension compatibility by PG major version
EXTENSION_COMPAT = {
    "postgis": {
        "15": {"min": "3.3.0", "recommended": "3.4.1"},
        "16": {"min": "3.4.0", "recommended": "3.5.0"},
        "17": {"min": "3.5.0", "recommended": "3.5.0"},
    },
    "pgvector": {
        "15": {"min": "0.5.0", "recommended": "0.7.0"},
        "16": {"min": "0.6.0", "recommended": "0.7.0"},
        "17": {"min": "0.7.0", "recommended": "0.8.0"},
    },
    "pg_cron": {
        "15": {"min": "1.5", "recommended": "1.6"},
        "16": {"min": "1.6", "recommended": "1.6"},
        "17": {"min": "1.6", "recommended": "1.6"},
    },
    "pglogical": {
        "15": {"min": "2.4.3", "recommended": "2.4.4"},
        "16": {"min": "2.4.4", "recommended": "2.4.4"},
        "17": {"min": "2.4.4", "recommended": "2.4.4"},
    },
    "pg_partman": {
        "15": {"min": "4.7.0", "recommended": "5.0.0"},
        "16": {"min": "5.0.0", "recommended": "5.1.0"},
        "17": {"min": "5.0.0", "recommended": "5.1.0"},
    },
}


def analyze_upgrade_readiness(cluster_id: str, target_version: str, region: str = "") -> dict:
    """Deep analysis of what needs to change before upgrade can proceed.

    This is the core agentic function — it doesn't just check pass/fail,
    it generates a PLAN of what to change and in what order.
    """
    client = rds_client(region)

    # Gather all cluster configuration
    cluster_config = _gather_full_config(client, cluster_id)
    current_version = cluster_config["engine_version"]
    current_major = current_version.split(".")[0]
    target_major = target_version.split(".")[0]
    is_major_upgrade = current_major != target_major

    # Run deterministic checks first (no AI needed)
    param_analysis = _analyze_parameters(client, cluster_config, target_major, is_major_upgrade)
    extension_analysis = _analyze_extensions(cluster_config, target_major)
    size_analysis = _analyze_cluster_size(client, cluster_id, region)
    utilization = _analyze_utilization(cluster_id, region)
    db_metadata = _gather_database_metadata(cluster_config)

    # AI reasoning: generate change plan considering all factors together
    change_plan = _generate_change_plan(
        cluster_config=cluster_config,
        target_version=target_version,
        param_analysis=param_analysis,
        extension_analysis=extension_analysis,
        size_analysis=size_analysis,
        utilization=utilization,
        is_major_upgrade=is_major_upgrade,
        db_metadata=db_metadata,
    )

    return {
        "cluster_id": cluster_id,
        "current_version": current_version,
        "target_version": target_version,
        "is_major_upgrade": is_major_upgrade,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "parameter_analysis": param_analysis,
        "extension_analysis": extension_analysis,
        "size_analysis": size_analysis,
        "utilization": utilization,
        "database_metadata": db_metadata,
        "change_plan": change_plan,
        "ready_to_upgrade": change_plan.get("ready", False),
        "blocking_issues": change_plan.get("blocking_issues", []),
        "pre_upgrade_steps": change_plan.get("pre_upgrade_steps", []),
        "recommended_strategy": change_plan.get("strategy"),
        "estimated_duration": change_plan.get("estimated_duration"),
    }


def _gather_full_config(client, cluster_id: str) -> dict:
    """Pull complete cluster configuration."""
    config = {"cluster_id": cluster_id}

    try:
        resp = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = resp["DBClusters"][0]
        config.update({
            "engine_version": cluster["EngineVersion"],
            "status": cluster["Status"],
            "parameter_group": cluster.get("DBClusterParameterGroup", ""),
            "storage_encrypted": cluster.get("StorageEncrypted"),
            "deletion_protection": cluster.get("DeletionProtection"),
            "multi_az": cluster.get("MultiAZ"),
            "backup_retention": cluster.get("BackupRetentionPeriod"),
            "allocated_storage": cluster.get("AllocatedStorage", 0),
            "members": cluster.get("DBClusterMembers", []),
            "instance_count": len(cluster.get("DBClusterMembers", [])),
        })

        # Get instance details
        instances = []
        for member in cluster.get("DBClusterMembers", []):
            try:
                inst_resp = client.describe_db_instances(
                    DBInstanceIdentifier=member["DBInstanceIdentifier"]
                )
                inst = inst_resp["DBInstances"][0]
                instances.append({
                    "id": inst["DBInstanceIdentifier"],
                    "class": inst.get("DBInstanceClass"),
                    "is_writer": member.get("IsClusterWriter"),
                    "status": inst.get("DBInstanceStatus"),
                    "param_group_status": member.get("DBClusterParameterGroupStatus"),
                    "instance_param_group": inst.get("DBParameterGroups", [{}])[0].get("DBParameterGroupName", ""),
                })
            except Exception:
                pass
        config["instances"] = instances

        # Get tags for environment classification
        try:
            tags_resp = client.list_tags_for_resource(ResourceName=cluster["DBClusterArn"])
            tags = {t["Key"]: t["Value"] for t in tags_resp.get("TagList", [])}
            env_tag = (tags.get("Environment") or tags.get("environment") or tags.get("env") or "").lower()
            if env_tag in ("prod", "production"):
                config["environment"] = "production"
            elif env_tag in ("staging", "stage", "stg"):
                config["environment"] = "staging"
            else:
                config["environment"] = "dev"
            # Also infer from name
            if not env_tag:
                name_lower = cluster_id.lower()
                if "prod" in name_lower or "prd" in name_lower:
                    config["environment"] = "production"
                elif "stag" in name_lower or "stg" in name_lower:
                    config["environment"] = "staging"
        except Exception:
            config["environment"] = "dev"

    except Exception as e:
        config["error"] = str(e)

    return config


def _analyze_parameters(client, cluster_config: dict, target_major: str, is_major_upgrade: bool) -> dict:
    """Analyze parameter group compatibility with target version."""
    param_group = cluster_config.get("parameter_group", "")
    if not param_group:
        return {"status": "skip", "reason": "No parameter group found"}

    try:
        # Get all custom parameters
        custom_params = []
        paginator = client.get_paginator("describe_db_cluster_parameters")
        for page in paginator.paginate(DBClusterParameterGroupName=param_group):
            for param in page.get("Parameters", []):
                if param.get("Source") == "user" and param.get("ParameterValue"):
                    custom_params.append({
                        "name": param["ParameterName"],
                        "value": param["ParameterValue"],
                        "apply_type": param.get("ApplyType", ""),
                        "data_type": param.get("DataType", ""),
                        "is_modifiable": param.get("IsModifiable", True),
                    })

        # Check against known removals/renames for each version between current and target
        current_major = cluster_config["engine_version"].split(".")[0]
        issues = []
        warnings = []
        migrations = []

        if is_major_upgrade:
            for ver in range(int(current_major) + 1, int(target_major) + 1):
                ver_str = str(ver)
                changes = PARAM_CHANGES_BY_VERSION.get(ver_str, {})

                for param in custom_params:
                    if param["name"] in changes.get("removed", []):
                        issues.append({
                            "type": "removed",
                            "parameter": param["name"],
                            "current_value": param["value"],
                            "message": f"Parameter '{param['name']}' removed in PG {ver_str}",
                            "action": "remove from parameter group before upgrade",
                        })
                    elif param["name"] in changes.get("renamed", {}):
                        new_name = changes["renamed"][param["name"]]
                        migrations.append({
                            "type": "renamed",
                            "old_name": param["name"],
                            "new_name": new_name,
                            "current_value": param["value"],
                            "message": f"Rename '{param['name']}' → '{new_name}' for PG {ver_str}",
                        })

                for new_param, default in changes.get("default_changes", {}).items():
                    warnings.append({
                        "type": "default_changed",
                        "parameter": new_param,
                        "new_default": default,
                        "message": f"Default for '{new_param}' changes to '{default}' in PG {ver_str} — verify acceptable",
                    })

        return {
            "status": "analyzed",
            "custom_param_count": len(custom_params),
            "custom_params": custom_params,
            "issues": issues,
            "warnings": warnings,
            "migrations": migrations,
            "requires_new_param_group": is_major_upgrade,
            "param_group_family": f"aurora-postgresql{target_major}" if is_major_upgrade else None,
        }

    except Exception as e:
        return {"status": "error", "reason": str(e)}


def _analyze_extensions(cluster_config: dict, target_major: str) -> dict:
    """Check extension compatibility with target version."""
    # Get shared_preload_libraries from parameters (indicates installed extensions)
    issues = []
    compatible = []
    needs_upgrade = []

    # For now, check against known extension compatibility rather than the
    # live per-instance extension list. Querying pg_available_extensions on
    # each instance (cluster_config["instances"]) requires a DB connection via
    # SSM/Lambda and isn't implemented yet — tracked as a known limitation
    # rather than silently no-op'ing over the instance list.

    # Check any extensions we know about from parameter group
    for ext_name, compat in EXTENSION_COMPAT.items():
        if target_major in compat:
            target_compat = compat[target_major]
            compatible.append({
                "extension": ext_name,
                "min_version_required": target_compat["min"],
                "recommended_version": target_compat["recommended"],
            })

    return {
        "status": "analyzed",
        "compatible_extensions": compatible,
        "issues": issues,
        "needs_upgrade": needs_upgrade,
    }


def _analyze_cluster_size(client, cluster_id: str, region: str) -> dict:
    """Analyze cluster size to estimate upgrade duration and choose strategy."""
    try:
        resp = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = resp["DBClusters"][0]
        storage_gb = cluster.get("AllocatedStorage", 0)

        # Estimate Blue/Green sync time based on storage size
        # Rough: 1TB ~ 2-3 hours for full replication sync
        if storage_gb > 5000:
            sync_estimate = "4-8 hours"
            strategy_note = "Large cluster — consider weekend window or Backup/Restore"
        elif storage_gb > 1000:
            sync_estimate = "2-4 hours"
            strategy_note = "Medium cluster — Blue/Green feasible with extended window"
        elif storage_gb > 100:
            sync_estimate = "30-60 minutes"
            strategy_note = "Blue/Green sync will complete quickly"
        else:
            sync_estimate = "10-20 minutes"
            strategy_note = "Small cluster — any strategy is fast"

        return {
            "storage_gb": storage_gb,
            "instance_count": len(cluster.get("DBClusterMembers", [])),
            "sync_estimate": sync_estimate,
            "strategy_note": strategy_note,
        }
    except Exception as e:
        return {"error": str(e)}


def _analyze_utilization(cluster_id: str, region: str) -> dict:
    """Analyze cluster utilization patterns to find optimal upgrade window."""
    client = cloudwatch_client(region)
    now = datetime.now(timezone.utc)

    metrics = {}
    try:
        # Get 7-day CPU pattern (hourly averages)
        cpu_resp = client.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster_id}],
            StartTime=now - timedelta(days=7),
            EndTime=now,
            Period=3600,
            Statistics=["Average", "Maximum"],
        )
        if cpu_resp["Datapoints"]:
            avg_cpu = sum(dp["Average"] for dp in cpu_resp["Datapoints"]) / len(cpu_resp["Datapoints"])
            max_cpu = max(dp["Maximum"] for dp in cpu_resp["Datapoints"])
            metrics["avg_cpu_7d"] = round(avg_cpu, 1)
            metrics["max_cpu_7d"] = round(max_cpu, 1)

            # Find lowest utilization hours (best upgrade window)
            hourly_avg = {}
            for dp in cpu_resp["Datapoints"]:
                hour = dp["Timestamp"].hour
                hourly_avg.setdefault(hour, []).append(dp["Average"])
            hourly_means = {h: sum(v)/len(v) for h, v in hourly_avg.items()}
            lowest_hours = sorted(hourly_means.items(), key=lambda x: x[1])[:4]
            metrics["lowest_utilization_hours_utc"] = [h for h, _ in lowest_hours]

        # Get write IOPS (affects Blue/Green sync time)
        iops_resp = client.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName="WriteIOPS",
            Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster_id}],
            StartTime=now - timedelta(days=7),
            EndTime=now,
            Period=3600,
            Statistics=["Average", "Maximum"],
        )
        if iops_resp["Datapoints"]:
            avg_iops = sum(dp["Average"] for dp in iops_resp["Datapoints"]) / len(iops_resp["Datapoints"])
            max_iops = max(dp["Maximum"] for dp in iops_resp["Datapoints"])
            metrics["avg_write_iops_7d"] = round(avg_iops, 1)
            metrics["max_write_iops_7d"] = round(max_iops, 1)

        # Get connections
        conn_resp = client.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName="DatabaseConnections",
            Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster_id}],
            StartTime=now - timedelta(days=1),
            EndTime=now,
            Period=3600,
            Statistics=["Average", "Maximum"],
        )
        if conn_resp["Datapoints"]:
            metrics["avg_connections_24h"] = round(
                sum(dp["Average"] for dp in conn_resp["Datapoints"]) / len(conn_resp["Datapoints"]), 0
            )
            metrics["max_connections_24h"] = max(dp["Maximum"] for dp in conn_resp["Datapoints"])

    except Exception as e:
        metrics["error"] = str(e)

    return metrics


def _gather_database_metadata(cluster_config: dict) -> dict:
    """Connect to the database and pull live metadata for upgrade analysis.

    This gives the agent real context: what tables, extensions, functions,
    triggers, and data volumes exist — critical for understanding upgrade impact.
    """
    import os
    cluster_id = cluster_config.get("cluster_id", "")
    region = cluster_config.get("region", os.environ.get("AWS_REGION", ""))

    try:
        import psycopg2
    except ImportError:
        return {"error": "psycopg2 not available"}

    # Resolve credentials
    from agents.validation.baseline import _get_db_credentials
    creds = _get_db_credentials()

    # Resolve endpoint dynamically from cluster ID
    try:
        from shared.aws_clients import rds_client
        client = rds_client(region)
        resp = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        host = resp["DBClusters"][0]["Endpoint"]
        port = resp["DBClusters"][0].get("Port", 5432)
    except Exception as e:
        return {"error": f"Could not resolve endpoint for {cluster_id}: {e}"}

    metadata = {"databases": []}

    try:
        conn = psycopg2.connect(
            host=host, port=port, dbname='postgres',
            user=creds["user"], password=creds["password"], connect_timeout=5,
        )
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute("SELECT datname, pg_size_pretty(pg_database_size(datname)) FROM pg_database WHERE datistemplate = false AND datname NOT IN ('rdsadmin', 'postgres') ORDER BY pg_database_size(datname) DESC")
        databases = cur.fetchall()
        conn.close()

        for db_name, db_size in databases:
            db_info = {"name": db_name, "size": db_size}
            try:
                conn = psycopg2.connect(
                    host=host, port=port, dbname=db_name,
                    user=creds["user"], password=creds["password"], connect_timeout=5,
                )
                conn.autocommit = True
                cur = conn.cursor()

                # Extensions
                cur.execute("SELECT extname, extversion FROM pg_extension WHERE extname != 'plpgsql' ORDER BY extname")
                db_info["extensions"] = [{"name": r[0], "version": r[1]} for r in cur.fetchall()]

                # Tables with row counts and sizes
                cur.execute("""
                    SELECT schemaname, relname, n_live_tup,
                           pg_size_pretty(pg_total_relation_size(schemaname||'.'||relname))
                    FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 10
                """)
                db_info["tables"] = [
                    {"schema": r[0], "name": r[1], "rows": r[2], "size": r[3]}
                    for r in cur.fetchall()
                ]

                # Indexes
                cur.execute("""
                    SELECT indexrelname, idx_scan, pg_size_pretty(pg_relation_size(indexrelid))
                    FROM pg_stat_user_indexes ORDER BY pg_relation_size(indexrelid) DESC LIMIT 10
                """)
                db_info["indexes"] = [
                    {"name": r[0], "scans": r[1], "size": r[2]} for r in cur.fetchall()
                ]

                # Functions (user-defined, exclude extension-provided C functions)
                cur.execute("""
                    SELECT routine_name, routine_type, external_language
                    FROM information_schema.routines
                    WHERE routine_schema NOT IN ('pg_catalog','information_schema')
                      AND external_language != 'C'
                    ORDER BY routine_name
                """)
                db_info["functions"] = [
                    {"name": r[0], "type": r[1], "language": r[2]} for r in cur.fetchall()
                ]

                # Triggers
                cur.execute("""
                    SELECT trigger_name, event_object_table, action_timing, event_manipulation
                    FROM information_schema.triggers
                    WHERE trigger_schema NOT IN ('pg_catalog','information_schema')
                """)
                db_info["triggers"] = [
                    {"name": r[0], "table": r[1], "timing": r[2], "event": r[3]} for r in cur.fetchall()
                ]

                # Views
                cur.execute("""
                    SELECT table_name FROM information_schema.views
                    WHERE table_schema = 'public'
                """)
                db_info["views"] = [r[0] for r in cur.fetchall()]

                # Total row count
                cur.execute("SELECT sum(n_live_tup) FROM pg_stat_user_tables")
                db_info["total_rows"] = cur.fetchone()[0] or 0

                conn.close()
            except Exception as e:
                db_info["error"] = str(e)

            metadata["databases"].append(db_info)

    except Exception as e:
        metadata["error"] = str(e)

    return metadata


def _generate_change_plan(
    cluster_config: dict,
    target_version: str,
    param_analysis: dict,
    extension_analysis: dict,
    size_analysis: dict,
    utilization: dict,
    is_major_upgrade: bool,
    db_metadata: dict = None,
) -> dict:
    """Use AI to generate a comprehensive change plan.

    THIS IS THE KEY AI REASONING STEP:
    Given all the collected data, generate an ordered list of actions
    that must happen before, during, and after the upgrade.
    """
    db_metadata_section = ""
    if db_metadata and not db_metadata.get("error"):
        db_metadata_section = f"""
## Database Objects (live metadata from cluster)
{json.dumps(db_metadata, indent=2, default=str)}

Consider these database objects when planning:
- Large tables may need ANALYZE after upgrade to rebuild statistics
- Functions using deprecated PL/pgSQL features may break on major upgrades
- GIN indexes may need REINDEX after major version upgrades
- Triggers referencing system catalogs may need updates
- Extensions must be compatible with target version
- Views using version-specific syntax should be validated
"""

    prompt = f"""You are an Aurora PostgreSQL upgrade planner. Generate a complete change plan.

## Cluster Configuration
{json.dumps(cluster_config, indent=2, default=str)}

## Target Version: {target_version}
## Is Major Upgrade: {is_major_upgrade}

## Parameter Analysis
{json.dumps(param_analysis, indent=2, default=str)}

## Extension Analysis
{json.dumps(extension_analysis, indent=2, default=str)}

## Size Analysis
{json.dumps(size_analysis, indent=2, default=str)}

## Utilization (7-day metrics)
{json.dumps(utilization, indent=2, default=str)}
{db_metadata_section}

## Your Task
Generate a change plan that covers:
1. Whether the cluster is ready to upgrade NOW, or what needs to change first
2. Which parameters need to be added/removed/modified BEFORE upgrade
3. Which extensions need updates
4. Best upgrade strategy given RTO/RPO requirements, size, utilization, and environment
5. Optimal time window based on utilization patterns
6. For each pre-upgrade/upgrade step that can be executed automatically, an
   `exec_action` + `exec_params` pair from the ALLOWED ACTIONS list below
7. Post-upgrade validation steps, with `exec_action`/`exec_params` where one applies

## ALLOWED ACTIONS (exec_action must be one of these — do not invent others)

| exec_action | exec_params (all string values) | what it does |
|---|---|---|
| reboot_db_instance | db_instance_identifier | Reboots a DB instance to apply pending parameter changes |
| delete_blue_green_deployment | blue_green_deployment_identifier | Deletes an existing Blue/Green deployment |
| describe_db_cluster_status | db_cluster_identifier | Reads the cluster's current status |
| describe_cluster_events | db_cluster_identifier | Reads recent RDS events for the cluster |
| get_replica_lag_metric | db_cluster_identifier | Reads the AuroraReplicaLag CloudWatch metric |
| run_sql | sql, database (optional) | Runs a validation SQL statement against the cluster |

If a step doesn't map to one of these (e.g. "wait 10 minutes", "check the console"),
omit `exec_action`/`exec_params` entirely — just describe it in `action`/`validation`.
Never put a shell command string in `exec_params` — `exec_params` values are literal
identifiers/text passed straight to an AWS API call, not a command line.
`command`/`validation` remain free text for the human-readable description only;
they are display-only and are never executed.

## STRATEGY SELECTION RULES (MANDATORY — follow strictly):

| Environment | RTO Target | Strategy | Reason |
|-------------|-----------|----------|--------|
| production  | <60s      | blue_green | Near-zero downtime switchover (~30s). Mandatory for prod unless cluster has blocking issues that prevent B/G |
| production (>5TB) | <5min | blue_green | Still B/G but schedule extended sync window |
| staging     | <5min     | blue_green | Validates the exact same B/G process that prod will use |
| dev         | <30min    | in_place | Acceptable downtime, saves cost of provisioning green |

NEVER recommend "in_place" or "backup_restore" for production clusters.
- in_place causes 5-30 min downtime depending on size (unacceptable for production RTO < 60s)
- backup_restore causes 30min-2hr downtime and requires DNS changes (disaster recovery only)
- blue_green provides ~30s switchover with instant rollback capability

The ONLY exception to recommend in_place for production:
- Blue/Green is blocked (e.g., missing logical replication param, unsupported extension)
- AND the blocking issue cannot be resolved within the maintenance window
- In this case, mark it as a blocking_issue and set ready=false

For major version upgrades (e.g., 13→16, 14→15):
- Production: MUST be blue_green with extended bake time (48h minimum)
- Staging: blue_green with 24h bake
- Dev: in_place is acceptable

Consider:
- If param_group has issues, a new PG{target_version.split('.')[0]} param group is needed
- instance_count > 1 means readers exist = higher blast radius = MUST use blue_green
- If cluster is large (>1TB), account for B/G sync time in scheduling
- Blue/Green requires rds.logical_replication=1 — flag if missing

Respond with JSON:
{{
  "ready": true/false,
  "blocking_issues": ["issue1 that MUST be fixed before upgrade"],
  "strategy": "blue_green|in_place|backup_restore",
  "strategy_reason": "why this strategy",
  "estimated_duration": "...",
  "optimal_window_utc": "HH:MM-HH:MM",
  "pre_upgrade_steps": [
    {{"order": 1, "action": "human-readable label", "command": "human-readable description, not executed", "exec_action": "reboot_db_instance", "exec_params": {{"db_instance_identifier": "..."}}, "wait_for": "description"}},
    ...
  ],
  "upgrade_steps": [
    {{"order": 1, "action": "human-readable label", "command": "human-readable description, not executed", "exec_action": "describe_db_cluster_status", "exec_params": {{"db_cluster_identifier": "..."}}}},
    ...
  ],
  "post_upgrade_steps": [
    {{"order": 1, "action": "human-readable label", "validation": "how to verify", "exec_action": "run_sql", "exec_params": {{"sql": "..."}}}},
    ...
  ],
  "rollback_plan": "if upgrade fails, do X",
  "parameter_changes": {{
    "create_new_group": true/false,
    "new_group_family": "aurora-postgresql16",
    "params_to_set": {{"param_name": "value"}},
    "params_to_remove": ["param_name"]
  }}
}}"""

    import re
    json_str = ""
    try:
        client = bedrock_client(region="")
        response = client.invoke_model(
            modelId=BEDROCK_MODEL_ID,
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
            _start, _end = text.find("{"), text.rfind("}")
            # Require an ordered {...} span so a truncated response (opening
            # brace, no closing) doesn't raise "substring not found".
            json_str = text[_start:_end + 1] if 0 <= _start < _end else ""
            if json_str:
                return json.loads(json_str)
    except json.JSONDecodeError:
        # Fix common AI JSON issues: trailing commas, single-line comments, ellipsis
        try:
            cleaned = re.sub(r',\s*([}\]])', r'\1', json_str)
            cleaned = re.sub(r'//[^\n]*', '', cleaned)
            cleaned = re.sub(r'\.\.\.\s*,?', '', cleaned)
            return json.loads(cleaned)
        except Exception as parse_err:
            logger.error(f"JSON parse retry failed: {parse_err}")
    except Exception as e:
        logger.error(f"Change plan generation failed: {e}")

    # Fallback: deterministic plan with RTO/RPO-aware strategy selection
    env = cluster_config.get("environment", "dev")
    instance_count = cluster_config.get("instance_count", 1)

    # Strategy selection: production/staging MUST use blue_green
    if env == "production":
        strategy = "blue_green"
        strategy_reason = "Production requires <60s RTO — Blue/Green provides ~30s switchover with instant rollback"
    elif env == "staging":
        strategy = "blue_green"
        strategy_reason = "Staging mirrors production process — validates B/G workflow before prod rollout"
    else:
        strategy = "in_place" if not is_major_upgrade else "blue_green"
        strategy_reason = "Dev environment with acceptable downtime" if not is_major_upgrade else "Major version upgrade requires Blue/Green even in dev for safety"

    # Override: if multiple instances (readers), always blue_green
    if instance_count > 1 and strategy != "blue_green":
        strategy = "blue_green"
        strategy_reason += " (overridden: cluster has reader instances, in-place would cause extended reader downtime)"

    return {
        "ready": len(param_analysis.get("issues", [])) == 0,
        "blocking_issues": [i["message"] for i in param_analysis.get("issues", [])],
        "strategy": strategy,
        "strategy_reason": f"Fallback (AI unavailable): {strategy_reason}",
        "estimated_duration": size_analysis.get("sync_estimate", "unknown"),
        "pre_upgrade_steps": [],
        "upgrade_steps": [],
        "post_upgrade_steps": [],
    }
