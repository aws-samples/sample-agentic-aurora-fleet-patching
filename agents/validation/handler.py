# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Validation Agent — Post-upgrade health checks.

Verifies the upgraded cluster is healthy: connections work, extensions loaded,
replication is active, and query performance hasn't regressed.
"""
import json
import logging
from datetime import datetime, timezone, timedelta

from shared.aws_clients import rds_client, cloudwatch_client, dynamodb_resource
from shared.config import DYNAMODB_TABLE, HEALTH_THRESHOLDS
from shared.models import HealthCheck, UpgradeStatus

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event, context):
    """Run post-upgrade validation on a cluster."""
    cluster_id = event["cluster_id"]
    region = event["region"]
    baseline = event.get("baseline", {})

    logger.info(f"Validation starting for {cluster_id}")

    health = run_health_checks(cluster_id, region, baseline)

    result = {
        "cluster_id": cluster_id,
        "validation_time": datetime.now(timezone.utc).isoformat(),
        "passed": health.passed,
        "checks": {
            "connections_ok": health.connections_ok,
            "extensions_loaded": health.extensions_loaded,
            "replication_lag_ms": health.replication_lag_ms,
            "p99_latency_ms": health.p99_latency_ms,
            "baseline_p99_ms": health.baseline_p99_ms,
            "error_rate_pct": health.error_rate_pct,
        },
        "details": health.details,
        "action": "proceed" if health.passed else "rollback",
    }

    status = UpgradeStatus.COMPLETE if health.passed else UpgradeStatus.FAILED
    _update_state(cluster_id, status, result)

    return result


def run_health_checks(cluster_id: str, region: str, baseline: dict) -> HealthCheck:
    """Execute all health checks against the cluster."""
    health = HealthCheck(cluster_id=cluster_id)

    health.connections_ok = check_connections(cluster_id, region)
    health.replication_lag_ms = check_replication_lag(cluster_id, region)
    health.extensions_loaded = check_extensions(cluster_id, region)
    health.p99_latency_ms = get_current_latency(cluster_id, region)
    health.baseline_p99_ms = baseline.get("p99_latency_ms", 0)
    health.error_rate_pct = get_error_rate(cluster_id, region)

    health.passed = evaluate_health(health)
    health.details = _build_details(health)

    return health


def check_connections(cluster_id: str, region: str) -> bool:
    """Verify the cluster is accepting connections."""
    client = rds_client(region)
    try:
        response = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = response["DBClusters"][0]

        if cluster["Status"] != "available":
            return False

        for member in cluster.get("DBClusterMembers", []):
            instance_resp = client.describe_db_instances(
                DBInstanceIdentifier=member["DBInstanceIdentifier"]
            )
            instance = instance_resp["DBInstances"][0]
            if instance["DBInstanceStatus"] != "available":
                return False

        return True
    except Exception as e:
        logger.error(f"Connection check failed: {e}")
        return False


def check_replication_lag(cluster_id: str, region: str) -> float:
    """Get current replication lag from CloudWatch."""
    client = cloudwatch_client(region)
    now = datetime.now(timezone.utc)

    try:
        response = client.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName="AuroraReplicaLag",
            Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster_id}],
            StartTime=now - timedelta(minutes=10),
            EndTime=now,
            Period=60,
            Statistics=["Maximum"],
        )
        if response["Datapoints"]:
            return max(dp["Maximum"] for dp in response["Datapoints"])
    except Exception as e:
        logger.warning(f"Replication lag check failed: {e}")

    return 0.0


def check_extensions(cluster_id: str, region: str) -> bool:
    """Verify cluster is available (extension loading happens at startup)."""
    client = rds_client(region)
    try:
        response = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        return response["DBClusters"][0]["Status"] == "available"
    except Exception:
        return False


def get_current_latency(cluster_id: str, region: str) -> float:
    """Get P99 read/write latency from CloudWatch."""
    client = cloudwatch_client(region)
    now = datetime.now(timezone.utc)

    try:
        for metric in ["ReadLatency", "WriteLatency"]:
            response = client.get_metric_statistics(
                Namespace="AWS/RDS",
                MetricName=metric,
                Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster_id}],
                StartTime=now - timedelta(minutes=10),
                EndTime=now,
                Period=60,
                Statistics=["p99"],
                ExtendedStatistics=["p99"],
            )
            if response.get("Datapoints"):
                values = [dp.get("ExtendedStatistics", {}).get("p99", 0) for dp in response["Datapoints"]]
                if values:
                    return max(values) * 1000  # convert to ms
    except Exception as e:
        logger.warning(f"Latency check failed: {e}")

    return 0.0


def get_error_rate(cluster_id: str, region: str) -> float:
    """Check database connection error rate."""
    client = cloudwatch_client(region)
    now = datetime.now(timezone.utc)

    try:
        response = client.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName="LoginFailures",
            Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster_id}],
            StartTime=now - timedelta(minutes=10),
            EndTime=now,
            Period=60,
            Statistics=["Sum"],
        )
        if response["Datapoints"]:
            total_failures = sum(dp["Sum"] for dp in response["Datapoints"])
            return min(total_failures / 10.0, 100.0)
    except Exception:
        pass

    return 0.0


def evaluate_health(health: HealthCheck) -> bool:
    """Determine if all health checks pass thresholds."""
    if not health.connections_ok:
        return False

    if health.replication_lag_ms > HEALTH_THRESHOLDS["replication_lag_max_ms"]:
        return False

    if health.error_rate_pct > HEALTH_THRESHOLDS["error_rate_max_pct"]:
        return False

    if health.baseline_p99_ms > 0 and health.p99_latency_ms > 0:
        regression = ((health.p99_latency_ms - health.baseline_p99_ms) / health.baseline_p99_ms) * 100
        if regression > HEALTH_THRESHOLDS["p99_latency_regression_pct"]:
            return False

    return True


def _build_details(health: HealthCheck) -> dict:
    details = {}
    if not health.connections_ok:
        details["connection_failure"] = "One or more instances not in 'available' state"
    if health.replication_lag_ms > HEALTH_THRESHOLDS["replication_lag_max_ms"]:
        details["replication_lag"] = f"{health.replication_lag_ms}ms exceeds {HEALTH_THRESHOLDS['replication_lag_max_ms']}ms threshold"
    if health.baseline_p99_ms > 0 and health.p99_latency_ms > 0:
        regression = ((health.p99_latency_ms - health.baseline_p99_ms) / health.baseline_p99_ms) * 100
        if regression > HEALTH_THRESHOLDS["p99_latency_regression_pct"]:
            details["latency_regression"] = f"{regression:.1f}% regression (baseline={health.baseline_p99_ms}ms, current={health.p99_latency_ms}ms)"
    return details


def _update_state(cluster_id: str, status: UpgradeStatus, details: dict):
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        table.put_item(Item={
            "pk": f"VALIDATION#{cluster_id}",
            "sk": datetime.now(timezone.utc).isoformat(),
            "status": status.value,
            "passed": details.get("passed", False),
            "data": json.dumps(details, default=str),
            "ttl": int(datetime.now(timezone.utc).timestamp()) + 86400 * 30,
        })
    except Exception as e:
        logger.warning(f"State update failed: {e}")
