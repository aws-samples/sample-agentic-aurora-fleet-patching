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

## What Works Out of the Box (read this first)

A default deployment is deliberately conservative: until you configure an API
token, it can't run scans, analysis, or upgrades, and it can't change your fleet
until you also grant extra permissions yourself. Some features also have known
limitations (see [Known Limitations and Workarounds](#known-limitations-and-workarounds)).

**Set an API token first.** The API is fail-closed: every `POST`/`PUT`/`DELETE`
request needs the token, and that includes **Scan Fleet**, the CVE scan,
analysis, Version Changes, the AI Advisor, and AI Diagnose, not just upgrades.
Without a token, the UI can only show results that were already stored.
See [API Authentication](#running-upgrades-api-authentication-important).

| Capability | With an API token set | To enable |
|---|---|---|
| Fleet scan, environment classification from tags | ✅ Works | — |
| CVE posture and urgency scoring | ✅ Works | — |
| Version Changes, cluster analysis, AI Advisor, AI Diagnose | ✅ Works if Bedrock model access is enabled | [Bedrock prerequisites](#what-you-need) |
| Viewing stored fleet, upgrade history, and maintenance windows | ✅ Works even without a token (see [Your UI URL](#your-ui-url)) | — |
| In-place upgrade | ✅ Works on clusters without **Copy tags to snapshots**. On clusters with it (the console default), the pre-upgrade snapshot needs `rds:AddTagsToResource`, so the upgrade stops safely at the snapshot step | [Enabling Blue/Green Execution](#enabling-bluegreen-execution-additional-iam-opt-in) (same opt-in policy) |
| Blue/Green create, switchover, delete | ❌ Fails with `AccessDenied` | [Enabling Blue/Green Execution](#enabling-bluegreen-execution-additional-iam-opt-in) |
| **Apply Fix**: reboot writer instance | ❌ Fails with `AccessDenied` | Same opt-in policy (`rds:RebootDBInstance`) |
| **Apply Fix**: grant a missing IAM permission | Only self-grants **read-only** actions | Grant write actions yourself |
| Capture Baseline, DB Validation, live database metadata | ❌ Needs database access | DB credentials and a network path: [Traffic Generator](#traffic-generator-optional) and [IAM Database Authentication](#iam-database-authentication) |
| Traffic generator | Deployed and scheduled; does nothing until DB credentials are set | Read the warning in [Traffic Generator](#traffic-generator-optional) first |
| Multi-account scanning | ✅ With the cross-account role | [Multi-Account Fleet Management](#multi-account-fleet-management) |
| Multi-account Blue/Green | ❌ Fails with `AccessDenied` | [Cross-account opt-in policy](#multi-account-the-cross-account-role-in-each-target-account) |
| Upgrades outside the default change windows | ❌ Blocked by the change-window guardrail | Set windows in the **Maintenance** tab, or select **Skip window check** |
| Scanning more than one Region | Scans the Region the agent is deployed in | Set `scan.regions` in `deploy-config.json` (or `SCAN_REGIONS`) |

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

The output URL ([Your UI URL](#your-ui-url)) gives you the full UI. To scan your fleet, view CVE exposure, analyze version drift, or run upgrades, first set an API token ([API Authentication](#running-upgrades-api-authentication-important)); Blue/Green upgrades also need the opt-in permissions in [Enabling Blue/Green Execution](#enabling-bluegreen-execution-additional-iam-opt-in).

> **Note:** "Scan-only" here means the orchestration stack isn't deployed. The
> UI stack still grants the agent its upgrade permissions; for read-only access
> to other accounts, use the cross-account role with `ReadOnlyMode=true`.

## Full Deployment (with Orchestration)

Includes DynamoDB state table, SNS notifications, StepFunctions orchestration, and scheduled discovery:

```bash
cdk deploy --all \
  -c vpc_id=vpc-xxx \
  -c subnet_ids=subnet-aaa,subnet-bbb \
  -c security_group_id=sg-xxx
```

## Your UI URL

The UI and API are served by an Amazon API Gateway endpoint created by
`AuroraPatchAgentUiStack`. The URL has this form:

```
https://<api-id>.execute-api.<region>.amazonaws.com/prod/
```

`<api-id>` is assigned by API Gateway and is different for every deployment;
keep the trailing `/prod/` (the API lives under `/prod/api/...`). Find it in the
`cdk deploy` output (`AuroraPatchAgentUiStack.UiUrl`), in the CloudFormation
console (**AuroraPatchAgentUiStack** > **Outputs** > `UiUrl`), or with:

```bash
aws cloudformation describe-stacks --region <region> --stack-name AuroraPatchAgentUiStack \
  --query "Stacks[0].Outputs[?OutputKey=='UiUrl'].OutputValue" --output text
```

The URL changes if you delete and recreate the stack. The sample doesn't set up
a custom domain; add an API Gateway custom domain if you need one.

> **⚠️ Treat the URL as sensitive.** The endpoint is reachable from the
> internet, and there is no sign-in in front of the UI. State-changing and
> scanning requests require the API token, but **`GET` requests don't**: anyone
> who has the URL can read the stored fleet inventory (account IDs, cluster
> names, engine versions, extensions), upgrade history, and maintenance
> windows. Don't share the URL, and for anything beyond a sandbox account,
> restrict access to the API, for example with an
> [API Gateway resource policy](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-resource-policies.html)
> limited to your network, or put it behind your organization's sign-in.

## What You Need

| Requirement | Notes |
|---|---|
| AWS account with Aurora clusters | PostgreSQL and/or MySQL, any version. The agent scans the Region it's deployed in, or the Regions in `scan.regions` / `SCAN_REGIONS` |
| Amazon Bedrock model access | Anthropic **Claude Sonnet 4.5** enabled in the account. The default model ID is the US cross-Region inference profile (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`), which works only when the agent runs in a US Region; elsewhere set `bedrock.region` / `bedrock.model_id` in `deploy-config.json`. Without model access, scanning still works but every AI feature fails. |
| VPC with public subnets | For NAT Gateway placement (Lambda needs internet for Bedrock/NVD API). Pass two public subnets, the first in AZ `<region>a` and the second in `<region>b` (the stack assumes this order), and leave `10.0.200.0/24` and `10.0.201.0/24` free (or set `private_subnet_cidrs`) for the Lambda private subnets the stack creates |
| Security group | Must allow outbound HTTPS; if connecting to clusters for traffic gen, needs inbound 5432 (PG) or 3306 (MySQL) rule |
| AWS CLI configured | With permissions for RDS, Lambda, API Gateway, Bedrock, IAM, EC2 |
| Node.js 18+ | For UI build |
| Python 3.12+ | For CDK and Lambda |
| AWS CDK CLI | `npm install -g aws-cdk` |

## Running Upgrades: API Authentication (important)

The API is **fail-closed by design.** Every `POST`, `PUT`, `PATCH`, and
`DELETE` request is **blocked by default**: not only starting an upgrade,
switchover, rollback, or deletion, but also **Scan Fleet**, the CVE scan,
analysis, Version Changes, the AI Advisor, AI Diagnose, and saving settings.
This means a freshly deployed instance can't be driven anonymously by anyone who
finds its URL. Only `GET` requests (viewing stored results) work without
authentication. The buttons are always visible in the UI; whether the backend
honors them depends on how you configure auth.

Pick one of these before you use the agent:

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

If you set neither, every `POST`/`PUT`/`PATCH`/`DELETE` request (including
scans and analysis) returns `503` with a message explaining how to enable them;
only viewing stored results works.

## Enabling Blue/Green Execution (additional IAM, opt-in)

The stacks in this repo grant the agent only the permissions its code calls
directly. **Blue/Green execution needs a few more, and they are not granted by
default.** RDS checks them on the caller's behalf while it creates, switches
over, and deletes a Blue/Green deployment, even though the agent never calls
these APIs itself. You decide whether to add them.

**Without these permissions:** scanning, analysis, and the AI Advisor work.
Upgrades stop safely before changing anything:

* Every upgrade (Blue/Green and in-place) first takes a pre-upgrade snapshot as
  its rollback anchor. On clusters with **Copy tags to snapshots** enabled (the
  console default), RDS tags that snapshot, which needs `rds:AddTagsToResource`,
  so the upgrade stops at the snapshot step. On clusters without it, in-place
  upgrades work.
* Creating, switching over, or deleting a Blue/Green deployment fails with
  `AccessDenied`.

The **Upgrades** tab shows the error, and **AI Diagnose** names the denied
action. **Apply Fix** won't grant it for you, because it only self-grants
read-only actions.

| Permission | Needed for | Why |
|---|---|---|
| `rds:AddTagsToResource` | Blue/Green create; the pre-upgrade snapshot of every upgrade | RDS copies the source cluster's tags onto the green resources, and onto snapshots of clusters with **Copy tags to snapshots** enabled. The agent also tags its pre-upgrade snapshots. |
| `rds:CreateDBClusterEndpoint` | Blue/Green create | RDS recreates custom cluster endpoints in the green environment. |
| `rds:ModifyDBInstance` | Blue/Green switchover | RDS renames the blue and green instances during switchover. |
| `rds:DeleteDBClusterEndpoint` | Deleting a Blue/Green deployment | Removes the green environment's endpoints. |
| `rds:DeleteGlobalCluster` | Deleting a Blue/Green deployment of an Aurora Global Database | Global Database clusters only. |
| `rds:RebootDBInstance` | The **reboot** Apply Fix and the `reboot_db_instance` change-plan step | Applies pending parameter-group changes. |

These follow AWS's
[Authorizing access to Aurora blue/green deployment operations](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/blue-green-deployments-authorizing-access.html).
`rds:ModifyDBInstance` isn't listed on that page but is required for switchover
in practice. Resources are scoped to your account (and Region, for a single
account). Aurora names green resources internally, so only `*` patterns work
for the resource IDs, not name prefixes.

Before you add these, also read the Blue/Green items in
[Known Limitations and Workarounds](#known-limitations-and-workarounds).

### Single account (the agent's own Lambda role)

After deploying the stack, attach this inline policy to the API function's role:

```bash
REGION=us-east-2   # the Region you deployed to
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ROLE=$(aws lambda get-function-configuration --region "$REGION" \
  --function-name aurora-patch-agent-api --query Role --output text | cut -d/ -f2)

cat > /tmp/aurora-patch-agent-bluegreen.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BlueGreenActionsCheckedByRds",
      "Effect": "Allow",
      "Action": [
        "rds:AddTagsToResource",
        "rds:CreateDBClusterEndpoint",
        "rds:DeleteDBClusterEndpoint",
        "rds:ModifyDBInstance"
      ],
      "Resource": [
        "arn:aws:rds:${REGION}:${ACCOUNT_ID}:cluster:*",
        "arn:aws:rds:${REGION}:${ACCOUNT_ID}:db:*",
        "arn:aws:rds:${REGION}:${ACCOUNT_ID}:cluster-endpoint:*",
        "arn:aws:rds:${REGION}:${ACCOUNT_ID}:cluster-snapshot:*",
        "arn:aws:rds:${REGION}:${ACCOUNT_ID}:deployment:*"
      ]
    },
    {
      "Sid": "RebootForApplyFix",
      "Effect": "Allow",
      "Action": "rds:RebootDBInstance",
      "Resource": "arn:aws:rds:${REGION}:${ACCOUNT_ID}:db:*"
    },
    {
      "Sid": "GlobalDatabaseBlueGreenDelete",
      "Effect": "Allow",
      "Action": "rds:DeleteGlobalCluster",
      "Resource": "arn:aws:rds::${ACCOUNT_ID}:global-cluster:*"
    }
  ]
}
EOF

aws iam put-role-policy --role-name "$ROLE" \
  --policy-name AuroraPatchAgentBlueGreenExecution \
  --policy-document file:///tmp/aurora-patch-agent-bluegreen.json
```

To turn it off again, and **before you delete the stack** (CloudFormation can't
delete a role that still has a policy it didn't create):

```bash
aws iam delete-role-policy --role-name "$ROLE" --policy-name AuroraPatchAgentBlueGreenExecution
```

### Multi-account (the cross-account role in each target account)

The `cdk/cross_account_role.yaml` write policy doesn't cover the Blue/Green
lifecycle by itself. Its Blue/Green statement is scoped to a
`blue-green-deployment:*` resource type, but RDS deployment ARNs use
`deployment:*`, so switchover and delete aren't authorized. In each target
account where you want Blue/Green execution (not needed for `ReadOnlyMode=true`
accounts), attach this policy to `AuroraPatchAgentRole`:

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

cat > /tmp/aurora-patch-agent-cross-account-bluegreen.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "BlueGreenDeploymentLifecycle",
      "Effect": "Allow",
      "Action": [
        "rds:CreateBlueGreenDeployment",
        "rds:SwitchoverBlueGreenDeployment",
        "rds:DeleteBlueGreenDeployment"
      ],
      "Resource": [
        "arn:aws:rds:*:${ACCOUNT_ID}:cluster:*",
        "arn:aws:rds:*:${ACCOUNT_ID}:deployment:*"
      ]
    },
    {
      "Sid": "BlueGreenActionsCheckedByRds",
      "Effect": "Allow",
      "Action": [
        "rds:AddTagsToResource",
        "rds:CreateDBCluster",
        "rds:CreateDBInstance",
        "rds:CreateDBClusterEndpoint",
        "rds:PromoteReadReplicaDBCluster",
        "rds:ModifyDBInstance",
        "rds:DeleteDBCluster",
        "rds:DeleteDBInstance",
        "rds:DeleteDBClusterEndpoint",
        "rds:RebootDBInstance"
      ],
      "Resource": [
        "arn:aws:rds:*:${ACCOUNT_ID}:cluster:*",
        "arn:aws:rds:*:${ACCOUNT_ID}:db:*",
        "arn:aws:rds:*:${ACCOUNT_ID}:cluster-endpoint:*",
        "arn:aws:rds:*:${ACCOUNT_ID}:cluster-snapshot:*",
        "arn:aws:rds:*:${ACCOUNT_ID}:cluster-pg:*",
        "arn:aws:rds:*:${ACCOUNT_ID}:subgrp:*",
        "arn:aws:rds:*:${ACCOUNT_ID}:deployment:*"
      ]
    }
  ]
}
EOF

aws iam put-role-policy --role-name AuroraPatchAgentRole \
  --policy-name AuroraPatchAgentBlueGreenExecution \
  --policy-document file:///tmp/aurora-patch-agent-cross-account-bluegreen.json
```

> A full Blue/Green create, switchover, and delete succeeded in a test account
> with the single-account permissions above. The Global Database statement and
> the cross-account policy follow the same AWS documentation but have not been
> tested end to end; validate them in a non-production account first.

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
| Full Access | + ModifyDBCluster, snapshots, Blue/Green API actions | Scan + in-place upgrades |
| Full Access + opt-in policy | + the policy in [Enabling Blue/Green Execution](#multi-account-the-cross-account-role-in-each-target-account) | Scan + Blue/Green and in-place upgrades |

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
- **Patch CVE** button for immediate vulnerability-driven upgrades (shown only for clusters with CVE exposure)
- **In-Place** upgrade button for clusters that don't support Blue/Green (PG < 14.6)
- Guardrails modal with pre-flight checks before any upgrade

### Upgrades Tab
- Monitor Blue/Green deployments and in-place upgrades in progress
- **AI Diagnose** button on failures — Bedrock analyzes the error and suggests fixes
- **Apply Fix** button to auto-remediate selected errors (it only self-grants **read-only** IAM actions; write permissions must be granted by you)
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
- Runtime model selector (Sonnet 4.5 default; the list also shows Sonnet 5 / Sonnet 4.6 / Haiku 4.5 / Opus 4.8). Only pick a model your account has Bedrock access to in the agent's Region; otherwise AI calls fail.

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

### PostgreSQL-Specific Notes
- **Blue/Green requires**: a custom DB cluster parameter group with `rds.logical_replication = 1`, and a **reboot of the writer instance** after you set it (the default parameter group can't be modified). Without it, Blue/Green create fails with `SourceClusterNotSupportedFault`.

## Known Limitations and Workarounds

Read these before running upgrades.

| Limitation | What you see | Workaround |
|---|---|---|
| **Default change windows:** production Sat–Sun 04:00–07:00 UTC, staging Mon–Fri 02:00–08:00 UTC, dev anytime. | The Change Window check fails and the upgrade is blocked. | Set the windows in the **Maintenance** tab, or select **Skip window check** in the guardrails dialog. |
| **Check the upgrade target.** When a cluster is already on the newest minor version, the suggested target can be the newest **major** version (for example, 15.19 → 18.6). | The guardrails dialog header shows a major-version jump. | Read the version in the dialog header before you choose **Proceed with Upgrade**. Use **Version Changes** in the cluster panel to review a major upgrade first. |
| **Schema changes cancel Blue/Green switchovers.** Blue/Green replicates with logical replication, which doesn't carry DDL. Any DDL on the source cluster after the green environment is created, even `CREATE ... IF NOT EXISTS`, makes RDS cancel the switchover, and the deployment must be deleted and created again. The agent's own tools (the traffic generator and **Capture Baseline**) only run DDL when their objects don't exist yet, but your application's migrations count too. | RDS event: "Switchover ... was cancelled because there are DDL or Large Object changes ... that can't be replicated". | Pause schema migrations on the source cluster until the switchover completes. Enable the traffic generator and capture baselines *before* creating a Blue/Green deployment, not during it. |
| **Major-version Blue/Green uses the default parameter group on green.** For minor upgrades the green cluster keeps your custom cluster parameter group; for a major upgrade it can't (the parameter-group family changes), so green gets the new family's default group. | After switchover, the cluster shows `default.aurora-...`. | Before switchover, **Modify** the green cluster in the RDS console to use a custom parameter group for the new family (apply immediately) and reboot the green writer. |
| **Aurora MySQL CVE data is a static list.** PostgreSQL CVEs come from the NVD API; MySQL uses a built-in list of 8 CVEs. | MySQL clusters are only flagged for CVEs on that list. | Check the [Aurora MySQL release notes](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraMySQLReleaseNotes/AuroraMySQL.Updates.30Updates.html) for newer security fixes. |
| **AWS Health events need Business or Enterprise Support.** | Logs show `SubscriptionRequiredException`; scoring skips Health events. | None needed; CVE and end-of-life scoring still work. |

## Security Scanning

Integrates with:
- **NVD API** (NIST): Live CVE lookups for PostgreSQL with 6-hour cache (persisted to DynamoDB). MySQL uses the static list only (see [Known Limitations](#known-limitations-and-workarounds)).
- **AWS Security Bulletins**: Aurora-specific advisories
- **Known CVE databases**: 5 PostgreSQL CVEs + 8 MySQL CVEs (static fallback)
- **Urgency scoring**: Critical (>=60), High (>=35), Medium (>=15) based on CVE severity, EOL proximity, version drift

## AI-Powered Error Diagnosis

When an upgrade fails, the AI diagnosis system:
1. Checks learned fixes from past successful resolutions (instant, no AI call)
2. Matches known error patterns against a static remediation map (instant)
3. Falls back to Bedrock Claude for novel errors (analyzes root cause, suggests fix)
4. Provides one-click "Apply Fix" for actionable remediations. Self-granted IAM fixes are limited to **read-only** actions; rebooting an instance needs the opt-in `rds:RebootDBInstance` permission ([Enabling Blue/Green Execution](#enabling-bluegreen-execution-additional-iam-opt-in))
5. Successful fixes are persisted — next occurrence resolves instantly

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
> [Known Limitations](#known-limitations-and-workarounds)), so enable it before
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

> The UI uses Vite 5. Open advisories for `vite` and its bundled `esbuild`
> affect the **development server** (`npm run dev`) only; fixing them requires a
> major Vite upgrade. The deployed UI is a static build (`npm run build`) served
> by the Lambda function and doesn't run the dev server. Run `npm run dev` only
> on `localhost`, never on a shared network.

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

## Cleanup

1. Remove the opt-in policy if you added it (CloudFormation can't delete a role
   that still has a policy it didn't create):
   `aws iam delete-role-policy --role-name <api-function-role> --policy-name AuroraPatchAgentBlueGreenExecution`
2. Delete any Blue/Green deployments the agent created, and any leftover
   `<cluster>-old1` clusters after a switchover. The agent doesn't delete these on
   stack deletion, and they keep accruing instance charges.
3. Delete the stacks: `cdk destroy --all` (or `cdk destroy AuroraPatchAgentUiStack`).
4. Delete the DynamoDB table `aurora-patch-agent-ui-state`. It is retained on
   stack deletion so upgrade history isn't lost by accident.
5. Review snapshots the agent created (`<cluster>-pre-upgrade-*` before each
   upgrade, `<cluster>-final-*` from **Delete Old**) and delete the ones you no
   longer need.
6. If you enabled the traffic generator, drop its tables from the clusters it
   ran against (see [Traffic Generator](#traffic-generator-optional)).

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

Aurora instances are billed separately. A Blue/Green deployment runs a full copy
of the cluster (the green environment) until you switch over and delete the old
cluster, so instance cost roughly doubles for that time.
