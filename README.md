# Agentic Aurora Fleet Patching

An **agentic** AI tool for fleet patching across **Aurora PostgreSQL** and **Aurora MySQL**. Scans your existing clusters, identifies CVE exposure, analyzes version drift, and orchestrates upgrades via Blue/Green deployment or In-Place upgrade — all from a single-page web UI backed by Amazon Bedrock (Claude Sonnet).

**Portable**: Deploy against any AWS account with Aurora clusters (PostgreSQL, MySQL, or both). No demo clusters required — the agent discovers and scans whatever is already in your account. Supports multi-account fleet management via cross-account IAM roles.

> **This is sample code, provided as-is.** It is intended to demonstrate a
> pattern for automating Aurora patch/upgrade workflows and is not a
> production-ready, AWS-supported artifact. It has not undergone the level of
> security, reliability, and operational review AWS requires before code is
> considered safe to run unmodified against production infrastructure. Some
> of what this agent does is inherently high-impact and hard to fully
> undo — for example, `ModifyDBCluster`, snapshot creation, and Blue/Green
> switchover/deletion against real Aurora clusters. Review the code,
> especially `cdk/cross_account_role.yaml`'s IAM permissions and the
> execute-step logic in `api/server.py`, adapt it to your environment, and
> test thoroughly in a non-production account before pointing it at anything
> you can't afford to lose. See [LICENSE](LICENSE) for the full "as is"
> warranty disclaimer.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  ORCHESTRATOR ACCOUNT                                                        │
│                                                                              │
│  ┌───────────────┐    ┌─────────────────────────────────────────────────┐   │
│  │ API Gateway   │───▶│  Lambda (FastAPI + React SPA)                   │   │
│  └───────────────┘    │  • 10 AI Agents (Bedrock Claude)                │   │
│                       │  • Recommendation Engine (human approves)        │   │
│                       │  • Outcome Learning (gets smarter over time)     │   │
│                       └─────────────────────────────────────────────────┘   │
│                              │                    │                          │
│  ┌───────────────────────────┼────────────────────┼───────────────────────┐ │
│  │  DynamoDB                 │                    │                        │ │
│  │  • Fleet state            │  Bedrock Claude    │                        │ │
│  │  • Recommendations        │  (reasoning,       │                        │ │
│  │  • Learned fixes          │   diagnosis,       │                        │ │
│  │  • Upgrade outcomes       │   fleet planning)  │                        │ │
│  │  • CVE cache              │                    │                        │ │
│  └───────────────────────────┘                    │                        │ │
│                                                   │                        │ │
│  Agents: Scanner → Analyzer → Planner → Executor  │                        │ │
│          Advisor, Supervisor, Validator, Rollback  │                        │ │
│                                                   │                        │ │
└───────────────────────────────────────────────────┼────────────────────────┘ │
                                                    │                          │
               ┌────────────────────────────────────┼──────────────┐           │
               │  AssumeRole (cross-account)        │              │           │
               ▼                                    ▼              ▼           │
     ┌──────────────────┐             ┌──────────────────┐  ┌──────────────┐  │
     │ Account A (prod) │             │ Account B (stg)  │  │ Account C    │  │
     │                  │             │                  │  │ (dev)        │  │
     │ Aurora PG  ×N    │             │ Aurora PG  ×N    │  │ Aurora PG ×N │  │
     │ Aurora MySQL ×N  │             │ Aurora MySQL ×N  │  │ Aurora MySQL │  │
     │                  │             │                  │  │              │  │
     │  us-east-2       │             │  us-east-2       │  │ us-east-2    │  │
     │  us-west-2 (GDB) │             │                  │  │              │  │
     └──────────────────┘             └──────────────────┘  └──────────────┘  │
```

## Quick Start (Scan-Only Mode)

Deploy just the UI + API stack to scan your existing clusters without creating any infrastructure:

```bash
# 1. Clone and install
git clone <repo-url> && cd sample-agentic-aurora-fleet-patching
cd ui && npm install && npm run build && cd ..
bash scripts/build-lambda.sh

# 2. Configure
cd cdk
cp cdk.json.example cdk.json
# Edit cdk.json: set vpc_id, subnet_ids, security_group_id for your account
# Set deploy_infra to "false" to skip orchestration stack

pip install -r requirements.txt

# 3. Deploy (scan-only — no DynamoDB/StepFunctions/SNS)
cdk deploy AuroraPatchAgentUiStack \
  -c vpc_id=vpc-xxx \
  -c subnet_ids=subnet-aaa,subnet-bbb \
  -c security_group_id=sg-xxx \
  -c deploy_infra=false
