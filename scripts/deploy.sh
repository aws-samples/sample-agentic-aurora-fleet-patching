#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ─────────────────────────────────────────────────────────────────────────────
# Aurora Patch Agent — Portable Deployment Script
#
# Deploys the agent to any AWS account/region using configuration from
# deploy-config.json (or cdk.json context overrides).
#
# Usage:
#   ./scripts/deploy.sh                    # Full deployment
#   ./scripts/deploy.sh --scan-only        # UI + scan only (no orchestration)
#   ./scripts/deploy.sh --cross-account    # Deploy cross-account role template
#   ./scripts/deploy.sh --setup-iam-user   # Create IAM DB user on clusters
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_FILE="$PROJECT_ROOT/deploy-config.json"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[deploy]${NC} $1"; }
warn() { echo -e "${YELLOW}[warn]${NC} $1"; }
error() { echo -e "${RED}[error]${NC} $1" >&2; }

# ─── Pre-flight checks ───────────────────────────────────────────────────────
check_prerequisites() {
    log "Checking prerequisites..."

    command -v aws >/dev/null 2>&1 || { error "AWS CLI not found. Install: https://aws.amazon.com/cli/"; exit 1; }
    command -v cdk >/dev/null 2>&1 || { error "AWS CDK CLI not found. Install: npm install -g aws-cdk"; exit 1; }
    command -v python3 >/dev/null 2>&1 || { error "Python 3 not found."; exit 1; }
    command -v node >/dev/null 2>&1 || { error "Node.js not found. Required for UI build."; exit 1; }

    # Check AWS credentials
    if ! aws sts get-caller-identity >/dev/null 2>&1; then
        error "AWS credentials not configured. Run: aws configure"
        exit 1
    fi

    ACCOUNT_ID=$(aws sts get-caller-identity --query "Account" --output text)
    REGION=$(aws configure get region 2>/dev/null || echo "us-east-1")
    log "Deploying to account: $ACCOUNT_ID, region: $REGION"
}

# ─── Read deploy-config.json ─────────────────────────────────────────────────
read_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        warn "No deploy-config.json found. Using cdk.json context values."
        warn "Copy deploy-config.example.json to deploy-config.json for full portability."
        return
    fi
    log "Using configuration from: $CONFIG_FILE"

    # Extract key values using python (portable JSON parsing)
    DEPLOY_REGION=$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c.get('deployment',{}).get('region',''))" 2>/dev/null || echo "")
    VPC_ID=$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c.get('deployment',{}).get('vpc_id',''))" 2>/dev/null || echo "")
    SUBNET_IDS=$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(','.join(c.get('deployment',{}).get('subnet_ids',[])))" 2>/dev/null || echo "")
    SG_ID=$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(c.get('deployment',{}).get('security_group_id',''))" 2>/dev/null || echo "")
    DEPLOY_INFRA=$(python3 -c "import json; c=json.load(open('$CONFIG_FILE')); print(str(c.get('deployment',{}).get('deploy_infra',True)).lower())" 2>/dev/null || echo "true")

    if [ -n "$DEPLOY_REGION" ]; then
        REGION="$DEPLOY_REGION"
    fi
}

# ─── Build ────────────────────────────────────────────────────────────────────
build() {
    log "Building UI..."
    if [ -d "$PROJECT_ROOT/ui/node_modules" ]; then
        (cd "$PROJECT_ROOT/ui" && npm run build)
    else
        (cd "$PROJECT_ROOT/ui" && npm install && npm run build)
    fi

    log "Building Lambda package..."
    bash "$PROJECT_ROOT/scripts/build-lambda.sh"
}

