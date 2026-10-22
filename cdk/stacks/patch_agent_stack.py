# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK Stack for Aurora PostgreSQL Fleet Patching Agent."""
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    Tags,
    aws_lambda as lambda_,
    aws_dynamodb as dynamodb,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as tasks,
    aws_events as events,
    aws_events_targets as targets,
    aws_sns as sns,
    aws_sns_subscriptions as subscriptions,
    aws_iam as iam,
)
from constructs import Construct


class AuroraPatchAgentStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        Tags.of(self).add("auto-delete", "no")
        Tags.of(self).add("project", "aurora-patch-agent")

        # ─────────────────────────────────────────────────────────
        # DynamoDB State Table
        # ─────────────────────────────────────────────────────────
        state_table = dynamodb.Table(
            self, "PatchAgentState",
            table_name="aurora-patch-agent-state",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
        )

        # ─────────────────────────────────────────────────────────
        # SNS Topic for Approvals & Notifications
        # ─────────────────────────────────────────────────────────
        approval_topic = sns.Topic(
            self, "ApprovalTopic",
            topic_name="aurora-patch-agent-approvals",
        )

        # ─────────────────────────────────────────────────────────
        # Shared Lambda Layer (shared/ module)
        # ─────────────────────────────────────────────────────────
        shared_layer = lambda_.LayerVersion(
            self, "SharedLayer",
            code=lambda_.Code.from_asset("../shared"),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="Shared utilities for aurora-patch-agent",
        )

        # Common environment variables
        # Default model matches shared/config.py and ui_stack.py: Sonnet 4.5 is
        # chosen deliberately (not 4.6/5) because it doesn't force extended
        # thinking and responds fast enough to keep synchronous endpoints under
        # API Gateway's 29s timeout. Override via the BEDROCK_MODEL_ID env var.
        common_env = {
            "PATCH_STATE_TABLE": state_table.table_name,
            "SNS_APPROVAL_TOPIC": approval_topic.topic_arn,
            "BEDROCK_MODEL_ID": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        }

        # ─────────────────────────────────────────────────────────
        # IAM policies — least-privilege, scoped to this account's resources.
        #
        # RDS Describe*/List* are fleet-discovery actions that operate across
        # whatever clusters exist in the account and do not support meaningful
        # per-resource ARN scoping, so they stay account-wide. The mutating
        # Blue/Green actions ARE scoped to this account's RDS cluster and
        # blue-green-deployment ARNs (region/identifier wildcarded, since the
        # agent is multi-region and cluster identifiers are discovered at
        # runtime). CloudWatch metrics and Bedrock InvokeModel do not take a
        # resource-level ARN in a way that usefully narrows this use case;
        # Bedrock is scoped to the inference-profile/foundation-model ARNs in
        # this account/region rather than a bare "*".
        # ─────────────────────────────────────────────────────────
        rds_read_policy = iam.PolicyStatement(
            actions=[
                "rds:DescribeDBClusters",
                "rds:DescribeDBInstances",
                "rds:DescribeDBEngineVersions",
                "rds:DescribeDBClusterParameters",
                "rds:ListTagsForResource",
                "rds:DescribeBlueGreenDeployments",
            ],
            resources=["*"],
        )

        rds_write_policy = iam.PolicyStatement(
            actions=[
                "rds:CreateBlueGreenDeployment",
                "rds:SwitchoverBlueGreenDeployment",
                "rds:DeleteBlueGreenDeployment",
            ],
            resources=[
                f"arn:aws:rds:*:{self.account}:cluster:*",
                f"arn:aws:rds:*:{self.account}:blue-green-deployment:*",
            ],
        )

        cloudwatch_policy = iam.PolicyStatement(
            actions=["cloudwatch:GetMetricStatistics", "cloudwatch:DescribeAlarms"],
            resources=["*"],
        )

        bedrock_policy = iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=[
                f"arn:aws:bedrock:*:{self.account}:inference-profile/*",
                "arn:aws:bedrock:*::foundation-model/*",
            ],
        )

        # ─────────────────────────────────────────────────────────
        # Lambda Functions — One per agent
        # ─────────────────────────────────────────────────────────
        discovery_fn = lambda_.Function(
            self, "DiscoveryAgent",
            function_name="aurora-patch-discovery",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("../agents/discovery"),
            layers=[shared_layer],
            environment=common_env,
            timeout=Duration.minutes(5),
            memory_size=512,
        )
        discovery_fn.add_to_role_policy(rds_read_policy)
        discovery_fn.add_to_role_policy(cloudwatch_policy)
        state_table.grant_read_write_data(discovery_fn)

        assessment_fn = lambda_.Function(
            self, "AssessmentAgent",
            function_name="aurora-patch-assessment",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("../agents/assessment"),
            layers=[shared_layer],
            environment=common_env,
            timeout=Duration.minutes(5),
            memory_size=512,
        )
        assessment_fn.add_to_role_policy(rds_read_policy)
        assessment_fn.add_to_role_policy(cloudwatch_policy)
        state_table.grant_read_write_data(assessment_fn)

        execution_fn = lambda_.Function(
            self, "ExecutionAgent",
            function_name="aurora-patch-execution",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("../agents/execution"),
            layers=[shared_layer],
            environment=common_env,
            timeout=Duration.minutes(10),
            memory_size=512,
        )
        execution_fn.add_to_role_policy(rds_read_policy)
        execution_fn.add_to_role_policy(rds_write_policy)
        state_table.grant_read_write_data(execution_fn)

        validation_fn = lambda_.Function(
            self, "ValidationAgent",
            function_name="aurora-patch-validation",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("../agents/validation"),
            layers=[shared_layer],
            environment=common_env,
            timeout=Duration.minutes(5),
            memory_size=512,
        )
        validation_fn.add_to_role_policy(rds_read_policy)
        validation_fn.add_to_role_policy(cloudwatch_policy)
        state_table.grant_read_write_data(validation_fn)

        rollback_fn = lambda_.Function(
            self, "RollbackAgent",
            function_name="aurora-patch-rollback",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("../agents/rollback"),
            layers=[shared_layer],
            environment=common_env,
            timeout=Duration.minutes(10),
            memory_size=512,
        )
        rollback_fn.add_to_role_policy(rds_read_policy)
        rollback_fn.add_to_role_policy(rds_write_policy)
        rollback_fn.add_to_role_policy(cloudwatch_policy)
        approval_topic.grant_publish(rollback_fn)
        state_table.grant_read_write_data(rollback_fn)

        supervisor_fn = lambda_.Function(
            self, "SupervisorAgent",
            function_name="aurora-patch-supervisor",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset("../agents/supervisor"),
            layers=[shared_layer],
            environment=common_env,
            timeout=Duration.minutes(5),
            memory_size=1024,
        )
        supervisor_fn.add_to_role_policy(bedrock_policy)
        approval_topic.grant_publish(supervisor_fn)
        state_table.grant_read_write_data(supervisor_fn)

        # ─────────────────────────────────────────────────────────
        # Step Functions — Orchestration State Machine
        # ─────────────────────────────────────────────────────────
        discover = tasks.LambdaInvoke(
            self, "Discover",
            lambda_function=discovery_fn,
            output_path="$.Payload",
        )

        assess = tasks.LambdaInvoke(
            self, "Assess",
            lambda_function=assessment_fn,
            output_path="$.Payload",
        )

        plan_rollout = tasks.LambdaInvoke(
            self, "PlanRollout",
            lambda_function=supervisor_fn,
            payload=sfn.TaskInput.from_object({
                "action": "plan_rollout",
                "assessments": sfn.JsonPath.string_at("$.assessments"),
            }),
            output_path="$.Payload",
        )

        # Per-cluster upgrade: execute → wait → validate → decide
        execute_upgrade = tasks.LambdaInvoke(
            self, "ExecuteUpgrade",
            lambda_function=execution_fn,
            output_path="$.Payload",
        )

        wait_for_green = sfn.Wait(
            self, "WaitForGreen",
            time=sfn.WaitTime.duration(Duration.minutes(5)),
        )

        check_deployment = tasks.LambdaInvoke(
            self, "CheckDeployment",
            lambda_function=execution_fn,
            payload=sfn.TaskInput.from_object({
                "action": "check_status",
                "deployment_id": sfn.JsonPath.string_at("$.deployment_id"),
                "region": sfn.JsonPath.string_at("$.region"),
            }),
            output_path="$.Payload",
        )

        switchover = tasks.LambdaInvoke(
            self, "Switchover",
            lambda_function=execution_fn,
            payload=sfn.TaskInput.from_object({
                "action": "switchover",
                "deployment_id": sfn.JsonPath.string_at("$.deployment_id"),
                "region": sfn.JsonPath.string_at("$.region"),
            }),
            output_path="$.Payload",
        )

        validate = tasks.LambdaInvoke(
            self, "Validate",
            lambda_function=validation_fn,
            output_path="$.Payload",
        )

        # Build the state machine
        definition = (
            discover
            .next(assess)
            .next(plan_rollout)
        )

        state_machine = sfn.StateMachine(
            self, "PatchOrchestrator",
            state_machine_name="aurora-patch-orchestrator",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=Duration.hours(48),
        )

        # ─────────────────────────────────────────────────────────
        # EventBridge — Scheduled Discovery Scan
        # ─────────────────────────────────────────────────────────
        events.Rule(
            self, "WeeklyDiscoveryScan",
            rule_name="aurora-patch-weekly-scan",
            schedule=events.Schedule.cron(
                week_day="MON",
                hour="6",
                minute="0",
            ),
            targets=[targets.SfnStateMachine(state_machine)],
        )