```

The output URL gives you the full UI — scan your fleet, view CVE exposure, analyze version drift, and run upgrades.

## Full Deployment (with Orchestration)

Includes DynamoDB state table, SNS notifications, StepFunctions orchestration, and scheduled discovery:

```bash
cdk deploy --all \
  -c vpc_id=vpc-xxx \
  -c subnet_ids=subnet-aaa,subnet-bbb \
  -c security_group_id=sg-xxx
```

## What You Need

| Requirement | Notes |
|---|---|
| AWS account with Aurora clusters | PostgreSQL and/or MySQL, any region, any version |
| VPC with public subnets | For NAT Gateway placement (Lambda needs internet for Bedrock/NVD API) |
| Security group | Must allow outbound HTTPS; if connecting to clusters for traffic gen, needs inbound 5432 (PG) or 3306 (MySQL) rule |
| AWS CLI configured | With permissions for RDS, Lambda, API Gateway, Bedrock, IAM, EC2 |
| Node.js 18+ | For UI build |
| Python 3.12+ | For CDK and Lambda |
| AWS CDK CLI | `npm install -g aws-cdk` |

## Running Upgrades: API Authentication (important)

The API is **fail-closed by design.** Read-only actions (scanning, viewing the
fleet, analysis) always work, but **state-changing actions** — starting an
upgrade, switchover, rollback, deleting a cluster or deployment — are **blocked
by default** so that a freshly deployed instance can't be driven anonymously by
anyone who finds its URL. The buttons are always visible in the UI; whether the
backend honors them depends on how you configure auth.

Pick one of these before you expect the upgrade/switchover/delete buttons to work:

**Option A — require a token (recommended for anything shared).**
Set an API token on the backend and give the same token to the UI:

```bash
# Backend: set the token (CDK context or deploy-config.json "security.api_auth_token")
cdk deploy -c api_auth_token=<your-strong-token> ...

# UI: supply the same token so the browser sends it. Either:
#   build-time —
VITE_API_AUTH_TOKEN=<your-strong-token> npm run build
#   or runtime, once, in the browser console (no rebuild) —
localStorage.setItem('apiAuthToken', '<your-strong-token>')
```

Every mutating request then carries the token (`Authorization: Bearer` /
`x-api-key`); requests without it get `401`.

**Option B — explicitly allow unauthenticated (local dev / throwaway demo only).**
Only for an instance that is not reachable by anyone else:

```bash
cdk deploy -c allow_unauthenticated=true ...
# (or "security": { "allow_unauthenticated": true } in deploy-config.json)
```

If you set neither, mutating endpoints return `503` with a message explaining
how to enable them — read-only scanning still works.

## Portable Deployment

The agent is fully externalized — no code changes needed to deploy across environments.

### Option 1: Configuration File (Recommended)

```bash
# 1. Copy the example config
cp deploy-config.example.json deploy-config.json

# 2. Edit with your environment details
# (vpc_id, subnet_ids, security_group_id, regions, accounts, etc.)

# 3. Deploy
./scripts/deploy.sh              # Full deployment
./scripts/deploy.sh --scan-only  # Scan-only mode (no orchestration)
```

### Option 2: CDK Context Overrides

```bash
cdk deploy -c vpc_id=vpc-xxx -c subnet_ids=subnet-a,subnet-b -c security_group_id=sg-xxx
```

### Option 3: Environment Variables

```bash
export SCAN_REGIONS="us-east-1,eu-west-1,ap-southeast-1"
export TARGET_ACCOUNTS='[{"account_id":"111","role_arn":"arn:aws:iam::111:role/AuroraPatchAgentRole","regions":["us-east-1"]}]'
export BEDROCK_REGION="us-east-1"
export DB_IAM_AUTH="true"
```

## Multi-Account Fleet Management

Scan and patch Aurora clusters across multiple AWS accounts from a single deployment.

### Setup

1. **Deploy the orchestrator** (this stack) in your central/tools account
2. **Deploy the cross-account role** in each target account:

```bash
# In each target account:
aws cloudformation deploy \
  --template-file cdk/cross_account_role.yaml \
  --stack-name aurora-patch-agent-role \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides OrchestratorAccountId=<your-tools-account-id>

# For scan-only (no write permissions):
aws cloudformation deploy \
  --template-file cdk/cross_account_role.yaml \
  --stack-name aurora-patch-agent-role \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    OrchestratorAccountId=<your-tools-account-id> \
    ReadOnlyMode=true
