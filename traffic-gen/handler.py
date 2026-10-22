# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Traffic Generator Lambda — Sends realistic workload to Aurora PG clusters.

Runs periodically (EventBridge) and generates environment-specific traffic patterns:
- Dev: Light, bursty traffic (simulates developer queries)
- Staging: Moderate, consistent (simulates integration tests + CI/CD)
- Production: Heavy, sustained with peak patterns (simulates real app)

This lets the Patch Agent's Analyzer/Validator see distinct baselines per environment
and make better upgrade timing and strategy decisions.
"""
import json
import random
import time
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg2
from psycopg2 import sql as pg_sql
from psycopg2.extras import execute_values
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

import os

DB_PORT = 5432
DB_NAME = os.environ.get("DB_NAME", "postgres")
REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-2"))

_cached_creds = None


def _get_credentials():
    """Resolve DB credentials from Secrets Manager or environment variables."""
    global _cached_creds
    if _cached_creds:
        return _cached_creds

    secret_arn = os.environ.get("DB_SECRET_ARN")
    if secret_arn:
        try:
            client = boto3.client("secretsmanager", region_name=REGION)
            resp = client.get_secret_value(SecretId=secret_arn)
            secret = json.loads(resp["SecretString"])
            _cached_creds = {
                "user": secret.get("username", "postgres"),
                "password": secret.get("password", ""),
            }
            return _cached_creds
        except Exception as e:
            logger.warning(f"Failed to fetch secret: {e}, falling back to env vars")

    _cached_creds = {
        "user": os.environ.get("DB_USER", "postgres"),
        "password": os.environ.get("DB_PASSWORD", ""),
    }
    return _cached_creds

# Traffic profiles per environment
PROFILES = {
    "dev": {
        "connections": 3,
        "queries_per_cycle": 20,
        "write_ratio": 0.3,
        "think_time_ms": (200, 1000),
        "batch_size": 5,
        "complex_query_ratio": 0.1,
    },
    "staging": {
        "connections": 8,
        "queries_per_cycle": 80,
        "write_ratio": 0.4,
        "think_time_ms": (50, 300),
        "batch_size": 20,
        "complex_query_ratio": 0.3,
    },
    "production": {
        "connections": 15,
        "queries_per_cycle": 200,
        "write_ratio": 0.5,
        "think_time_ms": (10, 100),
        "batch_size": 50,
        "complex_query_ratio": 0.4,
    },
}

# Schema setup — creates tables if they don't exist
SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE IF NOT EXISTS customers (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    name VARCHAR(255) NOT NULL,
    tier VARCHAR(20) DEFAULT 'free',
    created_at TIMESTAMP DEFAULT NOW(),
    last_login TIMESTAMP
);

CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    category VARCHAR(100),
    price NUMERIC(10,2) NOT NULL,
    stock INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER REFERENCES customers(id),
    total NUMERIC(10,2) NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER REFERENCES orders(id),
    product_id INTEGER REFERENCES products(id),
    quantity INTEGER NOT NULL,
    price NUMERIC(10,2) NOT NULL
);

CREATE TABLE IF NOT EXISTS page_views (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER,
    page VARCHAR(255),
    duration_ms INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_customers_email ON customers(email);
CREATE INDEX IF NOT EXISTS idx_customers_tier ON customers(tier);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_page_views_customer ON page_views(customer_id);
CREATE INDEX IF NOT EXISTS idx_page_views_created ON page_views(created_at);
"""

SEED_SQL = """
-- Seed customers if empty
INSERT INTO customers (email, name, tier)
SELECT
    'user' || g || '@example.com',
    'User ' || g,
    (ARRAY['free','basic','premium','enterprise'])[1 + (g % 4)]
FROM generate_series(1, 500) g
ON CONFLICT (email) DO NOTHING;
"""

SEED_PRODUCTS_SQL = """
INSERT INTO products (name, category, price, stock{sku_col})
SELECT
    'Product ' || g,
    (ARRAY['electronics','clothing','food','books','home'])[1 + (g % 5)],
    (random() * 200 + 5)::numeric(10,2),
    (random() * 1000)::integer
    {sku_val}
FROM generate_series(1, 200) g
ON CONFLICT DO NOTHING;
"""


def get_connection(endpoint, dbname=DB_NAME):
    creds = _get_credentials()
    return psycopg2.connect(
        host=endpoint, port=DB_PORT, user=creds["user"],
        password=creds["password"], dbname=dbname, connect_timeout=10,
    )


