# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""IAM database authentication for Aurora PostgreSQL.

Provides portable, passwordless database connections using RDS IAM auth tokens.
No static credentials required — works with any IAM role that has
rds-db:connect permission.

This is the AWS-standard approach for database connectivity from Lambda/EC2.
"""
import logging
import re
import ssl
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

# Postgres unquoted identifiers: letters, digits, underscores; must not start with a digit.
_VALID_PG_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_pg_identifier(identifier: str) -> str:
    """Safely quote a PostgreSQL identifier for inclusion in generated SQL text.

    This is the same double-quote-and-escape rule Postgres itself (and
    psycopg2.sql.Identifier) uses for quoted identifiers: wrap in double
    quotes, and double up any embedded double-quote characters. Unlike
    psycopg2.sql.Identifier.as_string(), this needs no live connection/cursor,
    which matters here — the caller is generating a standalone .sql script
    file for an operator to run later, not executing against an open
    connection. Combined with the caller-side _VALID_PG_IDENTIFIER check
    (restricting input to [A-Za-z_][A-Za-z0-9_]*), this leaves no character
    that could break out of the quoting.
    """
    return '"' + identifier.replace('"', '""') + '"'


def _quote_pg_string_literal(value: str) -> str:
    """Safely quote a value for inclusion as a PostgreSQL string literal.

    Standard SQL string-literal escaping: wrap in single quotes and double
    any embedded single-quote characters. Used here (rather than
    _quote_pg_identifier) for the one spot this module compares a value
    against pg_roles.rolname in a WHERE clause — a value position, not an
    identifier position.
    """
    return "'" + value.replace("'", "''") + "'"

# AWS RDS CA bundle path (bundled in Lambda runtime)
_RDS_CA_BUNDLE = "/opt/python/rds-combined-ca-bundle.pem"
# Fallback for local development
_RDS_CA_BUNDLE_LOCAL = "rds-combined-ca-bundle.pem"


def generate_auth_token(
    host: str,
    port: int = 5432,
    username: str = "aurora_patch_agent",
    region: Optional[str] = None,
) -> str:
    """Generate an IAM authentication token for RDS connection.

    The token is valid for 15 minutes but connections established with it
    remain valid for the life of the connection.

    Args:
        host: RDS cluster endpoint (writer or reader)
        port: Database port (default 5432)
        username: Database user mapped to IAM (must exist in PG with rds_iam role)
        region: AWS region of the cluster
    """
    client = boto3.client("rds", region_name=region)
    token = client.generate_db_auth_token(
        DBHostname=host,
        Port=port,
        DBUsername=username,
        Region=region,
    )
    logger.debug(f"Generated IAM auth token for {username}@{host}:{port}")
    return token


def get_connection_params(
    host: str,
    port: int = 5432,
    username: str = "aurora_patch_agent",
    database: str = "postgres",
    region: Optional[str] = None,
    use_iam: bool = True,
    password: Optional[str] = None,
    secret_arn: Optional[str] = None,
) -> dict:
    """Get connection parameters for psycopg2, with IAM auth or fallback.

    Priority:
    1. IAM auth (default, portable, no secrets to manage)
    2. Secrets Manager (if secret_arn provided)
    3. Static password (last resort, not recommended for production)

    Returns:
        dict with keys: host, port, user, password, dbname, sslmode, sslrootcert
    """
    params = {
        "host": host,
        "port": port,
        "user": username,
        "dbname": database,
        "sslmode": "verify-full",
    }

    # Try to use RDS CA bundle
    import os
    if os.path.exists(_RDS_CA_BUNDLE):
        params["sslrootcert"] = _RDS_CA_BUNDLE
    elif os.path.exists(_RDS_CA_BUNDLE_LOCAL):
        params["sslrootcert"] = _RDS_CA_BUNDLE_LOCAL
    else:
        # Fall back to require mode without cert verification
        params["sslmode"] = "require"

    if use_iam:
        try:
            params["password"] = generate_auth_token(host, port, username, region)
            logger.info(f"Using IAM auth for {username}@{host}")
            return params
        except Exception as e:
            logger.warning(f"IAM auth token generation failed: {e}")
            # Fall through to other methods

    if secret_arn:
        try:
            creds = _get_secret_credentials(secret_arn, region)
            params["user"] = creds.get("username", username)
            params["password"] = creds["password"]
            if creds.get("host"):
                params["host"] = creds["host"]
            if creds.get("port"):
                params["port"] = creds["port"]
            if creds.get("dbname"):
                params["dbname"] = creds["dbname"]
            logger.info(f"Using Secrets Manager credentials for {params['user']}@{params['host']}")
            return params
        except Exception as e:
            logger.warning(f"Secrets Manager lookup failed: {e}")

    if password:
        params["password"] = password
        logger.info(f"Using static password for {username}@{host}")
        return params

    raise ValueError(
        f"No valid authentication method available for {username}@{host}. "
        "Configure IAM auth, provide a secret_arn, or set DB_PASSWORD."
    )


def _get_secret_credentials(secret_arn: str, region: Optional[str] = None) -> dict:
    """Retrieve database credentials from Secrets Manager."""
    import json
    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_arn)
    return json.loads(response["SecretString"])


def create_iam_db_user_sql(username: str = "aurora_patch_agent") -> str:
    """Generate SQL to create a database user with IAM authentication.

    Run this once on each cluster to enable IAM-based connections:
      psql -h <cluster-endpoint> -U master_user -d postgres -f setup_iam_user.sql

    The created user has read-only privileges suitable for the Analyzer agent's
    metadata gathering (pg_catalog, information_schema, pg_extension).
    """
    # username is interpolated directly into the generated SQL script below, so
    # it must be restricted to a safe identifier shape before use — this script
    # is handed to an operator to run with psql, and psycopg2 parameter binding
    # doesn't apply to identifiers/DDL.
    if not _VALID_PG_IDENTIFIER.match(username):
        raise ValueError(
            f"Invalid username {username!r}: must contain only letters, digits, "
            "and underscores, and not start with a digit."
        )

    # This isn't a query executed against a live connection; it generates a
    # one-time setup script (see scripts/deploy.sh) that an operator runs
    # manually via `psql`. Every occurrence of the username below goes through
    # _quote_pg_identifier(), which double-quotes and escapes it the same way
    # psycopg2.sql.Identifier does — so this isn't bare string interpolation
    # into SQL, it's the same safe-identifier-quoting pattern used elsewhere
    # in this codebase (see agents/validation/baseline.py), just without
    # needing a live connection object.
    quoted_username = _quote_pg_identifier(username)
    lines = [
        "-- Create IAM-authenticated user for Aurora Patch Agent",
        "-- This user has read-only access to system catalogs for metadata gathering.",
        "",
        "DO $$",
        "BEGIN",
        f"    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = {_quote_pg_string_literal(username)}) THEN",
        f"        CREATE USER {quoted_username};",
        "    END IF;",
        "END $$;",
        "",
        "-- Grant IAM authentication",
        f"GRANT rds_iam TO {quoted_username};",
        "",
        "-- Grant read-only access to system catalogs (needed for Analyzer agent)",
        f"GRANT USAGE ON SCHEMA pg_catalog TO {quoted_username};",
        f"GRANT USAGE ON SCHEMA information_schema TO {quoted_username};",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA pg_catalog TO {quoted_username};",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA information_schema TO {quoted_username};",
        "",
        "-- Allow listing extensions",
        f"GRANT SELECT ON pg_extension TO {quoted_username};",
        f"GRANT SELECT ON pg_available_extensions TO {quoted_username};",
        "",
        "-- Allow checking table sizes and row estimates",
        f"GRANT SELECT ON pg_stat_user_tables TO {quoted_username};",
        f"GRANT SELECT ON pg_stat_user_indexes TO {quoted_username};",
        f"GRANT SELECT ON pg_class TO {quoted_username};",
        f"GRANT SELECT ON pg_namespace TO {quoted_username};",
        "",
        "-- Read-only connection tracking",
        f"GRANT SELECT ON pg_stat_activity TO {quoted_username};",
        f"GRANT SELECT ON pg_stat_replication TO {quoted_username};",
        "",
    ]
    return "\n".join(lines)
