# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""FastAPI server for Aurora Patch Agent UI.

Provides endpoints for:
- Fleet discovery and status
- Maintenance window configuration
- Triggering upgrades on-demand
- Tracking upgrade progress in real-time
"""
import os
import sys
import json
import uuid
import asyncio
import logging
import boto3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.discovery.handler import discover_clusters, find_available_upgrades, _serialize_cluster, _serialize_target
from agents.assessment.handler import handler as assess_handler, assess_cluster, _deserialize_target
from agents.execution.handler import create_blue_green, check_deployment_status, execute_switchover, delete_deployment, execute_inplace_upgrade
from agents.rollback.handler import execute_rollback
from agents.execution.preflight import run_preflight_checks, PREFLIGHT_CONFIG
from agents.validation.handler import handler as validate_handler
from agents.advisor.handler import get_advisor_recommendation
from agents.scanner.handler import scan_fleet_urgency
from agents.analyzer.handler import analyze_upgrade_readiness
from agents.planner.handler import generate_fleet_plan, adjust_plan_from_outcome

app = FastAPI(title="Aurora Patch Agent", version="1.0.0")

# CORS: the UI is served from the SAME origin as the API (LambdaRestApi proxy),
# so no cross-origin access is needed by default. Only allow the explicit origins
# listed in ALLOWED_ORIGINS (comma-separated). A wildcard with credentials is both
# insecure and rejected by browsers, so credentials are only enabled for an
# explicit allowlist.
_allowed_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=bool(_allowed_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── API authentication for state-changing endpoints (fail-closed) ────────────
# Every mutating request (POST/PUT/PATCH/DELETE) must present a token via
# `Authorization: Bearer <token>` or the `x-api-key` header. Read-only GETs stay
# open so the UI keeps rendering.
#
# SECURITY POSTURE: this is FAIL-CLOSED by default. If no API_AUTH_TOKEN is
# configured, every mutating endpoint is BLOCKED (503) rather than left open —
# so a default deploy cannot anonymously switch over, delete, or modify a
# cluster. There is exactly one escape hatch for local development:
# set ALLOW_UNAUTHENTICATED=true to explicitly run without a token. That env
# var is never set by the production CDK stack; opting out of auth is a
# conscious, auditable choice, not the default.
_API_AUTH_TOKEN = os.environ.get("API_AUTH_TOKEN", "")
_ALLOW_UNAUTHENTICATED = os.environ.get("ALLOW_UNAUTHENTICATED", "").lower() == "true"
_AUTH_EXEMPT_PREFIXES = ("/docs", "/openapi.json", "/redoc", "/static", "/health")
_MUTATING_METHODS = ("POST", "PUT", "PATCH", "DELETE")


@app.middleware("http")
async def _require_token_for_mutations(request, call_next):
    if request.method in _MUTATING_METHODS:
        path = request.url.path
        if not any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
            if _API_AUTH_TOKEN:
                # A token is configured — require a matching one.
                supplied = request.headers.get("x-api-key") or ""
                auth = request.headers.get("authorization", "")
                if auth.lower().startswith("bearer "):
                    supplied = auth[7:].strip()
                # Constant-time compare to avoid leaking the token via timing.
                import hmac
                if not (supplied and hmac.compare_digest(supplied, _API_AUTH_TOKEN)):
                    from starlette.responses import JSONResponse
                    return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            elif not _ALLOW_UNAUTHENTICATED:
                # No token configured and local-dev opt-out not set → fail closed.
                # This is the default posture: mutating endpoints are unavailable
                # until auth is configured (API_AUTH_TOKEN) or explicitly waived
                # for local development (ALLOW_UNAUTHENTICATED=true).
                from starlette.responses import JSONResponse
                return JSONResponse(
                    {"detail": (
                        "This endpoint changes infrastructure and is disabled "
                        "because no API_AUTH_TOKEN is configured. Set "
                        "API_AUTH_TOKEN to require authentication, or "
                        "ALLOW_UNAUTHENTICATED=true for local development only."
                    )},
                    status_code=503,
                )
    return await call_next(request)

# Persistent state — DynamoDB when available, file fallback for local dev.
# The /tmp path is Lambda's own per-invocation-environment scratch space (not
# a shared multi-tenant temp directory), and is only ever a fallback used
# when STATE_TABLE_NAME/DynamoDB isn't configured — production deployments
# via cdk/ always set STATE_TABLE_NAME, so this path is local-dev-only.
_STATE_TABLE_NAME = os.environ.get("STATE_TABLE_NAME", "")
_STATE_FILE = Path("/tmp/.state.json") if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") else Path(__file__).parent.parent / ".state.json"

HISTORY_TTL_DAYS = 60

_DEFAULT_STATE = {
    "maintenance_windows": {
        "dev": {"enabled": True, "start_hour_utc": 0, "end_hour_utc": 24, "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "clusters": []},
        "staging": {"enabled": True, "start_hour_utc": 2, "end_hour_utc": 8, "days": ["mon", "tue", "wed", "thu", "fri"], "clusters": []},
        "production": {"enabled": True, "start_hour_utc": 4, "end_hour_utc": 7, "days": ["sat", "sun"], "clusters": []},
    },
    "upgrades": {},
    "history": [],
    "last_scan": None,
    "clusters": [],
    "upgrade_targets": [],
}


def _purge_expired_history(state: dict):
    """Remove history entries older than TTL."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_TTL_DAYS)
    history = state.get("history", [])
    state["history"] = [
        h for h in history
        if datetime.fromisoformat(h.get("completed_at", h.get("started_at", "2000-01-01T00:00:00+00:00"))) > cutoff
    ]


_TERMINAL_STATUSES = ("complete", "failed", "preflight_failed", "validation_failed", "switchover_failed", "cancelled", "rolled_back")

# Only auto-migrate these to history — keep failed items visible in active for retry
_AUTO_ARCHIVE_STATUSES = ("complete", "cancelled", "rolled_back")


def _migrate_terminal_to_history(state: dict):
    """Move completed/cancelled upgrades to history. Failed upgrades stay in active for visibility."""
    to_move = [cid for cid, u in state.get("upgrades", {}).items() if u.get("status") in _AUTO_ARCHIVE_STATUSES]
    for cid in to_move:
        upgrade = state["upgrades"].pop(cid)
        if "completed_at" not in upgrade:
            upgrade["completed_at"] = upgrade.get("started_at", datetime.now(timezone.utc).isoformat())
        state["history"].append(upgrade)


def _ddb_table():
    """Get DynamoDB table resource if configured."""
    if not _STATE_TABLE_NAME:
        return None
    try:
        return boto3.resource("dynamodb").Table(_STATE_TABLE_NAME)
    except Exception:
        return None


def _load_state() -> dict:
    # Try DynamoDB first
    table = _ddb_table()
    if table:
        try:
            resp = table.get_item(Key={"pk": "STATE", "sk": "current"})
            if "Item" in resp:
                state = json.loads(resp["Item"]["data"])
                if "history" not in state:
                    state["history"] = []
                _migrate_terminal_to_history(state)
                _purge_expired_history(state)
                return state
        except Exception as e:
            logger.warning(f"DynamoDB load failed, falling back to file: {e}")

    # Fallback to file
    if _STATE_FILE.exists():
        try:
            state = json.loads(_STATE_FILE.read_text())
            if "history" not in state:
                state["history"] = []
            _migrate_terminal_to_history(state)
            _purge_expired_history(state)
            return state
        except (json.JSONDecodeError, IOError):
            pass
    return {**_DEFAULT_STATE}


