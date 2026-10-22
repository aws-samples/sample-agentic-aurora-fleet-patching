# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Assessment Agent — Evaluates upgrade compatibility and risk for each cluster.

Checks extension compatibility, parameter group conflicts, cluster size,
traffic patterns, and produces a risk score that determines rollout order.
"""
import json
import logging
from datetime import datetime, timezone

from shared.aws_clients import rds_client, cloudwatch_client, bedrock_client, dynamodb_resource
from shared.config import DYNAMODB_TABLE, BEDROCK_MODEL_ID
from shared.models import UpgradeTarget, RiskLevel, UpgradeStatus

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

KNOWN_PROBLEMATIC_EXTENSIONS = {
    "postgis": {"min_safe_version": "15.4", "note": "PostGIS 3.3+ required for PG 16"},
    "pglogical": {"min_safe_version": "15.0", "note": "Replication slots may break across major versions"},
    "pg_partman": {"min_safe_version": "15.2", "note": "Partition maintenance jobs need revalidation"},
    "pg_cron": {"min_safe_version": "14.6", "note": "Cron jobs paused during upgrade"},
}

# MySQL-specific upgrade concerns (plugins/features that may break across versions)
KNOWN_MYSQL_UPGRADE_ISSUES = {
    "mysql_native_password": {"note": "Deprecated in MySQL 8.0 — switch to caching_sha2_password"},
    "query_cache": {"note": "Removed in MySQL 8.0 — queries using SQL_CACHE hint will error"},
    "innodb_file_format": {"note": "Removed in MySQL 8.0 — was only Barracuda/Antelope distinction"},
    "group_replication": {"note": "Plugin version must match server version after upgrade"},
    "partition_engine": {"note": "Non-native partitioning removed in 8.0 — must convert to native InnoDB partitioning"},
}


def handler(event, context):
    """Assess each upgrade target for risk and compatibility."""
    targets = event.get("upgrade_targets", [])
    logger.info(f"Assessment starting for {len(targets)} clusters")

    assessed = []
    for target_data in targets:
        target = _deserialize_target(target_data)
        assessment = assess_cluster(target)
        assessed.append(assessment)

    assessed.sort(key=lambda t: t["risk_score"])

    result = {
        "assessment_time": datetime.now(timezone.utc).isoformat(),
        "total_assessed": len(assessed),
        "risk_summary": _risk_summary(assessed),
        "rollout_order": [a["cluster_id"] for a in assessed],
        "assessments": assessed,
    }

    persist_state(result)
    return result


def assess_cluster(target: UpgradeTarget) -> dict:
    """Run all assessment checks for a single cluster.

    Incorporates historical learning: past rollback causes and upgrade outcomes
    amplify or reduce risk scores based on real-world evidence.
    """
    from shared.outcome_learning import get_risk_amplifiers, get_duration_estimate, get_upgrade_stats

    risk_factors = []
    risk_score = 0.0

    ext_issues = check_extension_compatibility(target)
    if ext_issues:
        risk_factors.extend(ext_issues)
        risk_score += len(ext_issues) * 20

    param_issues = check_parameter_compatibility(target)
    if param_issues:
        risk_factors.extend(param_issues)
        risk_score += len(param_issues) * 15

    size_risk = assess_size_risk(target)
    risk_score += size_risk

    traffic_risk = assess_traffic_patterns(target)
    risk_score += traffic_risk

    if _is_major_version_upgrade(target):
        risk_factors.append("Major version upgrade (e.g., 15.x → 16.x) — higher risk")
        risk_score += 25

    # ─── Global Database: Check upgrade constraints ────────────────────────
    try:
        from shared.global_db import check_upgrade_eligibility
        gdb = check_upgrade_eligibility(
            target.cluster.cluster_id,
            target.cluster.region,
            target.target_version,
            target.cluster.engine_type.value,
        )
        if gdb.get("global_database"):
            if not gdb.get("eligible"):
                risk_factors.append(f"Global Database constraint: {gdb['reason']}")
                risk_score += 50  # Blocked — high risk flag
            elif gdb.get("strategy") == "detach_and_upgrade":
                risk_factors.append("Global Database: requires detaching secondaries before upgrade")
                risk_score += 20
            elif gdb.get("warnings"):
                for w in gdb["warnings"]:
                    risk_factors.append(f"Global DB: {w}")
                risk_score += 10
    except Exception as e:
        logger.warning(f"Global DB check failed for {target.cluster.cluster_id}: {e}")

    # ─── Historical Learning: Amplify risk based on past outcomes ──────────
    historical_amplifiers = get_risk_amplifiers(
        target.target_version, target.cluster.extensions
    )
    for amp in historical_amplifiers:
        risk_score += amp.get("risk_increase", 10)
        risk_factors.append(
            f"Historical rollback: extensions {amp['extensions_involved']} "
            f"caused issues in a previous upgrade ({amp['previous_reason']})"
        )

    # Historical success rate for this version pair
    upgrade_stats = get_upgrade_stats(
        target.cluster.engine_version, target.target_version
    )
    if upgrade_stats:
        total = upgrade_stats.get("total_attempts", 0)
        failures = upgrade_stats.get("total_failures", 0)
        if total >= 3 and failures > 0:
            failure_rate = (failures / total) * 100
            if failure_rate > 30:
                risk_score += 20
                risk_factors.append(
                    f"Historical failure rate: {failure_rate:.0f}% "
                    f"({failures}/{total} attempts for this version pair)"
                )
            elif failure_rate > 10:
                risk_score += 10
                risk_factors.append(
                    f"Moderate historical failure rate: {failure_rate:.0f}% "
                    f"({failures}/{total} attempts)"
                )

    # Duration estimate from history
    duration_estimate = get_duration_estimate(
        target.cluster.engine_version,
        target.target_version,
        "blue_green",
        target.cluster.storage_gb,
    )
    # ──────────────────────────────────────────────────────────────────────

    risk_score = min(risk_score, 100)
    risk_level = _score_to_level(risk_score)

    result = {
        "cluster_id": target.cluster.cluster_id,
        "current_version": target.cluster.engine_version,
        "target_version": target.target_version,
        "environment": target.cluster.environment.value,
        "region": target.cluster.region,
        "risk_score": risk_score,
        "risk_level": risk_level.value,
        "risk_factors": risk_factors,
        "extensions": target.cluster.extensions,
        "recommendation": _generate_recommendation(risk_level, risk_factors),
        "status": UpgradeStatus.READY.value if risk_score < 70 else UpgradeStatus.AWAITING_APPROVAL.value,
    }

    # Add historical context to assessment
    if duration_estimate:
        result["estimated_duration"] = duration_estimate
    if upgrade_stats:
        result["historical_stats"] = {
            "attempts": upgrade_stats.get("total_attempts", 0),
            "success_rate": f"{upgrade_stats.get('total_successes', 0) / max(upgrade_stats.get('total_attempts', 1), 1) * 100:.0f}%",
        }
    if historical_amplifiers:
        result["historical_warnings"] = historical_amplifiers

    return result


def check_extension_compatibility(target: UpgradeTarget) -> list[str]:
    """Check if installed extensions/plugins are compatible with target version."""
    issues = []
    if target.cluster.engine_type.value == "aurora-mysql":
        # MySQL-specific checks
        # Check if upgrading from v2 (5.7) to v3 (8.0)
        from agents.scanner.handler import _get_mysql_major_version
        current_major = _get_mysql_major_version(target.cluster.engine_version)
        target_major = _get_mysql_major_version(target.target_version)
        if current_major == "5.7" and target_major == "8.0":
            issues.append("Major version jump: MySQL 5.7 → 8.0. Reserved keywords may break queries (e.g., RANK, GROUPS, SYSTEM)")
            issues.append("utf8mb3 charset deprecated — review tables using utf8 (alias for utf8mb3)")
            issues.append("sql_mode defaults changed — review for NO_AUTO_CREATE_USER removal")
        return issues
    # Existing PostgreSQL logic
    for ext in target.cluster.extensions:
        if ext in KNOWN_PROBLEMATIC_EXTENSIONS:
            info = KNOWN_PROBLEMATIC_EXTENSIONS[ext]
            if target.target_version < info["min_safe_version"]:
                issues.append(f"Extension '{ext}': {info['note']}")
    return issues


def check_parameter_compatibility(target: UpgradeTarget) -> list[str]:
    """Check for parameter group changes that may cause issues."""
    client = rds_client(target.cluster.region)
    issues = []

    try:
        current_params = client.describe_db_cluster_parameters(
            DBClusterParameterGroupName=target.cluster.parameter_group
        )

        custom_params = [
            p for p in current_params.get("Parameters", [])
            if p.get("Source") == "user" and p.get("ParameterValue")
        ]

        deprecated_in_newer = ["wal_keep_segments"]  # replaced by wal_keep_size in PG 13+
        for param in custom_params:
            if param["ParameterName"] in deprecated_in_newer:
                issues.append(
                    f"Parameter '{param['ParameterName']}' is deprecated in target version"
                )

    except Exception as e:
        logger.warning(f"Parameter check failed for {target.cluster.cluster_id}: {e}")
        issues.append("Could not verify parameter compatibility (access error)")

    return issues


def assess_size_risk(target: UpgradeTarget) -> float:
    """Larger clusters = longer switchover = more risk."""
    storage = target.cluster.storage_gb
    if storage > 1000:
        return 15.0
    if storage > 500:
        return 10.0
    if storage > 100:
        return 5.0
    return 0.0


def assess_traffic_patterns(target: UpgradeTarget) -> float:
    """Check current write IOPS to assess risk of upgrading during high traffic."""
    client = cloudwatch_client(target.cluster.region)
    try:
        response = client.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName="WriteIOPS",
            Dimensions=[{"Name": "DBClusterIdentifier", "Value": target.cluster.cluster_id}],
            StartTime=datetime.now(timezone.utc).replace(hour=0, minute=0),
            EndTime=datetime.now(timezone.utc),
            Period=3600,
            Statistics=["Average"],
        )
        if response["Datapoints"]:
            avg_iops = max(dp["Average"] for dp in response["Datapoints"])
            if avg_iops > 10000:
                return 15.0
            if avg_iops > 5000:
                return 10.0
    except Exception:
        pass
    return 0.0


def _is_major_version_upgrade(target: UpgradeTarget) -> bool:
    current_major = target.cluster.engine_version.split(".")[0]
    target_major = target.target_version.split(".")[0]
    return current_major != target_major


def _score_to_level(score: float) -> RiskLevel:
    if score >= 70:
        return RiskLevel.CRITICAL
    if score >= 50:
        return RiskLevel.HIGH
    if score >= 25:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def _risk_summary(assessed: list[dict]) -> dict:
    summary = {"low": 0, "medium": 0, "high": 0, "critical": 0}
    for a in assessed:
        summary[a["risk_level"]] += 1
    return summary


def _generate_recommendation(risk_level: RiskLevel, risk_factors: list[str]) -> str:
    if risk_level == RiskLevel.LOW:
        return "Safe to proceed with automated Blue/Green upgrade."
    if risk_level == RiskLevel.MEDIUM:
        return f"Proceed with extended bake time. Watch: {', '.join(risk_factors[:2])}"
    if risk_level == RiskLevel.HIGH:
        return f"Recommend manual review before proceeding. Issues: {'; '.join(risk_factors)}"
    return "BLOCKED — requires human approval and manual compatibility testing."


def _deserialize_target(data: dict) -> UpgradeTarget:
    from shared.models import ClusterInfo, Environment
    cluster = ClusterInfo(
        cluster_id=data["cluster_id"],
        engine_version=data["current_version"],
        region=data["region"],
        environment=Environment(data["environment"]),
        account_id=data.get("account_id", ""),
        instance_count=data.get("instance_count", 2),
        storage_gb=data.get("storage_gb", 0),
        extensions=data.get("extensions", []),
        parameter_group=data.get("parameter_group", ""),
    )
    return UpgradeTarget(
        cluster=cluster,
        target_version=data["target_version"],
        available_versions=data.get("available_versions", []),
    )


def persist_state(result: dict):
    """Write assessment results to DynamoDB."""
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        table.put_item(Item={
            "pk": "ASSESSMENT",
            "sk": result["assessment_time"],
            "total_assessed": result["total_assessed"],
            "risk_summary": result["risk_summary"],
            "data": json.dumps(result, default=str),
            "ttl": int(datetime.now(timezone.utc).timestamp()) + 86400 * 7,
        })
    except Exception as e:
        logger.warning(f"Failed to persist assessment: {e}")
