#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK App entry point for Aurora Patch Agent infrastructure.

PORTABILITY:
  This app deploys to any AWS account/region. All environment-specific
  configuration is externalized to cdk.json context or environment variables.

Stacks:
  - AuroraPatchAgentUiStack (always deployed): Lambda + API Gateway + UI
  - AuroraPatchAgentStack (optional): DynamoDB, SNS, StepFunctions orchestration

To skip the orchestration/infra stack (scan-only mode):
    cdk deploy AuroraPatchAgentUiStack -c deploy_infra=false

To deploy to a different region:
    cdk deploy -c region=eu-west-1

Multi-account setup:
    1. Deploy this stack in your orchestrator account
    2. Deploy cross_account_role.yaml in each target account
    3. Configure TARGET_ACCOUNTS env var or use Organizations discovery
"""
import json
import os
import aws_cdk as cdk

from stacks.patch_agent_stack import AuroraPatchAgentStack
from stacks.ui_stack import UiStack

app = cdk.App()

# ─── Region & Account Resolution ─────────────────────────────────────────
# Priority: cdk.json context > env var > default
region = (
    app.node.try_get_context("region")
    or os.environ.get("CDK_DEFAULT_REGION")
    or "us-east-1"
)
deploy_infra = (app.node.try_get_context("deploy_infra") or "true").lower() != "false"

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=region,
)

# ─── Load deploy-config.json if present (for portable deployment) ─────────
config_path = os.path.join(os.path.dirname(__file__), "..", "deploy-config.json")
deploy_config = {}
if os.path.exists(config_path):
    with open(config_path, encoding="utf-8") as f:
        deploy_config = json.load(f)

# ─── Stack Deployment ─────────────────────────────────────────────────────
# Note: multi-account configuration is read from deploy_config directly inside
# UiStack (see cdk/stacks/ui_stack.py), so it does not need to be assembled here.
if deploy_infra:
    AuroraPatchAgentStack(app, "AuroraPatchAgentStack", env=env)

UiStack(app, "AuroraPatchAgentUiStack", env=env, deploy_config=deploy_config)

app.synth()