def setup_schema(endpoint):
    """Create database, tables, and seed data if needed."""
    # First connect to postgres to create ecommerce DB if missing
    conn = get_connection(endpoint, dbname="postgres")
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
    if not cur.fetchone():
        # DB_NAME is a config-supplied identifier, not a value, so it can't be
        # passed as a %s param — quote it safely instead of f-string interpolating it.
        # nosemgrep: sqlalchemy-execute-raw-query -- not SQLAlchemy; this is
        # psycopg2's documented safe identifier-quoting API, not string building.
        cur.execute(pg_sql.SQL("CREATE DATABASE {}").format(pg_sql.Identifier(DB_NAME)))
    cur.close()
    conn.close()

    # Now connect to ecommerce and set up schema
    conn = get_connection(endpoint)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(SCHEMA_SQL)

    # Handle existing tables missing columns (e.g. stock on products)
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'products' AND column_name = 'stock'
    """)
    if not cur.fetchone():
        cur.execute("ALTER TABLE products ADD COLUMN IF NOT EXISTS stock INTEGER DEFAULT 0")

    # Check if products table has a sku column (some clusters have NOT NULL constraint)
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'products' AND column_name = 'sku'
    """)
    has_sku = cur.fetchone() is not None

    cur.execute(SEED_SQL)

    if has_sku:
        seed_products = SEED_PRODUCTS_SQL.format(sku_col=", sku", sku_val=", 'SKU-' || g")
    else:
        seed_products = SEED_PRODUCTS_SQL.format(sku_col="", sku_val="")
    cur.execute(seed_products)
    cur.execute("SELECT count(*) FROM customers")
    customers = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM products")
    products = cur.fetchone()[0]
    cur.close()
    conn.close()
    return {"customers": customers, "products": products}


# ─────────────────────────────────────────────
# Query templates (realistic e-commerce patterns)
# ─────────────────────────────────────────────

READ_QUERIES = [
    # Simple lookups
    "SELECT * FROM customers WHERE id = {customer_id}",
    "SELECT * FROM products WHERE id = {product_id}",
    "SELECT * FROM orders WHERE customer_id = {customer_id} ORDER BY created_at DESC LIMIT 10",
    # Filtered searches
    "SELECT * FROM products WHERE category = '{category}' AND price < {price} ORDER BY price LIMIT 20",
    "SELECT * FROM customers WHERE tier = '{tier}' LIMIT 50",
    "SELECT * FROM orders WHERE status = '{status}' AND created_at > NOW() - INTERVAL '7 days'",
    # Aggregations
    "SELECT customer_id, COUNT(*) as order_count, SUM(total) as lifetime_value FROM orders GROUP BY customer_id ORDER BY lifetime_value DESC LIMIT 20",
    "SELECT category, COUNT(*) as cnt, AVG(price) as avg_price FROM products GROUP BY category",
    "SELECT DATE(created_at) as day, COUNT(*) as orders, SUM(total) as revenue FROM orders WHERE created_at > NOW() - INTERVAL '30 days' GROUP BY day ORDER BY day",
]

COMPLEX_QUERIES = [
    # Joins
    """SELECT c.name, c.tier, COUNT(o.id) as orders, SUM(o.total) as total_spend
       FROM customers c LEFT JOIN orders o ON c.id = o.customer_id
       WHERE c.tier = '{tier}'
       GROUP BY c.id, c.name, c.tier
       HAVING COUNT(o.id) > 0
       ORDER BY total_spend DESC LIMIT 25""",
    # Subqueries
    """SELECT p.name, p.category, p.price,
              (SELECT COUNT(*) FROM order_items oi WHERE oi.product_id = p.id) as times_ordered
       FROM products p
       WHERE p.category = '{category}'
       ORDER BY times_ordered DESC LIMIT 15""",
    # Window functions
    """SELECT customer_id, total, created_at,
              ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY created_at DESC) as rn,
              AVG(total) OVER (PARTITION BY customer_id) as avg_order
       FROM orders
       WHERE created_at > NOW() - INTERVAL '90 days'
       LIMIT 100""",
    # Analytics
    """SELECT DATE_TRUNC('hour', pv.created_at) as hour,
              COUNT(*) as views,
              COUNT(DISTINCT pv.customer_id) as unique_visitors,
              AVG(pv.duration_ms) as avg_duration
       FROM page_views pv
       WHERE pv.created_at > NOW() - INTERVAL '24 hours'
       GROUP BY hour ORDER BY hour""",
]

