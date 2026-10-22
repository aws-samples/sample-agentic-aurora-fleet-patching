# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Dependency Awareness Module — Cross-cluster relationship discovery and coordination.

Discovers relationships between Aurora PostgreSQL clusters so the planner
avoids upgrading tightly-coupled clusters simultaneously. Relationships are
discovered through multiple signals:

1. Explicit tags (depends-on, service-group, dependency-tier)
2. Naming conventions (shared prefixes, -replica/-ro suffixes)
3. Shared parameter groups (same custom PG = same application)
4. Security group cross-references (SG-A allows inbound from SG-B = B connects to A)

The module produces ordering constraints that the AI planner must respect.
"""
import logging
import re
from collections import defaultdict

from shared.aws_clients import ec2_client

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def discover_dependencies(clusters: list[dict], region: str) -> dict:
    """Discover cross-cluster dependency relationships.

    Args:
        clusters: List of cluster dicts from the scanner. Expected keys:
            cluster_id, tags (dict), parameter_group, vpc_security_groups,
            environment, endpoint (optional).
        region: AWS region to query for security group details.

    Returns:
        Dependency graph keyed by cluster_id:
        {
            cluster_id: {
                "depends_on": [cluster_ids this cluster reads from],
                "depended_by": [cluster_ids that read from this cluster],
                "group": "logical service group name or None",
                "coordination_required": bool,
            }
        }
    """
    # Initialize graph
    graph = {
        c["cluster_id"]: {
            "depends_on": [],
            "depended_by": [],
            "group": None,
            "coordination_required": False,
        }
        for c in clusters
    }

    # Run all discovery methods and merge results
    _merge_dependencies(graph, _discover_from_tags(clusters))
    _merge_dependencies(graph, _discover_from_naming(clusters))
    _merge_dependencies(graph, _discover_from_parameter_groups(clusters))

    try:
        sg_deps = _discover_from_security_groups(clusters, region)
        _merge_dependencies(graph, sg_deps)
    except Exception as e:
        logger.warning(f"Security group discovery failed (non-fatal): {e}")

    # Mark coordination_required for any cluster with dependencies
    for cluster_id, info in graph.items():
        if info["depends_on"] or info["depended_by"] or info["group"]:
            info["coordination_required"] = True

    return graph


# ---------------------------------------------------------------------------
# Discovery methods
# ---------------------------------------------------------------------------

def _discover_from_tags(clusters: list[dict]) -> dict:
    """Check tags for explicit dependency declarations.

    Recognized tags:
        - depends-on: comma-separated list of cluster IDs this cluster reads from
        - service-group: logical grouping (e.g., "payments-service")
        - dependency-tier: "data-source" | "read-replica-consumer" | "independent"
    """
    partial = {}

    for c in clusters:
        cluster_id = c["cluster_id"]
        tags = c.get("tags", {})
        info = {"depends_on": [], "depended_by": [], "group": None}

        # Explicit depends-on tag
        depends_on_raw = tags.get("depends-on", "")
        if depends_on_raw:
            deps = [d.strip() for d in depends_on_raw.split(",") if d.strip()]
            info["depends_on"] = deps

        # Service group tag
        service_group = tags.get("service-group", "")
        if service_group:
            info["group"] = service_group

        # dependency-tier informs direction but group assignment is primary signal
        partial[cluster_id] = info

    # Build reverse edges (depended_by) from depends_on
    all_cluster_ids = {c["cluster_id"] for c in clusters}
    for cluster_id, info in partial.items():
        for dep in info["depends_on"]:
            if dep in partial:
                partial[dep]["depended_by"].append(cluster_id)

    return partial


def _discover_from_naming(clusters: list[dict]) -> dict:
    """Infer relationships from naming conventions.

    Patterns detected:
        - Same prefix before environment suffix (payments-prod, payments-staging)
        - "-replica" or "-ro" suffixes indicate read-replica consumers
        - Reader endpoints suggest dependency on writer cluster
    """
    partial = {}
    env_suffixes = re.compile(
        r"[-_](prod|production|staging|stg|dev|development|test|qa|uat|canary|beta|alpha)$",
        re.IGNORECASE,
    )
    replica_suffixes = re.compile(r"[-_](replica|ro|reader|read|secondary)$", re.IGNORECASE)

    # Extract base names (strip environment suffix)
    base_names = {}  # base_name -> [cluster_ids]
    for c in clusters:
        cluster_id = c["cluster_id"]
        # Strip replica suffixes first, then env suffixes
        name = replica_suffixes.sub("", cluster_id)
        name = env_suffixes.sub("", name)
        base_names.setdefault(name, []).append(cluster_id)

    # Clusters with same base name are in the same service group
    for base, members in base_names.items():
        if len(members) > 1:
            for cluster_id in members:
                if cluster_id not in partial:
                    partial[cluster_id] = {"depends_on": [], "depended_by": [], "group": None}
                # Assign group based on base name
                partial[cluster_id]["group"] = base

    # Detect replica/ro suffixes -> depends on the non-replica variant
    cluster_ids_set = {c["cluster_id"] for c in clusters}
    for c in clusters:
        cluster_id = c["cluster_id"]
        if replica_suffixes.search(cluster_id):
            # Find the writer (same name without the suffix)
            writer_name = replica_suffixes.sub("", cluster_id)
            # Check if writer exists directly or with env suffix
            for potential_writer in cluster_ids_set:
                if potential_writer == writer_name or potential_writer.startswith(writer_name + "-"):
                    if potential_writer != cluster_id:
                        if cluster_id not in partial:
                            partial[cluster_id] = {"depends_on": [], "depended_by": [], "group": None}
                        if potential_writer not in partial:
                            partial[potential_writer] = {"depends_on": [], "depended_by": [], "group": None}
                        if potential_writer not in partial[cluster_id]["depends_on"]:
                            partial[cluster_id]["depends_on"].append(potential_writer)
                        if cluster_id not in partial[potential_writer]["depended_by"]:
                            partial[potential_writer]["depended_by"].append(cluster_id)
                        break

    return partial


def _discover_from_parameter_groups(clusters: list[dict]) -> dict:
    """Clusters sharing the same custom parameter group likely serve the same application.

    Only considers non-default parameter groups (custom ones indicate intentional
    configuration for a specific workload).
    """
    partial = {}

    # Group clusters by their parameter group
    pg_groups = defaultdict(list)  # param_group_name -> [cluster_ids]
    for c in clusters:
        pg = c.get("parameter_group", "")
        # Skip default parameter groups
        if pg and not pg.startswith("default."):
            pg_groups[pg].append(c["cluster_id"])

    # Clusters sharing a custom PG are in the same logical group
    for pg_name, members in pg_groups.items():
        if len(members) > 1:
            group_name = f"pg:{pg_name}"
            for cluster_id in members:
                if cluster_id not in partial:
                    partial[cluster_id] = {"depends_on": [], "depended_by": [], "group": None}
                # Only set group if not already set by a more explicit source
                if partial[cluster_id]["group"] is None:
                    partial[cluster_id]["group"] = group_name

    return partial


def _discover_from_security_groups(clusters: list[dict], region: str) -> dict:
    """Use security group cross-references to discover connectivity relationships.

    If cluster-A's security group allows inbound from cluster-B's security group,
    then cluster-B likely connects to cluster-A (B depends on A as a data source).
    """
    partial = {}

    # Build map: security_group_id -> [cluster_ids]
    sg_to_clusters = defaultdict(list)
    cluster_to_sgs = defaultdict(list)

    for c in clusters:
        cluster_id = c["cluster_id"]
        vpc_sgs = c.get("vpc_security_groups", [])
        for sg in vpc_sgs:
            sg_id = sg if isinstance(sg, str) else sg.get("VpcSecurityGroupId", sg.get("sg_id", ""))
            if sg_id:
                sg_to_clusters[sg_id].append(cluster_id)
                cluster_to_sgs[cluster_id].append(sg_id)

    if not sg_to_clusters:
        return partial

    # Fetch security group details from EC2
    all_sg_ids = list(sg_to_clusters.keys())
    ec2 = ec2_client(region=region)

    # Describe in batches of 200 (API limit)
    sg_details = {}
    for i in range(0, len(all_sg_ids), 200):
        batch = all_sg_ids[i:i + 200]
        try:
            response = ec2.describe_security_groups(GroupIds=batch)
            for sg in response.get("SecurityGroups", []):
                sg_details[sg["GroupId"]] = sg
        except Exception as e:
            logger.warning(f"Failed to describe security groups batch: {e}")

    # Check ingress rules for cross-references
    # If SG-A allows inbound from SG-B, and SG-A belongs to cluster-A,
    # and SG-B belongs to cluster-B, then B connects to A (B depends_on A)
    for sg_id, sg_info in sg_details.items():
        target_clusters = sg_to_clusters.get(sg_id, [])
        if not target_clusters:
            continue

        for rule in sg_info.get("IpPermissions", []):
            for user_id_group in rule.get("UserIdGroupPairs", []):
                source_sg_id = user_id_group.get("GroupId", "")
                source_clusters = sg_to_clusters.get(source_sg_id, [])

                # Source SG belongs to a cluster and references target cluster's SG
                for source_cluster in source_clusters:
                    for target_cluster in target_clusters:
                        if source_cluster == target_cluster:
                            continue

                        # source_cluster's SG is allowed into target_cluster's SG
                        # This means source_cluster connects to target_cluster
                        # => source_cluster depends_on target_cluster
                        if source_cluster not in partial:
                            partial[source_cluster] = {"depends_on": [], "depended_by": [], "group": None}
                        if target_cluster not in partial:
                            partial[target_cluster] = {"depends_on": [], "depended_by": [], "group": None}

                        if target_cluster not in partial[source_cluster]["depends_on"]:
                            partial[source_cluster]["depends_on"].append(target_cluster)
                        if source_cluster not in partial[target_cluster]["depended_by"]:
                            partial[target_cluster]["depended_by"].append(source_cluster)

    return partial


# ---------------------------------------------------------------------------
# Constraint generation
# ---------------------------------------------------------------------------

def get_coordination_constraints(dependency_graph: dict) -> list[dict]:
    """Derive upgrade ordering constraints from the dependency graph.

    Returns a list of constraints the planner must respect:
        - must_before: cluster A must be upgraded before cluster B
        - not_simultaneous: clusters must not be in the same wave
        - upgrade_together: clusters should be upgraded in close succession

    Constraint types:
        1. Staging validates before prod (same service group, different envs)
        2. Same service group + shared parameter group = not simultaneous
        3. Explicit depends-on = upgrade data source after consumer, or together
    """
    constraints = []
    seen_pairs = set()

    # Collect all groups and their members
    groups = defaultdict(list)  # group_name -> [cluster_ids]
    for cluster_id, info in dependency_graph.items():
        if info.get("group"):
            groups[info["group"]].append(cluster_id)

    # --- Constraint Type 1: staging before prod within same group ---
    env_patterns = {
        "staging": re.compile(r"(staging|stg)", re.IGNORECASE),
        "dev": re.compile(r"(dev|development)", re.IGNORECASE),
        "prod": re.compile(r"(prod|production)", re.IGNORECASE),
    }

    for group_name, members in groups.items():
        staging_clusters = [m for m in members if env_patterns["staging"].search(m)]
        dev_clusters = [m for m in members if env_patterns["dev"].search(m)]
        prod_clusters = [m for m in members if env_patterns["prod"].search(m)]

        # Dev before staging
        for dev in dev_clusters:
            for stg in staging_clusters:
                pair_key = (dev, stg)
                if pair_key not in seen_pairs:
                    constraints.append({
                        "type": "must_before",
                        "cluster": dev,
                        "before": stg,
                        "reason": f"dev validates before staging (group: {group_name})",
                    })
                    seen_pairs.add(pair_key)

        # Staging before prod
        for stg in staging_clusters:
            for prod in prod_clusters:
                pair_key = (stg, prod)
                if pair_key not in seen_pairs:
                    constraints.append({
                        "type": "must_before",
                        "cluster": stg,
                        "before": prod,
                        "reason": f"staging validates before prod (group: {group_name})",
                    })
                    seen_pairs.add(pair_key)

    # --- Constraint Type 2: not_simultaneous for same group ---
    for group_name, members in groups.items():
        if len(members) < 2:
            continue
        # Production clusters in same group should not upgrade simultaneously
        prod_in_group = [m for m in members if env_patterns["prod"].search(m)]
        if len(prod_in_group) >= 2:
            group_key = tuple(sorted(prod_in_group))
            if group_key not in seen_pairs:
                constraints.append({
                    "type": "not_simultaneous",
                    "clusters": prod_in_group,
                    "reason": f"same service group ({group_name}), shared risk profile",
                })
                seen_pairs.add(group_key)

    # --- Constraint Type 3: explicit depends-on relationships ---
    for cluster_id, info in dependency_graph.items():
        for dep in info.get("depends_on", []):
            pair_key = (cluster_id, dep, "depends")
            if pair_key not in seen_pairs:
                # If both are production, upgrade together in close succession
                both_prod = (
                    env_patterns["prod"].search(cluster_id)
                    and env_patterns["prod"].search(dep)
                )
                if both_prod:
                    constraints.append({
                        "type": "upgrade_together",
                        "clusters": [cluster_id, dep],
                        "reason": f"explicit depends-on relationship (both production)",
                    })
                else:
                    # Data source should be upgraded after its consumers are validated
                    constraints.append({
                        "type": "must_before",
                        "cluster": cluster_id,
                        "before": dep,
                        "reason": f"consumer ({cluster_id}) should validate before data source ({dep})",
                    })
                seen_pairs.add(pair_key)

    return constraints


# ---------------------------------------------------------------------------
# Plan validation
# ---------------------------------------------------------------------------

def validate_plan_against_dependencies(
    plan_waves: list[dict],
    constraints: list[dict],
) -> list[dict]:
    """Check a generated plan for dependency constraint violations.

    Args:
        plan_waves: List of wave dicts, each containing:
            wave_id (int), clusters (list of dicts with cluster_id)
        constraints: List of constraint dicts from get_coordination_constraints.

    Returns:
        List of violations found. Empty list means plan is valid.
        Each violation: {
            "violation": description,
            "wave_id": which wave has the problem,
            "clusters_affected": [cluster_ids involved],
            "fix": suggested remediation,
        }
    """
    violations = []

    # Build lookup: cluster_id -> wave_id
    cluster_wave_map = {}
    for wave in plan_waves:
        wave_id = wave.get("wave_id")
        for cluster_info in wave.get("clusters", []):
            cid = cluster_info if isinstance(cluster_info, str) else cluster_info.get("cluster_id", "")
            if cid:
                cluster_wave_map[cid] = wave_id

    # Build lookup: wave_id -> set of cluster_ids
    wave_clusters = defaultdict(set)
    for cid, wid in cluster_wave_map.items():
        wave_clusters[wid].add(cid)

    for constraint in constraints:
        ctype = constraint.get("type")

        if ctype == "must_before":
            first_cluster = constraint["cluster"]
            second_cluster = constraint["before"]

            first_wave = cluster_wave_map.get(first_cluster)
            second_wave = cluster_wave_map.get(second_cluster)

            # Only validate if both clusters are in the plan
            if first_wave is None or second_wave is None:
                continue

            if first_wave >= second_wave:
                violations.append({
                    "violation": (
                        f"'{first_cluster}' must be upgraded before '{second_cluster}' "
                        f"but is scheduled in wave {first_wave} (same or later than wave {second_wave})"
                    ),
                    "wave_id": second_wave,
                    "clusters_affected": [first_cluster, second_cluster],
                    "fix": f"move '{first_cluster}' to wave {second_wave - 1} or earlier, "
                           f"or move '{second_cluster}' to wave {first_wave + 1} or later",
                })

        elif ctype == "not_simultaneous":
            affected_clusters = constraint["clusters"]
            # Check if any two are in the same wave
            waves_used = {}
            for cid in affected_clusters:
                wid = cluster_wave_map.get(cid)
                if wid is not None:
                    waves_used.setdefault(wid, []).append(cid)

            for wid, cids in waves_used.items():
                if len(cids) > 1:
                    violations.append({
                        "violation": (
                            f"Clusters {cids} must not be upgraded simultaneously "
                            f"but are both in wave {wid}: {constraint['reason']}"
                        ),
                        "wave_id": wid,
                        "clusters_affected": cids,
                        "fix": f"spread clusters across different waves "
                               f"(move {cids[1:]} to wave {wid + 1} or later)",
                    })

        elif ctype == "upgrade_together":
            affected_clusters = constraint["clusters"]
            waves_for_group = set()
            present_clusters = []
            for cid in affected_clusters:
                wid = cluster_wave_map.get(cid)
                if wid is not None:
                    waves_for_group.add(wid)
                    present_clusters.append(cid)

            # "Together" means within 1 wave of each other
            if len(waves_for_group) > 1:
                min_wave = min(waves_for_group)
                max_wave = max(waves_for_group)
                if max_wave - min_wave > 1:
                    violations.append({
                        "violation": (
                            f"Clusters {present_clusters} should be upgraded together "
                            f"but are spread across waves {min_wave}-{max_wave}: "
                            f"{constraint['reason']}"
                        ),
                        "wave_id": max_wave,
                        "clusters_affected": present_clusters,
                        "fix": f"move all clusters to adjacent waves "
                               f"(within 1 wave of each other, target wave {min_wave} or {min_wave + 1})",
                    })

    return violations


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _merge_dependencies(graph: dict, partial: dict) -> None:
    """Merge partial dependency info into the main graph (in-place).

    Later sources do not overwrite group assignments from earlier sources
    unless the earlier source left group as None.
    """
    for cluster_id, info in partial.items():
        if cluster_id not in graph:
            continue  # Skip clusters not in our fleet

        entry = graph[cluster_id]

        # Merge depends_on (deduplicate)
        for dep in info.get("depends_on", []):
            if dep not in entry["depends_on"]:
                entry["depends_on"].append(dep)

        # Merge depended_by (deduplicate)
        for dep in info.get("depended_by", []):
            if dep not in entry["depended_by"]:
                entry["depended_by"].append(dep)

        # Set group only if not already set (first source wins)
        if info.get("group") and entry["group"] is None:
            entry["group"] = info["group"]
