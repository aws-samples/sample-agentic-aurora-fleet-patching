# Database access and the traffic generator

[Back to README](../README.md)

## IAM Database Authentication

The Analyzer agent can connect to your Aurora clusters to gather live metadata
(extensions, table sizes, function compatibility). Uses IAM auth — no passwords needed.

### Setup

```bash
# 1. Generate the SQL setup script
./scripts/deploy.sh --setup-iam-user

# 2. Run it on each cluster (one-time)
psql -h my-cluster.cluster-xxx.us-east-1.rds.amazonaws.com \
     -U admin_user -d postgres -f scripts/setup_iam_user.sql

# 3. Configure in deploy-config.json
# "database_auth": { "method": "iam", "iam_user": "aurora_patch_agent" }
```

The IAM user has read-only access to `pg_catalog` and `information_schema` — sufficient for metadata gathering without any risk to your data.

## Traffic Generator (Optional)

Generates realistic e-commerce workload to build performance baselines per environment:

| Environment | Connections | Queries/Cycle | Profile |
|---|---|---|---|
| Dev | 3 | 20 | Light, simple reads |
| Staging | 8 | 80 | Mixed read/write |
| Production | 15 | 200 | Heavy analytics |

Runs every 15 minutes via EventBridge. Requires DB credentials (via `db_user`/`db_password` or `db_secret_arn`).

> **⚠️ Warning: for demo and test accounts only. Never point it at clusters
> that hold real data.**
>
> The traffic generator is deployed and scheduled by `AuroraPatchAgentUiStack`
> by default. It does nothing until you provide DB credentials. Once it has
> them, **every 15 minutes, on every Aurora PostgreSQL cluster it can reach in
> the Region** (except `-old*` and `-green-*` clusters), it:
>
> * creates the `pg_stat_statements` extension and the tables `customers`,
>   `products`, `orders`, `order_items`, and `page_views` (plus indexes) in the
>   `postgres` database, if they don't exist yet;
> * inserts seed data once (500 customers, 200 products);
> * runs reads and writes to simulate a workload per environment.
>
> Schema statements run only when those objects are missing. If you enable the
> generator for the first time while a Blue/Green deployment is in progress,
> that first run's DDL makes RDS **cancel the switchover** (see
> [Known Limitations](known-limitations.md#known-limitations-and-workarounds)), so enable it before
> creating a deployment. Aurora MySQL clusters are skipped.
>
> To keep it off: don't set `db_user`/`db_password`/`db_secret_arn`, or disable
> the schedule after deploying:
>
> ```bash
> aws events disable-rule --region <region> --name aurora-patch-agent-traffic-schedule
> ```
>
> To remove what it created, drop those five tables from the `postgres`
> database of each affected cluster.
