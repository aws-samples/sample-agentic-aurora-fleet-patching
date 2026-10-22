# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Discovery Agent — Fleet inventory, version drift, and available upgrades.

This agent is read-only. It scans all Aurora PostgreSQL clusters across configured
accounts/regions, identifies their current versions, and determines which clusters
have upgrades available.

PORTABILITY:
- Supports single-account mode (local credentials) and multi-account mode
  (cross-account AssumeRole).
- Regions are configurable via environment or event payload.
- Works identically whether deployed as a standalone Lambda or orchestrated
  via Step Functions.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from shared.aws_clients import rds_client, dynamodb_resource
from shared.config import DYNAMODB_TABLE, DEFAULT_SCAN_REGIONS, ENABLE_ORG_DISCOVERY
from shared.models import ClusterInfo, ClusterType, Environment, UpgradeTarget
from shared.models import EngineType
from shared.multi_account import (
    TargetAccount,
    resolve_target_accounts,
    get_client_for_account,
)

# Statuses that indicate an active (in-progress) Blue/Green deployment
_BG_ACTIVE_STATUSES = ("PROVISIONING", "AVAILABLE", "SWITCHOVER_IN_PROGRESS")

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event, context):
    """Lambda entry point. Scans fleet and returns upgrade candidates.

    Modes:
    1. Multi-account: Scans all configured target accounts (env or Organizations)
    2. Single-account: Scans the deploying account across configured regions
    3. Override: Pass explicit regions/account_id in the event payload

    Event payload options:
        regions: list[str] — override scan regions
        account_id: str — override account (single-account mode only)
        environment: str — filter results by environment classification
        accounts: list[dict] — override target accounts for this invocation
    """
    env_filter = event.get("environment")

    clusters = []

    # Determine scan targets
    override_accounts = event.get("accounts")
    if override_accounts:
        # Explicit accounts passed in event (e.g., from Step Functions)
        target_accounts = [
            TargetAccount(
                account_id=a["account_id"],
                role_arn=a["role_arn"],
                regions=a.get("regions", DEFAULT_SCAN_REGIONS),
                alias=a.get("alias", ""),
                environment=a.get("environment", ""),
            )
            for a in override_accounts
        ]
        clusters = _scan_multiple_accounts(target_accounts)
    else:
        # Check for configured multi-account setup
        target_accounts = resolve_target_accounts()

        if target_accounts:
            # Multi-account mode
            logger.info(f"Multi-account discovery: {len(target_accounts)} target accounts")
            clusters = _scan_multiple_accounts(target_accounts)
        else:
            # Single-account mode (original behavior)
            regions = event.get("regions", DEFAULT_SCAN_REGIONS)
            account_id = _get_account_id(event, context)
            logger.info(f"Single-account discovery: regions={regions}, account={account_id}")
            for region in regions:
                clusters.extend(discover_clusters(region, account_id))

    if env_filter:
        clusters = [c for c in clusters if c.environment.value == env_filter]

    upgrade_targets = []
    for cluster in clusters:
        targets = find_available_upgrades(cluster)
        if targets:
            upgrade_targets.append(targets)

    result = {
        "scan_time": datetime.now(timezone.utc).isoformat(),
        "total_clusters": len(clusters),
        "upgrade_candidates": len(upgrade_targets),
        "accounts_scanned": len(target_accounts) if target_accounts else 1,
        "regions_scanned": _count_unique_regions(clusters),
        "clusters": [_serialize_cluster(c) for c in clusters],
        "upgrade_targets": [_serialize_target(t) for t in upgrade_targets],
    }

    persist_state(result)
    logger.info(
        f"Discovery complete: {len(clusters)} clusters across "
        f"{result['accounts_scanned']} accounts, {len(upgrade_targets)} need upgrades"
    )
    return result


def _get_account_id(event: dict, context) -> str:
    """Extract account ID from event or Lambda context."""
    if event.get("account_id"):
        return event["account_id"]
    try:
        return context.invoked_function_arn.split(":")[4]
    except (AttributeError, IndexError):
        import boto3
        return boto3.client("sts").get_caller_identity()["Account"]


def _scan_multiple_accounts(accounts: list[TargetAccount]) -> list[ClusterInfo]:
    """Scan multiple AWS accounts for Aurora clusters."""
    all_clusters = []
    for account in accounts:
        for region in account.regions:
            try:
                clusters = discover_clusters(
                    region,
                    account.account_id,
                    target_account=account,
                )
                # Apply account-level environment classification if set
                if account.environment:
                    for c in clusters:
                        c.environment = Environment(account.environment)
                all_clusters.extend(clusters)
            except Exception as e:
                logger.error(
                    f"Failed to scan account {account.account_id} "
                    f"({account.alias}) in {region}: {e}"
                )
                # Continue scanning other accounts — don't fail the whole scan
                continue
    return all_clusters


