# Enabling Blue/Green Execution (additional IAM, opt-in)

[Back to README](../README.md)


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
[Known Limitations and Workarounds](known-limitations.md#known-limitations-and-workarounds).

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
