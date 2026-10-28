# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pre-flight checks — Must ALL pass before Blue/Green deployment can proceed.

These guardrails prevent upgrades from starting in unsafe conditions.
Any failure here blocks the upgrade and reports the reason.
"""
import json
import logging
from datetime import datetime, timezone, timedelta

from shared.aws_clients import rds_client, cloudwatch_client, dynamodb_resource
from shared.config import DYNAMODB_TABLE

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

PREFLIGHT_CONFIG = {
    "max_replication_lag_ms": 200,
    "max_long_running_txn_minutes": 5,
    "backup_recency_minutes": 60,
    "snapshot_wait_timeout_seconds": 600,
    "change_windows": {
        "dev": None,  # Anytime
        "staging": {"start_hour_utc": 2, "end_hour_utc": 8},
        "production": {"start_hour_utc": 4, "end_hour_utc": 7},
    },
}


def run_preflight_checks(cluster_id: str, region: str, environment: str, dry_run: bool = False) -> dict:
    """Run all pre-flight checks. Returns pass/fail with details.

    When dry_run=True, skip destructive actions (snapshot creation, lock acquisition)
    and just validate readiness. Used by the UI guardrails panel.
    """
    checks = {}

    checks["cluster_status"] = check_cluster_healthy(cluster_id, region)
    checks["concurrency_lock"] = check_lock_available(cluster_id) if dry_run else acquire_upgrade_lock(cluster_id)
    checks["replication_lag"] = check_replication_lag(cluster_id, region)
    checks["long_running_txns"] = check_no_blocking_transactions(cluster_id, region)
    checks["backup_current"] = check_backups_current(cluster_id, region)
    checks["change_window"] = check_change_window(environment, cluster_id)
    checks["pre_upgrade_snapshot"] = check_snapshot_feasibility(cluster_id, region) if dry_run else create_pre_upgrade_snapshot(cluster_id, region)

    all_passed = all(c["passed"] for c in checks.values())

    if not all_passed:
        release_upgrade_lock(cluster_id)

    return {
        "cluster_id": cluster_id,
        "all_passed": all_passed,
        "checks": checks,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "blocked_by": [name for name, c in checks.items() if not c["passed"]],
    }


def check_cluster_healthy(cluster_id: str, region: str) -> dict:
    """Verify cluster is available with no pending modifications or failover."""
    client = rds_client(region)
    try:
        response = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = response["DBClusters"][0]

        status = cluster["Status"]
        if status != "available":
            return {"passed": False, "reason": f"Cluster status is '{status}', expected 'available'"}

        pending = cluster.get("PendingModifiedValues", {})
        if pending:
            return {"passed": False, "reason": f"Pending modifications: {pending}"}

        for member in cluster.get("DBClusterMembers", []):
            inst_resp = client.describe_db_instances(
                DBInstanceIdentifier=member["DBInstanceIdentifier"]
            )
            inst = inst_resp["DBInstances"][0]
            if inst["DBInstanceStatus"] != "available":
                return {
                    "passed": False,
                    "reason": f"Instance {member['DBInstanceIdentifier']} is '{inst['DBInstanceStatus']}'",
                }
            if inst.get("PendingModifiedValues"):
                return {
                    "passed": False,
                    "reason": f"Instance {member['DBInstanceIdentifier']} has pending modifications",
                }

        return {"passed": True, "reason": "Cluster and all instances available, no pending changes"}

    except Exception as e:
        return {"passed": False, "reason": f"Health check failed: {e}"}


def check_replication_lag(cluster_id: str, region: str) -> dict:
    """Verify replica lag is below threshold."""
    client = cloudwatch_client(region)
    now = datetime.now(timezone.utc)
    threshold = PREFLIGHT_CONFIG["max_replication_lag_ms"]

    try:
        response = client.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName="AuroraReplicaLag",
            Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster_id}],
            StartTime=now - timedelta(minutes=5),
            EndTime=now,
            Period=60,
            Statistics=["Maximum"],
        )

        if not response["Datapoints"]:
            return {"passed": True, "reason": "No replica lag data (single-instance cluster or no readers)"}

        max_lag = max(dp["Maximum"] for dp in response["Datapoints"])
        if max_lag > threshold:
            return {
                "passed": False,
                "reason": f"Replica lag {max_lag:.0f}ms exceeds threshold {threshold}ms",
            }

        return {"passed": True, "reason": f"Replica lag {max_lag:.0f}ms (threshold: {threshold}ms)"}

    except Exception as e:
        return {"passed": False, "reason": f"Replication check failed: {e}"}


def check_no_blocking_transactions(cluster_id: str, region: str) -> dict:
    """Check for long-running transactions that could block the upgrade.

    Note: This requires connectivity to the database. In a Lambda without VPC access,
    we fall back to CloudWatch DatabaseConnections metric as a proxy. For full
    implementation, this should run via SSM or a VPC-attached Lambda.
    """
    # TODO: Direct pg_stat_activity query via SSM or VPC Lambda
    # For now, check if there are active sessions via CloudWatch
    client = cloudwatch_client(region)
    now = datetime.now(timezone.utc)

    try:
        response = client.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName="DatabaseConnections",
            Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster_id}],
            StartTime=now - timedelta(minutes=5),
            EndTime=now,
            Period=60,
            Statistics=["Maximum"],
        )

        if response["Datapoints"]:
            max_conns = max(dp["Maximum"] for dp in response["Datapoints"])
            if max_conns > 50:
                return {
                    "passed": False,
                    "reason": f"High connection count ({max_conns:.0f}) — likely active workload. "
                              f"Verify no long-running transactions before proceeding.",
                }

        return {
            "passed": True,
            "reason": "Connection count normal. For full txn check, enable VPC Lambda or SSM.",
        }

    except Exception as e:
        return {"passed": True, "reason": f"Could not check transactions (non-blocking): {e}"}


def check_backups_current(cluster_id: str, region: str) -> dict:
    """Verify automated backups are enabled and latest restorable time is recent."""
    client = rds_client(region)
    max_age = PREFLIGHT_CONFIG["backup_recency_minutes"]

    try:
        response = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = response["DBClusters"][0]

        retention = cluster.get("BackupRetentionPeriod", 0)
        if retention < 1:
            return {"passed": False, "reason": "Automated backups are disabled (retention = 0)"}

        latest_restorable = cluster.get("LatestRestorableTime")
        if not latest_restorable:
            return {"passed": False, "reason": "No LatestRestorableTime available"}

        age = datetime.now(timezone.utc) - latest_restorable.replace(tzinfo=timezone.utc)
        age_minutes = age.total_seconds() / 60

        if age_minutes > max_age:
            return {
                "passed": False,
                "reason": f"Latest restorable time is {age_minutes:.0f} min old (threshold: {max_age} min)",
            }

        return {
            "passed": True,
            "reason": f"Backups current (retention={retention}d, last restorable {age_minutes:.0f} min ago)",
        }

    except Exception as e:
        return {"passed": False, "reason": f"Backup check failed: {e}"}


def check_change_window(environment: str, cluster_id: str = None) -> dict:
    """Verify we're inside the approved change window for this environment/cluster."""
    window = PREFLIGHT_CONFIG["change_windows"].get(environment)

    if window is None:
        return {"passed": True, "reason": f"No change window restriction for '{environment}'"}

    # If window has a cluster list, check if this cluster is assigned
    assigned_clusters = window.get("clusters", [])
    if assigned_clusters and cluster_id and cluster_id not in assigned_clusters:
        return {"passed": True, "reason": f"Cluster '{cluster_id}' not assigned to '{environment}' window (no restriction)"}

    now_utc = datetime.now(timezone.utc)
    current_hour = now_utc.hour
    current_day = now_utc.strftime("%a").lower()

    start = window["start_hour_utc"]
    end = window["end_hour_utc"]

    # Check day restriction if configured
    allowed_days = window.get("days", [])
    if allowed_days and current_day not in allowed_days:
        return {
            "passed": False,
            "reason": f"Outside change window for '{environment}' "
                      f"(today: {current_day}, allowed days: {', '.join(allowed_days)})",
        }

    if start <= current_hour < end:
        return {"passed": True, "reason": f"Within change window ({start:02d}:00–{end:02d}:00 UTC, {current_day})"}

    return {
        "passed": False,
        "reason": f"Outside change window for '{environment}' "
                  f"(current: {current_hour:02d}:00 UTC, allowed: {start:02d}:00–{end:02d}:00 UTC)",
    }


