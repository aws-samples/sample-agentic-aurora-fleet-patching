# Multi-Account Fleet Management

[Back to README](../README.md)


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
| Full Access + opt-in policy | + the policy in [Enabling Blue/Green Execution](blue-green-permissions.md#multi-account-the-cross-account-role-in-each-target-account) | Scan + Blue/Green and in-place upgrades |
