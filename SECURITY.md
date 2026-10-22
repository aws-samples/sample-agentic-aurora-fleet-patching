# Security Policy

## Reporting a Vulnerability

If you discover a potential security issue in this project, please notify AWS
Security via our
[vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/)
or directly at <aws-security@amazon.com>.

**Please do _not_ create a public GitHub issue** for security vulnerabilities.

## Scope and intended use

This is sample/reference code published to demonstrate an agentic approach to
Aurora fleet patching. It is **not** a production-ready, AWS-supported artifact.
Before deploying it anywhere beyond a test environment, review the code — in
particular:

- The API is **fail-closed**: state-changing endpoints require an
  `API_AUTH_TOKEN` (or an explicit, documented `ALLOW_UNAUTHENTICATED=true`
  local-dev opt-out). Configure authentication before exposing the API.
- The tool can perform **high-impact, hard-to-undo** operations against real
  Aurora clusters (Blue/Green switchover, in-place upgrade, cluster deletion).
  Destructive actions are human-initiated and gated by server-side
  confirmation and (for production) a manual approval step.
- Review the IAM permissions in `cdk/cross_account_role.yaml` and
  `cdk/stacks/ui_stack.py` and scope them to your environment.

See the README's non-production disclaimer for more detail.