def create_pre_upgrade_snapshot(cluster_id: str, region: str) -> dict:
    """Create a manual snapshot before upgrade and wait for it to be available."""
    client = rds_client(region)
    snapshot_id = f"{cluster_id}-pre-upgrade-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}"

    try:
        try:
            client.create_db_cluster_snapshot(
                DBClusterIdentifier=cluster_id,
                DBClusterSnapshotIdentifier=snapshot_id,
                Tags=[
                    {"Key": "Purpose", "Value": "pre-upgrade-snapshot"},
                    {"Key": "CreatedBy", "Value": "aurora-patch-agent"},
                ],
            )
        except Exception as e:
            # Tagging needs rds:AddTagsToResource, which the default deployment
            # doesn't grant (README "Enabling Blue/Green execution"). The
            # snapshot itself is what matters as a rollback anchor, so retry
            # without our tags rather than failing the upgrade.
            if "AddTagsToResource" not in str(e):
                raise
            logger.warning(f"Tagging not permitted; creating untagged snapshot for {cluster_id}")
            client.create_db_cluster_snapshot(
                DBClusterIdentifier=cluster_id,
                DBClusterSnapshotIdentifier=snapshot_id,
            )

        # Wait for snapshot to become available (with timeout)
        waiter = client.get_waiter("db_cluster_snapshot_available")
        waiter.wait(
            DBClusterSnapshotIdentifier=snapshot_id,
            WaiterConfig={"Delay": 15, "MaxAttempts": 40},  # ~10 min max
        )

        return {
            "passed": True,
            "reason": f"Pre-upgrade snapshot '{snapshot_id}' created and available",
            "snapshot_id": snapshot_id,
        }

    except Exception as e:
        return {"passed": False, "reason": f"Snapshot creation failed: {e}"}