def _count_unique_regions(clusters: list[ClusterInfo]) -> int:
    return len(set(c.region for c in clusters))


def discover_clusters(
    region: str,
    account_id: str,
    target_account: Optional[TargetAccount] = None,
) -> list[ClusterInfo]:
    """Describe all Aurora PostgreSQL clusters in a region.

    Args:
        region: AWS region to scan
        account_id: Account ID (for metadata)
        target_account: If provided, uses cross-account credentials
    """
    if target_account:
        client = get_client_for_account("rds", target_account, region)
    else:
        client = rds_client(region)

    clusters = []
    paginator = client.get_paginator("describe_db_clusters")

    # Get Blue/Green deployment info to classify clusters
    bg_map, bg_old_map = _get_blue_green_cluster_map(client)

    for page in paginator.paginate():
        for db_cluster in page["DBClusters"]:
            if db_cluster["Engine"] not in ("aurora-postgresql", "aurora-mysql"):
                continue

            cluster_id = db_cluster["DBClusterIdentifier"]
            tags = _get_cluster_tags(client, db_cluster["DBClusterArn"])
            env = _classify_environment(cluster_id, tags)

            # Determine cluster type based on Blue/Green deployment state
            # and Global Database membership
            cluster_type = ClusterType.PRIMARY
            source_cluster = None
            if cluster_id in bg_map:
                cluster_type = ClusterType.BLUE_GREEN
                source_cluster = bg_map[cluster_id]
            elif cluster_id in bg_old_map:
                cluster_type = ClusterType.OLD_PRIMARY
                source_cluster = bg_old_map[cluster_id]
            elif "-old" in cluster_id:
                # Naming convention: after B/G switchover, AWS renames the old
                # blue cluster with "-old1"/"-old2" suffix even after deployment
                # metadata is deleted
                import re
                base_id = re.sub(r'-old\d*$', '', cluster_id)
                cluster_type = ClusterType.OLD_PRIMARY
                source_cluster = base_id

            # Detect Global Database secondary (read-only replica in another region)
            global_cluster_id = db_cluster.get("GlobalClusterIdentifier")
            if global_cluster_id and cluster_type == ClusterType.PRIMARY:
                # If it has a GlobalClusterIdentifier but ReplicationSourceIdentifier is set,
                # it's a secondary. The writer cluster won't have ReplicationSourceIdentifier.
                repl_source = db_cluster.get("ReplicationSourceIdentifier", "")
                if repl_source:
                    cluster_type = ClusterType.GLOBAL_SECONDARY
                    source_cluster = repl_source.split(":")[-1] if ":" in repl_source else repl_source

            cluster = ClusterInfo(
                cluster_id=cluster_id,
                engine_version=db_cluster["EngineVersion"],
                region=region,
                environment=env,
                account_id=account_id,
                instance_count=len(db_cluster.get("DBClusterMembers", [])),
                storage_gb=db_cluster.get("AllocatedStorage", 0),
                extensions=_get_extensions(client, db_cluster),
                parameter_group=db_cluster.get("DBClusterParameterGroup", ""),
                tags=tags,
                cluster_type=cluster_type,
                source_cluster=source_cluster,
                engine_type=EngineType.MYSQL if db_cluster["Engine"] == "aurora-mysql" else EngineType.POSTGRESQL,
            )
            clusters.append(cluster)

    return clusters


def _get_blue_green_cluster_map(client) -> tuple[dict[str, str], dict[str, str]]:
    """Get mappings of cluster IDs to their Blue/Green roles.

    Returns:
        (active_green_map, old_primary_map)
        active_green_map: {green_cluster_id: source_cluster_id} — active B/G deployments
        old_primary_map: {old_source_cluster_id: new_cluster_id} — post-switchover retired clusters
    """
    active_green = {}
    old_primary = {}
    try:
        response = client.describe_blue_green_deployments()
        for deployment in response.get("BlueGreenDeployments", []):
            status = deployment.get("Status", "")
            source_arn = deployment.get("Source", "")
            target_arn = deployment.get("Target", "")

            if ":cluster:" not in source_arn or ":cluster:" not in target_arn:
                continue

            source_id = source_arn.split(":cluster:")[-1]
            target_id = target_arn.split(":cluster:")[-1]

            if status in _BG_ACTIVE_STATUSES:
                active_green[target_id] = source_id
            elif status == "SWITCHOVER_COMPLETED":
                old_primary[source_id] = target_id
    except Exception as e:
        logger.warning(f"Could not fetch Blue/Green deployments: {e}")
    return active_green, old_primary