def _save_state():
    state_json = json.dumps(_state, default=str)

    # Save to DynamoDB
    table = _ddb_table()
    if table:
        try:
            table.put_item(Item={
                "pk": "STATE",
                "sk": "current",
                "data": state_json,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            logger.warning(f"DynamoDB save failed: {e}")

    # Always save to file as backup
    try:
        _STATE_FILE.write_text(state_json)
    except IOError:
        pass


def _move_to_history(cluster_id: str):
    """Move a completed/failed/cancelled upgrade to history."""
    if cluster_id not in _state["upgrades"]:
        return
    upgrade = _state["upgrades"][cluster_id]
    upgrade["completed_at"] = datetime.now(timezone.utc).isoformat()
    _state["history"].append(upgrade)
    del _state["upgrades"][cluster_id]


_state = _load_state()


# ─────────────────────────────────────────────────────────────────
# Async job infrastructure
#
# API Gateway enforces a hard 29-second integration timeout that the Lambda
# timeout cannot override, and Mangum blocks the HTTP response until FastAPI
# BackgroundTasks finish — so slow Bedrock calls (analyze, version-drift) 504.
# Instead we self-invoke the Lambda asynchronously: the request handler creates
# a job, fires an async invoke (InvocationType=Event), and returns a job_id
# immediately; the async invocation runs the slow work within the 5-min Lambda
# budget and writes the result back to the job record. The UI polls GET
# /api/jobs/{job_id}. Falls back to running inline when not on Lambda (local dev).
# ─────────────────────────────────────────────────────────────────
JOB_TTL_DAYS = 1
_LAMBDA_FN_NAME = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")


def _job_key(job_id: str) -> dict:
    return {"pk": f"JOB#{job_id}", "sk": "job"}


def _create_job(kind: str, payload: dict) -> str:
    job_id = uuid.uuid4().hex
    record = {
        "job_id": job_id,
        "kind": kind,
        "status": "pending",
        "payload": payload,
        "result": None,
        "error": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_job(record)
    return job_id


def _write_job(record: dict):
    table = _ddb_table()
    if table:
        try:
            table.put_item(Item={
                **_job_key(record["job_id"]),
                "data": json.dumps(record, default=str),
                "ttl": int(datetime.now(timezone.utc).timestamp()) + 86400 * JOB_TTL_DAYS,
            })
            return
        except Exception as e:
            logger.warning(f"Job persist failed: {e}")
    # Local/file fallback keyed in-process
    _JOB_FALLBACK[record["job_id"]] = record


_JOB_FALLBACK: dict = {}


def _read_job(job_id: str) -> dict | None:
    table = _ddb_table()
    if table:
        try:
            resp = table.get_item(Key=_job_key(job_id))
            if "Item" in resp:
                return json.loads(resp["Item"]["data"])
        except Exception as e:
            logger.warning(f"Job read failed: {e}")
    return _JOB_FALLBACK.get(job_id)


# Registry of job kinds → synchronous worker functions (populated below).
_JOB_WORKERS: dict = {}


def _run_job(job_id: str):
    """Execute a job's worker synchronously and persist the outcome."""
    record = _read_job(job_id)
    if not record:
        logger.error(f"Job {job_id} not found")
        return
    worker = _JOB_WORKERS.get(record["kind"])
    if not worker:
        record["status"] = "failed"
        record["error"] = f"Unknown job kind: {record['kind']}"
        _write_job(record)
        return
    record["status"] = "running"
    record["started_at"] = datetime.now(timezone.utc).isoformat()
    _write_job(record)
    try:
        record["result"] = worker(record["payload"])
        record["status"] = "complete"
    except Exception as e:
        logger.error(f"Job {job_id} ({record['kind']}) failed: {e}")
        record["status"] = "failed"
        record["error"] = str(e)
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write_job(record)


def _dispatch_job(kind: str, payload: dict) -> dict:
    """Create a job and start it (async self-invoke on Lambda, inline otherwise)."""
    job_id = _create_job(kind, payload)
    if _LAMBDA_FN_NAME:
        try:
            boto3.client("lambda").invoke(
                FunctionName=_LAMBDA_FN_NAME,
                InvocationType="Event",  # async, returns immediately
                Payload=json.dumps({"__job__": job_id}).encode(),
            )
            return {"job_id": job_id, "status": "pending"}
        except Exception as e:
            logger.warning(f"Async self-invoke failed ({e}); running inline")
    # Local dev or invoke failure: run inline (may be slow, but correct).
    _run_job(job_id)
    return {"job_id": job_id, "status": _read_job(job_id).get("status", "complete")}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    record = _read_job(job_id)
    if not record:
        raise HTTPException(404, "Job not found")
    return {
        "job_id": job_id,
        "kind": record.get("kind"),
        "status": record.get("status"),
        "result": record.get("result"),
        "error": record.get("error"),
        "created_at": record.get("created_at"),
        "finished_at": record.get("finished_at"),
    }


# ─────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────
class MaintenanceWindow(BaseModel):
    enabled: bool = True
    start_hour_utc: int
    end_hour_utc: int
    days: list[str]
    clusters: list[str] = []  # empty = applies to all clusters in this environment


class UpgradeRequest(BaseModel):
    cluster_id: str
    target_version: str
    region: str
    environment: str
    strategy: str = "blue_green"
    reason: str = "version_upgrade"
    cve_ids: list[str] = []
    skip_preflight: bool = False
    skip_window_check: bool = False


class SwitchoverRequest(BaseModel):
    deployment_id: str
    region: str


class RollbackRequest(BaseModel):
    deployment_id: str
    region: str
    cluster_id: str


class AdvisorRequest(BaseModel):
    cluster_id: str
    region: str = ""
    question: Optional[str] = None
    error: Optional[str] = None
    target_version: Optional[str] = None


# ─────────────────────────────────────────────────────────────────
# Fleet Discovery
# ─────────────────────────────────────────────────────────────────
@app.get("/api/fleet")
async def get_fleet():
    """Return current fleet status from last scan."""
    return {
        "clusters": _state["clusters"],
        "upgrade_targets": _state["upgrade_targets"],
        "last_scan": _state["last_scan"],
    }


@app.post("/api/fleet/scan")
async def scan_fleet(regions: list[str] = None):
    """Trigger a fresh fleet scan across configured regions."""
    from shared.config import get_scan_regions
    if not regions:
        regions = get_scan_regions()

    all_clusters = []
    all_targets = []

    for region in regions:
        clusters = discover_clusters(region, "")
        for c in clusters:
            serialized = _serialize_cluster(c)
            serialized["region"] = region
            target = find_available_upgrades(c)
            if target:
                serialized["upgrade_available"] = True
                serialized["target_version"] = target.target_version
                serialized["available_versions"] = target.available_versions
                all_targets.append(_serialize_target(target))
            else:
                serialized["upgrade_available"] = False
                serialized["target_version"] = None
                serialized["available_versions"] = []

            # Add LTS info
            from agents.scanner.handler import _get_lts_info, _get_recommended_lts
            engine_type = serialized.get("engine_type", "aurora-postgresql")
            current_lts = _get_lts_info(serialized["engine_version"], engine_type)
            target_lts = _get_lts_info(serialized.get("target_version", ""), engine_type) if serialized.get("target_version") else None
            recommended_lts = _get_recommended_lts(serialized["engine_version"], engine_type)
            serialized["is_lts"] = current_lts is not None
            serialized["lts_info"] = current_lts
            serialized["target_is_lts"] = target_lts is not None
            serialized["recommended_lts"] = recommended_lts

            all_clusters.append(serialized)

    _state["clusters"] = all_clusters
    _state["upgrade_targets"] = all_targets
    _state["last_scan"] = datetime.now(timezone.utc).isoformat()
    _save_state()

    return {
        "clusters": all_clusters,
        "upgrade_targets": all_targets,
        "last_scan": _state["last_scan"],
    }


# ─────────────────────────────────────────────────────────────────
# Maintenance Windows
# ─────────────────────────────────────────────────────────────────
@app.get("/api/windows")
async def get_maintenance_windows():
    """Get all maintenance window configs."""
    return _state["maintenance_windows"]


@app.put("/api/windows/{environment}")
async def set_maintenance_window(environment: str, window: MaintenanceWindow):
    """Set maintenance window for an environment."""
    if environment not in ("dev", "staging", "production"):
        raise HTTPException(400, f"Invalid environment: {environment}")
    _state["maintenance_windows"][environment] = window.model_dump()
    PREFLIGHT_CONFIG["change_windows"][environment] = {
        "start_hour_utc": window.start_hour_utc,
        "end_hour_utc": window.end_hour_utc,
        "days": window.days,
        "clusters": window.clusters,
    } if window.enabled else None
    _save_state()
    return {"status": "updated", "environment": environment, "window": window.model_dump()}


# ─────────────────────────────────────────────────────────────────
# Upgrade Operations
# ─────────────────────────────────────────────────────────────────
@app.get("/api/upgrades")
async def get_upgrades():
    """Get all active upgrades + history."""
    return {
        "active": _state["upgrades"],
        "history": _state.get("history", []),
    }


@app.get("/api/upgrades/{cluster_id}")
async def get_upgrade_status(cluster_id: str):
    """Get upgrade progress for a specific cluster (checks active + history)."""
    if cluster_id in _state["upgrades"]:
        return _state["upgrades"][cluster_id]
    # Search history
    for h in reversed(_state.get("history", [])):
        if h.get("cluster_id") == cluster_id:
            return h
    raise HTTPException(404, f"No upgrade found for {cluster_id}")


@app.post("/api/upgrades/preflight")
async def run_preflight(request: UpgradeRequest):
    """Run pre-flight checks without starting upgrade."""
    env = request.environment if not request.skip_window_check else "dev"
    result = run_preflight_checks(request.cluster_id, request.region, env)
    return result


@app.post("/api/upgrades/assess")
async def assess_single_cluster(request: UpgradeRequest):
    """Run full assessment (risk + preflight) for a single cluster for the UI guardrails panel."""
    from agents.assessment.handler import assess_cluster, _deserialize_target

    target_data = {
        "cluster_id": request.cluster_id,
        "current_version": "",
        "target_version": request.target_version,
        "region": request.region,
        "environment": request.environment,
        "extensions": [],
        "parameter_group": "",
    }
    # Get live cluster info
    try:
        client = rds_client(request.region)
        resp = client.describe_db_clusters(DBClusterIdentifier=request.cluster_id)
        cluster_data = resp["DBClusters"][0]
        target_data["current_version"] = cluster_data["EngineVersion"]
        target_data["parameter_group"] = cluster_data.get("DBClusterParameterGroup", "")
        target_data["storage_gb"] = cluster_data.get("AllocatedStorage", 0)
        target_data["instance_count"] = len(cluster_data.get("DBClusterMembers", []))
    except Exception:
        pass

    # Assessment (risk scoring)
    target = _deserialize_target(target_data)
    assessment = assess_cluster(target)

    # Preflight checks (operational readiness) — dry_run=True skips snapshot creation and lock acquisition
    env = request.environment if not request.skip_window_check else "dev"
    preflight = run_preflight_checks(request.cluster_id, request.region, env, dry_run=True)

    return {
        "cluster_id": request.cluster_id,
        "assessment": assessment,
        "preflight": preflight,
        "ready_to_upgrade": preflight["all_passed"] and assessment["risk_score"] < 70,
    }


from shared.aws_clients import rds_client as _rds_client_fn
from shared.aws_clients import cloudwatch_client

def rds_client(region):
    return _rds_client_fn(region)


@app.post("/api/upgrades/start")
async def start_upgrade(request: UpgradeRequest, background_tasks: BackgroundTasks):
    """Start an upgrade (pre-flight + Blue/Green creation)."""
    cluster_id = request.cluster_id

    if cluster_id in _state["upgrades"] and _state["upgrades"][cluster_id].get("status") in ("preflight", "provisioning", "ready", "switching"):
        raise HTTPException(409, f"Upgrade already in progress for {cluster_id}")

    # Check Global Database constraints before starting
    from shared.global_db import check_upgrade_eligibility
    engine_type = request.context.get("engine_type", "aurora-postgresql") if hasattr(request, 'context') else "aurora-postgresql"
    # Determine engine from fleet state
    for c in _state.get("clusters", []):
        if c.get("cluster_id") == cluster_id:
            engine_type = c.get("engine_type", "aurora-postgresql")
            break
    gdb_check = check_upgrade_eligibility(cluster_id, request.region, request.target_version, engine_type)
    if not gdb_check.get("eligible", True):
        raise HTTPException(400, {
            "error": "global_database_constraint",
            "message": gdb_check["reason"],
            "strategy": gdb_check.get("strategy"),
            "steps": gdb_check.get("steps", []),
        })

    _state["upgrades"][cluster_id] = {
        "cluster_id": cluster_id,
        "target_version": request.target_version,
        "region": request.region,
        "environment": request.environment,
        "strategy": request.strategy,
        "reason": request.reason,
        "cve_ids": request.cve_ids,
        "status": "preflight",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "steps": [],
        "deployment_id": None,
        "error": None,
    }

    _save_state()
    background_tasks.add_task(_run_upgrade_pipeline, request)
    return {"status": "started", "cluster_id": cluster_id}


@app.post("/api/upgrades/switchover")
async def trigger_switchover(request: SwitchoverRequest):
    """Manually trigger switchover for a ready deployment."""
    result = execute_switchover(request.deployment_id, request.region)

    for cluster_id, upgrade in _state["upgrades"].items():
        if upgrade.get("deployment_id") == request.deployment_id:
            upgrade["status"] = "switching"
            upgrade["steps"].append({
                "step": "switchover",
                "time": datetime.now(timezone.utc).isoformat(),
                "result": result,
            })
            break

    _save_state()
    return result


@app.post("/api/upgrades/rollback")
async def rollback_upgrade(request: RollbackRequest):
    """Rollback an upgrade: deletes B/G deployment and green cluster (pre-switchover) or cleans up metadata (post-switchover)."""
    cluster_id = request.cluster_id

    # Use the Rollback Agent's logic which handles pre/post-switchover correctly
    result = execute_rollback(cluster_id, request.region, request.deployment_id)

    if cluster_id in _state["upgrades"]:
        upgrade = _state["upgrades"][cluster_id]
        upgrade["status"] = "rolled_back"
        upgrade["steps"].append({
            "step": "rollback",
            "time": datetime.now(timezone.utc).isoformat(),
            "result": result,
        })
        _move_to_history(cluster_id)
    else:
        # Update in history
        for h in _state["history"]:
            if h.get("deployment_id") == request.deployment_id:
                h["status"] = "rolled_back"
                h["steps"].append({
                    "step": "rollback",
                    "time": datetime.now(timezone.utc).isoformat(),
                    "result": result,
                })
                break

    _save_state()
    return {"status": "rolled_back", "cluster_id": cluster_id, "result": result}


@app.post("/api/upgrades/{cluster_id}/cancel")
async def cancel_upgrade(cluster_id: str, request: dict = None):
    """Cancel or dismiss an upgrade.

    Query param or body field 'dismiss=true' just moves to history without
    deleting the deployment (for failed items the user has reviewed).
    Without dismiss, actually deletes the Blue/Green deployment.
    """
    dismiss_only = False
    if request and request.get("dismiss"):
        dismiss_only = True

    # Check active upgrades first, then history
    if cluster_id not in _state["upgrades"]:
        # Try to find in history and delete deployment directly
        for h in _state["history"]:
            if h.get("cluster_id") == cluster_id and h.get("deployment_id") and h.get("status") not in ("cancelled", "rolled_back"):
                if not dismiss_only:
                    result = delete_deployment(h["deployment_id"], h["region"])
                    h["steps"].append({"step": "cancelled", "time": datetime.now(timezone.utc).isoformat(), "result": result})
                h["status"] = "cancelled"
                _save_state()
                return {"status": "cancelled", "cluster_id": cluster_id}
        raise HTTPException(404, f"No active upgrade for {cluster_id}")

    upgrade = _state["upgrades"][cluster_id]
    deployment_id = upgrade.get("deployment_id")

    if deployment_id and not dismiss_only:
        # Only delete the deployment if it's not in a ready/switchover state
        # and we're not just dismissing
        result = delete_deployment(deployment_id, upgrade.get("region", ""))
        upgrade["status"] = "cancelled"
        upgrade["steps"].append({
            "step": "cancelled",
            "time": datetime.now(timezone.utc).isoformat(),
            "result": result,
        })
    else:
        upgrade["status"] = "cancelled"

    _move_to_history(cluster_id)
    _save_state()
    return {"status": "cancelled", "cluster_id": cluster_id}


@app.post("/api/deployments/delete")
async def delete_bg_deployment(request: dict):
    """Delete a Blue/Green deployment directly (from Fleet view).

    Accepts either a deployment_id directly OR a cluster_id to look up the deployment.
    This handles:
    - Pre-switchover: deletes green cluster + deployment metadata
    - Post-switchover: only deletes deployment metadata (green is now primary)
    """
    deployment_id = request.get("deployment_id")
    cluster_id = request.get("cluster_id")
    region = request.get("region", "")
    delete_target = request.get("delete_target", True)

    # If no deployment_id, find it by cluster_id
    if not deployment_id and cluster_id:
        client = rds_client(region)
        try:
            resp = client.describe_blue_green_deployments()
            for dep in resp.get("BlueGreenDeployments", []):
                source_arn = dep.get("Source", "")
                target_arn = dep.get("Target", "")
                source_id = source_arn.split(":cluster:")[-1] if ":cluster:" in source_arn else ""
                target_id = target_arn.split(":cluster:")[-1] if ":cluster:" in target_arn else ""
                if cluster_id in (source_id, target_id):
                    deployment_id = dep["BlueGreenDeploymentIdentifier"]
                    break
        except Exception as e:
            return {"status": "error", "message": f"Failed to find deployment: {str(e)}"}

    if not deployment_id:
        raise HTTPException(404, "No Blue/Green deployment found for this cluster")

    result = delete_deployment(deployment_id, region, delete_target=delete_target)

    # Mark history entry as cleaned up
    if result.get("status") == "deleted":
        for h in _state.get("history", []):
            if h.get("deployment_id") == deployment_id:
                h["cleaned_up"] = True
                h["cleaned_up_at"] = datetime.now(timezone.utc).isoformat()
                break
        _save_state()

    return result


@app.post("/api/clusters/{cluster_id}/delete")
async def delete_old_cluster(cluster_id: str, request: dict):
    """Delete an old primary cluster that's no longer needed (post-switchover cleanup).

    Two server-side safety controls apply here, independent of (and in addition
    to) the UI confirmation dialog — so they also protect direct API callers:

    1. CONFIRMATION: the request body must include `confirm_cluster_id` equal to
       the cluster being deleted. This prevents a bare/accidental call (or a
       request that targets the wrong cluster) from deleting anything — the
       caller has to name the exact cluster twice.
    2. FINAL SNAPSHOT: a final snapshot is taken BY DEFAULT. Deletion only skips
       the final snapshot if the caller explicitly sets
       `skip_final_snapshot: true`, which is logged in the response. This makes
       data loss opt-in rather than the silent default.
    """
    region = request.get("region", "")
    confirm_cluster_id = request.get("confirm_cluster_id", "")
    skip_final_snapshot = bool(request.get("skip_final_snapshot", False))

    # Control 1: require explicit confirmation echoing the exact cluster id.
    if confirm_cluster_id != cluster_id:
        raise HTTPException(
            400,
            "Refusing to delete: request must include 'confirm_cluster_id' "
            f"equal to '{cluster_id}' to confirm this destructive action.",
        )

    client = rds_client(region)

    try:
        response = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
        if not response["DBClusters"]:
            raise HTTPException(404, f"Cluster {cluster_id} not found")

        # A short, unique, RDS-identifier-safe final snapshot name derived from
        # the cluster id and a timestamp. Only used when NOT skipping snapshot.
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        final_snapshot_id = f"{cluster_id}-final-{ts}"[:255]

        # Delete all instances first, then the cluster.
        # Instance-level deletes always skip the per-instance final snapshot —
        # for a cluster, the meaningful backup is the cluster-level final
        # snapshot taken below, not per-instance ones.
        cluster = response["DBClusters"][0]
        for member in cluster.get("DBClusterMembers", []):
            instance_id = member["DBInstanceIdentifier"]
            client.delete_db_instance(
                DBInstanceIdentifier=instance_id,
                SkipFinalSnapshot=True,
            )

        # Control 2: take a cluster final snapshot by default.
        if skip_final_snapshot:
            client.delete_db_cluster(
                DBClusterIdentifier=cluster_id,
                SkipFinalSnapshot=True,
            )
            snapshot_msg = "No final snapshot taken (skip_final_snapshot=true)."
        else:
            client.delete_db_cluster(
                DBClusterIdentifier=cluster_id,
                SkipFinalSnapshot=False,
                FinalDBSnapshotIdentifier=final_snapshot_id,
            )
            snapshot_msg = f"Final snapshot '{final_snapshot_id}' will be created."

        return {
            "status": "deleting",
            "cluster_id": cluster_id,
            "message": f"Cluster and instances deletion initiated. {snapshot_msg}",
            "final_snapshot_id": None if skip_final_snapshot else final_snapshot_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ─────────────────────────────────────────────────────────────────
# Remediation knowledge base — maps known errors to fix instructions
# ─────────────────────────────────────────────────────────────────
_REMEDIATION_MAP = [
    {
        "pattern": "SourceClusterNotSupportedFault",
        "match_key": "parameter group",
        "diagnosis": "Writer instance parameter group is out of sync with cluster parameter group.",
        "remediation": "Reboot the writer instance to apply pending cluster parameter group changes: aws rds reboot-db-instance --db-instance-identifier <writer-instance-id>",
        "auto_fixable": True,
        "fix_action": "reboot",
        "fix_details": {"description": "Reboot writer instance to sync parameter group"},
    },
    {
        "pattern": "SourceClusterNotSupportedFault",
        "match_key": "logical replication",
        "diagnosis": "Blue/Green deployment requires rds.logical_replication=1 in the cluster parameter group.",
        "remediation": "Create/modify a custom cluster parameter group with rds.logical_replication=1, apply it to the cluster, then reboot the writer.",
        "auto_fixable": True,
        "fix_action": "parameter_group",
        "fix_details": {"description": "Enable logical replication in cluster parameter group"},
    },
    {
        "pattern": "manage-master-user-password",
        "match_key": "",
        "diagnosis": "Blue/Green deployment does not support Secrets Manager managed passwords.",
        "remediation": "Switch to static master password: aws rds modify-db-cluster --db-cluster-identifier <cluster> --master-user-password <new-pw> --no-manage-master-user-password --apply-immediately",
        # Manual-only: changing a cluster's master password is a sensitive
        # operation and is deliberately NOT auto-applicable (no execute-step
        # action, no apply-fix branch). The operator runs the command above
        # themselves. See the removed _exec_modify_cluster_password note.
        "auto_fixable": False,
        "fix_action": "manual",
        "fix_details": {"description": "Disable Secrets Manager managed password (manual — run the command above)"},
    },
    {
        "pattern": "already_exists",
        "match_key": "",
        "diagnosis": "A Blue/Green deployment already exists for this cluster.",
        "remediation": "Delete the existing deployment first (use 'Delete Deployment' button in the UI or: aws rds delete-blue-green-deployment --blue-green-deployment-identifier <id> --delete-target), then retry.",
        "auto_fixable": True,
        "fix_action": "delete_deployment",
        "fix_details": {"description": "Delete existing Blue/Green deployment"},
    },
    {
        "pattern": "InvalidDBClusterStateFault",
        "match_key": "",
        "diagnosis": "Cluster is not in 'available' state (may be modifying, rebooting, or resetting credentials).",
        "remediation": "Wait for cluster to return to 'available' state, then retry. Check: aws rds describe-db-clusters --db-cluster-identifier <cluster> --query 'DBClusters[0].Status'",
        "auto_fixable": False,
        "fix_action": "wait",
        "fix_details": {"description": "Wait for cluster to reach available state"},
    },
    {
        "pattern": "INVALID_CONFIGURATION",
        "match_key": "",
        "diagnosis": "Green cluster failed to configure properly. May be caused by incompatible extensions or parameter settings.",
        "remediation": "Check the RDS events for the deployment: aws rds describe-events --source-identifier <deployment-id> --source-type db-cluster. Fix the underlying issue and retry.",
        "auto_fixable": False,
        "fix_action": "manual",
        "fix_details": {"description": "Manual investigation required"},
    },
    {
        "pattern": "replication lag",
        "match_key": "",
        "diagnosis": "Replica lag exceeded threshold. High write throughput or reader instance issues.",
        "remediation": "Reduce write load or wait for replicas to catch up. Monitor: aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name AuroraReplicaLag",
        "auto_fixable": False,
        "fix_action": "wait",
        "fix_details": {"description": "Wait for replication to catch up"},
    },
    {
        "pattern": "change_window",
        "match_key": "",
        "diagnosis": "Current time is outside the approved maintenance window for this environment.",
        "remediation": "Either wait for the next maintenance window, or use 'Skip window check' in the UI to override.",
        "auto_fixable": True,
        "fix_action": "skip_window",
        "fix_details": {"description": "Override maintenance window check"},
    },
    {
        "pattern": "long_running",
        "match_key": "",
        "diagnosis": "High connection count detected — possible long-running transactions that could block upgrade.",
        "remediation": "Identify and terminate blocking queries: SELECT pid, now()-query_start AS duration, query FROM pg_stat_activity WHERE state != 'idle' ORDER BY duration DESC;",
        "auto_fixable": False,
        "fix_action": "manual",
        "fix_details": {"description": "Terminate blocking queries manually"},
    },
    {
        "pattern": "AccessDenied",
        "match_key": "not authorized to perform",
        "diagnosis": "Lambda execution role is missing the required IAM permission.",
        "remediation": "Add the missing IAM permission to the Lambda role. The specific action needed is in the error message.",
        "auto_fixable": True,
        "fix_action": "iam_policy",
        "fix_details": {"description": "Add missing IAM permission to Lambda role"},
    },
]


def _diagnose_error(error_msg: str) -> dict:
    """Match error against known issues and return diagnosis + remediation."""
    if not error_msg:
        return {}
    error_lower = error_msg.lower()
    for entry in _REMEDIATION_MAP:
        if entry["pattern"].lower() in error_lower:
            if entry["match_key"] and entry["match_key"].lower() not in error_lower:
                continue
            return {
                "diagnosis": entry["diagnosis"],
                "remediation": entry["remediation"],
                "auto_fixable": entry["auto_fixable"],
                "fix_action": entry.get("fix_action", ""),
                "fix_details": entry.get("fix_details", {}),
            }
    return {
        "diagnosis": "Unknown error — manual investigation required.",
        "remediation": f"Check RDS console and CloudWatch logs. Error: {error_msg[:200]}",
        "auto_fixable": False,
        "fix_action": "",
        "fix_details": {},
    }


class DiagnoseRequest(BaseModel):
    error: str
    cluster_id: str = ""
    step: str = ""
    context: dict = {}


@app.post("/api/diagnose")
async def ai_diagnose_error(request: DiagnoseRequest):
    """AI-powered error diagnosis — checks learned fixes first, then Bedrock.

    Resolution priority:
    1. Learned fixes (instant, from past successful resolutions)
    2. Static remediation map (instant, hardcoded known patterns)
    3. Bedrock Claude (6s, for novel errors — result is learned if fix succeeds)
    """
    import re

    # Priority 1: Check learned fixes from past successful resolutions
    from shared.outcome_learning import lookup_learned_fix
    learned = lookup_learned_fix(request.error)
    if learned:
        learned["source"] = "learned"
        return learned

    # Priority 2: Try static map
    static = _diagnose_error(request.error)
    if static.get("diagnosis") and "Unknown error" not in static["diagnosis"]:
        static["source"] = "static"
        return static

    # Call Bedrock for intelligent diagnosis
    prompt = f"""You are an AWS Aurora PostgreSQL database operations expert. Analyze this error and provide a precise diagnosis and fix.

## Error
{request.error}

## Context
- Cluster: {request.cluster_id}
- Step that failed: {request.step}
- Additional context: {json.dumps(request.context, default=str)}

## Your task
1. Identify the ROOT CAUSE (be specific, not generic)
2. Provide the EXACT fix (AWS CLI commands, IAM policy changes, or config changes)
3. Determine if this can be auto-fixed programmatically

Common patterns:
- AccessDenied → missing IAM permission on the Lambda execution role
- SourceClusterNotSupportedFault → parameter group or logical replication issue
- InvalidDBClusterStateFault → cluster is in a non-available state

Respond with JSON only:
{{
  "diagnosis": "One clear sentence explaining the root cause",
  "remediation": "Step-by-step fix instructions with exact commands",
  "auto_fixable": true/false,
  "fix_action": "iam_policy|parameter_group|reboot|wait|manual",
  "fix_details": {{
    "description": "What the fix will do",
    "actions": ["action1", "action2"]
  }}
}}"""

    try:
        from shared.aws_clients import bedrock_client
        from shared.config import get_model_id
        client = bedrock_client()
        response = client.invoke_model(
            modelId=get_model_id(),
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        body = json.loads(response["body"].read())
        # Extract text from response — skip thinking blocks
        text = None
        if "content" in body and body["content"]:
            for block in body["content"]:
                if block.get("type") == "text" and block.get("text"):
                    text = block["text"]
                    break
        elif "output" in body:
            msg = body["output"].get("message", {})
            for block in msg.get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    text = block["text"]
                    break
        if not text:
            logger.error(f"No text block in Bedrock response: {json.dumps(body)[:500]}")
            return static
        # Strip markdown code fences if present
        if "```" in text:
            import re as _re
            fence_match = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, _re.DOTALL)
            if fence_match:
                text = fence_match.group(1)
        # Extract JSON from response
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            result = json.loads(text[start:end + 1])
            return result
        else:
            return {
                "diagnosis": text[:200].strip(),
                "remediation": text[200:].strip() if len(text) > 200 else "See diagnosis above.",
                "auto_fixable": False,
            }
    except Exception as e:
        logger.error(f"AI diagnosis failed: {e} — response text: {text[:200] if text else 'None'}")

    return static


@app.post("/api/diagnose/apply-fix")
async def apply_diagnosed_fix(request: DiagnoseRequest):
    """Apply an AI-recommended fix (currently supports IAM policy additions).

    When a fix succeeds, records the error→fix mapping in the learning store
    so future identical errors are resolved instantly without Bedrock.
    """
    import re
    from shared.outcome_learning import record_error_resolution

    error = request.error
    fix_action = request.context.get("fix_action", "")
    diagnosis = request.context.get("diagnosis", "")

    if fix_action == "iam_policy":
        # Extract the missing action from the error
        action_match = re.search(r"perform: (\S+)", error)
        if not action_match:
            return {"status": "error", "message": "Could not parse missing IAM action from error"}

        missing_action = action_match.group(1)

        # Security: only self-grant actions on an explicit allowlist. Without this,
        # parsing an arbitrary action from an error message and attaching it to our
        # own role is a privilege-escalation primitive (e.g. iam:*, sts:AssumeRole).
        # We only ever auto-add the read/describe + narrowly-scoped RDS actions this
        # agent legitimately needs; anything else requires a human.
        ALLOWED_SELF_GRANT = {
            "rds:DescribeDBClusters", "rds:DescribeDBInstances",
            "rds:DescribeDBEngineVersions", "rds:DescribeDBClusterParameters",
            "rds:DescribeDBClusterParameterGroups", "rds:DescribeDBParameterGroups",
            "rds:DescribeDBSubnetGroups", "rds:DescribeDBClusterSnapshots",
            "rds:DescribeBlueGreenDeployments", "rds:ListTagsForResource",
            "cloudwatch:GetMetricStatistics", "cloudwatch:DescribeAlarms",
        }
        if missing_action not in ALLOWED_SELF_GRANT:
            return {
                "status": "error",
                "message": (
                    f"Refusing to self-grant '{missing_action}': it is not on the "
                    "read-only auto-fix allowlist. Add this permission manually after review."
                ),
            }

        # Extract the role name
        role_match = re.search(r"assumed-role/([^/]+)/", error)
        if not role_match:
            return {"status": "error", "message": "Could not parse role name from error"}

        role_name = role_match.group(1)

        try:
            iam = boto3.client("iam")
            policy_name = "AuroraPatchAgentDynamicFix"

            # Get existing inline policy or create new one
            try:
                existing = iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)
                policy_doc = existing["PolicyDocument"]
                # Add the missing action to the existing statement
                stmt = policy_doc["Statement"][0]
                if missing_action not in stmt["Action"]:
                    stmt["Action"].append(missing_action)
            except iam.exceptions.NoSuchEntityException:
                policy_doc = {
                    "Version": "2012-10-17",
                    "Statement": [{
                        "Effect": "Allow",
                        "Action": [missing_action],
                        "Resource": "*",
                    }]
                }

            iam.put_role_policy(
                RoleName=role_name,
                PolicyName=policy_name,
                PolicyDocument=json.dumps(policy_doc),
            )

            # Learn from this successful fix
            record_error_resolution(
                error_pattern=error,
                diagnosis=diagnosis or f"Missing IAM permission: {missing_action}",
                fix_applied=f"Added {missing_action} to role {role_name} via inline policy '{policy_name}'",
                fix_succeeded=True,
                cluster_id=request.cluster_id,
                context={"fix_action": fix_action, "missing_action": missing_action},
            )

            return {
                "status": "success",
                "message": f"Added {missing_action} permission to role {role_name}",
                "action_taken": f"Attached inline policy '{policy_name}' with action '{missing_action}'",
                "next_step": "Retry the upgrade — the new permission takes effect immediately.",
            }
        except Exception as e:
            return {"status": "error", "message": f"Failed to apply IAM fix: {str(e)}"}

    elif fix_action == "reboot":
        # Reboot the writer instance to sync parameter group
        cluster_id = request.cluster_id
        if not cluster_id:
            return {"status": "error", "message": "cluster_id is required for reboot fix"}
        try:
            rds = boto3.client("rds", region_name=os.environ.get("AWS_REGION", "us-east-1"))
            # Find the writer instance
            resp = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)
            if not resp["DBClusters"]:
                return {"status": "error", "message": f"Cluster {cluster_id} not found"}
            cluster = resp["DBClusters"][0]
            writer_id = None
            for member in cluster.get("DBClusterMembers", []):
                if member.get("IsClusterWriter"):
                    writer_id = member["DBInstanceIdentifier"]
                    break
            if not writer_id:
                return {"status": "error", "message": "Could not find writer instance"}

            rds.reboot_db_instance(DBInstanceIdentifier=writer_id)

            record_error_resolution(
                error_pattern=error,
                diagnosis=diagnosis or "Parameter group out of sync",
                fix_applied=f"Rebooted writer instance {writer_id}",
                fix_succeeded=True,
                cluster_id=cluster_id,
                context={"fix_action": fix_action, "writer_id": writer_id},
            )

            return {
                "status": "success",
                "message": f"Rebooted writer instance {writer_id}",
                "action_taken": f"Initiated reboot of {writer_id} to sync parameter group",
                "next_step": "Wait 2-3 minutes for instance to become available, then retry the upgrade.",
            }
        except Exception as e:
            return {"status": "error", "message": f"Failed to reboot: {str(e)}"}

    elif fix_action == "delete_deployment":
        # Delete the existing Blue/Green deployment blocking a new one
        cluster_id = request.cluster_id
        if not cluster_id:
            return {"status": "error", "message": "cluster_id is required for delete deployment fix"}
        try:
            rds = boto3.client("rds", region_name=os.environ.get("AWS_REGION", "us-east-1"))
            # Find existing deployment for this cluster.
            # Match the cluster identifier EXACTLY against the one parsed out of
            # the deployment's Source ARN — NOT a substring test. A substring
            # match (`cluster_id in source`) over-matches badly: cluster_id="a"
            # would match nearly every deployment (the ARN always contains "a"),
            # and this path then calls delete_blue_green_deployment with
            # DeleteTarget=True, dropping the green cluster. Exact-match on the
            # ARN-tail cluster id prevents deleting an unrelated deployment.
            resp = rds.describe_blue_green_deployments()
            target_deployment = None
            for dep in resp.get("BlueGreenDeployments", []):
                source = dep.get("Source", "")
                source_cluster_id = source.split(":cluster:")[-1] if ":cluster:" in source else ""
                if source_cluster_id == cluster_id:
                    target_deployment = dep["BlueGreenDeploymentIdentifier"]
                    break

            if not target_deployment:
                return {"status": "error", "message": f"No existing deployment found for {cluster_id}"}

            rds.delete_blue_green_deployment(
                BlueGreenDeploymentIdentifier=target_deployment,
                DeleteTarget=True,
            )

            record_error_resolution(
                error_pattern=error,
                diagnosis=diagnosis or "Existing deployment blocking new creation",
                fix_applied=f"Deleted existing deployment {target_deployment}",
                fix_succeeded=True,
                cluster_id=cluster_id,
                context={"fix_action": fix_action, "deployment_id": target_deployment},
            )

            return {
                "status": "success",
                "message": f"Deleted deployment {target_deployment}",
                "action_taken": f"Removed existing Blue/Green deployment blocking new upgrade",
                "next_step": "Wait 1-2 minutes for cleanup, then retry the upgrade.",
            }
        except Exception as e:
            return {"status": "error", "message": f"Failed to delete deployment: {str(e)}"}

    elif fix_action in ("wait", "manual", "skip_window"):
        # These actions can't be auto-applied — provide guidance
        guidance = {
            "wait": "This issue will resolve itself. Wait a few minutes and retry.",
            "manual": "This requires manual investigation. See the remediation steps above.",
            "skip_window": "Retry the upgrade with 'Skip window check' enabled in the UI.",
        }
        return {
            "status": "info",
            "message": guidance.get(fix_action, "Manual action required"),
            "action_taken": "none",
            "next_step": guidance.get(fix_action, "Review the remediation steps and act manually."),
        }

    return {"status": "error", "message": f"Fix action '{fix_action}' is not yet supported for auto-apply."}


def _record_step(upgrade: dict, step_name: str, status: str, result: dict = None,
                 inputs: dict = None, error: str = None, duration_sec: float = None):
    """Record a detailed step entry in the upgrade log."""
    entry = {
        "step": step_name,
        "status": status,  # "started", "success", "failed", "skipped"
        "time": datetime.now(timezone.utc).isoformat(),
        "duration_sec": duration_sec,
        "inputs": inputs,
        "result": result,
        "error": error,
    }
    if error:
        entry["diagnosis"] = _diagnose_error(error)
    upgrade["steps"].append(entry)


# ─────────────────────────────────────────────────────────────────
# Background upgrade pipeline
# ─────────────────────────────────────────────────────────────────
async def _run_upgrade_pipeline(request: UpgradeRequest):
    """Run the full upgrade pipeline in the background."""
    import time as _time

    cluster_id = request.cluster_id
    upgrade = _state["upgrades"][cluster_id]

    try:
        # Step 1: Pre-flight
        if not request.skip_preflight:
            t0 = _time.time()
            env = request.environment if not request.skip_window_check else "dev"
            preflight = run_preflight_checks(cluster_id, request.region, env)
            elapsed = round(_time.time() - t0, 1)

            failed_checks = [name for name, c in preflight.get("checks", {}).items() if not c["passed"]]
            if not preflight["all_passed"]:
                error_detail = "; ".join(
                    f"{name}: {preflight['checks'][name]['reason']}" for name in failed_checks
                )
                _record_step(upgrade, "preflight", "failed",
                             result=preflight, inputs={"environment": env, "cluster_id": cluster_id},
                             error=error_detail, duration_sec=elapsed)
                upgrade["status"] = "preflight_failed"
                upgrade["error"] = error_detail
                _save_state()
                return
            else:
                _record_step(upgrade, "preflight", "success",
                             result=preflight, inputs={"environment": env, "cluster_id": cluster_id},
                             duration_sec=elapsed)
        else:
            _record_step(upgrade, "preflight", "skipped",
                         inputs={"reason": "Pre-flight already ran in guardrails panel"})

        # Step 2: Execute upgrade based on strategy
        strategy = request.strategy or "blue_green"
        upgrade["strategy"] = strategy

        if strategy == "in_place":
            # In-place upgrade path (for versions that don't support Blue/Green)
            upgrade["status"] = "upgrading_inplace"
            _save_state()
            t0 = _time.time()
            ip_result = execute_inplace_upgrade(cluster_id, request.target_version, request.region)
            elapsed = round(_time.time() - t0, 1)

            if ip_result.get("status") == "error":
                error_msg = ip_result.get("message", "In-place upgrade failed")
                _record_step(upgrade, "inplace_upgrade", "failed",
                             result=ip_result,
                             inputs={"cluster_id": cluster_id, "target_version": request.target_version, "strategy": "in_place"},
                             error=error_msg, duration_sec=elapsed)
                upgrade["status"] = "failed"
                upgrade["error"] = error_msg
                _save_state()
                return

            _record_step(upgrade, "inplace_upgrade", "success",
                         result=ip_result,
                         inputs={"cluster_id": cluster_id, "target_version": request.target_version, "strategy": "in_place"},
                         duration_sec=elapsed)

            # Wait for cluster to return to available state
            upgrade["status"] = "waiting_inplace"
            _save_state()
            t0 = _time.time()
            for _ in range(120):  # up to 60 min
                await asyncio.sleep(30)
                try:
                    client = rds_client(request.region)
                    resp = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
                    status = resp["DBClusters"][0]["Status"]
                    version = resp["DBClusters"][0]["EngineVersion"]
                    if status == "available" and version == request.target_version:
                        elapsed = round(_time.time() - t0, 1)
                        _record_step(upgrade, "wait_available", "success",
                                     result={"status": status, "version": version},
                                     duration_sec=elapsed)
                        validation = validate_handler({
                            "cluster_id": cluster_id,
                            "region": request.region,
                            "baseline": {},
                        }, None)
                        if validation["passed"]:
                            _record_step(upgrade, "post_upgrade_validation", "success", result=validation)
                            upgrade["status"] = "complete"
                            upgrade["completed_at"] = datetime.now(timezone.utc).isoformat()
                        else:
                            _record_step(upgrade, "post_upgrade_validation", "failed", result=validation)
                            upgrade["status"] = "validation_failed"
                            upgrade["error"] = "Post-upgrade validation failed"
                        _save_state()
                        _move_to_history(cluster_id)
                        return
                except Exception:
                    pass

            elapsed = round(_time.time() - t0, 1)
            _record_step(upgrade, "wait_available", "failed",
                         error="Timed out waiting for cluster to become available",
                         duration_sec=elapsed)
            upgrade["status"] = "failed"
            upgrade["error"] = "In-place upgrade timed out — cluster did not return to available state within 60 minutes"
            _save_state()
            return

        # Blue/Green deployment path
        upgrade["status"] = "provisioning"
        _save_state()
        t0 = _time.time()
        bg_result = create_blue_green(cluster_id, request.target_version, request.region)
        elapsed = round(_time.time() - t0, 1)

        if bg_result.get("status") in ("error", "already_exists"):
            error_msg = bg_result.get("message") or f"Blue/Green creation returned status: {bg_result.get('status')}"
            _record_step(upgrade, "create_blue_green", "failed",
                         result=bg_result,
                         inputs={"cluster_id": cluster_id, "target_version": request.target_version, "region": request.region},
                         error=error_msg, duration_sec=elapsed)
            upgrade["status"] = "failed"
            upgrade["error"] = error_msg
            _save_state()
            return

        deployment_id = bg_result.get("deployment_id")
        if not deployment_id:
            error_msg = f"No deployment_id in response: {json.dumps(bg_result)[:200]}"
            _record_step(upgrade, "create_blue_green", "failed",
                         result=bg_result,
                         inputs={"cluster_id": cluster_id, "target_version": request.target_version, "region": request.region},
                         error=error_msg, duration_sec=elapsed)
            upgrade["status"] = "failed"
            upgrade["error"] = error_msg
            _save_state()
            return
        upgrade["deployment_id"] = deployment_id
        _record_step(upgrade, "create_blue_green", "success",
                     result={"deployment_id": deployment_id},
                     inputs={"cluster_id": cluster_id, "target_version": request.target_version},
                     duration_sec=elapsed)
        _save_state()

        # Step 3: Wait for green to be ready
        poll_count = 0
        t0 = _time.time()
        while True:
            await asyncio.sleep(30)
            poll_count += 1
            status = check_deployment_status(deployment_id, request.region)

            if status.get("ready_for_switchover"):
                elapsed = round(_time.time() - t0, 1)
                upgrade["status"] = "ready"
                _record_step(upgrade, "wait_green_ready", "success",
                             result={"polls": poll_count, "final_status": status.get("status")},
                             inputs={"deployment_id": deployment_id},
                             duration_sec=elapsed)
                _save_state()
                break
            elif status.get("status") in ("INVALID_CONFIGURATION", "DELETING"):
                elapsed = round(_time.time() - t0, 1)
                error_msg = f"Deployment entered {status.get('status')} state after {poll_count} polls"
                _record_step(upgrade, "wait_green_ready", "failed",
                             result={"polls": poll_count, "final_status": status.get("status")},
                             inputs={"deployment_id": deployment_id},
                             error=error_msg, duration_sec=elapsed)
                upgrade["status"] = "failed"
                upgrade["error"] = error_msg
                _save_state()
                return

        # Step 4: Auto-switchover for non-prod, wait for manual for prod
        if request.environment != "production":
            upgrade["status"] = "switching"
            t0 = _time.time()
            switch_result = execute_switchover(deployment_id, request.region)
            _record_step(upgrade, "switchover_initiated", "success",
                         result=switch_result,
                         inputs={"deployment_id": deployment_id, "auto": True, "environment": request.environment},
                         duration_sec=round(_time.time() - t0, 1))
            _save_state()

            # Wait for switchover to complete
            await asyncio.sleep(60)
            final_status = check_deployment_status(deployment_id, request.region)

            if final_status.get("status") == "SWITCHOVER_COMPLETED":
                _record_step(upgrade, "switchover_complete", "success",
                             result={"status": final_status.get("status")},
                             inputs={"deployment_id": deployment_id})

                # Step 5: Validate
                upgrade["status"] = "validating"
                t0 = _time.time()

                class MockContext:
                    invoked_function_arn = f"arn:aws:lambda:{request.region}:000000:function:test"

                validation = validate_handler({
                    "cluster_id": cluster_id,
                    "region": request.region,
                    "baseline": {},
                }, MockContext())
                elapsed = round(_time.time() - t0, 1)

                if validation["passed"]:
                    _record_step(upgrade, "post_upgrade_validation", "success",
                                 result=validation,
                                 inputs={"cluster_id": cluster_id, "checks": list(validation.get("details", {}).keys())},
                                 duration_sec=elapsed)
                    upgrade["status"] = "complete"
                else:
                    failed_validations = [k for k, v in validation.get("details", {}).items() if not v.get("passed", True)]
                    error_msg = f"Validation failed: {', '.join(failed_validations)}"
                    _record_step(upgrade, "post_upgrade_validation", "failed",
                                 result=validation,
                                 inputs={"cluster_id": cluster_id},
                                 error=error_msg, duration_sec=elapsed)
                    upgrade["status"] = "validation_failed"
                    upgrade["error"] = error_msg
            else:
                error_msg = f"Switchover did not complete. Final status: {final_status.get('status')}"
                _record_step(upgrade, "switchover_complete", "failed",
                             result=final_status,
                             inputs={"deployment_id": deployment_id},
                             error=error_msg)
                upgrade["status"] = "switchover_failed"
                upgrade["error"] = error_msg
        else:
            # Production: leave at "ready" for manual switchover
            upgrade["status"] = "awaiting_approval"
            _record_step(upgrade, "awaiting_manual_switchover", "success",
                         inputs={"environment": "production", "deployment_id": deployment_id},
                         result={"message": "Production upgrade requires manual switchover approval"})

    except Exception as e:
        _record_step(upgrade, "pipeline_exception", "failed",
                     error=str(e),
                     inputs={"cluster_id": cluster_id, "stage": upgrade.get("status")})
        upgrade["status"] = "failed"
        upgrade["error"] = str(e)
    finally:
        if upgrade["status"] in ("complete", "failed", "preflight_failed", "validation_failed", "switchover_failed"):
            _move_to_history(cluster_id)
        _save_state()


def _synthesize_steps_from_aws(aws_status: str, create_iso: str, dep: dict) -> list:
    """Build synthetic step history from AWS deployment status."""
    steps = []
    steps.append({"step": "create_blue_green", "status": "success", "time": create_iso, "duration_sec": None, "error": None})

    if aws_status in ("AVAILABLE", "SWITCHOVER_IN_PROGRESS", "SWITCHOVER_COMPLETED"):
        steps.append({"step": "provision_green", "status": "success", "time": create_iso, "duration_sec": None, "error": None})

    if aws_status == "SWITCHOVER_IN_PROGRESS":
        steps.append({"step": "switchover", "status": "started", "time": datetime.now(timezone.utc).isoformat(), "duration_sec": None, "error": None})

    if aws_status == "SWITCHOVER_COMPLETED":
        steps.append({"step": "switchover", "status": "success", "time": create_iso, "duration_sec": None, "error": None})
        steps.append({"step": "complete", "status": "success", "time": create_iso, "duration_sec": None, "error": None})

    if aws_status in ("INVALID_CONFIGURATION", "DELETING"):
        error = dep.get("StatusDetails", aws_status)
        steps.append({"step": "provision_green", "status": "failed", "time": create_iso, "duration_sec": None, "error": error})

    return steps


# ─────────────────────────────────────────────────────────────────
# Discover active B/G deployments from AWS (fallback if state is lost)
# ─────────────────────────────────────────────────────────────────
@app.post("/api/deployments/discover")
async def discover_deployments(regions: list[str] = None):
    """Find active Blue/Green deployments from AWS and sync into state."""
    from shared.config import get_scan_regions
    if not regions:
        regions = get_scan_regions()
    discovered = []
    for region in regions:
        client = rds_client(region)
        try:
            resp = client.describe_blue_green_deployments()
            for dep in resp.get("BlueGreenDeployments", []):
                dep_id = dep["BlueGreenDeploymentIdentifier"]
                source_arn = dep.get("Source", "")
                cluster_id = source_arn.split(":")[-1] if source_arn else dep_id
                status = dep.get("Status", "UNKNOWN")

                # Extract target version from the green cluster
                target_version = ""
                target_arn = dep.get("Target", "")
                if target_arn:
                    try:
                        target_cluster_id = target_arn.split(":")[-1]
                        tgt_resp = client.describe_db_clusters(DBClusterIdentifier=target_cluster_id)
                        target_version = tgt_resp["DBClusters"][0].get("EngineVersion", "")
                    except Exception:
                        pass
                if not target_version:
                    # Fall back to source cluster version
                    try:
                        src_resp = client.describe_db_clusters(DBClusterIdentifier=cluster_id)
                        target_version = src_resp["DBClusters"][0].get("EngineVersion", "")
                    except Exception:
                        pass

                mapped_status = "ready" if status == "AVAILABLE" else "provisioning" if status == "PROVISIONING" else "complete" if status == "SWITCHOVER_COMPLETED" else status.lower()
                create_time = dep.get("CreateTime", datetime.now(timezone.utc))
                create_iso = create_time.isoformat() if hasattr(create_time, 'isoformat') else str(create_time)

                steps = _synthesize_steps_from_aws(status, create_iso, dep)

                entry = {
                    "cluster_id": cluster_id,
                    "deployment_id": dep_id,
                    "region": region,
                    "status": mapped_status,
                    "target_version": target_version,
                    "environment": "unknown",
                    "started_at": create_iso,
                    "steps": steps,
                    "error": None,
                    "aws_status": status,
                }
                discovered.append(entry)

                existing_history = next((h for h in _state["history"] if h.get("deployment_id") == dep_id), None)
                if existing_history:
                    if not existing_history.get("steps"):
                        existing_history["steps"] = steps
                elif cluster_id in _state["upgrades"]:
                    # Update existing active entry if AWS status has progressed
                    existing = _state["upgrades"][cluster_id]
                    if existing.get("deployment_id") == dep_id and existing.get("status") != mapped_status:
                        existing["status"] = mapped_status
                        existing["aws_status"] = status
                        if not existing.get("steps"):
                            existing["steps"] = steps
                else:
                    if entry["status"] in _TERMINAL_STATUSES:
                        entry["completed_at"] = entry["started_at"]
                        _state["history"].append(entry)
                    else:
                        _state["upgrades"][cluster_id] = entry

        except Exception as e:
            discovered.append({"error": f"Failed to describe deployments in {region}: {e}"})

    _save_state()
    return {"discovered": discovered, "total": len(discovered)}


# ─────────────────────────────────────────────────────────────────
# Health
# ─────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────
# Advisor Agent (Bedrock-powered)
# ─────────────────────────────────────────────────────────────────
@app.post("/api/advisor/recommend")
async def advisor_recommend(request: AdvisorRequest):
    """Get AI-powered upgrade strategy recommendation or error diagnosis."""
    # Gather upgrade history for this cluster from state
    cluster_history = [
        h for h in _state.get("history", [])
        if h.get("cluster_id") == request.cluster_id
    ]

    context = {
        "cluster_id": request.cluster_id,
        "region": request.region,
        "error": request.error,
        "upgrade_history": cluster_history,
        "target_version": request.target_version,
    }

    result = get_advisor_recommendation(context, question=request.question)
    return result


# ─────────────────────────────────────────────────────────────────
# Agentic Fleet Operations
# ─────────────────────────────────────────────────────────────────
@app.post("/api/agent/scan")
async def agent_scan():
    """Scanner Agent: Assess CVE exposure, EOL risk, and urgency for entire fleet."""
    clusters = _state.get("clusters", [])
    if not clusters:
        raise HTTPException(400, "No clusters in state. Run fleet scan first.")
    result = scan_fleet_urgency(clusters)
    _state["last_urgency_scan"] = result
    _save_state()
    return result


class AnalyzeRequest(BaseModel):
    cluster_id: str
    target_version: str
    region: str = ""


def _analyze_worker(payload: dict) -> dict:
    """Slow Bedrock analysis — runs in an async job to dodge the 29s gateway limit."""
    result = analyze_upgrade_readiness(
        cluster_id=payload["cluster_id"],
        target_version=payload["target_version"],
        region=payload.get("region", ""),
    )
    # Cache analysis result (reload state so we don't clobber concurrent writes).
    global _state
    _state = _load_state()
    _state.setdefault("analyses", {})[payload["cluster_id"]] = result
    _save_state()
    return result


_JOB_WORKERS["analyze"] = _analyze_worker


@app.post("/api/agent/analyze")
async def agent_analyze(request: AnalyzeRequest):
    """Analyzer Agent: Deep parameter/config analysis and change plan generation.

    Dispatched as an async job (Bedrock latency exceeds API Gateway's 29s limit).
    Returns {job_id, status}; poll GET /api/jobs/{job_id} for the result.
    """
    return _dispatch_job("analyze", {
        "cluster_id": request.cluster_id,
        "target_version": request.target_version,
        "region": request.region,
    })


class VersionDriftRequest(BaseModel):
    current_version: str
    target_version: str
    cluster_id: str = ""


def _version_drift_worker(payload: dict) -> dict:
    """Slow Bedrock version-drift analysis — runs in an async job."""
    import re as _re
    current_version = payload["current_version"]
    target_version = payload["target_version"]

    prompt = f"""You are an Aurora PostgreSQL database expert. Analyze the differences between PostgreSQL version {current_version} and {target_version}.

Provide a comprehensive but concise list of:
1. **Parameter Default Changes** — ONLY parameters whose defaults ACTUALLY CHANGED (old != new). Do NOT include parameters that kept the same default value.
2. **Deprecated/Removed Features** — what was removed or deprecated
3. **New Features** — notable additions in the target version
4. **Behavioral Changes** — any changes that could break existing applications
5. **Performance Impact** — query planner changes, optimizer improvements, etc.
6. **Extension Compatibility** — extensions that need updating or are no longer supported
7. **Recommendations** — things to check/fix before upgrading

CRITICAL RULES:
- For parameter_changes: ONLY include entries where old_default is DIFFERENT from new_default. If old == new, DO NOT list it.
- Focus on Aurora PostgreSQL specifically (not vanilla PostgreSQL).

Respond with JSON:
{{
  "parameter_changes": [
    {{"parameter": "name", "old_default": "OLD_VALUE", "new_default": "DIFFERENT_NEW_VALUE", "impact": "high|medium|low", "note": "explanation"}}
  ],
  "deprecated_features": [
    {{"feature": "name", "impact": "description of what breaks"}}
  ],
  "new_features": [
    {{"feature": "name", "benefit": "why it matters"}}
  ],
  "behavioral_changes": [
    {{"change": "description", "impact": "high|medium|low", "mitigation": "what to do"}}
  ],
  "performance_changes": [
    {{"change": "description", "expected_impact": "positive|neutral|negative"}}
  ],
  "extension_notes": [
    {{"extension": "name", "action": "update|remove|check", "note": "details"}}
  ],
  "recommendations": [
    {{"priority": "critical|high|medium|low", "action": "what to do", "reason": "why"}}
  ],
  "summary": "One paragraph overview of the upgrade path"
}}"""

    try:
        from shared.aws_clients import bedrock_client, extract_bedrock_text
        from shared.config import get_model_id
        client = bedrock_client()
        response = client.invoke_model(
            modelId=get_model_id(),
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                # Sonnet 5 uses extended thinking by default; a small budget gets
                # consumed entirely by the thinking block (stop_reason=max_tokens)
                # leaving NO text output. This runs as an async job, so a larger
                # budget is safe and necessary to get an actual JSON answer.
                "max_tokens": 8192,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        body = json.loads(response["body"].read())
        # Use the shared extractor so thinking-block-first responses (where the
        # first content block is "thinking", not "text") are handled correctly.
        text = extract_bedrock_text(body)
        if not text:
            raise ValueError("No text in response")
        # Strip markdown fences
        if "```" in text:
            fence_match = _re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, _re.DOTALL)
            if fence_match:
                text = fence_match.group(1)
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            result = json.loads(text[start:end + 1])
            # Filter out parameter changes where old == new (model sometimes includes these)
            if "parameter_changes" in result:
                result["parameter_changes"] = [
                    p for p in result["parameter_changes"]
                    if str(p.get("old_default", "")).strip() != str(p.get("new_default", "")).strip()
                ]
            return result
        raise ValueError("No JSON object found in response")
    except Exception as e:
        logger.error(f"Version drift analysis failed: {e}")
        return {
            "error": str(e),
            "summary": f"Failed to analyze version drift from {current_version} to {target_version}",
            "parameter_changes": [],
            "deprecated_features": [],
            "new_features": [],
            "behavioral_changes": [],
            "recommendations": [],
        }


_JOB_WORKERS["version_drift"] = _version_drift_worker


@app.post("/api/agent/version-drift")
async def agent_version_drift(request: VersionDriftRequest):
    """AI analysis of version differences. Dispatched as an async job (Bedrock
    latency exceeds API Gateway's 29s limit). Poll GET /api/jobs/{job_id}."""
    return _dispatch_job("version_drift", {
        "current_version": request.current_version,
        "target_version": request.target_version,
        "cluster_id": request.cluster_id,
    })


@app.post("/api/agent/plan")
async def agent_plan():
    """Planner Agent: Generate fleet-wide upgrade plan with wave scheduling."""
    scan_results = _state.get("last_urgency_scan", {}).get("clusters", [])
    if not scan_results:
        raise HTTPException(400, "No urgency scan results. Run /api/agent/scan first.")

    analyses = list(_state.get("analyses", {}).values())
    windows = _state.get("maintenance_windows", PREFLIGHT_CONFIG["change_windows"])
    active = list(_state.get("upgrades", {}).keys())
    history = _state.get("history", [])

    plan = generate_fleet_plan(
        scan_results=scan_results,
        analysis_results=analyses,
        maintenance_windows=windows,
        active_upgrades=active,
        previous_outcomes=history[-10:],
    )
    _state["fleet_plan"] = plan
    _save_state()
    return plan


@app.get("/api/agent/plan")
async def get_current_plan():
    """Get the current fleet upgrade plan."""
    plan = _state.get("fleet_plan")
    if not plan:
        return {"status": "no_plan", "message": "No fleet plan generated yet. Run POST /api/agent/plan"}
    return plan


class WaveOutcome(BaseModel):
    wave_id: int
    results: list[dict]


@app.post("/api/agent/plan/adjust")
async def agent_adjust_plan(outcome: WaveOutcome):
    """Planner Agent: Adjust plan based on observed wave outcomes (agentic loop)."""
    current_plan = _state.get("fleet_plan")
    if not current_plan:
        raise HTTPException(400, "No active plan to adjust")

    adjusted = adjust_plan_from_outcome(current_plan, outcome.model_dump())
    _state["fleet_plan"] = adjusted
    _save_state()
    return adjusted


@app.post("/api/agent/run")
async def agent_full_run(background_tasks: BackgroundTasks):
    """Autonomous Agent: Full scan → analyze → plan → execute cycle.

    This is the fully agentic endpoint. It:
    1. Scans fleet for urgency
    2. Analyzes each candidate cluster deeply
    3. Generates an optimal fleet plan
    4. Returns the plan for approval (doesn't execute without human gate)
    """
    clusters = _state.get("clusters", [])
    if not clusters:
        raise HTTPException(400, "No clusters in state. Run fleet scan first.")

    # Step 1: Urgency scan
    scan = scan_fleet_urgency(clusters)
    _state["last_urgency_scan"] = scan

    # Step 2: Analyze top candidates (limit to avoid long waits)
    candidates = [c for c in scan["clusters"] if c.get("urgency_score", 0) > 10][:10]
    analyses = []
    for candidate in candidates:
        try:
            analysis = analyze_upgrade_readiness(
                cluster_id=candidate["cluster_id"],
                target_version=candidate.get("target_version", ""),
                region="",
            )
            analyses.append(analysis)
            _state.setdefault("analyses", {})[candidate["cluster_id"]] = analysis
        except Exception as e:
            analyses.append({"cluster_id": candidate["cluster_id"], "error": str(e)})

    # Step 3: Generate fleet plan
    windows = _state.get("maintenance_windows", PREFLIGHT_CONFIG["change_windows"])
    active = list(_state.get("upgrades", {}).keys())
    plan = generate_fleet_plan(
        scan_results=scan["clusters"],
        analysis_results=analyses,
        maintenance_windows=windows,
        active_upgrades=active,
        previous_outcomes=_state.get("history", [])[-10:],
    )
    _state["fleet_plan"] = plan
    _save_state()

    return {
        "status": "plan_generated",
        "urgency_scan": {
            "total": scan["total_clusters"],
            "critical": scan["critical_count"],
            "high": scan["high_count"],
        },
        "analyzed": len(analyses),
        "plan": plan,
        "message": "Plan generated. Review and approve before execution begins.",
        "next_action": "POST /api/agent/plan/approve to start wave 1",
    }


# ─────────────────────────────────────────────────────────────────
# execute-step operation handlers — closed allowlist, no subprocess.
#
# Each function takes only the named params it needs and makes exactly
# one boto3/psycopg2 call. Adding a new capability means adding a new
# named entry here, not widening what strings can reach a shell.
# ─────────────────────────────────────────────────────────────────

def _exec_reboot_db_instance(params: dict, region: str) -> dict:
    client = rds_client(region)
    resp = client.reboot_db_instance(DBInstanceIdentifier=params["db_instance_identifier"])
    status = resp.get("DBInstance", {}).get("DBInstanceStatus", "unknown")
    return {"status": "success", "stdout": f"Reboot initiated for {params['db_instance_identifier']}: {status}"}


# NOTE: an earlier `_exec_modify_cluster_password` action was intentionally
# REMOVED from the execute-step allowlist. It set a cluster's master password to
# a caller-supplied value (to work around Blue/Green not supporting Secrets
# Manager managed passwords). Exposing "set the master password to an arbitrary
# value" as a callable action is a master-password-takeover primitive, so it is
# no longer an executable step. The underlying situation is still handled — the
# remediation knowledge base (_REMEDIATION_MAP, "manage-master-user-password")
# surfaces the fix as human-readable guidance for an operator to apply
# deliberately, rather than as an action any API caller can trigger.


def _exec_delete_blue_green_deployment(params: dict, region: str) -> dict:
    client = rds_client(region)
    client.delete_blue_green_deployment(
        BlueGreenDeploymentIdentifier=params["blue_green_deployment_identifier"],
        DeleteTarget=True,
    )
    return {"status": "success", "stdout": f"Deletion initiated for {params['blue_green_deployment_identifier']}"}


def _exec_describe_db_cluster_status(params: dict, region: str) -> dict:
    client = rds_client(region)
    resp = client.describe_db_clusters(DBClusterIdentifier=params["db_cluster_identifier"])
    status = resp["DBClusters"][0]["Status"]
    return {"status": "success", "stdout": f"Status: {status}"}


def _exec_describe_cluster_events(params: dict, region: str) -> dict:
    client = rds_client(region)
    resp = client.describe_events(SourceIdentifier=params["db_cluster_identifier"], SourceType="db-cluster")
    events = resp.get("Events", [])
    lines = [f"{e['Date'].isoformat()}  {e['Message']}" for e in events]
    return {"status": "success", "stdout": "\n".join(lines) if lines else "No recent events."}


def _exec_get_replica_lag_metric(params: dict, region: str) -> dict:
    client = cloudwatch_client(region)
    resp = client.get_metric_statistics(
        Namespace="AWS/RDS",
        MetricName="AuroraReplicaLag",
        Dimensions=[{"Name": "DBClusterIdentifier", "Value": params["db_cluster_identifier"]}],
        StartTime=datetime.now(timezone.utc) - timedelta(minutes=30),
        EndTime=datetime.now(timezone.utc),
        Period=300,
        Statistics=["Maximum"],
    )
    points = sorted(resp.get("Datapoints", []), key=lambda d: d["Timestamp"])
    lines = [f"{p['Timestamp'].isoformat()}  {p['Maximum']:.1f}ms" for p in points]
    return {"status": "success", "stdout": "\n".join(lines) if lines else "No datapoints in the last 30 minutes."}


def _exec_run_sql(params: dict, region: str, cluster_id: str) -> dict:
    from agents.validation.baseline import execute_db_command
    result = execute_db_command(cluster_id, params["sql"], region, database=params.get("database", "ecommerce"))
    return {
        "status": result.get("status", "error"),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("error", ""),
    }


# action name -> (handler, required param keys, optional param keys).
# "run_sql" is special-cased in execute_step because it also needs
# cluster_id (already part of the request, not a per-action param).
_EXEC_ACTIONS = {
    "reboot_db_instance": (_exec_reboot_db_instance, {"db_instance_identifier"}, set()),
    "delete_blue_green_deployment": (_exec_delete_blue_green_deployment, {"blue_green_deployment_identifier"}, set()),
    "describe_db_cluster_status": (_exec_describe_db_cluster_status, {"db_cluster_identifier"}, set()),
    "describe_cluster_events": (_exec_describe_cluster_events, {"db_cluster_identifier"}, set()),
    "get_replica_lag_metric": (_exec_get_replica_lag_metric, {"db_cluster_identifier"}, set()),
    "run_sql": (None, {"sql"}, {"database"}),
}


@app.post("/api/agent/execute-step")
async def execute_step(request: dict):
    """Execute a single pre-upgrade/upgrade/post-upgrade step.

    Steps are dispatched by a fixed `action` name against the closed
    allowlist in _EXEC_ACTIONS, each backed by one boto3/psycopg2 call
    with an explicit, named parameter set. There is no free-text command
    string parsed or shelled out here: `action` must be a key in
    _EXEC_ACTIONS, and `params` must contain exactly the keys that
    action expects (extras and missing keys are both rejected). This
    replaces an earlier design that parsed an LLM-generated "aws rds ..."
    string and ran it via subprocess — that let any request body choose
    which aws rds/cloudwatch subcommand executed, bounded only by a
    two-level string prefix, which is a real risk, not a false positive.
    """
    action = request.get("action", "")
    params = request.get("params", {})
    cluster_id = request.get("cluster_id", "")
    step_type = request.get("step_type", "")
    region = request.get("region", "")

    if not action:
        raise HTTPException(400, "action is required")
    if action not in _EXEC_ACTIONS:
        raise HTTPException(400, f"Unknown action '{action}'. Allowed: {sorted(_EXEC_ACTIONS)}")
    if not isinstance(params, dict):
        raise HTTPException(400, "params must be an object")

    _, required_keys, optional_keys = _EXEC_ACTIONS[action]
    allowed_keys = required_keys | optional_keys
    extra_keys = set(params) - allowed_keys
    missing_keys = required_keys - set(params)
    if extra_keys:
        raise HTTPException(400, f"Unexpected params for '{action}': {sorted(extra_keys)}")
    if missing_keys:
        raise HTTPException(400, f"Missing params for '{action}': {sorted(missing_keys)}")
    if not all(isinstance(v, str) and v for v in params.values()):
        raise HTTPException(400, "All param values must be non-empty strings")

    try:
        if action == "run_sql":
            result = _exec_run_sql(params, region, cluster_id)
        else:
            handler, _, _ = _EXEC_ACTIONS[action]
            result = handler(params, region)
        result.setdefault("return_code", 0 if result.get("status") == "success" else 1)
        result["action"] = action
        result["cluster_id"] = cluster_id
        result["step_type"] = step_type
        return result
    except HTTPException:
        raise
    except Exception as e:
        return {"status": "error", "action": action, "cluster_id": cluster_id, "step_type": step_type, "message": str(e)}


# ─────────────────────────────────────────────────────────────────
# Performance Baseline & Validation (Gap #5 and #6)
# ─────────────────────────────────────────────────────────────────

@app.post("/api/agent/baseline/capture")
async def capture_perf_baseline(request: dict):
    """Capture pre-upgrade performance baseline from pg_stat_statements.

    Must be called BEFORE upgrading a cluster. Stores the baseline in state
    for later comparison.
    """
    from agents.validation.baseline import capture_baseline

    cluster_id = request.get("cluster_id")
    region = request.get("region", "")

    if not cluster_id:
        raise HTTPException(400, "cluster_id is required")

    baseline = capture_baseline(cluster_id, region)

    if baseline.get("error"):
        return {"status": "error", "error": baseline["error"]}

    # Store baseline in state for later comparison
    _state.setdefault("baselines", {})[cluster_id] = baseline
    _save_state()

    return {
        "status": "captured",
        "cluster_id": cluster_id,
        "query_count": baseline.get("query_count", 0),
        "total_tracked_time_ms": baseline.get("total_tracked_time_ms", 0),
        "captured_at": baseline.get("captured_at"),
    }


@app.post("/api/agent/baseline/compare")
async def compare_perf_baseline(request: dict):
    """Compare current performance against stored pre-upgrade baseline.

    Call AFTER upgrading a cluster. Returns regression analysis showing
    which queries got slower, plan changes, and index usage shifts.
    """
    from agents.validation.baseline import compare_with_baseline

    cluster_id = request.get("cluster_id")
    region = request.get("region", "")

    if not cluster_id:
        raise HTTPException(400, "cluster_id is required")

    baseline = _state.get("baselines", {}).get(cluster_id)
    if not baseline:
        raise HTTPException(404, f"No baseline found for {cluster_id}. Capture one before upgrading.")

    comparison = compare_with_baseline(cluster_id, baseline, region)
    return comparison


@app.post("/api/agent/validate-db")
async def validate_database(request: dict):
    """Run post-upgrade database validation checks via direct connection.

    Validates extensions, functions, triggers, views, indexes without
    needing psql binary (uses psycopg2 directly — solves Gap #6).
    """
    from agents.validation.baseline import run_validation_queries

    cluster_id = request.get("cluster_id")
    region = request.get("region", "")

    if not cluster_id:
        raise HTTPException(400, "cluster_id is required")

    result = run_validation_queries(cluster_id, region)
    return result


@app.get("/api/settings/model")
async def get_model():
    """Get the current Bedrock model and available options."""
    from shared.config import get_model_id, AVAILABLE_MODELS
    return {
        "current_model": get_model_id(),
        "available_models": AVAILABLE_MODELS,
    }


@app.put("/api/settings/model")
async def set_model(request: dict):
    """Change the Bedrock model used by all agents."""
    from shared.config import set_model_id, AVAILABLE_MODELS
    model_id = request.get("model_id", "")
    if not model_id:
        raise HTTPException(400, "model_id is required")
    valid_ids = [m["id"] for m in AVAILABLE_MODELS]
    if model_id not in valid_ids:
        raise HTTPException(400, f"Invalid model_id. Choose from: {valid_ids}")
    set_model_id(model_id)
    return {"status": "updated", "current_model": model_id}


@app.post("/api/traffic/generate")
async def generate_traffic(request: dict = {}):
    """Generate realistic traffic across all clusters to populate pg_stat_statements.

    Runs synchronously — generates traffic for all clusters and returns results.
    API Gateway timeout is 29s, so this targets a subset if needed.
    """
    from traffic_gen.handler import discover_clusters, setup_schema, generate_traffic as gen_traffic

    targets = discover_clusters()
    cluster_filter = request.get("clusters", [])
    if cluster_filter:
        targets = [t for t in targets if t["cluster_id"] in cluster_filter]

    results = []
    for t in targets:
        try:
            setup_schema(t["endpoint"])
        except Exception as e:
            results.append({"cluster_id": t["cluster_id"], "error": f"Schema setup: {e}"})
            continue
        try:
            stats = gen_traffic(t["endpoint"], t["environment"], t["cluster_id"])
            results.append(stats)
        except Exception as e:
            results.append({"cluster_id": t["cluster_id"], "error": str(e)})

    return {"status": "complete", "clusters_processed": len(results), "results": results}


@app.get("/api/health")
async def health():
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/cluster/{cluster_id}/global-db")
async def get_global_db_info(cluster_id: str, region: str = ""):
    """Get Global Database info and upgrade eligibility for a cluster."""
    from shared.global_db import get_global_cluster_info, check_upgrade_eligibility
    from shared.config import DEPLOYMENT_REGION
    if not region:
        # Find region from fleet state
        for c in _state.get("clusters", []):
            if c.get("cluster_id") == cluster_id:
                region = c.get("region", "")
                break
    if not region:
        region = DEPLOYMENT_REGION

    global_info = get_global_cluster_info(cluster_id, region)
    if not global_info:
        return {"global_database": False, "cluster_id": cluster_id}

    # Find target version from fleet state
    target_version = ""
    engine_type = "aurora-postgresql"
    for c in _state.get("clusters", []):
        if c.get("cluster_id") == cluster_id:
            target_version = c.get("target_version", "")
            engine_type = c.get("engine_type", "aurora-postgresql")
            break

    eligibility = check_upgrade_eligibility(cluster_id, region, target_version, engine_type) if target_version else {}

    return {
        "global_database": True,
        "cluster_id": cluster_id,
        "global_info": global_info,
        "upgrade_eligibility": eligibility,
    }


# ─────────────────────────────────────────────────────────────────
# Learning & Outcome History
# ─────────────────────────────────────────────────────────────────
@app.get("/api/learning/stats")
async def get_learning_stats():
    """Get summary of what the agent has learned from past outcomes.

    Shows: upgrade success rates by version pair, learned error fixes,
    rollback patterns, and duration estimates.
    """
    from shared.aws_clients import dynamodb_resource
    from shared.config import DYNAMODB_TABLE

    try:
        table = dynamodb_resource().Table(DYNAMODB_TABLE)

        # Get version pair stats
        version_response = table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": "LEARNING#VERSION_STATS"},
            Limit=50,
        )
        version_stats = [
            json.loads(item["data"]) for item in version_response.get("Items", [])
        ]

        # Get learned error resolutions
        error_response = table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": "LEARNING#ERROR_RESOLUTION"},
            Limit=50,
        )
        learned_fixes = [
            {
                "pattern": item["sk"][:80],
                "use_count": json.loads(item["data"]).get("use_count", 0),
                "last_used": json.loads(item["data"]).get("last_used", ""),
            }
            for item in error_response.get("Items", [])
        ]

        # Get rollback causes
        rollback_response = table.query(
            KeyConditionExpression="pk = :pk",
            ExpressionAttributeValues={":pk": "LEARNING#ROLLBACK_CAUSE"},
            Limit=20,
        )
        rollback_causes = [
            json.loads(item["data"]) for item in rollback_response.get("Items", [])
        ]

        return {
            "version_pair_stats": version_stats,
            "learned_fixes": learned_fixes,
            "learned_fix_count": len(learned_fixes),
            "rollback_patterns": rollback_causes,
            "total_outcomes_recorded": sum(
                s.get("total_attempts", 0) for s in version_stats
            ),
        }
    except Exception as e:
        return {"error": str(e), "version_pair_stats": [], "learned_fixes": []}