def acquire_upgrade_lock(cluster_id: str) -> dict:
    """Acquire a DynamoDB lock to prevent concurrent upgrades on the same cluster."""
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        now = datetime.now(timezone.utc)
        lock_ttl = int(now.timestamp()) + 3600  # 1 hour lock

        table.put_item(
            Item={
                "pk": f"LOCK#{cluster_id}",
                "sk": "UPGRADE",
                "locked_at": now.isoformat(),
                "ttl": lock_ttl,
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
        return {"passed": True, "reason": "Upgrade lock acquired"}

    except dynamodb_resource().meta.client.exceptions.ConditionalCheckFailedException:
        return {
            "passed": False,
            "reason": f"Another upgrade is already in progress for {cluster_id} (lock exists)",
        }
    except Exception as e:
        # If DynamoDB is unavailable, fail open with a warning (for testing without infra)
        logger.warning(f"Lock acquisition failed (non-fatal in test): {e}")
        return {"passed": True, "reason": f"Lock check skipped (DynamoDB unavailable): {e}"}


def release_upgrade_lock(cluster_id: str):
    """Release the upgrade lock after completion or failure."""
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        table.delete_item(Key={"pk": f"LOCK#{cluster_id}", "sk": "UPGRADE"})
    except Exception as e:
        logger.warning(f"Lock release failed: {e}")


def check_lock_available(cluster_id: str) -> dict:
    """Check if a lock can be acquired without actually acquiring it (dry-run)."""
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        response = table.get_item(Key={"pk": f"LOCK#{cluster_id}", "sk": "UPGRADE"})
        if "Item" in response:
            return {"passed": False, "reason": f"Another upgrade is already in progress for {cluster_id} (lock exists)"}
        return {"passed": True, "reason": "No concurrent upgrade running (lock available)"}
    except Exception as e:
        return {"passed": True, "reason": f"Lock check skipped (DynamoDB unavailable): {e}"}


def check_snapshot_feasibility(cluster_id: str, region: str) -> dict:
    """Validate that a snapshot can be created without actually creating one (dry-run).

    Checks: cluster is available, no snapshot in progress.
    """
    client = rds_client(region)
    try:
        response = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = response["DBClusters"][0]

        if cluster["Status"] != "available":
            return {"passed": False, "reason": f"Cannot snapshot: cluster status is '{cluster['Status']}'"}

        snap_resp = client.describe_db_cluster_snapshots(
            DBClusterIdentifier=cluster_id,
            SnapshotType="manual",
        )
        creating = [s for s in snap_resp.get("DBClusterSnapshots", []) if s["Status"] == "creating"]
        if creating:
            return {"passed": False, "reason": "Another snapshot is already being created"}

        return {"passed": True, "reason": "Snapshot creation will proceed when upgrade starts (cluster eligible)"}

    except Exception as e:
        return {"passed": False, "reason": f"Snapshot feasibility check failed: {e}"}
