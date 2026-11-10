# What the agent can do

[Back to README](../README.md)

## What Works Out of the Box

A default deployment is deliberately conservative: until you configure an API
token, it can't run scans, analysis, or upgrades, and it can't change your fleet
until you also grant extra permissions yourself. Some features also have known
limitations (see [Known Limitations and Workarounds](known-limitations.md#known-limitations-and-workarounds)).

**Set an API token first.** The API is fail-closed: every `POST`/`PUT`/`DELETE`
request needs the token, and that includes **Scan Fleet**, the CVE scan,
analysis, Version Changes, the AI Advisor, and AI Diagnose, not just upgrades.
Without a token, the UI can only show results that were already stored.
See [API Authentication](api-authentication.md#running-upgrades-api-authentication-important).

| Capability | With an API token set | To enable |
|---|---|---|
| Fleet scan, environment classification | ✅ Works. Tag clusters with `Environment` | [Tag your clusters](known-limitations.md#known-limitations-and-workarounds) |
| CVE posture and urgency scoring | ✅ Works | — |
| Version Changes, cluster analysis, AI Advisor, AI Diagnose | ✅ Works if Bedrock model access is enabled | [Bedrock prerequisites](deployment.md#what-you-need) |
| Viewing stored fleet, upgrade history, and maintenance windows | ✅ Works even without a token (see [Your UI URL](deployment.md#your-ui-url)) | — |
| In-place upgrade | ✅ Works on clusters without **Copy tags to snapshots**. On clusters with it (the console default), the pre-upgrade snapshot needs `rds:AddTagsToResource`, so the upgrade stops safely at the snapshot step | [Enabling Blue/Green Execution](blue-green-permissions.md#enabling-bluegreen-execution-additional-iam-opt-in) (same opt-in policy) |
| Blue/Green create, switchover, delete | ❌ Fails with `AccessDenied` | [Enabling Blue/Green Execution](blue-green-permissions.md#enabling-bluegreen-execution-additional-iam-opt-in) |
| **Apply Fix**: reboot writer instance | ❌ Fails with `AccessDenied` | Same opt-in policy (`rds:RebootDBInstance`) |
| **Apply Fix**: grant a missing IAM permission | Only self-grants **read-only** actions | Grant write actions yourself |
| Capture Baseline, DB Validation, live database metadata | ❌ Needs database access | DB credentials and a network path: [Traffic Generator](database-access.md#traffic-generator-optional) and [IAM Database Authentication](database-access.md#iam-database-authentication) |
| Traffic generator | Deployed and scheduled; does nothing until DB credentials are set | Read the warning in [Traffic Generator](database-access.md#traffic-generator-optional) first |
| Multi-account scanning | ✅ With the cross-account role | [Multi-Account Fleet Management](multi-account.md#multi-account-fleet-management) |
| Multi-account Blue/Green | ❌ Fails with `AccessDenied` | [Cross-account opt-in policy](blue-green-permissions.md#multi-account-the-cross-account-role-in-each-target-account) |
| Upgrades outside the default change windows | ❌ Blocked by the change-window guardrail | Set windows in the **Maintenance** tab, or select **Skip window check** |
| Scanning more than one Region | Scans the Region the agent is deployed in | Set `scan.regions` in `deploy-config.json` and redeploy |

## UI Features

### Dashboard
- Fleet metrics: total clusters, pending upgrades, compliance %, active upgrades
- Security & CVE posture: live NVD scan with urgency scoring
- CVE-exposed clusters with one-click patch buttons
- Active upgrades with real-time progress
- Recent activity timeline

### Fleet Tab
- Full cluster inventory (version, environment, status, upgrade targets)
- **CVE Risk** column showing exposure per cluster
- **Patch CVE** button for immediate vulnerability-driven upgrades (shown only for clusters with CVE exposure)
- **In-Place** upgrade button for clusters that don't support Blue/Green (PG < 14.6)
- Guardrails modal with pre-flight checks before any upgrade

### Upgrades Tab
- Monitor Blue/Green deployments and in-place upgrades in progress
- **AI Diagnose** button on failures — Bedrock analyzes the error and suggests fixes
- **Apply Fix** button to auto-remediate selected errors (it only self-grants **read-only** IAM actions; write permissions must be granted by you)
- Real-time status polling (10s interval)

### Cluster Detail Panel
- Click any cluster to see deep analysis
- **Version Changes** button: AI-powered drift analysis between current and target version
  - Parameter default changes (with old/new values and impact level)
  - Behavioral changes and mitigations
  - Deprecated/removed features
  - New features and benefits
  - Performance impact predictions
  - Prioritized recommendations
- Baseline capture and comparison
- Database validation

### AI Advisor Tab
- Natural language Q&A about fleet status, upgrade strategies, CVE exposure
- Model-powered recommendations with risk assessment

### Settings
- Runtime model selector (Sonnet 4.6 default; the list also shows Sonnet 5 / Sonnet 4.5 (legacy) / Haiku 4.5 / Opus 4.8). Only pick a model your account has Bedrock access to in the agent's Region; otherwise AI calls fail.

## Upgrade Strategies

| Strategy | When Used | Downtime |
|---|---|---|
| **Blue/Green** | PG >= 14.6 or MySQL with binlog enabled (default for prod) | Seconds (during switchover) |
| **In-Place** | PG < 14.6, MySQL minor patches, or user choice | Minutes (cluster restart) |

The agent automatically suggests the appropriate strategy based on engine type, version, and environment. Users can override via the UI.

### MySQL-Specific Notes
- **Blue/Green requires**: `binlog_format` enabled in the cluster parameter group
- **Aurora MySQL v2 → v3** (5.7 → 8.0): Major version jump with breaking changes (reserved keywords, charset, auth plugin changes)
- **Aurora MySQL v2 EOL**: Standard support ended Oct 2024 — extended support charges apply

### PostgreSQL-Specific Notes
- **Blue/Green requires**: a custom DB cluster parameter group with `rds.logical_replication = 1`, and a **reboot of the writer instance** after you set it (the default parameter group can't be modified). Without it, Blue/Green create fails with `SourceClusterNotSupportedFault`.

## Security Scanning

Integrates with:
- **NVD API** (NIST): Live CVE lookups for PostgreSQL with 6-hour cache (persisted to DynamoDB). MySQL uses the static list only (see [Known Limitations](known-limitations.md#known-limitations-and-workarounds)).
- **AWS Security Bulletins**: Aurora-specific advisories
- **Known CVE databases**: 5 PostgreSQL CVEs + 8 MySQL CVEs (static fallback)
- **Urgency scoring**: Critical (>=60), High (>=35), Medium (>=15) based on CVE severity, EOL proximity, version drift

## AI-Powered Error Diagnosis

When an upgrade fails, the AI diagnosis system:
1. Checks learned fixes from past successful resolutions (instant, no AI call)
2. Matches known error patterns against a static remediation map (instant)
3. Falls back to Bedrock Claude for novel errors (analyzes root cause, suggests fix)
4. Provides one-click "Apply Fix" for actionable remediations. Self-granted IAM fixes are limited to **read-only** actions; rebooting an instance needs the opt-in `rds:RebootDBInstance` permission ([Enabling Blue/Green Execution](blue-green-permissions.md#enabling-bluegreen-execution-additional-iam-opt-in))
5. Successful fixes are persisted — next occurrence resolves instantly
