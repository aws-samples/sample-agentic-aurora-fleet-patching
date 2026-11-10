# Deployment and configuration

[Back to README](../README.md)

## What You Need

| Requirement | Notes |
|---|---|
| AWS account with Aurora clusters | PostgreSQL and/or MySQL, any version. The agent scans the Region it's deployed in, or the Regions in `scan.regions` in `deploy-config.json` |
| Amazon Bedrock model access | Anthropic **Claude Sonnet 4.6** enabled in the account. The default model ID is the US cross-Region inference profile (`us.anthropic.claude-sonnet-4-6`), which works only when the agent runs in a US Region; elsewhere set `bedrock.region` / `bedrock.model_id` in `deploy-config.json`. Without model access, scanning still works but every AI feature fails. |
| VPC with public subnets | For NAT Gateway placement (Lambda needs internet for Bedrock/NVD API). Pass two public subnets, the first in AZ `<region>a` and the second in `<region>b` (the stack assumes this order), and leave `10.0.200.0/24` and `10.0.201.0/24` free (or set `private_subnet_cidrs`; required for the default VPC, see the Quick Start tips) for the Lambda private subnets the stack creates |
| Security group | Must allow outbound HTTPS; if connecting to clusters for traffic gen, needs inbound 5432 (PG) or 3306 (MySQL) rule |
| AWS CLI configured | With permissions for RDS, Lambda, API Gateway, Bedrock, IAM, EC2 |
| Node.js 18+ (22 or 24 recommended) | For UI build. The CDK CLI warns on other Node versions, such as 26, but still works |
| Python 3.12+ | For CDK and Lambda |
| AWS CDK CLI | `npm install -g aws-cdk` (use the latest: `cdk/requirements.txt` pins `aws-cdk-lib`, and an older CLI fails with "Cloud assembly schema version mismatch"). Run `cdk bootstrap` once per account and Region |

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

## Full Deployment (with Orchestration)

Includes DynamoDB state table, SNS notifications, StepFunctions orchestration, and scheduled discovery:

```bash
cdk deploy --all \
  -c vpc_id=vpc-xxx \
  -c subnet_ids=subnet-aaa,subnet-bbb \
  -c security_group_id=sg-xxx
```

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

These are the settings the Lambda function reads when it runs. Exporting them in your shell before `cdk deploy` has no effect. To set them, use `deploy-config.json` (Option 1), or edit the function's environment variables in the Lambda console.


```bash
export SCAN_REGIONS="us-east-1,eu-west-1,ap-southeast-1"
export TARGET_ACCOUNTS='[{"account_id":"111","role_arn":"arn:aws:iam::111:role/AuroraPatchAgentRole","regions":["us-east-1"]}]'
export BEDROCK_REGION="us-east-1"
export DB_IAM_AUTH="true"
```

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
