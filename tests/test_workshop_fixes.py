# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regression tests for bugs found by the end-to-end workshop run.

* State: Lambda containers must not overwrite each other's state.
* Pipeline: upgrades run as async jobs; long waits are reconciled from RDS.
* Blue/Green: the green cluster keeps a same-family custom parameter group.
* Scanner: Aurora MySQL versions match the MySQL CVE ranges.
"""
import copy
import json
from unittest.mock import MagicMock

import pytest

import api.server as server
from agents.execution import preflight
from agents.execution.handler import _resolve_target_cluster_parameter_group
from agents.scanner.handler import KNOWN_MYSQL_CVES, _assess_urgency


class FakeTable:
    """Minimal stand-in for the DynamoDB Table resource used by api.server."""

    def __init__(self):
        self.items = {}

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803 (boto3 casing)
        key = (Key["pk"], Key["sk"])
        return {"Item": copy.deepcopy(self.items[key])} if key in self.items else {}

    def put_item(self, Item):  # noqa: N803
        self.items[(Item["pk"], Item["sk"])] = copy.deepcopy(Item)

    # Helpers for simulating "another container" writing the stored state.
    def stored(self) -> dict:
        return json.loads(self.items[("STATE", "current")]["data"])

    def write_stored(self, state: dict):
        self.put_item({"pk": "STATE", "sk": "current", "data": json.dumps(state)})


def _base_state(**overrides):
    state = copy.deepcopy(server._DEFAULT_STATE)
    state.update(overrides)
    return state


@pytest.fixture
def table(monkeypatch, tmp_path):
    t = FakeTable()
    monkeypatch.setattr(server, "_ddb_table", lambda: t)
    monkeypatch.setattr(server, "_STATE_FILE", tmp_path / "state.json")
    original_windows = copy.deepcopy(preflight.PREFLIGHT_CONFIG["change_windows"])
    yield t
    preflight.PREFLIGHT_CONFIG["change_windows"].clear()
    preflight.PREFLIGHT_CONFIG["change_windows"].update(original_windows)


def _upgrade(cluster_id, status, **extra):
    return {"cluster_id": cluster_id, "status": status, "steps": [], "region": "us-east-2",
            "environment": "production", "target_version": "15.19", **extra}


class TestMultiContainerState:
    def test_stale_container_does_not_reset_other_containers_writes(self, table):
        # The exact failure from the workshop run: container B saved the change
        # windows; container A (loaded earlier) ran a fleet scan and its save
        # reset the windows to defaults and wiped the upgrade history.
        table.write_stored(_base_state())
        server._refresh_state()  # container A loads

        other = table.stored()  # container B writes in the meantime
        other["maintenance_windows"]["production"].update(
            start_hour_utc=0, end_hour_utc=24, days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
        other["history"].append({"cluster_id": "orders-dev", "status": "complete",
                                 "started_at": "2026-10-28T13:27:00+00:00"})
        table.write_stored(other)

        server._state["clusters"] = [{"cluster_id": "orders-prod"}]  # A: fleet scan
        server._save_state()

        stored = table.stored()
        assert stored["maintenance_windows"]["production"]["start_hour_utc"] == 0
        assert len(stored["maintenance_windows"]["production"]["days"]) == 7
        assert [h["cluster_id"] for h in stored["history"]] == ["orders-dev"]
        assert stored["clusters"] == [{"cluster_id": "orders-prod"}]

    def test_both_containers_append_history(self, table):
        table.write_stored(_base_state())
        server._refresh_state()
        other = table.stored()
        now = server.datetime.now(server.timezone.utc).isoformat()
        other["history"].append({"cluster_id": "a", "status": "complete", "started_at": now})
        table.write_stored(other)
        server._state["history"].append({"cluster_id": "b", "status": "failed", "started_at": now})
        server._save_state()
        assert sorted(h["cluster_id"] for h in table.stored()["history"]) == ["a", "b"]

    def test_same_run_archived_by_two_containers_is_kept_once(self, table):
        # Regression from the workshop run: the pipeline worker and a
        # GET /api/upgrades reconcile both archived orders-dev, 0.18 s apart,
        # and the History tab showed the run twice.
        started = server.datetime.now(server.timezone.utc).isoformat()
        table.write_stored(_base_state(upgrades={
            "orders-dev": _upgrade("orders-dev", "waiting_inplace", started_at=started)}))
        server._refresh_state()  # container A (pipeline worker)

        other = table.stored()  # container B archives the run first
        run = other["upgrades"].pop("orders-dev")
        run.update(status="complete", completed_at=server.datetime.now(server.timezone.utc).isoformat())
        other["history"].append(run)
        table.write_stored(other)

        server._state["upgrades"]["orders-dev"]["status"] = "complete"
        server._move_to_history("orders-dev")  # A archives it too
        server._save_state()

        history = table.stored()["history"]
        assert [h["cluster_id"] for h in history] == ["orders-dev"]
        assert "orders-dev" not in table.stored()["upgrades"]

    def test_existing_duplicate_history_is_cleaned_on_load(self, table):
        t0 = server.datetime.now(server.timezone.utc)
        iso = lambda secs: (t0 + server.timedelta(seconds=secs)).isoformat()  # noqa: E731
        run = {"cluster_id": "orders-dev", "status": "complete", "started_at": iso(0)}
        table.write_stored(_base_state(history=[
            {**run, "completed_at": iso(300)}, {**run, "completed_at": iso(300.2)},
            {"cluster_id": "orders-dev", "status": "complete", "started_at": iso(600), "completed_at": iso(900)},
        ]))
        server._refresh_state()
        assert len(server._state["history"]) == 2

    def test_pipeline_upgrade_reference_stays_live_across_merges(self, table):
        table.write_stored(_base_state(upgrades={"orders-prod": _upgrade("orders-prod", "provisioning")}))
        server._refresh_state()
        upgrade = server._state["upgrades"]["orders-prod"]  # held by the pipeline

        other = table.stored()  # another container changes something unrelated
        other["maintenance_windows"]["staging"]["start_hour_utc"] = 0
        table.write_stored(other)
        server._save_state()
        assert server._state["upgrades"]["orders-prod"] is upgrade

        upgrade["status"] = "awaiting_approval"
        server._save_state()
        assert table.stored()["upgrades"]["orders-prod"]["status"] == "awaiting_approval"
        assert table.stored()["maintenance_windows"]["staging"]["start_hour_utc"] == 0

    def test_move_to_history_merges_with_other_containers_upgrades(self, table):
        table.write_stored(_base_state(upgrades={
            "orders-dev": _upgrade("orders-dev", "complete"),
            "orders-prod": _upgrade("orders-prod", "provisioning"),
        }))
        server._refresh_state()
        other = table.stored()
        other["upgrades"]["orders-prod"]["status"] = "awaiting_approval"
        table.write_stored(other)

        server._move_to_history("orders-dev")
        server._save_state()
        stored = table.stored()
        assert "orders-dev" not in stored["upgrades"]
        assert stored["upgrades"]["orders-prod"]["status"] == "awaiting_approval"
        assert any(h["cluster_id"] == "orders-dev" for h in stored["history"])

    def test_refresh_syncs_change_window_guardrail(self, table):
        state = _base_state()
        state["maintenance_windows"]["production"].update(
            start_hour_utc=0, end_hour_utc=24, days=["mon", "tue", "wed", "thu", "fri", "sat", "sun"])
        table.write_stored(state)
        server._refresh_state()
        assert preflight.check_change_window("production")["passed"] is True


class TestReconcile:
    def test_green_ready_moves_production_to_awaiting_approval(self, table, monkeypatch):
        table.write_stored(_base_state(upgrades={
            "orders-prod": _upgrade("orders-prod", "provisioning", deployment_id="bgd-1")}))
        server._refresh_state()
        monkeypatch.setattr(server, "check_deployment_status",
                            lambda dep, region: {"status": "AVAILABLE", "ready_for_switchover": True})
        assert server._reconcile_active_upgrades() is True
        assert server._state["upgrades"]["orders-prod"]["status"] == "awaiting_approval"

    def test_green_ready_moves_staging_to_ready(self, table, monkeypatch):
        table.write_stored(_base_state(upgrades={
            "inventory-staging": _upgrade("inventory-staging", "provisioning",
                                          deployment_id="bgd-2", environment="staging")}))
        server._refresh_state()
        monkeypatch.setattr(server, "check_deployment_status",
                            lambda dep, region: {"status": "AVAILABLE", "ready_for_switchover": True})
        server._reconcile_active_upgrades()
        assert server._state["upgrades"]["inventory-staging"]["status"] == "ready"

    def test_completed_switchover_moves_to_history(self, table, monkeypatch):
        table.write_stored(_base_state(upgrades={
            "orders-prod": _upgrade("orders-prod", "switching", deployment_id="bgd-1")}))
        server._refresh_state()
        monkeypatch.setattr(server, "check_deployment_status",
                            lambda dep, region: {"status": "SWITCHOVER_COMPLETED", "ready_for_switchover": False})
        assert server._reconcile_active_upgrades() is True
        assert "orders-prod" not in server._state["upgrades"]
        assert server._state["history"][-1]["status"] == "complete"

    def test_still_provisioning_is_unchanged(self, table, monkeypatch):
        table.write_stored(_base_state(upgrades={
            "orders-prod": _upgrade("orders-prod", "provisioning", deployment_id="bgd-1")}))
        server._refresh_state()
        monkeypatch.setattr(server, "check_deployment_status",
                            lambda dep, region: {"status": "PROVISIONING", "ready_for_switchover": False})
        assert server._reconcile_active_upgrades() is False

    def test_inplace_upgrade_completes_when_cluster_is_back(self, table, monkeypatch):
        table.write_stored(_base_state(upgrades={
            "orders-dev": _upgrade("orders-dev", "waiting_inplace", strategy="in_place")}))
        server._refresh_state()
        rds = MagicMock()
        rds.describe_db_clusters.return_value = {
            "DBClusters": [{"Status": "available", "EngineVersion": "15.19"}]}
        monkeypatch.setattr(server, "rds_client", lambda region: rds)
        assert server._reconcile_active_upgrades() is True
        assert server._state["history"][-1]["cluster_id"] == "orders-dev"


def _fleet_cluster(cluster_id, version, environment):
    return {"cluster_id": cluster_id, "engine_version": version, "engine_type": "aurora-postgresql",
            "environment": environment, "region": "us-east-2", "upgrade_available": True,
            "target_version": "15.19", "available_versions": ["15.19"]}


def _cve_ids(cluster):
    return {c.get("cve_id") for c in _assess_urgency(cluster)["cve_exposure"]}


class TestCveClearsAfterPatch:
    # Regression from the workshop run: after orders-dev and orders-prod were
    # patched 15.10 -> 15.19, the security scan still reported CVE-2025-1094
    # because it reads the stored fleet, which only a fleet scan refreshed.
    def test_precondition_15_10_is_exposed(self):
        assert "CVE-2025-1094" in _cve_ids(_fleet_cluster("orders-dev", "15.10", "dev"))

    def test_inplace_completion_updates_fleet_and_clears_cve(self, table):
        table.write_stored(_base_state(
            clusters=[_fleet_cluster("orders-dev", "15.10", "dev")],
            upgrades={"orders-dev": _upgrade("orders-dev", "complete", strategy="in_place",
                                             environment="dev")}))
        server._refresh_state()  # load archives the completed run
        server._save_state()
        cluster = table.stored()["clusters"][0]
        assert cluster["engine_version"] == "15.19"
        assert cluster["upgrade_available"] is False and cluster["target_version"] is None
        assert "CVE-2025-1094" not in _cve_ids(cluster)

    def test_switchover_completion_updates_fleet_and_clears_cve(self, table, monkeypatch):
        table.write_stored(_base_state(
            clusters=[_fleet_cluster("orders-prod", "15.10", "production"),
                      _fleet_cluster("orders-dev", "15.10", "dev")],
            upgrades={"orders-prod": _upgrade("orders-prod", "switching", deployment_id="bgd-1")}))
        server._refresh_state()
        monkeypatch.setattr(server, "check_deployment_status",
                            lambda dep, region: {"status": "SWITCHOVER_COMPLETED", "ready_for_switchover": False})
        server._reconcile_active_upgrades()
        server._save_state()
        by_id = {c["cluster_id"]: c for c in table.stored()["clusters"]}
        assert by_id["orders-prod"]["engine_version"] == "15.19"
        assert "CVE-2025-1094" not in _cve_ids(by_id["orders-prod"])
        # Clusters that weren't upgraded keep their version and exposure.
        assert by_id["orders-dev"]["engine_version"] == "15.10"
        assert "CVE-2025-1094" in _cve_ids(by_id["orders-dev"])

    def test_failed_upgrade_leaves_fleet_version_alone(self, table):
        table.write_stored(_base_state(
            clusters=[_fleet_cluster("orders-dev", "15.10", "dev")],
            upgrades={"orders-dev": _upgrade("orders-dev", "failed", environment="dev")}))
        server._refresh_state()
        server._move_to_history("orders-dev")
        server._save_state()
        assert table.stored()["clusters"][0]["engine_version"] == "15.10"

    def test_agent_scan_endpoint_reports_no_cve_after_patch(self, table, monkeypatch):
        from fastapi.testclient import TestClient
        import agents.scanner.handler as scanner
        import shared.cve_cache
        # Keep the scan offline: static CVE list only, no NVD/bulletins/Health/Bedrock.
        monkeypatch.setattr(shared.cve_cache, "get_cves_with_fallback", lambda fetch, static: dict(static))
        monkeypatch.setattr(scanner, "_fetch_live_cves", lambda: {})
        monkeypatch.setattr(scanner, "_fetch_aws_security_bulletins", lambda region="": [])
        monkeypatch.setattr(scanner, "_check_aws_security_notifications", lambda region="": [])
        monkeypatch.setattr(scanner, "_generate_fleet_summary", lambda results: {})
        monkeypatch.setattr(server, "_ALLOW_UNAUTHENTICATED", True)
        monkeypatch.setattr(server, "_API_AUTH_TOKEN", "")
        table.write_stored(_base_state(
            clusters=[_fleet_cluster("orders-dev", "15.10", "dev")],
            upgrades={"orders-dev": _upgrade("orders-dev", "complete", environment="dev")}))
        res = TestClient(server.app).post("/api/agent/scan")
        assert res.status_code == 200, res.text
        exposure = res.json()["clusters"][0]["cve_exposure"]
        assert all(c.get("cve_id") != "CVE-2025-1094" for c in exposure)


class TestSwitchover:
    def _client(self, monkeypatch):
        from fastapi.testclient import TestClient
        monkeypatch.setattr(server, "_ALLOW_UNAUTHENTICATED", True)
        monkeypatch.setattr(server, "_API_AUTH_TOKEN", "")
        return TestClient(server.app)

    def test_failed_switchover_is_reported_and_stays_retryable(self, table, monkeypatch):
        table.write_stored(_base_state(upgrades={
            "orders-prod": _upgrade("orders-prod", "awaiting_approval", deployment_id="bgd-1")}))
        monkeypatch.setattr(server, "execute_switchover", lambda dep, region: {
            "status": "error", "message": "AccessDenied ... not authorized to perform: rds:ModifyDBInstance"})
        resp = self._client(monkeypatch).post("/api/upgrades/switchover",
                                              json={"deployment_id": "bgd-1", "region": "us-east-2"})
        assert resp.status_code == 502
        upgrade = table.stored()["upgrades"]["orders-prod"]
        assert upgrade["status"] == "awaiting_approval"
        assert upgrade["steps"][-1]["status"] == "failed"

    def test_stuck_switching_reverts_when_rds_never_switched(self, table, monkeypatch):
        old = "2026-10-28T14:55:41+00:00"
        table.write_stored(_base_state(upgrades={"orders-prod": _upgrade(
            "orders-prod", "switching", deployment_id="bgd-1",
            steps=[{"step": "switchover", "time": old, "result": {"status": "error"}}])}))
        server._refresh_state()
        monkeypatch.setattr(server, "check_deployment_status",
                            lambda dep, region: {"status": "AVAILABLE", "ready_for_switchover": True})
        assert server._reconcile_active_upgrades() is True
        assert server._state["upgrades"]["orders-prod"]["status"] == "awaiting_approval"


class TestStartUpgradeDispatch:
    def test_start_runs_pipeline_as_async_job_on_lambda(self, table, monkeypatch):
        from fastapi.testclient import TestClient

        import shared.global_db

        table.write_stored(_base_state())
        invoked = []
        fake_lambda = MagicMock()
        fake_lambda.invoke.side_effect = lambda **kw: invoked.append(kw)
        monkeypatch.setattr(server, "_LAMBDA_FN_NAME", "aurora-patch-agent-api")
        monkeypatch.setattr(server, "_ALLOW_UNAUTHENTICATED", True)
        monkeypatch.setattr(server, "_API_AUTH_TOKEN", "")
        monkeypatch.setattr(server.boto3, "client", lambda name, **kw: fake_lambda)
        monkeypatch.setattr(shared.global_db, "check_upgrade_eligibility",
                            lambda *a, **k: {"eligible": True})
        ran_inline = []
        monkeypatch.setattr(server, "_run_upgrade_pipeline", lambda req: ran_inline.append(req))

        resp = TestClient(server.app).post("/api/upgrades/start", json={
            "cluster_id": "orders-prod", "target_version": "15.19", "region": "us-east-2",
            "environment": "production", "skip_preflight": True})

        assert resp.status_code == 200
        assert len(invoked) == 1 and invoked[0]["InvocationType"] == "Event"
        job_id = json.loads(invoked[0]["Payload"])["__job__"]
        assert server._read_job(job_id)["kind"] == "upgrade_pipeline"
        assert ran_inline == []  # not run as a request-blocking background task
        assert table.stored()["upgrades"]["orders-prod"]["status"] == "preflight"


class TestPipelineWorker:
    def test_worker_leaves_a_usable_event_loop_for_the_next_request(self, table, monkeypatch):
        # Regression: asyncio.run() in the worker unset the thread's loop, so the
        # next API Gateway request in the same container failed in Mangum.
        import asyncio

        async def fake_pipeline(request):
            return None

        monkeypatch.setattr(server, "_run_upgrade_pipeline", fake_pipeline)
        server._upgrade_pipeline_worker({
            "cluster_id": "orders-prod", "target_version": "15.19",
            "region": "us-east-2", "environment": "production"})
        loop = asyncio.get_event_loop()
        assert not loop.is_closed()
        assert loop.run_until_complete(asyncio.sleep(0, result="ok")) == "ok"


class TestDiagnosis:
    # The exact message RDS returned in the workshop run.
    LOGICAL_REPLICATION_ERROR = (
        "An error occurred (SourceClusterNotSupportedFault) when calling the "
        "CreateBlueGreenDeployment operation: Blue/Green Deployments require a DB cluster "
        "with logical replication enabled. Before you create a Blue/Green Deployment for a "
        "DB cluster, associate the DB cluster with a custom DB cluster parameter group that "
        "enables logical replication."
    )

    def test_logical_replication_error_is_not_misdiagnosed_as_reboot(self):
        diagnosis = server._diagnose_error(self.LOGICAL_REPLICATION_ERROR)
        assert "logical_replication" in diagnosis["diagnosis"]
        assert diagnosis["fix_action"] == "manual"
        assert diagnosis["auto_fixable"] is False  # no misleading Apply Fix button


class TestBlueGreenParameterGroup:
    def _client(self, source_family, target_family):
        client = MagicMock()
        client.describe_db_engine_versions.return_value = {
            "DBEngineVersions": [{"DBParameterGroupFamily": target_family}]}
        client.describe_db_cluster_parameter_groups.return_value = {
            "DBClusterParameterGroups": [{"DBParameterGroupFamily": source_family}]}
        return client

    def test_minor_upgrade_keeps_custom_group(self):
        client = self._client("aurora-postgresql15", "aurora-postgresql15")
        assert _resolve_target_cluster_parameter_group(
            client, "aurora-postgresql", "15.19", "workshop-pg15-bluegreen") == "workshop-pg15-bluegreen"

    def test_major_upgrade_uses_target_family_default(self):
        client = self._client("aurora-postgresql14", "aurora-postgresql16")
        assert _resolve_target_cluster_parameter_group(
            client, "aurora-postgresql", "16.15", "workshop-pg14-bluegreen") == "default.aurora-postgresql16"


class TestMySqlCveMatching:
    def _cves(self, version):
        cluster = {"cluster_id": "catalog-prod", "engine_version": version, "environment": "production"}
        result = _assess_urgency(cluster, cve_database=KNOWN_MYSQL_CVES, engine_type="aurora-mysql")
        return {c["cve_id"] for c in result["cve_exposure"]}

    def test_affected_version_is_flagged(self):
        assert {"CVE-2025-50094", "CVE-2025-50078", "CVE-2025-50084"} <= self._cves("8.0.mysql_aurora.3.08.0")

    def test_patched_version_is_clean(self):
        assert self._cves("8.0.mysql_aurora.3.08.2") == set()


class _RecordingCursor:
    """Fake psycopg2 cursor that records SQL and answers the setup queries."""

    def __init__(self, schema_ready: bool):
        self.schema_ready = schema_ready
        self.statements = []
        self._last = ""

    def execute(self, sql, params=None):
        text = sql if isinstance(sql, str) else str(sql)
        self.statements.append(text)
        self._last = text

    def fetchone(self):
        q = self._last
        if "pg_database" in q:
            return (1,)
        if "to_regclass" in q:
            return (self.schema_ready,)
        if "information_schema.columns" in q:
            return (1,) if self.schema_ready else None
        if "count(*)" in q:
            return (500,)
        return None

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.autocommit = False

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def _load_traffic_gen():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parent.parent / "traffic-gen" / "handler.py"
    spec = importlib.util.spec_from_file_location("traffic_gen_handler", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ddl(statements):
    return [s for s in statements if any(k in s.upper() for k in ("CREATE ", "ALTER ", "DROP "))]


class TestTrafficGeneratorDdl:
    # Regression: the generator ran CREATE ... IF NOT EXISTS every 15 minutes,
    # which made Aurora cancel Blue/Green switchovers.
    def test_no_ddl_when_schema_already_exists(self, monkeypatch):
        tg = _load_traffic_gen()
        cur = _RecordingCursor(schema_ready=True)
        monkeypatch.setattr(tg, "get_connection", lambda endpoint, dbname=None: _FakeConn(cur))
        tg.setup_schema("orders-prod.cluster.example")
        assert _ddl(cur.statements) == []

    def test_no_ddl_during_blue_green_even_if_schema_missing(self, monkeypatch):
        tg = _load_traffic_gen()
        cur = _RecordingCursor(schema_ready=False)
        monkeypatch.setattr(tg, "get_connection", lambda endpoint, dbname=None: _FakeConn(cur))
        result = tg.setup_schema("orders-prod.cluster.example", allow_ddl=False)
        assert _ddl(cur.statements) == []
        assert "skipped" in result

    def test_creates_schema_on_first_run(self, monkeypatch):
        tg = _load_traffic_gen()
        cur = _RecordingCursor(schema_ready=False)
        monkeypatch.setattr(tg, "get_connection", lambda endpoint, dbname=None: _FakeConn(cur))
        tg.setup_schema("orders-dev.cluster.example")
        assert any("CREATE TABLE IF NOT EXISTS customers" in s for s in cur.statements)

    def test_products_are_seeded_once(self, monkeypatch):
        tg = _load_traffic_gen()
        cur = _RecordingCursor(schema_ready=True)  # count(*) answers 500 (already seeded)
        monkeypatch.setattr(tg, "get_connection", lambda endpoint, dbname=None: _FakeConn(cur))
        tg.setup_schema("orders-prod.cluster.example")
        assert not any("INSERT INTO products" in s for s in cur.statements)


class TestLtsHint:
    def test_lts_target_is_never_lower_than_current(self):
        from agents.scanner.handler import _get_recommended_lts, _version_tuple
        rec = _get_recommended_lts("15.12")  # string compare used to return 15.8
        assert rec is None or rec["is_major"] or _version_tuple(rec["version"]) > (15, 12)
        assert rec is None or rec["version"] != "15.8"

    def test_lts_target_still_offered_when_ahead(self):
        from agents.scanner.handler import AURORA_PG_LTS_VERSIONS, _get_recommended_lts, _version_tuple
        same_major = [v for v in AURORA_PG_LTS_VERSIONS if v.startswith("15.")]
        if same_major:  # e.g. 15.8: a cluster on 15.4 should be offered it
            rec = _get_recommended_lts("15.4")
            assert rec and _version_tuple(rec["version"]) > (15, 4)


class TestScanRegions:
    def test_null_body_uses_configured_regions(self, table, monkeypatch):
        from fastapi.testclient import TestClient
        table.write_stored(_base_state())
        monkeypatch.setattr(server, "_ALLOW_UNAUTHENTICATED", True)
        monkeypatch.setattr(server, "_API_AUTH_TOKEN", "")
        seen = []
        monkeypatch.setattr(server, "discover_clusters", lambda region, account: seen.append(region) or [])
        import shared.config
        monkeypatch.setattr(shared.config, "get_scan_regions", lambda: ["eu-west-1"])
        resp = TestClient(server.app).post("/api/fleet/scan", content="[]",
                                           headers={"Content-Type": "application/json"})
        assert resp.status_code == 200
        assert seen == ["eu-west-1"]


class TestAuthBeforeState:
    def test_unauthenticated_mutation_is_rejected_before_state_is_read(self, table, monkeypatch):
        from fastapi.testclient import TestClient
        table.write_stored(_base_state())
        monkeypatch.setattr(server, "_API_AUTH_TOKEN", "secret-token")
        reads = []
        monkeypatch.setattr(server, "_refresh_state", lambda: reads.append(1))
        resp = TestClient(server.app).post("/api/fleet/scan", content="[]",
                                           headers={"Content-Type": "application/json"})
        assert resp.status_code == 401
        assert reads == []


class TestValidationLatency:
    def test_p99_uses_extended_statistics_only(self, monkeypatch):
        import agents.validation.handler as validation
        cw = MagicMock()
        cw.get_metric_statistics.return_value = {"Datapoints": [{"ExtendedStatistics": {"p99": 0.004}}]}
        monkeypatch.setattr(validation, "cloudwatch_client", lambda region: cw)
        assert validation.get_current_latency("orders-prod", "us-east-2") == pytest.approx(4.0)
        kwargs = cw.get_metric_statistics.call_args.kwargs
        assert "Statistics" not in kwargs and kwargs["ExtendedStatistics"] == ["p99"]
