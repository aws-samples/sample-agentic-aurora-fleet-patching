# Architecture

[Back to README](../README.md)

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  ORCHESTRATOR ACCOUNT                                                        │
│                                                                              │
│  ┌───────────────┐    ┌─────────────────────────────────────────────────┐   │
│  │ API Gateway   │───▶│  Lambda (FastAPI + React SPA)                   │   │
│  └───────────────┘    │  • 10 AI Agents (Bedrock Claude)                │   │
│                       │  • Recommendation Engine (human approves)        │   │
│                       │  • Outcome Learning (gets smarter over time)     │   │
│                       └─────────────────────────────────────────────────┘   │
│                              │                    │                          │
│  ┌───────────────────────────┼────────────────────┼───────────────────────┐ │
│  │  DynamoDB                 │                    │                        │ │
│  │  • Fleet state            │  Bedrock Claude    │                        │ │
│  │  • Recommendations        │  (reasoning,       │                        │ │
│  │  • Learned fixes          │   diagnosis,       │                        │ │
│  │  • Upgrade outcomes       │   fleet planning)  │                        │ │
│  │  • CVE cache              │                    │                        │ │
│  └───────────────────────────┘                    │                        │ │
│                                                   │                        │ │
│  Agents: Scanner → Analyzer → Planner → Executor  │                        │ │
│          Advisor, Supervisor, Validator, Rollback  │                        │ │
│                                                   │                        │ │
└───────────────────────────────────────────────────┼────────────────────────┘ │
                                                    │                          │
               ┌────────────────────────────────────┼──────────────┐           │
               │  AssumeRole (cross-account)        │              │           │
               ▼                                    ▼              ▼           │
     ┌──────────────────┐             ┌──────────────────┐  ┌──────────────┐  │
     │ Account A (prod) │             │ Account B (stg)  │  │ Account C    │  │
     │                  │             │                  │  │ (dev)        │  │
     │ Aurora PG  ×N    │             │ Aurora PG  ×N    │  │ Aurora PG ×N │  │
     │ Aurora MySQL ×N  │             │ Aurora MySQL ×N  │  │ Aurora MySQL │  │
     │                  │             │                  │  │              │  │
     │  us-east-2       │             │  us-east-2       │  │ us-east-2    │  │
     │  us-west-2 (GDB) │             │                  │  │              │  │
     └──────────────────┘             └──────────────────┘  └──────────────┘  │
```

## Tech Stack

| Layer | Technology |
|---|---|
| UI | React + Vite (served via Lambda static mount) |
| API | FastAPI + Mangum on Lambda behind API Gateway |
| AI | Amazon Bedrock — Claude Sonnet 4.6 (default), runtime-selectable |
| Compute | Python 3.12 Lambda (VPC-attached, private subnets + NAT) |
| State | DynamoDB (upgrade history, learned fixes, CVE cache, recommendations) |
| Databases | Aurora PostgreSQL + Aurora MySQL (any version) |
| IaC | AWS CDK (Python) |

## Project Structure

```
sample-agentic-aurora-fleet-patching/
├── cdk/                          # CDK infrastructure
│   ├── app.py                    #   Entry point (deploy_infra flag)
│   ├── cdk.json.example          #   Template — copy to cdk.json
│   ├── cross_account_role.yaml   #   CloudFormation for target account IAM role
│   └── stacks/
│       ├── ui_stack.py           #   Lambda, API GW, VPC, traffic gen
│       └── patch_agent_stack.py  #   DynamoDB, SNS, StepFunctions (optional)
├── api/
│   └── server.py                 # FastAPI backend (all endpoints)
├── agents/
│   ├── scanner/                  # Fleet inventory, CVE scanning
│   ├── analyzer/                 # Compatibility, baselines
│   ├── planner/                  # Wave planning
│   ├── advisor/                  # AI advisor (NL Q&A)
│   ├── execution/                # Blue/Green + In-Place upgrades
│   ├── validation/               # Post-upgrade health
│   ├── rollback/                 # Auto-revert
│   └── supervisor/               # Orchestration
├── shared/
│   ├── config.py                 # Environment-driven configuration
│   ├── aws_clients.py            # Cached boto3 clients
│   ├── models.py                 # Shared data models
│   ├── multi_account.py          # Cross-account session management
│   ├── iam_auth.py               # IAM database authentication
│   └── cve_cache.py              # Persistent CVE cache (DynamoDB)
├── ui/
│   └── src/App.jsx               # Full SPA (React)
├── traffic-gen/
│   └── handler.py                # Workload generator (EventBridge Lambda)
├── scripts/
│   ├── build-lambda.sh           # Build .lambda-build package
│   ├── deploy.sh                 # Portable deployment script
│   └── tag-resources.sh          # Tag resources (auto-delete=no)
├── tests/
├── deploy-config.example.json    # Portable deployment config template
├── pyproject.toml                # Python packaging & tool config
└── README.md
```
