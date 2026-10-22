# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Execution Agent — Creates and manages Blue/Green deployments for Aurora upgrades.

PHILOSOPHY: This agent only EXECUTES actions that have been explicitly approved
by the user. It will refuse to create deployments, switchover, or modify clusters
unless there's a corresponding approved recommendation.

The one exception is preflight checks — those are read-only and always allowed.
"""
import json
import logging
import time
from datetime import datetime, timezone

from shared.aws_clients import rds_client, dynamodb_resource
from shared.config import DYNAMODB_TABLE
from shared.models import UpgradeStatus, RiskLevel
from shared.recommendations import (
    recommend_upgrade,
    recommend_switchover,
    require_approval,
    is_approved,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event, context):
    """Execute upgrade for a single cluster via Blue/Green deployment.

    State-changing actions (create, switchover, delete) require an approved
    recommendation_id in the event payload.
    """
    cluster_id = event["cluster_id"]
    target_version = event.get("target_version", "")
    region = event["region"]
    action = event.get("action", "create")
    recommendation_id = event.get("recommendation_id")

    logger.info(f"Execution agent: action={action}, cluster={cluster_id}, target={target_version}")

    # Read-only: always allowed
    if action == "preflight":
        from agents.execution.preflight import run_preflight_checks
        environment = event.get("environment", "dev")
        return run_preflight_checks(cluster_id, region, environment)

    if action == "check_status":
        return check_deployment_status(event["deployment_id"], region)

    if action == "release_lock":
        from agents.execution.preflight import release_upgrade_lock
        release_upgrade_lock(cluster_id)
        return {"status": "lock_released", "cluster_id": cluster_id}

    # State-changing: require approval
    if require_approval(action, cluster_id):
        if not recommendation_id:
            return {
                "status": "approval_required",
                "message": (
                    f"Action '{action}' on {cluster_id} requires user approval. "
                    f"No recommendation_id provided. Use the UI to approve the upgrade first."
                ),
            }
        if not is_approved(recommendation_id):
            return {
                "status": "not_approved",
                "message": (
                    f"Recommendation {recommendation_id} has not been approved. "
                    f"Approve it in the UI before execution can proceed."
                ),
                "recommendation_id": recommendation_id,
            }

    if action == "create":
        from agents.execution.preflight import run_preflight_checks
        environment = event.get("environment", "dev")
        preflight = run_preflight_checks(cluster_id, region, environment)
        if not preflight["all_passed"]:
            logger.warning(f"Pre-flight FAILED for {cluster_id}: {preflight['blocked_by']}")
            _update_state(cluster_id, UpgradeStatus.FAILED, {"preflight": preflight})
            return {"status": "preflight_failed", "preflight": preflight}
        return create_blue_green(cluster_id, target_version, region)
    elif action == "switchover":
        return execute_switchover(event["deployment_id"], region)
    elif action == "delete":
        return delete_deployment(event["deployment_id"], region)
    else:
        raise ValueError(f"Unknown action: {action}")


def create_blue_green(cluster_id: str, target_version: str, region: str) -> dict:
    """Create a Blue/Green deployment for the cluster."""
    # NOTE: Aurora MySQL B/G requires binlog_format to be set in the
    # cluster parameter group. If the source cluster has binary logging
    # disabled, the B/G creation will fail. The preflight checks should
    # catch this.
    client = rds_client(region)
    deployment_id = f"bgd-{cluster_id}-{target_version.replace('.', '-')}"
    # Sanitize: AWS requires [a-zA-Z](?:-?[a-zA-Z0-9]+)* — no underscores,
    # no consecutive hyphens, must start with a letter.
    import re
    deployment_id = re.sub(r'[^a-zA-Z0-9-]', '-', deployment_id)  # replace invalid chars
    deployment_id = re.sub(r'-{2,}', '-', deployment_id)           # collapse consecutive hyphens
    deployment_id = deployment_id.strip('-')[:60]                  # trim and cap length

    cluster = _get_cluster(client, cluster_id)
    if not cluster:
        return {"status": "error", "message": f"Cluster {cluster_id} not found"}
    cluster_arn = cluster["DBClusterArn"]

    try:
        params = {
            "BlueGreenDeploymentName": deployment_id,
            "Source": cluster_arn,
            "TargetEngineVersion": target_version,
        }
        # When the source cluster uses a custom (non-default) DB cluster
        # parameter group, RDS requires an explicit target parameter group for
        # the engine-version upgrade. Resolve the default group for the target
        # version's family so the upgrade is accepted.
        source_pg = cluster.get("DBClusterParameterGroup", "")
        if source_pg and not source_pg.startswith("default."):
            target_pg = _resolve_target_cluster_parameter_group(
                client, cluster.get("Engine", ""), target_version
            )
            if target_pg:
                params["TargetDBClusterParameterGroupName"] = target_pg
                logger.info(
                    f"Source uses custom cluster PG '{source_pg}'; "
                    f"targeting '{target_pg}' for the upgrade"
                )
            else:
                logger.warning(
                    f"Could not resolve a target cluster parameter group for "
                    f"engine version {target_version}; Blue/Green create may fail"
                )
        response = client.create_blue_green_deployment(**params)

        bg_deployment = response["BlueGreenDeployment"]
        result = {
            "deployment_id": bg_deployment["BlueGreenDeploymentIdentifier"],
            "cluster_id": cluster_id,
            "target_version": target_version,
            "status": bg_deployment["Status"],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        _update_state(cluster_id, UpgradeStatus.EXECUTING, result)
        logger.info(f"Blue/Green deployment created: {result['deployment_id']}")
        return result

    except client.exceptions.BlueGreenDeploymentAlreadyExistsFault:
        logger.warning(f"Deployment already exists for {cluster_id}")
        return {"status": "already_exists", "cluster_id": cluster_id}
    except Exception as e:
        logger.error(f"Failed to create Blue/Green for {cluster_id}: {e}")
        _update_state(cluster_id, UpgradeStatus.FAILED, {"error": str(e)})
        return {"status": "error", "message": str(e)}


def check_deployment_status(deployment_id: str, region: str) -> dict:
    """Check if the green environment is ready for switchover."""
    client = rds_client(region)

    try:
        response = client.describe_blue_green_deployments(
            BlueGreenDeploymentIdentifier=deployment_id,
        )

        if not response["BlueGreenDeployments"]:
            return {"status": "not_found", "deployment_id": deployment_id}

        deployment = response["BlueGreenDeployments"][0]
        status = deployment["Status"]

        return {
            "deployment_id": deployment_id,
            "status": status,
            "ready_for_switchover": status == "AVAILABLE",
            "switchover_details": deployment.get("SwitchoverDetails", []),
            "tasks": deployment.get("Tasks", []),
        }

    except Exception as e:
        logger.error(f"Status check failed for {deployment_id}: {e}")
        return {"status": "error", "message": str(e)}


def execute_switchover(deployment_id: str, region: str) -> dict:
    """Execute the switchover from blue to green."""
    client = rds_client(region)

    status = check_deployment_status(deployment_id, region)
    if not status.get("ready_for_switchover"):
        return {
            "status": "not_ready",
            "message": f"Deployment status: {status.get('status')}. Cannot switchover yet.",
        }

    try:
        response = client.switchover_blue_green_deployment(
            BlueGreenDeploymentIdentifier=deployment_id,
            SwitchoverTimeout=300,
        )

        result = {
            "deployment_id": deployment_id,
            "status": response["BlueGreenDeployment"]["Status"],
            "switchover_initiated": datetime.now(timezone.utc).isoformat(),
        }

        logger.info(f"Switchover initiated: {deployment_id}")
        return result

    except Exception as e:
        logger.error(f"Switchover failed for {deployment_id}: {e}")
        return {"status": "error", "message": str(e)}


def delete_deployment(deployment_id: str, region: str, delete_target: bool = True) -> dict:
    """Delete Blue/Green deployment and optionally delete the target (green) cluster.

    Args:
        delete_target: If True, deletes the green cluster resources. Set True for
                       rollback (discard green) or cleanup. Set False only when you
                       want to keep both clusters (e.g. post-switchover, green is now primary).
    """
    client = rds_client(region)

    try:
        # Check deployment status to determine correct action
        resp = client.describe_blue_green_deployments(
            BlueGreenDeploymentIdentifier=deployment_id,
        )
        deployments = resp.get("BlueGreenDeployments", [])
        if not deployments:
            return {"status": "not_found", "message": f"Deployment {deployment_id} not found"}

        status = deployments[0].get("Status", "")

        # Post-switchover: the green IS now the primary, never delete it
        # Only the deployment metadata gets cleaned up
        if status == "SWITCHOVER_COMPLETED":
            client.delete_blue_green_deployment(
                BlueGreenDeploymentIdentifier=deployment_id,
                DeleteTarget=False,
            )
            return {"status": "deleted", "deployment_id": deployment_id,
                    "note": "Post-switchover cleanup — green is now primary, only deployment metadata removed"}

        # Pre-switchover: delete_target controls whether green cluster is removed
        client.delete_blue_green_deployment(
            BlueGreenDeploymentIdentifier=deployment_id,
            DeleteTarget=delete_target,
        )
        return {"status": "deleted", "deployment_id": deployment_id,
                "target_deleted": delete_target,
                "note": "Green cluster deleted" if delete_target else "Green cluster preserved"}
    except Exception as e:
        logger.error(f"Delete failed for {deployment_id}: {e}")
        return {"status": "error", "message": str(e)}


def execute_inplace_upgrade(cluster_id: str, target_version: str, region: str = "") -> dict:
    """Execute an in-place minor version upgrade using ModifyDBCluster.

    Used when the source version doesn't support Blue/Green deployments
    (e.g. Aurora PostgreSQL < 14.6). Causes downtime during the upgrade.
    """
    client = rds_client(region)
    try:
        resp = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        cluster = resp["DBClusters"][0]
        current_version = cluster["EngineVersion"]

        logger.info(f"Starting in-place upgrade: {cluster_id} from {current_version} to {target_version}")

        client.modify_db_cluster(
            DBClusterIdentifier=cluster_id,
            EngineVersion=target_version,
            ApplyImmediately=True,
            AllowMajorVersionUpgrade=True,
        )

        return {
            "status": "upgrading",
            "cluster_id": cluster_id,
            "strategy": "in_place",
            "from_version": current_version,
            "target_version": target_version,
            "message": f"In-place upgrade initiated: {current_version} → {target_version}. Cluster will restart.",
        }
    except Exception as e:
        logger.error(f"In-place upgrade failed for {cluster_id}: {e}")
        return {"status": "error", "message": str(e)}


def _get_cluster(client, cluster_id: str) -> dict | None:
    try:
        response = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        clusters = response.get("DBClusters", [])
        return clusters[0] if clusters else None
    except Exception:
        return None


def _resolve_target_cluster_parameter_group(client, engine: str, target_version: str) -> str | None:
    """Return the default DB cluster parameter group name for the target
    engine version's parameter-group family (e.g. 'default.aurora-postgresql15').

    RDS rejects a Blue/Green create when the source uses a custom cluster
    parameter group unless a target group is named explicitly. The default
    group for the target family is a safe, always-present choice.
    """
    try:
        kwargs = {"EngineVersion": target_version}
        if engine:
            kwargs["Engine"] = engine
        versions = client.describe_db_engine_versions(**kwargs).get("DBEngineVersions", [])
        if not versions:
            return None
        family = versions[0].get("DBParameterGroupFamily")
        return f"default.{family}" if family else None
    except Exception as e:
        logger.warning(f"Failed to resolve target cluster parameter group: {e}")
        return None


def _update_state(cluster_id: str, status: UpgradeStatus, details: dict):
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        table.put_item(Item={
            "pk": f"UPGRADE#{cluster_id}",
            "sk": datetime.now(timezone.utc).isoformat(),
            "status": status.value,
            "details": json.dumps(details, default=str),
            "ttl": int(datetime.now(timezone.utc).timestamp()) + 86400 * 30,
        })
    except Exception as e:
        logger.warning(f"State update failed: {e}")
