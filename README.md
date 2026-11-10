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

## Quick Start (Scan-Only Mode)

Deploys the UI and API so you can scan your existing clusters. It takes about 5 minutes. It costs about $45 a month while it runs, mostly for a NAT gateway ([cost breakdown](docs/deployment.md#estimated-monthly-cost)); [Cleanup](#cleanup) removes everything.

**Before you start**

- The AWS CLI is signed in to the account that has your Aurora clusters (`aws sts get-caller-identity` works).
- Anthropic Claude Sonnet 4.6 is available to you in Amazon Bedrock. Without it, scanning works but the AI features fail.
- Node.js 22 or 24, Python 3.12+, and the CDK CLI (`npm install -g aws-cdk`).

Run these in one terminal, in order.

```bash
# 1. Pick a Region and look up your network. This finds your default VPC.
#    Using your own VPC instead? Set VPC_ID, SG_ID, SUBNET_IDS (two public subnets,
#    first in AZ "a", second in AZ "b") and PRIVATE_CIDRS (two unused ranges inside
#    the VPC's address range) yourself.
export AWS_REGION=us-east-2        # change to your Region
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
VPC_ID=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
SUBNET_IDS=$(aws ec2 describe-subnets \
  --filters Name=vpc-id,Values=$VPC_ID Name=availability-zone,Values=${AWS_REGION}a,${AWS_REGION}b Name=default-for-az,Values=true \
  --query 'sort_by(Subnets,&AvailabilityZone)[].SubnetId' --output text | tr '\t' ',')
SG_ID=$(aws ec2 describe-security-groups \
  --filters Name=vpc-id,Values=$VPC_ID Name=group-name,Values=default \
  --query 'SecurityGroups[0].GroupId' --output text)
PRIVATE_CIDRS=172.31.200.0/24,172.31.201.0/24

# 2. Make a secret token. The API refuses scans and upgrades without it.
#    Save it somewhere safe: you need the same token if you deploy again.
export API_AUTH_TOKEN=$(openssl rand -hex 24)

# 3. Get the code and build it
git clone https://github.com/aws-samples/sample-agentic-aurora-fleet-patching.git
cd sample-agentic-aurora-fleet-patching
(cd ui && npm install && VITE_API_AUTH_TOKEN=$API_AUTH_TOKEN npm run build)
bash scripts/build-lambda.sh

# 4. Set up CDK (bootstrap is needed once per account and Region)
cd cdk
cp cdk.json.example cdk.json
pip install -r requirements.txt
cdk bootstrap aws://$ACCOUNT_ID/$AWS_REGION

# 5. Deploy
cdk deploy AuroraPatchAgentUiStack \
  -c region=$AWS_REGION \
  -c vpc_id=$VPC_ID \
  -c subnet_ids=$SUBNET_IDS \
  -c security_group_id=$SG_ID \
  -c private_subnet_cidrs=$PRIVATE_CIDRS \
  -c deploy_infra=false \
  -c api_auth_token=$API_AUTH_TOKEN
```

When it finishes, open the `UiUrl` from the output and choose **Scan Fleet**.

**If something goes wrong**

- **Scan Fleet shows nothing.** The agent scans only the Region it's deployed in. To scan more, create `deploy-config.json` in the repo root, then run step 5 again:

  ```json
  { "scan": { "regions": ["us-east-1", "us-west-1"] } }
  ```

  Regions that can't be reached (for example an opt-in Region that isn't enabled) are skipped.
- **Scan Fleet fails with 401 or 503.** The token is missing or different from the one you deployed with. See [API Authentication](docs/api-authentication.md#running-upgrades-api-authentication-important).
- **The deploy failed and rolled back.** Fix the error shown, delete the failed stack (`aws cloudformation delete-stack --stack-name AuroraPatchAgentUiStack`), and run step 5 again.

## Before You Run Upgrades

Scans and analysis only read your clusters. The upgrade buttons change them, and the agent has permission to do that from the start (the "scan-only" deploy only skips the optional orchestration stack). Check these first:

1. **Tag every cluster with `Environment`** (`production`, `staging`, or `dev`). It sets the change window. Untagged clusters are guessed from their name, and anything not recognized counts as `dev`, which can be upgraded at any time.
2. **Blue/Green upgrades need extra permissions** that aren't granted by default. See [Enabling Blue/Green execution](docs/blue-green-permissions.md#enabling-bluegreen-execution-additional-iam-opt-in).
3. **Check the target version** in the upgrade dialog. It can be a major-version jump.
4. **Pause schema changes** on a cluster while its Blue/Green deployment runs. Any DDL cancels the switchover.
5. **Keep the UI URL private.** Anyone who has it can see your fleet inventory, though changes still need the token.

The full list is in [Known limitations](docs/known-limitations.md#known-limitations-and-workarounds).

## Cleanup

If you only scanned, steps 3 and 4 are all you need.

1. Remove the opt-in policy if you added it (CloudFormation can't delete a role
   that still has a policy it didn't create):
   `aws iam delete-role-policy --role-name <api-function-role> --policy-name AuroraPatchAgentBlueGreenExecution`
2. Delete any Blue/Green deployments the agent created, and any leftover
   `<cluster>-old1` clusters after a switchover. The agent doesn't delete these on
   stack deletion, and they keep accruing instance charges.
3. Delete the stack, in the Region you deployed to:
   `aws cloudformation delete-stack --region <region> --stack-name AuroraPatchAgentUiStack`
   (also `AuroraPatchAgentStack` if you did the [full deployment](docs/deployment.md#full-deployment-with-orchestration)).
   Don't run a plain `cdk destroy`: it uses the Region in `cdk.json` (`us-east-2` by default), not the one you deployed to.
4. Delete the DynamoDB table `aurora-patch-agent-ui-state`. It is retained on
   stack deletion so upgrade history isn't lost by accident.
5. Review snapshots the agent created (`<cluster>-pre-upgrade-*` before each
   upgrade, `<cluster>-final-*` from **Delete Old**) and delete the ones you no
   longer need.
6. If you enabled the traffic generator, drop its tables from the clusters it
   ran against (see [Traffic Generator](docs/database-access.md#traffic-generator-optional)).

## Documentation

| Topic | Page |
|---|---|
| What works out of the box, UI features, upgrade strategies | [Capabilities](docs/capabilities.md) |
| Requirements, finding your UI URL, other ways to deploy, settings, cost | [Deployment](docs/deployment.md) |
| The API token | [API authentication](docs/api-authentication.md) |
| Permissions for Blue/Green upgrades (opt-in) | [Blue/Green permissions](docs/blue-green-permissions.md) |
| Scanning more than one AWS account | [Multi-account](docs/multi-account.md) |
| Database access and the traffic generator | [Database access](docs/database-access.md) |
| Known limitations | [Known limitations](docs/known-limitations.md) |
| Architecture and code layout | [Architecture](docs/architecture.md) |
| Running locally | [Development](docs/development.md) |

## Security

To report a security issue, see [SECURITY.md](SECURITY.md). Contributions: [CONTRIBUTING.md](CONTRIBUTING.md).

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
