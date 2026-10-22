# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Traffic-pattern persistence, retrieval, and upgrade-strategy scoring.

The traffic generator emits per-cluster workload stats (read/write mix, latency
percentiles, error counts) every run. We persist those alongside authoritative
RDS CloudWatch metrics so the advisor can ground its upgrade-strategy
recommendation in real traffic instead of guessing.

Strategy scoring is deterministic and rule-based (see ``score_strategy``); the
LLM is used only to explain the resulting recommendation, not to choose it.
"""
import json
import logging
from datetime import datetime, timezone, timedelta

from shared.aws_clients import dynamodb_resource, cloudwatch_client
from shared.config import DYNAMODB_TABLE

logger = logging.getLogger(__name__)

TRAFFIC_TTL_DAYS = 14

# Strategy scoring thresholds — tuned for Aurora PostgreSQL major upgrades.
WRITE_RATIO_HIGH = 0.4          # write-heavy: blue/green switchover replication risk
WRITE_TPUT_HIGH_BPS = 5_000_000  # ~5 MB/s sustained writes
CONNECTIONS_HIGH = 50           # many concurrent clients: in-place downtime hurts
TRAFFIC_CV_TROUGH = 0.5         # coefficient of variation implying clear low-traffic windows
ERROR_RATE_HIGH = 0.02          # already-unhealthy cluster: prefer most reversible path
REPLICA_LAG_HIGH_MS = 500


def persist_traffic_stats(stats: dict):
    """Persist one traffic-generation result for a cluster (best-effort)."""
    cluster_id = stats.get("cluster_id")
    if not cluster_id:
        return
    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        ts = stats.get("timestamp") or datetime.now(timezone.utc).isoformat()
        table.put_item(Item={
            "pk": f"TRAFFIC#{cluster_id}",
            "sk": ts,
            # Store as a JSON string so DynamoDB never sees raw float types.
            "data": json.dumps(stats, default=str),
            "ttl": int(datetime.now(timezone.utc).timestamp()) + 86400 * TRAFFIC_TTL_DAYS,
        })
    except Exception as e:
        logger.warning(f"Traffic stats persist failed for {cluster_id}: {e}")


def get_recent_traffic(cluster_id: str, limit: int = 20) -> list:
    """Return recent persisted traffic samples for a cluster, newest first."""
    try:
        from boto3.dynamodb.conditions import Key
        table = dynamodb_resource().Table(DYNAMODB_TABLE)
        resp = table.query(
            KeyConditionExpression=Key("pk").eq(f"TRAFFIC#{cluster_id}"),
            ScanIndexForward=False,
            Limit=limit,
        )
        out = []
        for it in resp.get("Items", []):
            try:
                out.append(json.loads(it["data"]))
            except Exception:
                continue
        return out
    except Exception as e:
        logger.warning(f"Traffic history query failed for {cluster_id}: {e}")
        return []


def get_cloudwatch_traffic_metrics(cluster_id: str, region: str, hours: int = 24) -> dict:
    """Pull authoritative RDS cluster traffic metrics over the recent window."""
    metrics = {}
    wanted = {
        "DatabaseConnections": "Average",
        "WriteThroughput": "Average",
        "ReadThroughput": "Average",
        "WriteIOPS": "Average",
        "CommitLatency": "Average",
        "CPUUtilization": "Average",
        "AuroraReplicaLag": "Maximum",
    }
    try:
        cw = cloudwatch_client(region)
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=hours)
        for metric, stat in wanted.items():
            resp = cw.get_metric_statistics(
                Namespace="AWS/RDS",
                MetricName=metric,
                Dimensions=[{"Name": "DBClusterIdentifier", "Value": cluster_id}],
                StartTime=start,
                EndTime=end,
                Period=3600,
                Statistics=[stat],
            )
            dps = resp.get("Datapoints", [])
            if dps:
                vals = [d[stat] for d in dps]
                metrics[metric] = {
                    "avg": round(sum(vals) / len(vals), 2),
                    "peak": round(max(vals), 2),
                }
    except Exception as e:
        logger.warning(f"CloudWatch metrics fetch failed for {cluster_id}: {e}")
    return metrics


def summarize_traffic(samples: list, cw: dict) -> dict:
    """Aggregate raw generator samples + CloudWatch metrics into a compact profile."""
    summary = {"sample_count": len(samples), "has_cloudwatch": bool(cw)}

    if samples:
        total_reads = sum(s.get("reads", 0) for s in samples)
        total_writes = sum(s.get("writes", 0) for s in samples)
        total = total_reads + total_writes
        total_err = sum(s.get("errors", 0) for s in samples)
        summary["write_ratio"] = round(total_writes / total, 3) if total else None
        summary["error_rate"] = round(total_err / (total + total_err), 4) if (total + total_err) else 0.0
        summary["avg_p99_ms"] = round(sum(s.get("p99_ms", 0) for s in samples) / len(samples), 2)
        summary["peak_p99_ms"] = round(max((s.get("p99_ms", 0) for s in samples), default=0), 2)
        summary["environment"] = samples[0].get("environment")

        # Traffic variability across samples: a high coefficient of variation
        # with a low minimum implies clear low-traffic troughs (maintenance windows).
        loads = [s.get("total_queries", 0) for s in samples]
        if len(loads) >= 3 and sum(loads):
            mean = sum(loads) / len(loads)
            std = (sum((x - mean) ** 2 for x in loads) / len(loads)) ** 0.5
            summary["traffic_cv"] = round(std / mean, 3) if mean else 0.0
            summary["min_load"] = min(loads)
            summary["max_load"] = max(loads)

    if cw:
        if "DatabaseConnections" in cw:
            summary["connections_avg"] = cw["DatabaseConnections"]["avg"]
            summary["connections_peak"] = cw["DatabaseConnections"]["peak"]
        if "WriteThroughput" in cw:
            summary["write_throughput_avg_bps"] = cw["WriteThroughput"]["avg"]
            summary["write_throughput_peak_bps"] = cw["WriteThroughput"]["peak"]
        if "CommitLatency" in cw:
            summary["commit_latency_avg_ms"] = cw["CommitLatency"]["avg"]
        if "AuroraReplicaLag" in cw:
            summary["replica_lag_max_ms"] = cw["AuroraReplicaLag"]["peak"]
        if "CPUUtilization" in cw:
            summary["cpu_peak_pct"] = cw["CPUUtilization"]["peak"]

    return summary


def score_strategy(traffic: dict, cluster_info: dict) -> dict:
    """Deterministically score the three upgrade strategies from traffic signals.

    Returns the winning strategy, a ranked score list, and per-strategy reasons
    so the recommendation is fully auditable. Reads-only inputs; no side effects.
    """
    env = (traffic.get("environment") or cluster_info.get("environment") or "dev").lower()
    write_ratio = traffic.get("write_ratio")
    write_tput_peak = traffic.get("write_throughput_peak_bps")
    conns_peak = traffic.get("connections_peak")
    cv = traffic.get("traffic_cv")
    min_load = traffic.get("min_load")
    max_load = traffic.get("max_load")
    error_rate = traffic.get("error_rate", 0.0)
    replica_lag = traffic.get("replica_lag_max_ms")

    scores = {"blue_green": 0.0, "in_place": 0.0, "backup_restore": 0.0}
    reasons = {"blue_green": [], "in_place": [], "backup_restore": []}

    def add(strategy, pts, reason):
        scores[strategy] += pts
        reasons[strategy].append(reason)

    # 1. Environment — production cannot tolerate in-place downtime.
    if env in ("production", "prod"):
        add("blue_green", 3, "Production workload: blue/green gives near-zero-downtime switchover.")
        add("in_place", -2, "Production: in-place upgrade incurs multi-minute downtime for all clients.")
    elif env in ("staging", "stage"):
        add("blue_green", 1, "Staging: blue/green lets you validate the green cluster before cutover.")
        add("in_place", 1, "Staging can usually absorb a short in-place downtime.")
    else:  # dev / unknown
        add("in_place", 2, "Dev environment: in-place is fastest and cheapest, downtime is acceptable.")
        add("backup_restore", 1, "Dev: backup/restore is a safe, low-cost way to trial the upgrade.")

    # 2. Write intensity — heavy writes create replication lag that can stall B/G switchover.
    heavy_writes = (write_ratio is not None and write_ratio >= WRITE_RATIO_HIGH) or \
                   (write_tput_peak is not None and write_tput_peak >= WRITE_TPUT_HIGH_BPS)
    if heavy_writes:
        add("blue_green", -1, f"Write-heavy (write_ratio={write_ratio}, peak_write_tput={write_tput_peak}): "
                              "green replication may lag; schedule switchover in a low-traffic window.")
        add("in_place", 1, "Write-heavy: in-place avoids the replication-catch-up step entirely.")
    elif write_ratio is not None:
        add("blue_green", 1, f"Read-mostly (write_ratio={write_ratio}): green stays in sync, switchover is low-risk.")

    # 3. Concurrency — many clients amplify in-place downtime impact.
    if conns_peak is not None and conns_peak >= CONNECTIONS_HIGH:
        add("blue_green", 2, f"High concurrency (peak {conns_peak} connections): in-place downtime would hit many clients.")
        add("in_place", -1, f"High concurrency (peak {conns_peak} connections) makes in-place downtime costly.")

    # 4. Traffic troughs — clear low-traffic windows make in-place / backup_restore viable.
    has_trough = cv is not None and cv >= TRAFFIC_CV_TROUGH and \
        (min_load is not None and max_load is not None and max_load > 0 and min_load <= 0.25 * max_load)
    if has_trough:
        add("in_place", 2, f"Traffic is bursty (cv={cv}, min={min_load} vs max={max_load}): a clear low-traffic "
                           "window exists for a short in-place outage.")
        add("backup_restore", 1, "Low-traffic window available for a backup/restore cutover.")
    elif cv is not None:
        add("blue_green", 1, f"Traffic is steady (cv={cv}) with no obvious window: blue/green avoids needing one.")

    # 5. Cluster health — an already-unhealthy cluster favors the most isolated/reversible path.
    unhealthy = (error_rate is not None and error_rate >= ERROR_RATE_HIGH) or \
                (replica_lag is not None and replica_lag >= REPLICA_LAG_HIGH_MS)
    if unhealthy:
        add("backup_restore", 2, f"Elevated errors/replica-lag (err={error_rate}, lag={replica_lag}ms): "
                                 "backup/restore fully isolates the upgrade from the live cluster.")
        add("blue_green", 1, "Instability present: blue/green keeps the original cluster untouched until cutover.")
        add("in_place", -2, "In-place on an unhealthy cluster risks a failed upgrade with no clean fallback.")

    # 6. Major-version jump — larger jumps are riskier in-place.
    src = str(cluster_info.get("engine_version", "")).split(".")[0]
    tgt = str(cluster_info.get("target_version", "")).split(".")[0]
    if src.isdigit() and tgt.isdigit():
        jump = int(tgt) - int(src)
        if jump >= 2:
            add("blue_green", 2, f"Large version jump ({src}→{tgt}): validate on green before cutover.")
            add("backup_restore", 1, f"Large version jump ({src}→{tgt}): backup/restore lets you test without touching prod.")
            add("in_place", -1, f"Large version jump ({src}→{tgt}) is higher-risk in-place.")

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    winner = ranked[0][0]
    # Confidence from the margin between the top two strategies.
    margin = ranked[0][1] - ranked[1][1]
    confidence = "high" if margin >= 3 else "medium" if margin >= 1 else "low"

    return {
        "recommended_strategy": winner,
        "confidence": confidence,
        "scores": {k: round(v, 1) for k, v in scores.items()},
        "ranked": [s for s, _ in ranked],
        "reasons": reasons,
        "signals_used": traffic,
    }
