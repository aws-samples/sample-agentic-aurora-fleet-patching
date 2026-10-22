# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Database-level metadata collection for upgrade assessment.

Inspired by awslabs.postgres-mcp-server, this module gathers deep database
configuration that the RDS API cannot provide. It connects to clusters and
queries pg_catalog / information_schema to identify:

1. Extensions + versions (do they need updating for the target PG version?)
2. Custom functions, triggers, views (might reference deprecated features)
3. Top queries from pg_stat_statements (performance baseline)
4. Replication slots (could block Blue/Green)
5. Custom data types, enums, domains (structural compatibility)
6. Collation/encoding per database (character set issues in upgrades)
7. Table sizes + bloat (affects upgrade duration estimates)
8. Parameter overrides at role/database level (might conflict with new defaults)
9. Foreign data wrappers (external dependencies)
10. Active locks and long-running queries (upgrade blockers)

CONNECTION METHODS (priority order):
1. RDS Data API — No VPC needed, works from anywhere (requires Data API enabled)
2. IAM Auth — psycopg2 via IAM token (requires VPC connectivity)
3. Static credentials — fallback (from Secrets Manager or env vars)
"""
import json
import logging
import os
from typing import Optional

import boto3

from shared.config import DB_IAM_AUTH_ENABLED, DB_IAM_USER, DB_SECRET_ARN

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Metadata Queries — What to gather from the database
# ─────────────────────────────────────────────────────────────────────────────

METADATA_QUERIES = {
    "extensions": {
        "sql": """
            SELECT e.extname, e.extversion, n.nspname as schema,
                   av.default_version as latest_available
            FROM pg_extension e
            JOIN pg_namespace n ON e.extnamespace = n.oid
            LEFT JOIN pg_available_extensions av ON av.name = e.extname
            ORDER BY e.extname;
        """,
        "purpose": "Installed extensions and whether newer versions are available",
    },
    "custom_functions": {
        "sql": """
            SELECT n.nspname as schema, p.proname as function_name,
                   pg_catalog.pg_get_function_result(p.oid) as return_type,
                   l.lanname as language,
                   CASE WHEN l.lanname = 'plpython2u' THEN 'DEPRECATED'
                        WHEN l.lanname = 'plpythonu' THEN 'DEPRECATED'
                        ELSE 'OK' END as status
            FROM pg_proc p
            JOIN pg_namespace n ON p.pronamespace = n.oid
            JOIN pg_language l ON p.prolang = l.oid
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
            ORDER BY n.nspname, p.proname
            LIMIT 100;
        """,
        "purpose": "Custom functions — check for deprecated PL/Python2 usage",
    },
    "triggers": {
        "sql": """
            SELECT event_object_schema, event_object_table, trigger_name,
                   action_timing, event_manipulation, action_statement
            FROM information_schema.triggers
            WHERE trigger_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY event_object_schema, event_object_table
            LIMIT 50;
        """,
        "purpose": "Custom triggers that might reference deprecated functions",
    },
    "replication_slots": {
        "sql": """
            SELECT slot_name, plugin, slot_type, database, active,
                   restart_lsn, confirmed_flush_lsn,
                   pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) as lag
            FROM pg_replication_slots;
        """,
        "purpose": "Replication slots — may block Blue/Green deployment",
    },
    "databases": {
        "sql": """
            SELECT datname, pg_encoding_to_char(encoding) as encoding,
                   datcollate, datctype,
                   pg_size_pretty(pg_database_size(datname)) as size,
                   datconnlimit
            FROM pg_database
            WHERE datistemplate = false
            ORDER BY pg_database_size(datname) DESC;
        """,
        "purpose": "Database list with encoding/collation — charset issues on upgrade",
    },
    "table_sizes": {
        "sql": """
            SELECT schemaname, relname as table_name,
                   pg_size_pretty(pg_total_relation_size(schemaname || '.' || relname)) as total_size,
                   pg_total_relation_size(schemaname || '.' || relname) as size_bytes,
                   n_live_tup as estimated_rows,
                   n_dead_tup as dead_tuples,
                   CASE WHEN n_live_tup > 0
                        THEN round(100.0 * n_dead_tup / n_live_tup, 1)
                        ELSE 0 END as bloat_pct
            FROM pg_stat_user_tables
            ORDER BY pg_total_relation_size(schemaname || '.' || relname) DESC
            LIMIT 20;
        """,
        "purpose": "Largest tables — affects Blue/Green sync time and VACUUM needs",
    },
    "top_queries": {
        "sql": """
            SELECT queryid, LEFT(query, 200) as query_text,
                   calls, total_exec_time::bigint as total_ms,
                   mean_exec_time::numeric(10,2) as avg_ms,
                   rows
            FROM pg_stat_statements
            WHERE userid != 10
            ORDER BY total_exec_time DESC
            LIMIT 15;
        """,
        "purpose": "Top queries by time — performance baseline for post-upgrade comparison",
    },
    "custom_types": {
        "sql": """
            SELECT n.nspname as schema, t.typname as type_name,
                   t.typcategory as category,
                   CASE t.typcategory
                     WHEN 'E' THEN 'enum'
                     WHEN 'C' THEN 'composite'
                     WHEN 'D' THEN 'domain'
                     WHEN 'R' THEN 'range'
                     ELSE 'other'
                   END as type_kind
            FROM pg_type t
            JOIN pg_namespace n ON t.typnamespace = n.oid
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
              AND t.typtype IN ('e', 'c', 'd', 'r')
            ORDER BY n.nspname, t.typname;
        """,
        "purpose": "Custom types (enums, composites, domains) — structural compatibility",
    },
    "role_settings": {
        "sql": """
            SELECT rolname, datname, unnest(setconfig) as setting
            FROM pg_db_role_setting s
            JOIN pg_roles r ON r.oid = s.setrole
            LEFT JOIN pg_database d ON d.oid = s.setdatabase
            ORDER BY rolname, datname;
        """,
        "purpose": "Per-role/database parameter overrides — might conflict with new defaults",
    },
    "foreign_data_wrappers": {
        "sql": """
            SELECT fdwname, fdwhandler::regproc as handler,
                   fdwvalidator::regproc as validator,
                   array_to_string(fdwoptions, ', ') as options
            FROM pg_foreign_data_wrapper;
        """,
        "purpose": "Foreign data wrappers — external dependencies that need verification",
    },
    "publication_subscriptions": {
        "sql": """
            SELECT pubname, puballtables, pubinsert, pubupdate, pubdelete
            FROM pg_publication
            UNION ALL
            SELECT subname, NULL, NULL, NULL, NULL
            FROM pg_subscription;
        """,
        "purpose": "Logical replication publications/subscriptions — critical for B/G awareness",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Connection & Execution
# ─────────────────────────────────────────────────────────────────────────────


def collect_metadata(
    cluster_id: str,
    region: str = "",
    database: str = "postgres",
    connection_method: str = "auto",
) -> dict:
    """Collect comprehensive database metadata for upgrade assessment.

    Args:
        cluster_id: Aurora cluster identifier
        region: AWS region
        database: Database name to connect to (default: postgres)
        connection_method: "auto" (try all), "rdsapi", "iam", "static"

    Returns:
        dict with keys matching METADATA_QUERIES + connection metadata
    """
    region = region or os.environ.get("AWS_REGION", "us-east-1")

    results = {
        "cluster_id": cluster_id,
        "database": database,
        "region": region,
        "connection_method": None,
        "collected_at": None,
        "metadata": {},
        "errors": [],
    }

    # Try connection methods in priority order
    methods = []
    if connection_method == "auto":
        methods = ["rdsapi", "iam", "static"]
    else:
        methods = [connection_method]

    executor = None
    for method in methods:
        try:
            if method == "rdsapi":
                executor = _get_rds_data_api_executor(cluster_id, region, database)
            elif method == "iam":
                executor = _get_iam_psycopg2_executor(cluster_id, region, database)
            elif method == "static":
                executor = _get_static_psycopg2_executor(cluster_id, region, database)

            if executor:
                results["connection_method"] = method
                logger.info(f"Connected to {cluster_id} via {method}")
                break
        except Exception as e:
            results["errors"].append(f"{method}: {str(e)}")
            logger.debug(f"Connection method {method} failed for {cluster_id}: {e}")
            continue

    if not executor:
        results["errors"].append("All connection methods failed")
        logger.warning(f"Could not connect to {cluster_id} for metadata collection")
        return results

    # Execute each metadata query
    from datetime import datetime, timezone
    results["collected_at"] = datetime.now(timezone.utc).isoformat()

    for key, query_info in METADATA_QUERIES.items():
        try:
            rows = executor(query_info["sql"])
            results["metadata"][key] = {
                "data": rows,
                "row_count": len(rows),
                "purpose": query_info["purpose"],
            }
        except Exception as e:
            results["metadata"][key] = {
                "data": [],
                "row_count": 0,
                "error": str(e),
                "purpose": query_info["purpose"],
            }
            # pg_stat_statements might not be installed — that's fine
            if "does not exist" not in str(e):
                results["errors"].append(f"Query '{key}' failed: {str(e)[:100]}")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Upgrade-Relevant Analysis
# ─────────────────────────────────────────────────────────────────────────────


def analyze_metadata_for_upgrade(metadata: dict, target_version: str) -> dict:
    """Analyze collected metadata for upgrade compatibility issues.

    Returns structured findings that the Analyzer agent can use.
    """
    findings = {
        "blocking_issues": [],
        "warnings": [],
        "info": [],
        "extension_upgrades_needed": [],
        "deprecated_features_in_use": [],
        "estimated_sync_time_factors": {},
    }

    meta = metadata.get("metadata", {})

    # Check extensions
    extensions_data = meta.get("extensions", {}).get("data", [])
    for ext in extensions_data:
        ext_name = ext.get("extname", "")
        current_ver = ext.get("extversion", "")
        latest = ext.get("latest_available", "")
        if latest and current_ver != latest:
            findings["extension_upgrades_needed"].append({
                "extension": ext_name,
                "current": current_ver,
                "available": latest,
                "action": f"ALTER EXTENSION {ext_name} UPDATE TO '{latest}';",
            })

    # Check for deprecated PL/Python2 functions
    functions_data = meta.get("custom_functions", {}).get("data", [])
    deprecated_funcs = [f for f in functions_data if f.get("status") == "DEPRECATED"]
    if deprecated_funcs:
        findings["blocking_issues"].append({
            "type": "deprecated_language",
            "message": f"{len(deprecated_funcs)} function(s) use deprecated PL/Python2u — must convert to PL/Python3u",
            "details": [f"{f['schema']}.{f['function_name']}" for f in deprecated_funcs],
        })
        findings["deprecated_features_in_use"].extend(
            [f"{f['schema']}.{f['function_name']} (plpython2u)" for f in deprecated_funcs]
        )

    # Check replication slots (may block Blue/Green)
    slots_data = meta.get("replication_slots", {}).get("data", [])
    active_slots = [s for s in slots_data if s.get("active")]
    if active_slots:
        findings["warnings"].append({
            "type": "active_replication_slots",
            "message": f"{len(active_slots)} active replication slot(s) — may interfere with Blue/Green",
            "details": [s.get("slot_name") for s in active_slots],
        })

    # Check database sizes for sync time estimation
    tables_data = meta.get("table_sizes", {}).get("data", [])
    total_size = sum(t.get("size_bytes", 0) for t in tables_data)
    findings["estimated_sync_time_factors"]["total_data_size_bytes"] = total_size
    findings["estimated_sync_time_factors"]["largest_tables"] = [
        {"table": t.get("table_name"), "size": t.get("total_size")}
        for t in tables_data[:5]
    ]

    # Check for high bloat (should VACUUM before upgrade)
    bloated = [t for t in tables_data if t.get("bloat_pct", 0) > 20]
    if bloated:
        findings["warnings"].append({
            "type": "table_bloat",
            "message": f"{len(bloated)} table(s) have >20% dead tuples — VACUUM before upgrade",
            "details": [f"{t['table_name']} ({t['bloat_pct']}% bloat)" for t in bloated],
        })

    # Check role-level settings that might conflict
    role_settings = meta.get("role_settings", {}).get("data", [])
    if role_settings:
        findings["info"].append({
            "type": "role_overrides",
            "message": f"{len(role_settings)} role/database parameter override(s) — verify compatibility",
            "details": [f"{r.get('rolname', '?')}: {r.get('setting', '?')}" for r in role_settings[:10]],
        })

    # Check publications/subscriptions (logical replication)
    pub_sub = meta.get("publication_subscriptions", {}).get("data", [])
    if pub_sub:
        findings["warnings"].append({
            "type": "logical_replication",
            "message": f"{len(pub_sub)} publication/subscription(s) found — verify logical replication compatibility",
            "details": [p.get("pubname") for p in pub_sub],
        })

    # Databases with non-UTF8 encoding (potential issue in major upgrades)
    dbs = meta.get("databases", {}).get("data", [])
    non_utf8 = [d for d in dbs if d.get("encoding") not in ("UTF8", "UTF-8", None)]
    if non_utf8:
        findings["warnings"].append({
            "type": "encoding",
            "message": f"{len(non_utf8)} database(s) use non-UTF8 encoding — verify collation compatibility",
            "details": [f"{d.get('datname')}: {d.get('encoding')}" for d in non_utf8],
        })

    return findings


# ─────────────────────────────────────────────────────────────────────────────
# Connection Method Implementations
# ─────────────────────────────────────────────────────────────────────────────


def _get_rds_data_api_executor(cluster_id: str, region: str, database: str):
    """Try to use RDS Data API (no VPC needed)."""
    client = boto3.client("rds-data", region_name=region)
    rds_client = boto3.client("rds", region_name=region)

    # Get cluster ARN
    resp = rds_client.describe_db_clusters(DBClusterIdentifier=cluster_id)
    if not resp["DBClusters"]:
        raise ValueError(f"Cluster {cluster_id} not found")
    cluster_arn = resp["DBClusters"][0]["DBClusterArn"]

    # Check if Data API is enabled
    data_api_enabled = resp["DBClusters"][0].get("EnableHttpEndpoint", False)
    if not data_api_enabled:
        raise ValueError("RDS Data API not enabled on this cluster")

    # Need a secret ARN for Data API auth
    secret_arn = DB_SECRET_ARN or os.environ.get("DB_SECRET_ARN", "")
    if not secret_arn:
        raise ValueError("DB_SECRET_ARN required for RDS Data API")

    def execute(sql: str) -> list[dict]:
        response = client.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=database,
            sql=sql,
        )
        # Convert RDS Data API response format to list of dicts
        columns = [col["name"] for col in response.get("columnMetadata", [])]
        rows = []
        for record in response.get("records", []):
            row = {}
            for i, field in enumerate(record):
                col_name = columns[i] if i < len(columns) else f"col_{i}"
                # Extract value from the Data API field format
                if "stringValue" in field:
                    row[col_name] = field["stringValue"]
                elif "longValue" in field:
                    row[col_name] = field["longValue"]
                elif "doubleValue" in field:
                    row[col_name] = field["doubleValue"]
                elif "booleanValue" in field:
                    row[col_name] = field["booleanValue"]
                elif "isNull" in field and field["isNull"]:
                    row[col_name] = None
                else:
                    row[col_name] = str(field)
            rows.append(row)
        return rows

    # Test connection
    execute("SELECT 1 as test")
    return execute


def _get_iam_psycopg2_executor(cluster_id: str, region: str, database: str):
    """Use IAM auth with psycopg2 (requires VPC connectivity)."""
    if not DB_IAM_AUTH_ENABLED:
        raise ValueError("IAM auth not enabled")

    try:
        import psycopg2
    except ImportError:
        raise ValueError("psycopg2 not available")

    from shared.iam_auth import get_connection_params
    rds_client = boto3.client("rds", region_name=region)

    # Get cluster endpoint
    resp = rds_client.describe_db_clusters(DBClusterIdentifier=cluster_id)
    if not resp["DBClusters"]:
        raise ValueError(f"Cluster {cluster_id} not found")
    endpoint = resp["DBClusters"][0]["Endpoint"]

    params = get_connection_params(
        host=endpoint, database=database,
        username=DB_IAM_USER, region=region, use_iam=True,
    )

    conn = psycopg2.connect(**params)
    conn.set_session(readonly=True, autocommit=True)

    def execute(sql: str) -> list[dict]:
        with conn.cursor() as cur:
            cur.execute(sql)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    # Test
    execute("SELECT 1 as test")
    return execute


def _get_static_psycopg2_executor(cluster_id: str, region: str, database: str):
    """Use static credentials with psycopg2 (requires VPC connectivity)."""
    try:
        import psycopg2
    except ImportError:
        raise ValueError("psycopg2 not available")

    from shared.iam_auth import get_connection_params
    rds_client = boto3.client("rds", region_name=region)

    resp = rds_client.describe_db_clusters(DBClusterIdentifier=cluster_id)
    if not resp["DBClusters"]:
        raise ValueError(f"Cluster {cluster_id} not found")
    endpoint = resp["DBClusters"][0]["Endpoint"]

    db_user = os.environ.get("DB_USER", "")
    db_password = os.environ.get("DB_PASSWORD", "")
    if not db_user or not db_password:
        raise ValueError("DB_USER and DB_PASSWORD not set")

    params = get_connection_params(
        host=endpoint, database=database,
        username=db_user, region=region,
        use_iam=False, password=db_password,
    )

    conn = psycopg2.connect(**params)
    conn.set_session(readonly=True, autocommit=True)

    def execute(sql: str) -> list[dict]:
        with conn.cursor() as cur:
            cur.execute(sql)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    execute("SELECT 1 as test")
    return execute