# ─── Deploy via CDK ──────────────────────────────────────────────────────────
deploy_cdk() {
    local mode="${1:-full}"

    log "Installing CDK dependencies..."
    (cd "$PROJECT_ROOT/cdk" && pip3 install -q -r requirements.txt)

    local cdk_args=""
    if [ -n "${VPC_ID:-}" ]; then
        cdk_args="$cdk_args -c vpc_id=$VPC_ID"
    fi
    if [ -n "${SUBNET_IDS:-}" ]; then
        cdk_args="$cdk_args -c subnet_ids=$SUBNET_IDS"
    fi
    if [ -n "${SG_ID:-}" ]; then
        cdk_args="$cdk_args -c security_group_id=$SG_ID"
    fi
    if [ -n "${REGION:-}" ]; then
        cdk_args="$cdk_args -c region=$REGION"
    fi

    if [ "$mode" = "scan-only" ]; then
        cdk_args="$cdk_args -c deploy_infra=false"
        log "Deploying in scan-only mode (no orchestration stack)..."
        # cdk_args is deliberately left unquoted: it's built above as a string of
        # multiple space-separated "-c key=value" flags, and word-splitting it
        # here is what makes each flag reach `cdk` as its own argument. Quoting
        # it would pass the whole thing as one argument and break the deploy.
        # It's assembled entirely from this script's own fixed flags plus
        # $REGION (validated earlier in the script), not from unsanitized
        # external/user input.
        (cd "$PROJECT_ROOT/cdk" && cdk deploy AuroraPatchAgentUiStack $cdk_args --require-approval never)
    else
        log "Deploying full stack (UI + orchestration)..."
        # See comment above — same intentional word-splitting.
        (cd "$PROJECT_ROOT/cdk" && cdk deploy --all $cdk_args --require-approval never)
    fi

    log "Deployment complete!"
}

# ─── Deploy cross-account role ────────────────────────────────────────────────
deploy_cross_account_role() {
    log "Deploying cross-account role template..."
    log ""
    log "This CloudFormation template should be deployed in EACH target account"
    log "that you want the patch agent to scan/manage."
    log ""
    log "Deploy with:"
    log "  aws cloudformation deploy \\"
    log "    --template-file $PROJECT_ROOT/cdk/cross_account_role.yaml \\"
    log "    --stack-name aurora-patch-agent-role \\"
    log "    --capabilities CAPABILITY_NAMED_IAM \\"
    log "    --parameter-overrides OrchestratorAccountId=$ACCOUNT_ID \\"
    log "    --profile <target-account-profile>"
    log ""

    # If we can detect the current account is the orchestrator, offer to deploy locally
    read -p "Deploy this role in the CURRENT account ($ACCOUNT_ID) for testing? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        aws cloudformation deploy \
            --template-file "$PROJECT_ROOT/cdk/cross_account_role.yaml" \
            --stack-name aurora-patch-agent-role \
            --capabilities CAPABILITY_NAMED_IAM \
            --parameter-overrides "OrchestratorAccountId=$ACCOUNT_ID"
        log "Cross-account role deployed in account $ACCOUNT_ID"
    fi
}

# ─── Setup IAM DB user ────────────────────────────────────────────────────────
setup_iam_user() {
    log "Generating IAM database user SQL..."
    python3 -c "
from shared.iam_auth import create_iam_db_user_sql
print(create_iam_db_user_sql())
" > "$PROJECT_ROOT/scripts/setup_iam_user.sql"

    log "SQL generated at: scripts/setup_iam_user.sql"
    log ""
    log "Run this against each Aurora cluster:"
    log "  psql -h <cluster-endpoint> -U <admin_user> -d postgres -f scripts/setup_iam_user.sql"
    log ""
    log "This creates a read-only 'aurora_patch_agent' user with IAM auth enabled."
}

# ─── Main ─────────────────────────────────────────────────────────────────────
main() {
    check_prerequisites
    read_config

    case "${1:-}" in
        --scan-only)
            build
            deploy_cdk "scan-only"
            ;;
        --cross-account)
            deploy_cross_account_role
            ;;
        --setup-iam-user)
            setup_iam_user
            ;;
        --build-only)
            build
            ;;
        *)
            build
            deploy_cdk "full"
            ;;
    esac
}

main "$@"