WRITE_QUERIES = [
    "insert_order",
    "insert_page_view",
    "update_order_status",
    "update_stock",
    "update_last_login",
]


# `random` (not `secrets`) is used throughout this module deliberately: this
# file only generates synthetic load-test traffic (mock customer IDs, prices,
# query choice) for exercising a cluster's read/write mix, never anything
# security- or cryptography-sensitive. `secrets`/os.urandom would add
# overhead with no benefit here.
def random_params():
    return {
        "customer_id": random.randint(1, 500),
        "product_id": random.randint(1, 200),
        "category": random.choice(["electronics", "clothing", "food", "books", "home"]),
        "price": random.randint(20, 500),
        "tier": random.choice(["free", "basic", "premium", "enterprise"]),
        "status": random.choice(["pending", "confirmed", "shipped", "delivered"]),
    }


def execute_read(cur, profile):
    """Execute a read query."""
    params = random_params()
    if random.random() < profile["complex_query_ratio"]:
        query = random.choice(COMPLEX_QUERIES).format(**params)
    else:
        query = random.choice(READ_QUERIES).format(**params)
    cur.execute(query)
    cur.fetchall()


def execute_write(cur, profile):
    """Execute a write operation."""
    op = random.choice(WRITE_QUERIES)
    params = random_params()

    if op == "insert_order":
        cur.execute(
            "INSERT INTO orders (customer_id, total, status) VALUES (%s, %s, %s) RETURNING id",
            (params["customer_id"], round(random.uniform(10, 500), 2), "pending")
        )
        order_id = cur.fetchone()[0]
        items = random.randint(1, 5)
        for _ in range(items):
            cur.execute(
                "INSERT INTO order_items (order_id, product_id, quantity, price) VALUES (%s, %s, %s, %s)",
                (order_id, random.randint(1, 200), random.randint(1, 3), round(random.uniform(5, 200), 2))
            )

    elif op == "insert_page_view":
        cur.execute(
            "INSERT INTO page_views (customer_id, page, duration_ms) VALUES (%s, %s, %s)",
            (params["customer_id"], random.choice(["/", "/products", "/cart", "/checkout", "/account"]),
             random.randint(100, 30000))
        )

    elif op == "update_order_status":
        new_status = random.choice(["confirmed", "shipped", "delivered"])
        cur.execute(
            "UPDATE orders SET status = %s, updated_at = NOW() WHERE id = (SELECT id FROM orders WHERE status = 'pending' LIMIT 1)",
            (new_status,)
        )

    elif op == "update_stock":
        cur.execute(
            "UPDATE products SET stock = stock - %s WHERE id = %s AND stock > %s",
            (random.randint(1, 5), params["product_id"], 5)
        )

    elif op == "update_last_login":
        cur.execute(
            "UPDATE customers SET last_login = NOW() WHERE id = %s",
            (params["customer_id"],)
        )


def run_worker(endpoint, profile, worker_id, queries_per_worker):
    """Single connection worker — runs a batch of queries."""
    results = {"reads": 0, "writes": 0, "errors": 0, "latency_ms": []}
    try:
        conn = get_connection(endpoint)
        conn.autocommit = True
        cur = conn.cursor()

        for _ in range(queries_per_worker):
            t0 = time.time()
            try:
                if random.random() < profile["write_ratio"]:
                    execute_write(cur, profile)
                    results["writes"] += 1
                else:
                    execute_read(cur, profile)
                    results["reads"] += 1
                latency = (time.time() - t0) * 1000
                results["latency_ms"].append(latency)
            except Exception as e:
                results["errors"] += 1
                logger.debug(f"Worker {worker_id} query error: {e}")

            # Think time
            think_min, think_max = profile["think_time_ms"]
            time.sleep(random.uniform(think_min, think_max) / 1000)

        cur.close()
        conn.close()
    except Exception as e:
        results["errors"] += 1
        logger.error(f"Worker {worker_id} connection error: {e}")

    return results


