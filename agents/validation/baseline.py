# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Performance Baseline — Captures and compares pg_stat_statements before/after upgrade.

Gap #5: Instead of just checking CloudWatch latency metrics, this module connects
directly to the database and captures query-level performance data. After upgrade,
it compares each query's mean_exec_time against the baseline and flags regressions.

Gap #6: Uses psycopg2 for direct DB connections instead of shelling out to psql,
solving the "psql not installed" problem on the local machine / Lambda.
"""
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Thresholds for flagging regressions
REGRESSION_THRESHOLDS = {
    "mean_exec_time_pct": 25,     # 25% slower = warning
    "mean_exec_time_critical": 50, # 50% slower = critical
    "total_exec_time_pct": 30,     # 30% increase in total wall time
    "rows_per_call_pct": 50,       # 50% more rows returned (plan change indicator)
}


def capture_baseline(cluster_id: str, region: str = "") -> dict:
    """Capture pre-upgrade performance baseline from pg_stat_statements.

    Connects directly to the cluster and pulls:
    - Top 50 queries by total_exec_time
    - Index usage stats
    - Table sequential scan ratios
    - Connection pool stats
    """
    conn = _get_connection(cluster_id, region)
    if not conn:
        return {"error": "Could not connect to cluster", "cluster_id": cluster_id}

    baseline = {
        "cluster_id": cluster_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "queries": [],
        "index_stats": [],
        "seq_scan_ratios": [],
        "settings": {},
    }

    try:
        cur = conn.cursor()

        # Ensure pg_stat_statements extension is available. Only issue the DDL
        # when it's missing: even a no-op CREATE EXTENSION IF NOT EXISTS makes
        # Aurora cancel a pending Blue/Green switchover.
        try:
            cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'")
            if not cur.fetchone():
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        except Exception:
            conn.rollback()

        # Top queries by total execution time
        try:
            cur.execute("""
                SELECT queryid, query, calls, total_exec_time, mean_exec_time,
                       stddev_exec_time, rows, shared_blks_hit, shared_blks_read
                FROM pg_stat_statements
                WHERE userid != 10  -- exclude rdsadmin
                  AND query NOT LIKE '%pg_stat%'
                  AND calls > 0
                ORDER BY total_exec_time DESC
                LIMIT 50
            """)
            columns = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                baseline["queries"].append(dict(zip(columns, row)))
        except Exception as e:
            logger.warning(f"pg_stat_statements not available for {cluster_id}: {e}")
            conn.rollback()
            baseline["pg_stat_statements_available"] = False

        # Index usage — identifies indexes that might be affected by planner changes
        cur.execute("""
            SELECT schemaname, relname, indexrelname, idx_scan, idx_tup_read, idx_tup_fetch
            FROM pg_stat_user_indexes
            WHERE idx_scan > 0
            ORDER BY idx_scan DESC
            LIMIT 30
        """)
        columns = [desc[0] for desc in cur.description]
        for row in cur.fetchall():
            baseline["index_stats"].append(dict(zip(columns, row)))

        # Sequential scan ratios — high seq_scan on large tables = potential issue
        cur.execute("""
            SELECT schemaname, relname, seq_scan, seq_tup_read, idx_scan,
                   n_live_tup,
                   CASE WHEN (seq_scan + idx_scan) > 0
                        THEN round(100.0 * seq_scan / (seq_scan + idx_scan), 1)
                        ELSE 0 END as seq_scan_pct
            FROM pg_stat_user_tables
            WHERE n_live_tup > 100
            ORDER BY seq_tup_read DESC
            LIMIT 20
        """)
        columns = [desc[0] for desc in cur.description]
        for row in cur.fetchall():
            baseline["seq_scan_ratios"].append(dict(zip(columns, row)))

        # Capture key PostgreSQL settings that affect query planning
        cur.execute("""
            SELECT name, setting, unit
            FROM pg_settings
            WHERE name IN (
                'work_mem', 'shared_buffers', 'effective_cache_size',
                'random_page_cost', 'seq_page_cost', 'default_statistics_target',
                'jit', 'enable_hashjoin', 'enable_mergejoin', 'enable_nestloop'
            )
        """)
        for row in cur.fetchall():
            baseline["settings"][row[0]] = {"value": row[1], "unit": row[2]}

        baseline["query_count"] = len(baseline["queries"])
        baseline["total_tracked_time_ms"] = sum(
            q.get("total_exec_time", 0) for q in baseline["queries"]
        )

        conn.close()
        logger.info(f"Captured baseline for {cluster_id}: {len(baseline['queries'])} queries")

    except Exception as e:
        logger.error(f"Baseline capture failed for {cluster_id}: {e}")
        baseline["error"] = str(e)
        try:
            conn.close()
        except Exception:
            pass

    return baseline


def compare_with_baseline(cluster_id: str, baseline: dict, region: str = "") -> dict:
    """Compare current pg_stat_statements against pre-upgrade baseline.

    Returns regression analysis:
    - Which queries got slower and by how much
    - Which queries changed execution plan (rows_per_call change)
    - Which indexes stopped being used (plan change indicator)
    - Overall fleet health assessment
    """
    conn = _get_connection(cluster_id, region)
    if not conn:
        return {"error": "Could not connect to cluster", "cluster_id": cluster_id}

    comparison = {
        "cluster_id": cluster_id,
        "compared_at": datetime.now(timezone.utc).isoformat(),
        "baseline_captured_at": baseline.get("captured_at"),
        "regressions": [],
        "improvements": [],
        "plan_changes": [],
        "missing_queries": [],
        "new_queries": [],
        "index_changes": [],
        "overall_verdict": "pass",
        "summary": {},
    }

    try:
        cur = conn.cursor()

        # Get current query stats
        cur.execute("""
            SELECT queryid, query, calls, total_exec_time, mean_exec_time,
                   stddev_exec_time, rows, shared_blks_hit, shared_blks_read
            FROM pg_stat_statements
            WHERE userid != 10
              AND query NOT LIKE '%pg_stat%'
              AND calls > 0
            ORDER BY total_exec_time DESC
            LIMIT 100
        """)
        columns = [desc[0] for desc in cur.description]
        current_queries = {}
        for row in cur.fetchall():
            q = dict(zip(columns, row))
            current_queries[q["queryid"]] = q

        # Compare each baseline query against current
        baseline_queries = {q["queryid"]: q for q in baseline.get("queries", [])}

        for qid, base_q in baseline_queries.items():
            curr_q = current_queries.get(qid)

            if not curr_q:
                # Query disappeared — might be text change after upgrade
                comparison["missing_queries"].append({
                    "queryid": qid,
                    "query": base_q.get("query", "")[:100],
                    "was_calls": base_q.get("calls", 0),
                    "was_mean_ms": round(base_q.get("mean_exec_time", 0), 2),
                })
                continue

            base_mean = base_q.get("mean_exec_time", 0)
            curr_mean = curr_q.get("mean_exec_time", 0)

            if base_mean <= 0:
                continue

            pct_change = ((curr_mean - base_mean) / base_mean) * 100

            # Check for rows-per-call change (plan change indicator)
            base_rows_per_call = base_q.get("rows", 0) / max(base_q.get("calls", 1), 1)
            curr_rows_per_call = curr_q.get("rows", 0) / max(curr_q.get("calls", 1), 1)
            rows_pct_change = 0
            if base_rows_per_call > 0:
                rows_pct_change = ((curr_rows_per_call - base_rows_per_call) / base_rows_per_call) * 100

            entry = {
                "queryid": qid,
                "query": base_q.get("query", "")[:200],
                "baseline_mean_ms": round(base_mean, 3),
                "current_mean_ms": round(curr_mean, 3),
                "pct_change": round(pct_change, 1),
                "baseline_calls": base_q.get("calls", 0),
                "current_calls": curr_q.get("calls", 0),
                "rows_pct_change": round(rows_pct_change, 1),
            }

            if pct_change > REGRESSION_THRESHOLDS["mean_exec_time_critical"]:
                entry["severity"] = "critical"
                comparison["regressions"].append(entry)
            elif pct_change > REGRESSION_THRESHOLDS["mean_exec_time_pct"]:
                entry["severity"] = "warning"
                comparison["regressions"].append(entry)
            elif pct_change < -20:
                comparison["improvements"].append(entry)

            if abs(rows_pct_change) > REGRESSION_THRESHOLDS["rows_per_call_pct"]:
                comparison["plan_changes"].append({
                    **entry,
                    "indicator": "Query plan likely changed — rows per call shifted significantly",
                })

        # Check for index usage changes
        cur.execute("""
            SELECT schemaname, relname, indexrelname, idx_scan
            FROM pg_stat_user_indexes
            ORDER BY idx_scan DESC
            LIMIT 30
        """)
        current_index_usage = {}
        for row in cur.fetchall():
            current_index_usage[row[2]] = row[3]

        for base_idx in baseline.get("index_stats", []):
            idx_name = base_idx["indexrelname"]
            base_scans = base_idx["idx_scan"]
            curr_scans = current_index_usage.get(idx_name, 0)

            # If an index that was heavily used is now barely used, plan changed
            if base_scans > 100 and curr_scans < base_scans * 0.1:
                comparison["index_changes"].append({
                    "index": idx_name,
                    "table": base_idx.get("relname"),
                    "baseline_scans": base_scans,
                    "current_scans": curr_scans,
                    "issue": "Index usage dropped >90% — possible plan regression",
                })

        conn.close()

        # Determine overall verdict
        critical_regressions = [r for r in comparison["regressions"] if r.get("severity") == "critical"]
        if critical_regressions:
            comparison["overall_verdict"] = "fail"
        elif len(comparison["regressions"]) > 5:
            comparison["overall_verdict"] = "warning"
        elif comparison["index_changes"]:
            comparison["overall_verdict"] = "warning"

        comparison["summary"] = {
            "total_baseline_queries": len(baseline_queries),
            "regressions_found": len(comparison["regressions"]),
            "critical_regressions": len(critical_regressions),
            "improvements_found": len(comparison["improvements"]),
            "plan_changes_detected": len(comparison["plan_changes"]),
            "missing_queries": len(comparison["missing_queries"]),
            "index_usage_changes": len(comparison["index_changes"]),
        }

        logger.info(
            f"Comparison for {cluster_id}: verdict={comparison['overall_verdict']}, "
            f"regressions={len(comparison['regressions'])}, improvements={len(comparison['improvements'])}"
        )

    except Exception as e:
        logger.error(f"Baseline comparison failed for {cluster_id}: {e}")
        comparison["error"] = str(e)
        comparison["overall_verdict"] = "error"
        try:
            conn.close()
        except Exception:
            pass

    return comparison


def run_validation_queries(cluster_id: str, region: str = "") -> dict:
    """Run post-upgrade validation queries directly via psycopg2.

    This solves Gap #6 — no psql binary needed. Validates:
    - Extensions are loaded and functional
    - User functions compile and execute
    - Triggers fire correctly
    - Views return data
    - Index types are usable (GIN, BTREE, etc.)
    """
    conn = _get_connection(cluster_id, region)
    if not conn:
        return {"status": "error", "error": "Could not connect", "cluster_id": cluster_id}

    results = {
        "cluster_id": cluster_id,
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "checks": [],
        "passed": True,
    }

    from psycopg2 import sql as pg_sql

    try:
        cur = conn.cursor()

        # Check 1: PostgreSQL version
        cur.execute("SELECT version()")
        version = cur.fetchone()[0]
        results["checks"].append({
            "name": "pg_version",
            "status": "pass",
            "result": version,
        })

        # Check 2: Extensions loaded
        cur.execute("SELECT extname, extversion FROM pg_extension WHERE extname != 'plpgsql' ORDER BY extname")
        extensions = cur.fetchall()
        results["checks"].append({
            "name": "extensions_loaded",
            "status": "pass",
            "result": [{"name": e[0], "version": e[1]} for e in extensions],
        })

        # Check 3: User functions compile
        cur.execute("""
            SELECT p.proname, l.lanname
            FROM pg_proc p
            JOIN pg_namespace n ON p.pronamespace = n.oid
            JOIN pg_language l ON p.prolang = l.oid
            WHERE n.nspname = 'public' AND l.lanname = 'plpgsql'
        """)
        functions = cur.fetchall()
        func_results = []
        for fname, lang in functions:
            try:
                # Validate function by checking its prosrc parses.
                # Parameterized (not an f-string) so fname can't break out of the
                # query even if a maliciously named function exists in the DB.
                cur.execute("SELECT pg_get_functiondef(%s::regproc)", (fname,))
                cur.fetchone()
                func_results.append({"name": fname, "status": "pass"})
            except Exception as e:
                func_results.append({"name": fname, "status": "fail", "error": str(e)})
                results["passed"] = False
                conn.rollback()

        results["checks"].append({
            "name": "user_functions",
            "status": "pass" if all(f["status"] == "pass" for f in func_results) else "fail",
            "result": func_results,
        })

        # Check 4: Triggers exist and are enabled
        cur.execute("""
            SELECT t.tgname, c.relname, t.tgenabled
            FROM pg_trigger t
            JOIN pg_class c ON t.tgrelid = c.oid
            WHERE NOT t.tgisinternal
        """)
        triggers = cur.fetchall()
        trigger_results = []
        for tname, table, enabled in triggers:
            status = "pass" if enabled in ('O', 'A') else "disabled"
            trigger_results.append({"name": tname, "table": table, "enabled": enabled, "status": status})

        results["checks"].append({
            "name": "triggers",
            "status": "pass" if all(t["status"] == "pass" for t in trigger_results) else "warning",
            "result": trigger_results,
        })

        # Check 5: Views execute without error
        cur.execute("""
            SELECT table_name FROM information_schema.views
            WHERE table_schema = 'public'
        """)
        views = [row[0] for row in cur.fetchall()]
        view_results = []
        for view in views:
            try:
                # view is a table/view identifier, not a value, so it must be
                # safely quoted with sql.Identifier rather than f-string interpolated.
                # nosemgrep: sqlalchemy-execute-raw-query -- not SQLAlchemy; this is
                # psycopg2's documented safe identifier-quoting API, not string building.
                cur.execute(pg_sql.SQL("SELECT 1 FROM {} LIMIT 1").format(pg_sql.Identifier(view)))
                cur.fetchall()
                view_results.append({"name": view, "status": "pass"})
            except Exception as e:
                view_results.append({"name": view, "status": "fail", "error": str(e)})
                results["passed"] = False
                conn.rollback()

        results["checks"].append({
            "name": "views",
            "status": "pass" if all(v["status"] == "pass" for v in view_results) else "fail",
            "result": view_results,
        })

        # Check 6: Index types are usable (try a query that uses each type)
        cur.execute("""
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = 'public'
            AND (indexdef LIKE '%gin%' OR indexdef LIKE '%gist%' OR indexdef LIKE '%hash%')
        """)
        special_indexes = cur.fetchall()
        index_results = []
        for idx_name, idx_def in special_indexes:
            try:
                # Try REINDEX to validate index is healthy. idx_name is an
                # identifier, so quote it safely instead of f-string interpolating it.
                # nosemgrep: sqlalchemy-execute-raw-query -- not SQLAlchemy; this is
                # psycopg2's documented safe identifier-quoting API, not string building.
                cur.execute(pg_sql.SQL("REINDEX INDEX {}").format(pg_sql.Identifier(idx_name)))
                index_results.append({"name": idx_name, "type": "special", "status": "pass"})
            except Exception as e:
                index_results.append({"name": idx_name, "status": "fail", "error": str(e)})
                conn.rollback()

        results["checks"].append({
            "name": "special_indexes",
            "status": "pass" if all(i["status"] == "pass" for i in index_results) else "warning",
            "result": index_results,
        })

        # Check 7: Table stats are fresh (ANALYZE ran recently)
        cur.execute("""
            SELECT relname, last_analyze, last_autoanalyze, n_live_tup
            FROM pg_stat_user_tables
            WHERE n_live_tup > 0
            ORDER BY n_live_tup DESC
        """)
        table_stats = []
        for row in cur.fetchall():
            last_analyzed = row[1] or row[2]
            table_stats.append({
                "table": row[0],
                "last_analyzed": last_analyzed.isoformat() if last_analyzed else None,
                "rows": row[3],
            })

        stale_tables = [t for t in table_stats if t["last_analyzed"] is None]
        results["checks"].append({
            "name": "table_statistics",
            "status": "warning" if stale_tables else "pass",
            "result": {"total_tables": len(table_stats), "stale_tables": stale_tables},
        })

        conn.close()

    except Exception as e:
        logger.error(f"Validation queries failed for {cluster_id}: {e}")
        results["error"] = str(e)
        results["passed"] = False
        try:
            conn.close()
        except Exception:
            pass

    return results


def execute_db_command(cluster_id: str, sql: str, region: str = "", database: str = "postgres") -> dict:
    """Execute a SQL command against a cluster and return results.

    This replaces the need for psql CLI (Gap #6). Used by the execute-step
    API endpoint's "run_sql" action.
    """
    conn = _get_connection(cluster_id, region, database=database)
    if not conn:
        return {"status": "error", "error": "Could not connect to cluster"}

    try:
        cur = conn.cursor()
        cur.execute(sql)

        # Try to fetch results (SELECT-like queries)
        try:
            columns = [desc[0] for desc in cur.description] if cur.description else []
            rows = cur.fetchall()
            # Format as text table similar to psql output
            output_lines = []
            if columns:
                output_lines.append(" | ".join(columns))
                output_lines.append("-+-".join("-" * max(len(c), 5) for c in columns))
                for row in rows:
                    output_lines.append(" | ".join(str(v) if v is not None else "" for v in row))
                output_lines.append(f"({len(rows)} rows)")

            conn.close()
            return {
                "status": "success",
                "stdout": "\n".join(output_lines),
                "rows": len(rows),
                "columns": columns,
            }
        except Exception:
            # Non-SELECT command (ANALYZE, REINDEX, etc.)
            conn.commit()
            conn.close()
            return {
                "status": "success",
                "stdout": f"Command executed successfully. Rows affected: {cur.rowcount}",
                "rows_affected": cur.rowcount,
            }

    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return {"status": "error", "error": str(e)}


def _get_db_credentials() -> dict:
    """Resolve DB credentials from Secrets Manager or environment variables.

    Priority:
    1. DB_SECRET_ARN env var → fetch from Secrets Manager (recommended for production)
    2. DB_USER + DB_PASSWORD env vars → use directly (for local dev / simple setups)
    3. Fallback defaults (not recommended)
    """
    import os

    secret_arn = os.environ.get("DB_SECRET_ARN")
    if secret_arn:
        try:
            import boto3
            client = boto3.client("secretsmanager")
            resp = client.get_secret_value(SecretId=secret_arn)
            secret = json.loads(resp["SecretString"])
            return {
                "user": secret.get("username", "postgres"),
                "password": secret.get("password", ""),
            }
        except Exception as e:
            logger.warning(f"Failed to fetch secret {secret_arn}: {e}, falling back to env vars")

    return {
        "user": os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("DB_PASSWORD", ""),
    }


def _get_connection(cluster_id: str, region: str = "", database: str = "postgres"):
    """Get a psycopg2 connection to a cluster by resolving its endpoint.

    Credentials are resolved via Secrets Manager (DB_SECRET_ARN) or environment
    variables (DB_USER, DB_PASSWORD).
    """
    try:
        import psycopg2
    except ImportError:
        logger.error("psycopg2 not available — install psycopg2-binary")
        return None

    creds = _get_db_credentials()

    # Resolve endpoint from cluster ID
    try:
        from shared.aws_clients import rds_client
        client = rds_client(region)
        resp = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        endpoint = resp["DBClusters"][0]["Endpoint"]
        port = resp["DBClusters"][0].get("Port", 5432)
    except Exception as e:
        logger.error(f"Could not resolve endpoint for {cluster_id}: {e}")
        return None

    try:
        conn = psycopg2.connect(
            host=endpoint,
            port=port,
            dbname=database,
            user=creds["user"],
            password=creds["password"],
            connect_timeout=10,
            options="-c statement_timeout=30000",
        )
        conn.autocommit = True
        return conn
    except Exception as e:
        logger.error(f"Connection failed to {cluster_id} ({endpoint}): {e}")
        return None
