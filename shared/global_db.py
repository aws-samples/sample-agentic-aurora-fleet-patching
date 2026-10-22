# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Global Database upgrade awareness.

Handles the special requirements for upgrading Aurora Global Database clusters:

POSTGRESQL GLOBAL DATABASE UPGRADE RULES:
─────────────────────────────────────────
Minor version upgrade:
  1. Upgrade the PRIMARY cluster — secondaries upgrade automatically
  2. No need to detach secondaries for minor patches
  3. Brief replication lag during upgrade (seconds)

Major version upgrade:
  1. Disable RPO (Recovery Point Objective) if enabled
  2. Detach all secondary regions (removes from Global Cluster)
  3. Upgrade the primary cluster (Blue/Green or in-place)
  4. Re-add secondary regions (creates fresh replicas at new version)
  5. Re-enable RPO if needed
  Note: Cannot use Blue/Green on the Global Cluster directly — must detach first

MYSQL GLOBAL DATABASE UPGRADE RULES:
─────────────────────────────────────
Minor version upgrade:
  1. Upgrade all SECONDARY clusters FIRST (opposite of PostgreSQL!)
  2. Then upgrade the PRIMARY cluster
  3. Version mismatch is tolerated during the rolling upgrade window

Major version upgrade (v2 → v3 / MySQL 5.7 → 8.0):
  1. Delete secondary clusters (cannot in-place upgrade across major versions)
  2. Upgrade the primary cluster
  3. Re-create secondary clusters at the new version
  Note: For some v2 minor versions, must delete secondaries even for minor upgrades

