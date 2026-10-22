# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK Stack for Aurora Patch Agent UI + API — single Lambda behind API Gateway.

Required context values (set in cdk.json or via -c flag):
  - vpc_id: VPC where Aurora clusters reside
  - subnet_ids: Comma-separated public subnet IDs (for NAT Gateway placement)
  - security_group_id: Security group shared with Aurora clusters (must have self-referencing rule)
  - private_subnet_cidrs: (optional) Two CIDR blocks for new private subnets, default "10.0.200.0/24,10.0.201.0/24"
  - db_secret_arn: (optional) Secrets Manager ARN containing DB credentials
"""
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    Tags,
    CfnOutput,
    aws_lambda as lambda_,
    aws_apigateway as apigw,
    aws_iam as iam,
    aws_ec2 as ec2,
    aws_dynamodb as dynamodb,
    aws_secretsmanager as sm,
    aws_events as events,
    aws_events_targets as targets,
)
from constructs import Construct


class UiStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, deploy_config: dict = None, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        deploy_config = deploy_config or {}

        Tags.of(self).add("auto-delete", "no")
        Tags.of(self).add("project", "aurora-patch-agent")

        # ─── Read context parameters ──────────────────────────────────────
        vpc_id = self.node.try_get_context("vpc_id")
        subnet_ids_raw = self.node.try_get_context("subnet_ids") or ""
        security_group_id = self.node.try_get_context("security_group_id")
        private_cidrs_raw = self.node.try_get_context("private_subnet_cidrs") or "10.0.200.0/24,10.0.201.0/24"
        db_secret_arn = self.node.try_get_context("db_secret_arn") or ""

        if not vpc_id:
            raise ValueError(
                "Context value 'vpc_id' is required. "
                "Set it in cdk.json or pass via: cdk deploy -c vpc_id=vpc-xxx"
            )
        if not subnet_ids_raw:
            raise ValueError(
                "Context value 'subnet_ids' is required (comma-separated public subnet IDs). "
                "Set it in cdk.json or pass via: cdk deploy -c subnet_ids=subnet-aaa,subnet-bbb"
            )
        if not security_group_id:
            raise ValueError(
                "Context value 'security_group_id' is required (Aurora cluster security group with self-ref rule). "
                "Set it in cdk.json or pass via: cdk deploy -c security_group_id=sg-xxx"
            )

        public_subnet_ids = [s.strip() for s in subnet_ids_raw.split(",") if s.strip()]
        private_cidrs = [s.strip() for s in private_cidrs_raw.split(",") if s.strip()]

        if len(private_cidrs) < 2:
            raise ValueError("private_subnet_cidrs must contain at least 2 CIDR blocks (comma-separated)")

        # ─── Import existing VPC ──────────────────────────────────────────
        azs = [f"{self.region}a", f"{self.region}b", f"{self.region}c"][:len(public_subnet_ids)]
        vpc = ec2.Vpc.from_vpc_attributes(
            self, "AuroraVpc",
            vpc_id=vpc_id,
            availability_zones=azs,
            public_subnet_ids=public_subnet_ids,
        )

        # ─── NAT Gateway for Lambda outbound internet access ──────────────
        nat_eip = ec2.CfnEIP(self, "NatEip", domain="vpc")

        nat_gw = ec2.CfnNatGateway(
            self, "NatGateway",
            allocation_id=nat_eip.attr_allocation_id,
            subnet_id=public_subnet_ids[0],
        )

        # ─── Private subnets for Lambda placement ─────────────────────────
        private_subnet_a = ec2.CfnSubnet(
            self, "LambdaPrivateSubnetA",
            vpc_id=vpc_id,
            cidr_block=private_cidrs[0],
            availability_zone=azs[0],
            map_public_ip_on_launch=False,
        )
        Tags.of(private_subnet_a).add("Name", "aurora-patch-agent-lambda-private-a")

        private_subnet_b = ec2.CfnSubnet(
            self, "LambdaPrivateSubnetB",
            vpc_id=vpc_id,
            cidr_block=private_cidrs[1],
            availability_zone=azs[1 % len(azs)],
            map_public_ip_on_launch=False,
        )
        Tags.of(private_subnet_b).add("Name", "aurora-patch-agent-lambda-private-b")

        # Route table → NAT Gateway
        private_rt = ec2.CfnRouteTable(
            self, "LambdaPrivateRouteTable",
            vpc_id=vpc_id,
        )
        Tags.of(private_rt).add("Name", "aurora-patch-agent-lambda-private-rt")

        ec2.CfnRoute(
            self, "PrivateDefaultRoute",
            route_table_id=private_rt.ref,
            destination_cidr_block="0.0.0.0/0",
            nat_gateway_id=nat_gw.ref,
        )

        ec2.CfnSubnetRouteTableAssociation(
            self, "PrivateSubnetARtAssoc",
            subnet_id=private_subnet_a.ref,
            route_table_id=private_rt.ref,
        )
        ec2.CfnSubnetRouteTableAssociation(
            self, "PrivateSubnetBRtAssoc",
            subnet_id=private_subnet_b.ref,
            route_table_id=private_rt.ref,
        )

        private_subnets = ec2.SubnetSelection(
            subnets=[
                ec2.Subnet.from_subnet_id(self, "PrivateSubnetA", private_subnet_a.ref),
                ec2.Subnet.from_subnet_id(self, "PrivateSubnetB", private_subnet_b.ref),
            ]
        )

        # ─── Security group (shared with Aurora clusters) ─────────────────
        aurora_sg = ec2.SecurityGroup.from_security_group_id(
            self, "AuroraSg", security_group_id
        )

        # ─── DynamoDB State Table ─────────────────────────────────────────
        state_table = dynamodb.Table(
            self, "UiStateTable",
            table_name="aurora-patch-agent-ui-state",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
        )

        # ─── Lambda environment ───────────────────────────────────────────
        db_user = self.node.try_get_context("db_user") or ""
        db_password = self.node.try_get_context("db_password") or ""

        lambda_env = {
            "BEDROCK_MODEL_ID": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "STATE_TABLE_NAME": state_table.table_name,
            "AGENT_VERSION": deploy_config.get("agent", {}).get("version", "1.0.0"),
            "STACK_NAME": self.stack_name,
        }

        # Multi-account configuration
        multi_account = deploy_config.get("multi_account", {})
        if multi_account.get("enabled") and multi_account.get("accounts"):
            import json as json_mod
            lambda_env["TARGET_ACCOUNTS"] = json_mod.dumps(multi_account["accounts"])
            lambda_env["CROSS_ACCOUNT_ROLE_NAME"] = multi_account.get("role_name", "AuroraPatchAgentRole")

        # Scan regions
        scan_config = deploy_config.get("scan", {})
        if scan_config.get("regions"):
            lambda_env["SCAN_REGIONS"] = ",".join(scan_config["regions"])

        if scan_config.get("enable_org_discovery"):
            lambda_env["ENABLE_ORG_DISCOVERY"] = "true"

        # Database auth configuration
        db_auth = deploy_config.get("database_auth", {})
        if db_auth.get("method") == "iam":
            lambda_env["DB_IAM_AUTH"] = "true"
            lambda_env["DB_IAM_USER"] = db_auth.get("iam_user", "aurora_patch_agent")

        # Bedrock region override
        bedrock_config = deploy_config.get("bedrock", {})
        if bedrock_config.get("region"):
            lambda_env["BEDROCK_REGION"] = bedrock_config["region"]
        if bedrock_config.get("model_id"):
            lambda_env["BEDROCK_MODEL_ID"] = bedrock_config["model_id"]

        # Notifications
        notif_config = deploy_config.get("notifications", {})
        if notif_config.get("slack_webhook_url"):
            lambda_env["SLACK_WEBHOOK_URL"] = notif_config["slack_webhook_url"]
        if notif_config.get("sns_topic_arn"):
            lambda_env["SNS_APPROVAL_TOPIC"] = notif_config["sns_topic_arn"]

        # Security controls (see api/server.py auth middleware).
        #
        # IMPORTANT — the API is FAIL-CLOSED. The app blocks all mutating
        # endpoints (upgrade/switchover/delete/etc.) unless EITHER:
        #   • api_auth_token is set here (recommended — requires a bearer/
        #     x-api-key token on every mutation), OR
        #   • allow_unauthenticated is explicitly set to "true" (local-dev /
        #     scan-only convenience; leaves mutations open — do NOT use for any
        #     deployment reachable beyond the operator).
        # A plain deploy with neither set can still scan/read, but will refuse
        # destructive actions by design. This prevents an anonymous caller from
        # deleting or switching over a cluster against a default deployment.
        allowed_origins = self.node.try_get_context("allowed_origins") or ""
        api_auth_token = self.node.try_get_context("api_auth_token") or ""
        allow_unauthenticated = self.node.try_get_context("allow_unauthenticated") or ""
        security_config = deploy_config.get("security", {})
        if security_config.get("allowed_origins"):
            allowed_origins = security_config["allowed_origins"]
        if security_config.get("api_auth_token"):
            api_auth_token = security_config["api_auth_token"]
        if security_config.get("allow_unauthenticated"):
            allow_unauthenticated = str(security_config["allow_unauthenticated"])

        if allowed_origins:
            lambda_env["ALLOWED_ORIGINS"] = allowed_origins
        if api_auth_token:
            lambda_env["API_AUTH_TOKEN"] = api_auth_token
        if allow_unauthenticated:
            lambda_env["ALLOW_UNAUTHENTICATED"] = allow_unauthenticated
        if db_secret_arn:
            lambda_env["DB_SECRET_ARN"] = db_secret_arn
        if db_user:
            lambda_env["DB_USER"] = db_user
        if db_password:
            lambda_env["DB_PASSWORD"] = db_password

        # ─── Lambda Function ──────────────────────────────────────────────
        api_fn = lambda_.Function(
            self, "ApiFunction",
            function_name="aurora-patch-agent-api",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="api.server.handler",
            code=lambda_.Code.from_asset("../.lambda-build"),
            timeout=Duration.minutes(5),
            memory_size=512,
            vpc=vpc,
            vpc_subnets=private_subnets,
            security_groups=[aurora_sg],
            environment=lambda_env,
        )

        # ─── IAM Permissions ──────────────────────────────────────────────
        # This list previously also granted rds:ModifyDBInstance,
        # rds:DeleteDBClusterSnapshot, rds:AddTagsToResource,
        # rds:RemoveTagsFromResource, rds:CreateDBClusterParameterGroup, and
        # rds:CopyDBClusterParameterGroup. None of those 6 actions have any
        # call site in agents/, api/server.py, traffic-gen/, or tests/ —
        # confirmed by tracing every boto3 RDS call in the codebase, including
        # dispatch tables (_EXEC_ACTIONS in api/server.py) for indirect calls.
        # Removed as unused attack surface, matching the same cleanup already
        # done in cdk/cross_account_role.yaml for the identical reason.
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "rds:DescribeDBClusters",
                "rds:DescribeDBInstances",
                "rds:DescribeDBEngineVersions",
                "rds:DescribeDBClusterParameters",
                "rds:DescribeDBSubnetGroups",
                "rds:DescribeDBClusterSnapshots",
                "rds:CreateDBClusterSnapshot",
                "rds:ListTagsForResource",
                "rds:DescribeBlueGreenDeployments",
                "rds:CreateBlueGreenDeployment",
                "rds:SwitchoverBlueGreenDeployment",
                "rds:DeleteBlueGreenDeployment",
                "rds:PromoteReadReplicaDBCluster",
                "rds:PromoteReadReplica",
                "rds:DescribeGlobalClusters",
                "rds:RemoveFromGlobalCluster",
                "rds:CreateGlobalCluster",
                "rds:ModifyGlobalCluster",
                "rds:CreateDBCluster",
                "rds:CreateDBInstance",
                "rds:ModifyDBCluster",
                "rds:DeleteDBCluster",
                "rds:DeleteDBInstance",
                "rds:DescribeDBClusterParameterGroups",
                "rds:DescribeDBParameterGroups",
            ],
            resources=["*"],
        ))
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=["*"],
        ))
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["cloudwatch:GetMetricStatistics", "cloudwatch:DescribeAlarms"],
            resources=["*"],
        ))
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "iam:GetRolePolicy",
                "iam:PutRolePolicy",
                "iam:PassRole",
            ],
            resources=[api_fn.role.role_arn],
        ))
        # Allow the function to invoke ITSELF asynchronously for long-running jobs
        # (analyze, version-drift) that would otherwise hit API Gateway's 29s limit.
        api_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[
                f"arn:aws:lambda:{self.region}:{self.account}:function:aurora-patch-agent-api"
            ],
        ))

        # ─── Cross-Account AssumeRole (for multi-account fleet scanning) ──
        if multi_account.get("enabled") and multi_account.get("accounts"):
            cross_account_arns = [
                acct["role_arn"] for acct in multi_account["accounts"]
                if acct.get("role_arn")
            ]
            if cross_account_arns:
                api_fn.add_to_role_policy(iam.PolicyStatement(
                    sid="CrossAccountAssumeRole",
                    actions=["sts:AssumeRole"],
                    resources=cross_account_arns,
                ))

        # Organizations discovery (if enabled)
        if scan_config.get("enable_org_discovery"):
            api_fn.add_to_role_policy(iam.PolicyStatement(
                sid="OrganizationsDiscovery",
                actions=[
                    "organizations:ListAccounts",
                    "organizations:DescribeOrganization",
                ],
                resources=["*"],
            ))

        # ─── IAM Database Auth (for Analyzer agent DB metadata gathering) ──
        if db_auth.get("method") == "iam":
            api_fn.add_to_role_policy(iam.PolicyStatement(
                sid="RDSIAMConnect",
                actions=["rds-db:connect"],
                resources=[
                    f"arn:aws:rds-db:{self.region}:{self.account}:dbuser:*/{db_auth.get('iam_user', 'aurora_patch_agent')}"
                ],
            ))
            # Also need rds:GenerateDbAuthToken (implicitly via rds-db:connect)
            api_fn.add_to_role_policy(iam.PolicyStatement(
                sid="RDSGenerateAuthToken",
                actions=["rds:GenerateDbAuthToken"],
                resources=["*"],
            ))

        # Grant DynamoDB access for persistent state
        state_table.grant_read_write_data(api_fn)

        # Grant access to DB credentials secret if provided
        if db_secret_arn:
            secret = sm.Secret.from_secret_complete_arn(self, "DbSecret", db_secret_arn)
            secret.grant_read(api_fn)

        # ─── API Gateway ──────────────────────────────────────────────────
        api = apigw.LambdaRestApi(
            self, "PatchAgentApi",
            rest_api_name="aurora-patch-agent",
            handler=api_fn,
            proxy=True,
        )

        CfnOutput(self, "UiUrl",
                  value=api.url,
                  description="Aurora Patch Agent URL (UI + API)")

        # ─── Traffic Generator Lambda ─────────────────────────────────────
        traffic_env = {
            "DB_NAME": "postgres",
            "STATE_TABLE_NAME": state_table.table_name,
        }
        if db_user:
            traffic_env["DB_USER"] = db_user
        if db_password:
            traffic_env["DB_PASSWORD"] = db_password
        if db_secret_arn:
            traffic_env["DB_SECRET_ARN"] = db_secret_arn

        traffic_fn = lambda_.Function(
            self, "TrafficGenFunction",
            function_name="aurora-patch-agent-traffic-gen",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="traffic_gen.handler.handler",
            code=lambda_.Code.from_asset("../.lambda-build"),
            timeout=Duration.minutes(5),
            memory_size=512,
            vpc=vpc,
            vpc_subnets=private_subnets,
            security_groups=[aurora_sg],
            environment=traffic_env,
        )
        Tags.of(traffic_fn).add("auto-delete", "no")

        traffic_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "rds:DescribeDBClusters",
                "rds:ListTagsForResource",
            ],
            resources=["*"],
        ))
        # Persist per-run traffic stats so the advisor can ground strategy
        # recommendations in real workload patterns.
        state_table.grant(traffic_fn, "dynamodb:PutItem")

        if db_secret_arn:
            secret_ref = sm.Secret.from_secret_complete_arn(self, "TrafficDbSecret", db_secret_arn)
            secret_ref.grant_read(traffic_fn)

        # EventBridge: run every 15 minutes to build traffic patterns
        rule = events.Rule(
            self, "TrafficSchedule",
            rule_name="aurora-patch-agent-traffic-schedule",
            schedule=events.Schedule.rate(Duration.minutes(15)),
            description="Generate periodic traffic to Aurora clusters for baseline learning",
        )
        rule.add_target(targets.LambdaFunction(traffic_fn))

        CfnOutput(self, "TrafficGenFunctionName",
                  value=traffic_fn.function_name,
                  description="Traffic Generator Lambda")