@app.post("/api/learning/record-outcome")
async def record_upgrade_outcome_api(request: dict):
    """Manually record an upgrade outcome (used by UI after upgrade completes)."""
    from shared.outcome_learning import record_upgrade_outcome

    record_upgrade_outcome(
        cluster_id=request.get("cluster_id", ""),
        from_version=request.get("from_version", ""),
        to_version=request.get("to_version", ""),
        strategy=request.get("strategy", "blue_green"),
        success=request.get("success", True),
        duration_minutes=request.get("duration_minutes", 0),
        instance_class=request.get("instance_class", ""),
        storage_gb=request.get("storage_gb", 0),
        extensions=request.get("extensions", []),
        performance_delta=request.get("performance_delta", {}),
        failure_reason=request.get("failure_reason", ""),
        rollback=request.get("rollback", False),
    )

    return {"status": "recorded"}


# Serve frontend
ui_path = Path(__file__).parent.parent / "ui" / "dist"
if ui_path.exists():
    app.mount("/", StaticFiles(directory=str(ui_path), html=True), name="frontend")


# Lambda handler (API Gateway proxy integration)
try:
    from mangum import Mangum
    _mangum_handler = Mangum(app, lifespan="off")

    def handler(event, context):
        # Async self-invoke for long-running jobs (see _dispatch_job): these
        # events carry {"__job__": <id>} and are NOT API Gateway requests, so we
        # run the job directly and skip Mangum/ASGI routing entirely.
        if isinstance(event, dict) and event.get("__job__"):
            _run_job(event["__job__"])
            return {"status": "done", "job_id": event["__job__"]}
        return _mangum_handler(event, context)
except ImportError:
    pass