```

3. **Configure target accounts** in `deploy-config.json`:

```json
{
  "multi_account": {
    "enabled": true,
    "role_name": "AuroraPatchAgentRole",
    "accounts": [
      {
        "account_id": "111111111111",
        "role_arn": "arn:aws:iam::111111111111:role/AuroraPatchAgentRole",
        "regions": ["us-east-1", "us-west-2"],
        "alias": "production",
        "environment": "production"
      }
    ]
  }
}
```

4. **Or use Organizations auto-discovery**:

```json
{
  "scan": { "enable_org_discovery": true }
}
```

### Cross-Account Role Permissions

| Mode | Permissions | Use Case |
|---|---|---|
| Read-Only | Describe clusters, CloudWatch metrics, Health API | Scan + assess only |
| Full Access | + Blue/Green create/switchover/delete, ModifyDBCluster | Scan + execute upgrades |

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

## CDK Context Parameters

| Parameter | Required | Description |
|---|---|---|
| `vpc_id` | Yes | VPC where Lambda runs (should be same VPC as your Aurora clusters for traffic gen) |
| `subnet_ids` | Yes | Comma-separated public subnet IDs (NAT Gateway placed in first one) |
| `security_group_id` | Yes | Security group for Lambda (needs outbound HTTPS; optionally PG 5432 for traffic gen) |
| `private_subnet_cidrs` | No | Two CIDR blocks for Lambda private subnets (default: `10.0.200.0/24,10.0.201.0/24`) |
| `region` | No | AWS region (default: us-east-2) |
| `deploy_infra` | No | Set to `"false"` to skip orchestration stack (default: `"true"`) |
| `db_secret_arn` | No | Secrets Manager ARN for DB credentials (for traffic gen/baseline) |
| `db_user` | No | DB username (alternative to secret) |
| `db_password` | No | DB password (alternative to secret) |

## UI Features

### Dashboard
- Fleet metrics: total clusters, pending upgrades, compliance %, active upgrades
- Security & CVE posture: live NVD scan with urgency scoring
- CVE-exposed clusters with one-click patch buttons
- Active upgrades with real-time progress
- Recent activity timeline

### Fleet Tab
- Full cluster inventory (version, environment, status, upgrade targets)
- **CVE Risk** column showing exposure per cluster
- **Patch CVE** button for immediate vulnerability-driven upgrades
- **In-Place** upgrade button for clusters that don't support Blue/Green (PG < 14.6)
- Guardrails modal with pre-flight checks before any upgrade

### Upgrades Tab
- Monitor Blue/Green deployments and in-place upgrades in progress
- **AI Diagnose** button on failures — Bedrock analyzes the error and suggests fixes
- **Apply Fix** button to auto-remediate (e.g., missing IAM permissions)
- Real-time status polling (10s interval)

### Cluster Detail Panel
- Click any cluster to see deep analysis
- **Version Changes** button: AI-powered drift analysis between current and target version
  - Parameter default changes (with old/new values and impact level)
  - Behavioral changes and mitigations
  - Deprecated/removed features
  - New features and benefits
  - Performance impact predictions
  - Prioritized recommendations
- Baseline capture and comparison
- Database validation

### AI Advisor Tab
- Natural language Q&A about fleet status, upgrade strategies, CVE exposure
- Model-powered recommendations with risk assessment

### Settings
- Runtime model selector (Sonnet 5 / Sonnet 4.6 / Haiku 4.5 / Opus 4.8)

## Upgrade Strategies

| Strategy | When Used | Downtime |
|---|---|---|
| **Blue/Green** | PG >= 14.6 or MySQL with binlog enabled (default for prod) | Seconds (during switchover) |
| **In-Place** | PG < 14.6, MySQL minor patches, or user choice | Minutes (cluster restart) |

The agent automatically suggests the appropriate strategy based on engine type, version, and environment. Users can override via the UI.

### MySQL-Specific Notes
- **Blue/Green requires**: `binlog_format` enabled in the cluster parameter group
- **Aurora MySQL v2 → v3** (5.7 → 8.0): Major version jump with breaking changes (reserved keywords, charset, auth plugin changes)
- **Aurora MySQL v2 EOL**: Standard support ended Oct 2024 — extended support charges apply

## Security Scanning

Integrates with:
- **NVD API** (NIST): Live CVE lookups for PostgreSQL and MySQL with 6-hour cache (persisted to DynamoDB)
- **AWS Security Bulletins**: Aurora-specific advisories
- **Known CVE databases**: 5 PostgreSQL CVEs + 8 MySQL CVEs (static fallback)
- **Urgency scoring**: Critical (>=60), High (>=35), Medium (>=15) based on CVE severity, EOL proximity, version drift

## AI-Powered Error Diagnosis

When an upgrade fails, the AI diagnosis system:
1. Checks learned fixes from past successful resolutions (instant, no AI call)
2. Matches known error patterns against a static remediation map (instant)
3. Falls back to Bedrock Claude for novel errors (analyzes root cause, suggests fix)
4. Provides one-click "Apply Fix" for actionable remediations (e.g., adding IAM permissions, rebooting instances)
5. Successful fixes are persisted — next occurrence resolves instantly

## Traffic Generator (Optional)

Generates realistic e-commerce workload to build performance baselines per environment:

| Environment | Connections | Queries/Cycle | Profile |
|---|---|---|---|
| Dev | 3 | 20 | Light, simple reads |
| Staging | 8 | 80 | Mixed read/write |
| Production | 15 | 200 | Heavy analytics |

Runs every 15 minutes via EventBridge. Requires DB credentials (via `db_user`/`db_password` or `db_secret_arn`).

## Project Structure

```
sample-agentic-aurora-fleet-patching/
├── cdk/                          # CDK infrastructure
│   ├── app.py                    #   Entry point (deploy_infra flag)
│   ├── cdk.json.example          #   Template — copy to cdk.json
│   ├── cross_account_role.yaml   #   CloudFormation for target account IAM role
│   └── stacks/
│       ├── ui_stack.py           #   Lambda, API GW, VPC, traffic gen
│       └── patch_agent_stack.py  #   DynamoDB, SNS, StepFunctions (optional)
├── api/
│   └── server.py                 # FastAPI backend (all endpoints)
├── agents/
│   ├── scanner/                  # Fleet inventory, CVE scanning
│   ├── analyzer/                 # Compatibility, baselines
│   ├── planner/                  # Wave planning
│   ├── advisor/                  # AI advisor (NL Q&A)
│   ├── execution/                # Blue/Green + In-Place upgrades
│   ├── validation/               # Post-upgrade health
│   ├── rollback/                 # Auto-revert
│   └── supervisor/               # Orchestration
├── shared/
│   ├── config.py                 # Environment-driven configuration
│   ├── aws_clients.py            # Cached boto3 clients
│   ├── models.py                 # Shared data models
│   ├── multi_account.py          # Cross-account session management
│   ├── iam_auth.py               # IAM database authentication
│   └── cve_cache.py              # Persistent CVE cache (DynamoDB)
├── ui/
│   └── src/App.jsx               # Full SPA (React)
├── traffic-gen/
│   └── handler.py                # Workload generator (EventBridge Lambda)
├── scripts/
│   ├── build-lambda.sh           # Build .lambda-build package
│   ├── deploy.sh                 # Portable deployment script
│   └── tag-resources.sh          # Tag resources (auto-delete=no)
├── tests/
├── deploy-config.example.json    # Portable deployment config template
├── pyproject.toml                # Python packaging & tool config
└── README.md
```

## Tech Stack

| Layer | Technology |
|---|---|
| UI | React + Vite (served via Lambda static mount) |
| API | FastAPI + Mangum on Lambda behind API Gateway |
| AI | Amazon Bedrock — Claude Sonnet 4.5 (default), runtime-selectable |
| Compute | Python 3.12 Lambda (VPC-attached, private subnets + NAT) |
| State | DynamoDB (upgrade history, learned fixes, CVE cache, recommendations) |
| Databases | Aurora PostgreSQL + Aurora MySQL (any version) |
| IaC | AWS CDK (Python) |

## Development

```bash
# Run API locally
cd api && uvicorn server:app --port 8000 --reload

# Run UI locally (proxies /api to localhost:8000)
cd ui && npm run dev
```

## Updating Deployed Lambda (without CDK)

When CDK doesn't detect code changes:

```bash
# Rebuild and push directly
bash scripts/build-lambda.sh
cd .lambda-build && zip -r /tmp/lambda-build.zip . && cd ..
aws lambda update-function-code \
  --function-name aurora-patch-agent-api \
  --zip-file fileb:///tmp/lambda-build.zip \
  --region us-east-2
```

## Estimated Monthly Cost

| Component | Cost |
|---|---|
| Lambda (API + traffic gen) | ~$5 |
| NAT Gateway | ~$35 |
| API Gateway | ~$1 |
| Bedrock inference | ~$5-10 |
| DynamoDB (if deployed) | ~$1 |
| **Total (scan-only)** | **~$45/month** |
| **Total (full + demo clusters)** | **~$85/month** |