def generate_traffic(endpoint, environment, cluster_id):
    """Generate traffic for a single cluster based on its environment profile."""
    profile = PROFILES.get(environment, PROFILES["dev"])
    num_workers = profile["connections"]
    queries_per_worker = profile["queries_per_cycle"] // num_workers

    logger.info(f"[{cluster_id}] Starting traffic: {num_workers} workers × {queries_per_worker} queries ({environment})")

    all_results = {"reads": 0, "writes": 0, "errors": 0, "latency_ms": []}
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [
            executor.submit(run_worker, endpoint, profile, i, queries_per_worker)
            for i in range(num_workers)
        ]
        for f in as_completed(futures):
            r = f.result()
            all_results["reads"] += r["reads"]
            all_results["writes"] += r["writes"]
            all_results["errors"] += r["errors"]
            all_results["latency_ms"].extend(r["latency_ms"])

    # Compute stats
    latencies = sorted(all_results["latency_ms"]) if all_results["latency_ms"] else [0]
    stats = {
        "cluster_id": cluster_id,
        "environment": environment,
        "total_queries": all_results["reads"] + all_results["writes"],
        "reads": all_results["reads"],
        "writes": all_results["writes"],
        "errors": all_results["errors"],
        "p50_ms": round(latencies[len(latencies)//2], 2),
        "p95_ms": round(latencies[int(len(latencies)*0.95)], 2),
        "p99_ms": round(latencies[int(len(latencies)*0.99)], 2),
        "avg_ms": round(sum(latencies)/len(latencies), 2),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(f"[{cluster_id}] Done: {stats['total_queries']} queries (r:{stats['reads']} w:{stats['writes']} err:{stats['errors']}) p50={stats['p50_ms']}ms p99={stats['p99_ms']}ms")
    return stats


def discover_clusters():
    """Discover all PG clusters and their environments."""
    client = boto3.client("rds", region_name=REGION)
    clusters = client.describe_db_clusters()["DBClusters"]
    targets = []

    for c in clusters:
        cid = c["DBClusterIdentifier"]
        if c["Engine"] != "aurora-postgresql":
            continue
        if "-old" in cid or "-green-" in cid:
            continue

        endpoint = c["Endpoint"]
        tags_resp = client.list_tags_for_resource(ResourceName=c["DBClusterArn"])
        tags = {t["Key"]: t["Value"] for t in tags_resp.get("TagList", [])}
        env = (tags.get("Environment") or tags.get("environment") or tags.get("env") or "dev").lower()

        targets.append({
            "cluster_id": cid,
            "endpoint": endpoint,
            "environment": env,
        })

    return targets


def _persist_stats(stats):
    """Persist traffic stats to DynamoDB for the advisor (best-effort, lazy import)."""
    try:
        from shared.traffic import persist_traffic_stats
        persist_traffic_stats(stats)
    except Exception as e:
        logger.warning(f"Could not persist traffic stats: {e}")


def handler(event, context):
    """Lambda handler — discovers clusters and generates traffic per environment."""
    targets = discover_clusters()
    logger.info(f"Discovered {len(targets)} clusters")

    # Setup schema on all clusters first
    for t in targets:
        try:
            counts = setup_schema(t["endpoint"])
            logger.info(f"[{t['cluster_id']}] Schema ready: {counts}")
        except Exception as e:
            logger.warning(f"[{t['cluster_id']}] Schema setup failed: {e}")

    # Generate traffic per cluster (sequentially to avoid overwhelming)
    results = []
    for t in targets:
        try:
            stats = generate_traffic(t["endpoint"], t["environment"], t["cluster_id"])
            _persist_stats(stats)
            results.append(stats)
        except Exception as e:
            logger.error(f"[{t['cluster_id']}] Traffic generation failed: {e}")
            results.append({"cluster_id": t["cluster_id"], "error": str(e)})

    summary = {
        "total_clusters": len(results),
        "by_environment": {},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    for r in results:
        env = r.get("environment", "unknown")
        if env not in summary["by_environment"]:
            summary["by_environment"][env] = {"clusters": 0, "total_queries": 0, "errors": 0}
        summary["by_environment"][env]["clusters"] += 1
        summary["by_environment"][env]["total_queries"] += r.get("total_queries", 0)
        summary["by_environment"][env]["errors"] += r.get("errors", 0)

    logger.info(f"Traffic generation complete: {json.dumps(summary)}")
    return {"statusCode": 200, "body": summary, "results": results}


# Allow local execution for testing
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Can target a single cluster for testing
    if len(sys.argv) > 1:
        endpoint = sys.argv[1]
        env = sys.argv[2] if len(sys.argv) > 2 else "dev"
        setup_schema(endpoint)
        result = generate_traffic(endpoint, env, "local-test")
        print(json.dumps(result, indent=2))
    else:
        result = handler({}, None)
        print(json.dumps(result["body"], indent=2))
