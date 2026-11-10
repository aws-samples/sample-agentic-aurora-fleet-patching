# Running Upgrades: API Authentication (important)

[Back to README](../README.md)


The API is **fail-closed by design.** Every `POST`, `PUT`, `PATCH`, and
`DELETE` request is **blocked by default**: not only starting an upgrade,
switchover, rollback, or deletion, but also **Scan Fleet**, the CVE scan,
analysis, Version Changes, the AI Advisor, AI Diagnose, and saving settings.
This means a freshly deployed instance can't be driven anonymously by anyone who
finds its URL. Only `GET` requests (viewing stored results) work without
authentication. The buttons are always visible in the UI; whether the backend
honors them depends on how you configure auth.

Pick one of these before you use the agent:

**Option A — require a token (recommended for anything shared).**
Set an API token on the backend and give the same token to the UI:

```bash
# Backend: set the token (CDK context or deploy-config.json "security.api_auth_token")
cdk deploy -c api_auth_token=<your-strong-token> ...

# UI: supply the same token so the browser sends it. Either:
#   build-time —
VITE_API_AUTH_TOKEN=<your-strong-token> npm run build
#   or runtime, once, in the browser console (no rebuild) —
localStorage.setItem('apiAuthToken', '<your-strong-token>')
```

Every mutating request then carries the token (`Authorization: Bearer` /
`x-api-key`); requests without it get `401`.

**Option B — explicitly allow unauthenticated (local dev / throwaway demo only).**
Only for an instance that is not reachable by anyone else:

```bash
cdk deploy -c allow_unauthenticated=true ...
# (or "security": { "allow_unauthenticated": true } in deploy-config.json)
```

If you set neither, every `POST`/`PUT`/`PATCH`/`DELETE` request (including
scans and analysis) returns `503` with a message explaining how to enable them;
only viewing stored results works.
