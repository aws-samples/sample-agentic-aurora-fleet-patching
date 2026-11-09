# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Regression tests for bugs found while deploying the sample as a new customer.

* Preflight endpoint: must be read-only (no snapshot, no lock) and fast.
* Fleet scan: one unreachable region must not fail the whole scan.
* Environment classification: "nonprod" is not production.
"""
import copy

import pytest
from fastapi.testclient import TestClient

import api.server as server
from agents.discovery.handler import _classify_environment
from shared.models import Environment


@pytest.fixture
def client(monkeypatch, tmp_path):
    """TestClient with auth disabled and state kept in memory/tmp."""
    monkeypatch.setattr(server, "_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(server, "_ddb_table", lambda: None, raising=False)
    monkeypatch.setattr(server, "_ALLOW_UNAUTHENTICATED", True)
    monkeypatch.setattr(server, "_API_AUTH_TOKEN", "")
    monkeypatch.setattr(server, "_refresh_state", lambda: None)
    monkeypatch.setattr(server, "_save_state", lambda: None)
    monkeypatch.setattr(server, "_state", copy.deepcopy(server._DEFAULT_STATE))
    return TestClient(server.app)


class TestPreflightEndpointIsReadOnly:
    def test_preflight_endpoint_runs_in_dry_run_mode(self, client, monkeypatch):
        calls = []

        def fake_run(cluster_id, region, env, dry_run=False):
            calls.append(dry_run)
            return {"cluster_id": cluster_id, "all_passed": True, "checks": {}, "blocked_by": []}

        monkeypatch.setattr(server, "run_preflight_checks", fake_run)
        resp = client.post(
            "/api/upgrades/preflight",
            json={"cluster_id": "c1", "target_version": "15.19", "region": "us-east-1", "environment": "dev"},
        )
        assert resp.status_code == 200
        assert calls == [True], "preflight endpoint must pass dry_run=True"

    def test_dry_run_does_not_create_snapshot_or_lock(self, monkeypatch):
        from agents.execution import preflight

        for name in ("check_cluster_healthy", "check_replication_lag", "check_no_blocking_transactions",
                     "check_backups_current"):
            monkeypatch.setattr(preflight, name, lambda *a, **k: {"passed": True, "reason": "ok"})
        monkeypatch.setattr(preflight, "check_change_window", lambda *a, **k: {"passed": True, "reason": "ok"})
        monkeypatch.setattr(preflight, "check_lock_available", lambda *a, **k: {"passed": True, "reason": "ok"})
        monkeypatch.setattr(preflight, "check_snapshot_feasibility", lambda *a, **k: {"passed": True, "reason": "ok"})

        def boom(*a, **k):
            raise AssertionError("dry run must not create a snapshot or acquire the lock")

        monkeypatch.setattr(preflight, "create_pre_upgrade_snapshot", boom)
        monkeypatch.setattr(preflight, "acquire_upgrade_lock", boom)
        assert preflight.run_preflight_checks("c1", "us-east-1", "dev", dry_run=True)["all_passed"]


class TestFleetScanSkipsBadRegions:
    @staticmethod
    def _discover(good):
        def fake(region, account):
            if region not in good:
                raise RuntimeError(f"Connect timeout on endpoint URL: rds.{region}.amazonaws.com")
            return []

        return fake

    def test_bad_region_is_reported_and_others_still_scan(self, client, monkeypatch):
        monkeypatch.setattr(server, "discover_clusters", self._discover({"us-east-1"}))
        resp = client.post("/api/fleet/scan", json=["us-east-1", "me-south-1"])
        assert resp.status_code == 200
        body = resp.json()
        assert list(body["region_errors"]) == ["me-south-1"]
        assert "Connect timeout" in body["region_errors"]["me-south-1"]

    def test_region_that_never_responds_is_cut_off_at_the_deadline(self, client, monkeypatch):
        import time

        monkeypatch.setattr(server, "_REGION_SCAN_TIMEOUT_SEC", 1)

        def fake(region, account):
            if region == "me-south-1":
                time.sleep(5)  # accepts the connection, never answers
            return []

        monkeypatch.setattr(server, "discover_clusters", fake)
        started = time.monotonic()
        resp = client.post("/api/fleet/scan", json=["us-east-1", "me-south-1"])
        assert resp.status_code == 200
        assert time.monotonic() - started < 4, "scan must not wait for the unresponsive region"
        assert list(resp.json()["region_errors"]) == ["me-south-1"]
        assert "No response within 1s" in resp.json()["region_errors"]["me-south-1"]

    def test_all_regions_failing_keeps_stored_fleet(self, client, monkeypatch):
        server._state["clusters"] = [{"cluster_id": "keep-me"}]
        monkeypatch.setattr(server, "discover_clusters", self._discover(set()))
        resp = client.post("/api/fleet/scan", json=["me-south-1", "af-south-1"])
        assert resp.status_code == 502
        assert server._state["clusters"] == [{"cluster_id": "keep-me"}]

    def test_clean_scan_has_empty_region_errors(self, client, monkeypatch):
        monkeypatch.setattr(server, "discover_clusters", self._discover({"us-east-1", "us-west-1"}))
        body = client.post("/api/fleet/scan", json=["us-east-1", "us-west-1"]).json()
        assert body["region_errors"] == {}


class TestEnvironmentClassification:
    @pytest.mark.parametrize("name", ["tar-nonprod-cluster", "orders-non-prod", "billing_non_prod_db", "nonprd-x"])
    def test_nonprod_names_are_not_production(self, name):
        assert _classify_environment(name, {}) == Environment.DEV

    @pytest.mark.parametrize("name", ["tar-prod-cluster", "orders-prd", "prod"])
    def test_prod_names_are_still_production(self, name):
        assert _classify_environment(name, {}) == Environment.PRODUCTION

    def test_explicit_tag_still_wins(self):
        assert _classify_environment("tar-nonprod-cluster", {"Environment": "production"}) == Environment.PRODUCTION

    def test_staging_names_unchanged(self):
        assert _classify_environment("orders-staging", {}) == Environment.STAGING


class TestDryRunKeepsExistingLock:
    def test_failed_dry_run_does_not_release_another_upgrades_lock(self, monkeypatch):
        from agents.execution import preflight

        ok = {"passed": True, "reason": "ok"}
        for name in ("check_cluster_healthy", "check_replication_lag", "check_no_blocking_transactions",
                     "check_backups_current", "check_snapshot_feasibility"):
            monkeypatch.setattr(preflight, name, lambda *a, **k: ok)
        monkeypatch.setattr(preflight, "check_change_window", lambda *a, **k: ok)
        monkeypatch.setattr(preflight, "check_lock_available",
                            lambda *a, **k: {"passed": False, "reason": "lock exists"})
        released = []
        monkeypatch.setattr(preflight, "release_upgrade_lock", lambda cid: released.append(cid))

        result = preflight.run_preflight_checks("c1", "us-east-1", "dev", dry_run=True)
        assert not result["all_passed"]
        assert released == [], "a dry run must not release a lock it did not acquire"

    def test_failed_real_run_still_releases_its_own_lock(self, monkeypatch):
        from agents.execution import preflight

        ok = {"passed": True, "reason": "ok"}
        for name in ("check_cluster_healthy", "check_replication_lag", "check_no_blocking_transactions",
                     "check_backups_current", "create_pre_upgrade_snapshot"):
            monkeypatch.setattr(preflight, name, lambda *a, **k: ok)
        monkeypatch.setattr(preflight, "acquire_upgrade_lock", lambda *a, **k: ok)
        monkeypatch.setattr(preflight, "check_change_window",
                            lambda *a, **k: {"passed": False, "reason": "outside window"})
        released = []
        monkeypatch.setattr(preflight, "release_upgrade_lock", lambda cid: released.append(cid))

        assert not preflight.run_preflight_checks("c1", "us-east-1", "dev")["all_passed"]
        assert released == ["c1"]