def find_available_upgrades(cluster: ClusterInfo) -> UpgradeTarget | None:
    """Check what upgrade versions are available for a cluster."""
    client = rds_client(cluster.region)

    try:
        response = client.describe_db_engine_versions(
            Engine=cluster.engine_type.value,
            EngineVersion=cluster.engine_version,
        )
    except client.exceptions.ClientError as e:
        logger.error(f"Failed to get versions for {cluster.cluster_id}: {e}")
        return None

    if not response["DBEngineVersions"]:
        return None

    current = response["DBEngineVersions"][0]
    upgrade_targets = current.get("ValidUpgradeTarget", [])

    if not upgrade_targets:
        return None

    available = [t["EngineVersion"] for t in upgrade_targets]
    # Prefer latest minor version in the same major
    current_major = cluster.engine_version.rsplit(".", 1)[0]
    minor_upgrades = [v for v in available if v.startswith(current_major)]
    target = minor_upgrades[-1] if minor_upgrades else available[-1]

    return UpgradeTarget(
        cluster=cluster,
        target_version=target,
        available_versions=available,
    )


def _get_cluster_tags(client, cluster_arn: str) -> dict[str, str]:
    """Fetch tags for a cluster."""
    try:
        response = client.list_tags_for_resource(ResourceName=cluster_arn)
        return {tag["Key"]: tag["Value"] for tag in response.get("TagList", [])}
    except Exception:
        return {}


def _classify_environment(cluster_id: str, tags: dict) -> Environment:
    """Determine environment from tags or naming convention."""
    env_tag = (tags.get("Environment") or tags.get("environment") or tags.get("env") or "").lower()
    if env_tag:
        if env_tag in ("prod", "production"):
            return Environment.PRODUCTION
        elif env_tag in ("staging", "stage", "stg"):
            return Environment.STAGING
        return Environment.DEV

    cluster_lower = cluster_id.lower()
    if any(p in cluster_lower for p in ("prod", "prd")):
        return Environment.PRODUCTION
    elif any(p in cluster_lower for p in ("staging", "stg", "stage")):
        return Environment.STAGING
    return Environment.DEV


def _get_extensions(client, db_cluster: dict) -> list[str]:
    """Get installed extensions by querying the parameter group for shared_preload_libraries."""
    pg_name = db_cluster.get("DBClusterParameterGroup", "")
    if not pg_name:
        return []

    try:
        response = client.describe_db_cluster_parameters(
            DBClusterParameterGroupName=pg_name,
            Filters=[{"Name": "parameter-name", "Values": ["shared_preload_libraries"]}],
        )
        params = response.get("Parameters", [])
        if params and params[0].get("ParameterValue"):
            return [ext.strip() for ext in params[0]["ParameterValue"].split(",")]
    except Exception:
        pass
    return []


def persist_state(result: dict):
    """Write discovery results to DynamoDB for downstream agents."""
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        table.put_item(Item={
            "pk": "DISCOVERY",
            "sk": result["scan_time"],
            "total_clusters": result["total_clusters"],
            "upgrade_candidates": result["upgrade_candidates"],
            "data": json.dumps(result, default=str),
            "ttl": int(datetime.now(timezone.utc).timestamp()) + 86400 * 7,
        })
    except Exception as e:
        logger.warning(f"Failed to persist state: {e}")


def _serialize_cluster(c: ClusterInfo) -> dict:
    return {
        "cluster_id": c.cluster_id,
        "engine_version": c.engine_version,
        "engine_type": c.engine_type.value,
        "region": c.region,
        "environment": c.environment.value,
        "account_id": c.account_id,
        "instance_count": c.instance_count,
        "storage_gb": c.storage_gb,
        "extensions": c.extensions,
        "parameter_group": c.parameter_group,
        "cluster_type": c.cluster_type.value,
        "source_cluster": c.source_cluster,
    }


def _serialize_target(t: UpgradeTarget) -> dict:
    return {
        "cluster_id": t.cluster.cluster_id,
        "current_version": t.cluster.engine_version,
        "target_version": t.target_version,
        "available_versions": t.available_versions,
        "environment": t.cluster.environment.value,
        "engine_type": t.cluster.engine_type.value,
        "region": t.cluster.region,
    }
