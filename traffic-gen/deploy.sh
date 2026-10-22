#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Deploy the Traffic Generator Lambda
# Prerequisites: AWS SAM CLI, Docker (for building the layer)
#
# Usage:
#   ./deploy.sh <vpc-id> <subnet-ids> <security-group-id>
#
# Example:
#   ./deploy.sh vpc-0abc123 "subnet-aaa,subnet-bbb" sg-0xyz789

set -euo pipefail

STACK_NAME="aurora-patch-agent-traffic-gen"
REGION="us-east-2"
S3_BUCKET="aurora-patch-agent-artifacts-${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"

VPC_ID="${1:?Usage: ./deploy.sh <vpc-id> <subnet-ids> <sg-id>}"
SUBNET_IDS="${2:?Provide comma-separated subnet IDs}"
SG_ID="${3:?Provide security group ID}"

echo "=== Building Traffic Generator ==="
sam build --template template.yaml --use-container

echo "=== Creating S3 bucket (if needed) ==="
aws s3 mb "s3://${S3_BUCKET}" --region "$REGION" 2>/dev/null || true

echo "=== Deploying stack: ${STACK_NAME} ==="
sam deploy \
    --stack-name "$STACK_NAME" \
    --region "$REGION" \
    --s3-bucket "$S3_BUCKET" \
    --capabilities CAPABILITY_IAM \
    --parameter-overrides \
        "VpcId=${VPC_ID}" \
        "SubnetIds=${SUBNET_IDS}" \
        "SecurityGroupId=${SG_ID}" \
    --no-confirm-changeset

echo "=== Done! ==="
echo "Function: $(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --query 'Stacks[0].Outputs[?OutputKey==`FunctionName`].OutputValue' --output text --region "$REGION")"
echo ""
echo "To invoke manually:"
echo "  aws lambda invoke --function-name aurora-patch-agent-traffic-gen --region $REGION /dev/stdout"