COMMON RULES:
─────────────
- NEVER directly upgrade a PostgreSQL secondary (it follows the primary)
- NEVER upgrade a MySQL primary before its secondaries (for minor upgrades)
- Global Clusters don't support Blue/Green on the global cluster itself
- Custom parameter groups must be created in EACH region separately
"""
import logging
from typing import Optional

from shared.aws_clients import rds_client
from shared.models import ClusterType, EngineType

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Global Database Detection
# ─────────────────────────────────────────────────────────────────────────────


def get_global_cluster_info(cluster_id: str, region: str) -> Optional[dict]:
    """Get Global Database info for a cluster, if it's part of one.

    Returns None if the cluster is standalone (not part of a Global DB).
    Returns dict with global cluster details if it is.
    """
    client = rds_client(region)
    try:
        resp = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        if not resp["DBClusters"]:
            return None
        cluster = resp["DBClusters"][0]
        global_id = cluster.get("GlobalClusterIdentifier")
        if not global_id:
            return None

        # Get full global cluster details
        global_resp = client.describe_global_clusters(
            GlobalClusterIdentifier=global_id
        )
        if not global_resp.get("GlobalClusters"):
            return None

        global_cluster = global_resp["GlobalClusters"][0]
        members = global_cluster.get("GlobalClusterMembers", [])

        # Determine role of this cluster
        is_writer = False
        for member in members:
            member_arn = member.get("DBClusterArn", "")
            if cluster_id in member_arn and member.get("IsWriter"):
                is_writer = True
                break

        return {
            "global_cluster_id": global_id,
            "engine": global_cluster.get("Engine", ""),
            "engine_version": global_cluster.get("EngineVersion", ""),
            "status": global_cluster.get("Status", ""),
            "is_writer": is_writer,
            "members": [
                {
                    "arn": m.get("DBClusterArn", ""),
                    "is_writer": m.get("IsWriter", False),
                    "region": m.get("DBClusterArn", "").split(":")[3] if ":" in m.get("DBClusterArn", "") else "",
                    "cluster_id": m.get("DBClusterArn", "").split(":")[-1] if ":" in m.get("DBClusterArn", "") else "",
                }
                for m in members
            ],
            "total_regions": len(members),
        }
    except Exception as e:
        logger.warning(f"Failed to get global cluster info for {cluster_id}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Upgrade Eligibility Check
# ─────────────────────────────────────────────────────────────────────────────


def check_upgrade_eligibility(
    cluster_id: str,
    region: str,
    target_version: str,
    engine_type: str = "aurora-postgresql",
) -> dict:
    """Check if a cluster can be upgraded, considering Global Database constraints.

    Returns:
        {
            "eligible": bool,
            "reason": str,
            "strategy": str,  # "upgrade_primary", "upgrade_secondary_first", "detach_and_upgrade", "blocked"
            "steps": list[str],  # Ordered steps for the upgrade
            "warnings": list[str],
        }
    """
    global_info = get_global_cluster_info(cluster_id, region)

    # Standalone cluster — always eligible
    if not global_info:
        return {
            "eligible": True,
            "reason": "Standalone cluster — standard upgrade applies",
            "global_database": False,
            "strategy": "standard",
            "steps": [],
            "warnings": [],
        }

    is_writer = global_info["is_writer"]
    is_major = _is_major_version_jump(global_info.get("engine_version", ""), target_version, engine_type)
    secondaries = [m for m in global_info["members"] if not m["is_writer"]]

    result = {
        "eligible": False,
        "global_database": True,
        "global_cluster_id": global_info["global_cluster_id"],
        "total_regions": global_info["total_regions"],
        "is_writer": is_writer,
        "is_major_upgrade": is_major,
        "secondaries": secondaries,
        "reason": "",
        "strategy": "",
        "steps": [],
        "warnings": [],
    }

    # ─── PostgreSQL Rules ─────────────────────────────────────────────────
    if engine_type == "aurora-postgresql":
        if not is_writer:
            # Secondary — CANNOT be upgraded directly
            result["eligible"] = False
            result["reason"] = (
                "This is a Global Database secondary (read replica). "
                "PostgreSQL secondaries upgrade automatically when the primary is upgraded. "
                "Upgrade the primary cluster instead."
            )
            result["strategy"] = "blocked"
            result["steps"] = [
                f"Upgrade the primary cluster instead: {_find_writer(global_info)}",
            ]
            return result

        if is_major:
            # Major upgrade — must detach secondaries first
            result["eligible"] = True
            result["reason"] = (
                "Major version upgrade on Global Database primary. "
                "Secondaries must be detached before the primary can be upgraded, "
                "then re-added at the new version."
            )
            result["strategy"] = "detach_and_upgrade"
            result["steps"] = [
                "Disable RPO on the Global Cluster (if enabled)",
                *[f"Detach secondary: {s['cluster_id']} ({s['region']})" for s in secondaries],
                f"Upgrade primary cluster {cluster_id} to {target_version}",
                "Wait for primary upgrade to complete",
                *[f"Re-add secondary in {s['region']} (creates fresh replica at new version)" for s in secondaries],
                "Re-enable RPO if needed",
            ]
            result["warnings"] = [
                "Secondaries will be unavailable during the upgrade window",
                "Re-adding secondaries requires full data sync (may take hours for large DBs)",
                "Custom parameter groups must be created in each region separately",
                "Cannot use Blue/Green on the Global Cluster directly",
            ]
        else:
            # Minor upgrade — just upgrade primary, secondaries follow
            result["eligible"] = True
            result["reason"] = (
                "Minor version upgrade on Global Database primary. "
                "Secondaries will upgrade automatically after the primary."
            )
            result["strategy"] = "upgrade_primary"
            result["steps"] = [
                f"Upgrade primary cluster {cluster_id} to {target_version}",
                "Secondaries upgrade automatically (brief replication lag expected)",
            ]
            result["warnings"] = [
                "Brief replication lag in secondary regions during upgrade",
            ]

    # ─── MySQL Rules ──────────────────────────────────────────────────────
    elif engine_type == "aurora-mysql":
        if is_major:
            if not is_writer:
                result["eligible"] = False
                result["reason"] = (
                    "Major version upgrade requires deleting secondaries and upgrading the primary. "
                    "This secondary will be re-created after the primary is upgraded."
                )
                result["strategy"] = "blocked"
                result["steps"] = [
                    f"Delete this secondary cluster first",
                    f"Then upgrade the primary: {_find_writer(global_info)}",
                    f"Re-create secondary after primary upgrade completes",
                ]
                return result

            # Primary, major upgrade
            result["eligible"] = True
            result["reason"] = (
                "Major version upgrade on MySQL Global Database. "
                "All secondary clusters must be deleted before upgrading the primary."
            )
            result["strategy"] = "detach_and_upgrade"
            result["steps"] = [
                *[f"Delete secondary cluster: {s['cluster_id']} ({s['region']})" for s in secondaries],
                f"Upgrade primary cluster {cluster_id} to {target_version}",
                "Wait for primary upgrade to complete",
                *[f"Re-create secondary in {s['region']} at new version" for s in secondaries],
            ]
            result["warnings"] = [
                "All secondary regions lose read capacity during upgrade",
                "Re-creating secondaries requires full data sync",
            ]
        else:
            # Minor upgrade — MySQL requires upgrading secondaries FIRST
            if not is_writer:
                result["eligible"] = True
                result["reason"] = (
                    "MySQL Global Database minor upgrade — secondaries must be upgraded before the primary. "
                    "This secondary is eligible for upgrade."
                )
                result["strategy"] = "upgrade_secondary_first"
                result["steps"] = [
                    f"Upgrade this secondary cluster to {target_version}",
                    f"After ALL secondaries are upgraded, upgrade the primary: {_find_writer(global_info)}",
                ]
                result["warnings"] = [
                    "Do NOT upgrade the primary until all secondaries are at the new version",
                ]
            else:
                # Primary — check if all secondaries are already at target version
                result["eligible"] = True
                result["reason"] = (
                    "MySQL Global Database minor upgrade — verify all secondaries are already upgraded "
                    "before upgrading the primary."
                )
                result["strategy"] = "upgrade_primary"
                result["steps"] = [
                    "Verify all secondaries are at target version (upgrade them first if not)",
                    *[f"Check secondary: {s['cluster_id']} ({s['region']})" for s in secondaries],
                    f"Upgrade primary cluster {cluster_id} to {target_version}",
                ]
                result["warnings"] = [
                    "Upgrading the primary before secondaries may cause replication errors",
                    "Ensure all secondaries are at the target version first",
                ]

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _is_major_version_jump(current: str, target: str, engine_type: str) -> bool:
    """Determine if this is a major version upgrade."""
    if engine_type == "aurora-postgresql":
        current_major = current.split(".")[0]
        target_major = target.split(".")[0]
        return current_major != target_major
    elif engine_type == "aurora-mysql":
        from agents.scanner.handler import _get_mysql_major_version
        current_major = _get_mysql_major_version(current)
        target_major = _get_mysql_major_version(target)
        return current_major != target_major
    return False


def _find_writer(global_info: dict) -> str:
    """Find the writer cluster ID in a Global Database."""
    for member in global_info.get("members", []):
        if member.get("is_writer"):
            return member.get("cluster_id", "unknown")
    return "unknown"
