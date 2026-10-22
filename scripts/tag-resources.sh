#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Tag all aurora-patch-agent resources with auto-delete=no
# Run this once to tag existing resources that were created before the tag was added.

set -euo pipefail
REGION="us-east-2"

echo "=== Tagging Aurora PG clusters ==="
CLUSTER_ARNS=$(aws rds describe-db-clusters --region "$REGION" \
    --query "DBClusters[?starts_with(DBClusterIdentifier, 'patch-agent-')].DBClusterArn" \
    --output text)

for ARN in $CLUSTER_ARNS; do
    echo "  Tagging: $ARN"
    aws rds add-tags-to-resource --region "$REGION" \
        --resource-name "$ARN" \
        --tags Key=auto-delete,Value=no Key=project,Value=aurora-patch-agent
done

echo "=== Tagging Aurora DB instances ==="
INSTANCE_ARNS=$(aws rds describe-db-instances --region "$REGION" \
    --query "DBInstances[?starts_with(DBClusterIdentifier, 'patch-agent-')].DBInstanceArn" \
    --output text)

for ARN in $INSTANCE_ARNS; do
    echo "  Tagging: $ARN"
    aws rds add-tags-to-resource --region "$REGION" \
        --resource-name "$ARN" \
        --tags Key=auto-delete,Value=no Key=project,Value=aurora-patch-agent
done

echo "=== Tagging Lambda functions ==="
for FN in aurora-patch-discovery aurora-patch-assessment aurora-patch-execution \
           aurora-patch-validation aurora-patch-rollback aurora-patch-supervisor \
           aurora-patch-agent-traffic-gen; do
    ARN=$(aws lambda get-function --function-name "$FN" --region "$REGION" \
        --query "Configuration.FunctionArn" --output text 2>/dev/null) || continue
    echo "  Tagging: $FN"
    aws lambda tag-resource --region "$REGION" --resource "$ARN" \
        --tags auto-delete=no,project=aurora-patch-agent
done

echo "=== Tagging DynamoDB table ==="
TABLE_ARN=$(aws dynamodb describe-table --table-name aurora-patch-agent-state --region "$REGION" \
    --query "Table.TableArn" --output text 2>/dev/null) || true
if [ -n "$TABLE_ARN" ]; then
    echo "  Tagging: aurora-patch-agent-state"
    aws dynamodb tag-resource --region "$REGION" --resource-arn "$TABLE_ARN" \
        --tags Key=auto-delete,Value=no Key=project,Value=aurora-patch-agent
fi

echo "=== Done — all resources tagged with auto-delete=no ==="
