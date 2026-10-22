# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the Discovery Agent."""
import json
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, "..")

from agents.discovery.handler import (
    discover_clusters,
    find_available_upgrades,
    _classify_environment,
)
from shared.models import ClusterInfo, Environment


class TestClassifyEnvironment:
    def test_prod_tag(self):
        assert _classify_environment("my-cluster", {"Environment": "production"}) == Environment.PRODUCTION

    def test_staging_tag(self):
        assert _classify_environment("my-cluster", {"Environment": "staging"}) == Environment.STAGING

    def test_dev_tag(self):
        assert _classify_environment("my-cluster", {"Environment": "dev"}) == Environment.DEV

    def test_prod_in_name(self):
        assert _classify_environment("myapp-prod-cluster", {}) == Environment.PRODUCTION

    def test_staging_in_name(self):
        assert _classify_environment("myapp-stg-db", {}) == Environment.STAGING

    def test_defaults_to_dev(self):
        assert _classify_environment("random-cluster", {}) == Environment.DEV


class TestDiscoverClusters:
    @patch("agents.discovery.handler.rds_client")
    def test_filters_aurora_engines_only(self, mock_rds_client):
        mock_client = MagicMock()
        mock_rds_client.return_value = mock_client

        mock_paginator = MagicMock()
        mock_client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {
                "DBClusters": [
                    {
                        "DBClusterIdentifier": "pg-cluster-1",
                        "Engine": "aurora-postgresql",
                        "EngineVersion": "15.4",
                        "DBClusterArn": "arn:aws:rds:us-east-1:123456:cluster:pg-cluster-1",
                        "DBClusterMembers": [{"DBInstanceIdentifier": "i-1"}],
                        "AllocatedStorage": 100,
                        "DBClusterParameterGroup": "default.aurora-postgresql15",
                    },
                    {
                        "DBClusterIdentifier": "mysql-cluster",
                        "Engine": "aurora-mysql",
                        "EngineVersion": "3.04",
                        "DBClusterArn": "arn:aws:rds:us-east-1:123456:cluster:mysql-cluster",
                        "DBClusterMembers": [],
                        "AllocatedStorage": 50,
                        "DBClusterParameterGroup": "default.aurora-mysql8.0",
                    },
                    {
                        "DBClusterIdentifier": "docdb-cluster",
                        "Engine": "docdb",
                        "EngineVersion": "5.0",
                        "DBClusterArn": "arn:aws:rds:us-east-1:123456:cluster:docdb-cluster",
                        "DBClusterMembers": [],
                        "AllocatedStorage": 50,
                    },
                ]
            }
        ]
        mock_client.list_tags_for_resource.return_value = {"TagList": []}
        mock_client.describe_db_cluster_parameters.return_value = {"Parameters": []}

        clusters = discover_clusters("us-east-1", "123456")

        # Should include both PostgreSQL and MySQL, but NOT DocumentDB
        assert len(clusters) == 2
        assert clusters[0].cluster_id == "pg-cluster-1"
        assert clusters[0].engine_type.value == "aurora-postgresql"
        assert clusters[1].cluster_id == "mysql-cluster"
        assert clusters[1].engine_type.value == "aurora-mysql"


class TestFindAvailableUpgrades:
    @patch("agents.discovery.handler.rds_client")
    def test_returns_target_when_upgrades_available(self, mock_rds_client):
        mock_client = MagicMock()
        mock_rds_client.return_value = mock_client

        mock_client.describe_db_engine_versions.return_value = {
            "DBEngineVersions": [
                {
                    "EngineVersion": "15.4",
                    "ValidUpgradeTarget": [
                        {"EngineVersion": "15.5"},
                        {"EngineVersion": "15.6"},
                        {"EngineVersion": "16.1"},
                    ],
                }
            ]
        }

        cluster = ClusterInfo(
            cluster_id="test-cluster",
            engine_version="15.4",
            region="us-east-1",
            environment=Environment.DEV,
            account_id="123456",
            instance_count=2,
            storage_gb=100,
        )

        target = find_available_upgrades(cluster)

        assert target is not None
        assert target.target_version == "15.6"  # latest minor
        assert "16.1" in target.available_versions

    @patch("agents.discovery.handler.rds_client")
    def test_returns_none_when_no_upgrades(self, mock_rds_client):
        mock_client = MagicMock()
        mock_rds_client.return_value = mock_client

        mock_client.describe_db_engine_versions.return_value = {
            "DBEngineVersions": [
                {
                    "EngineVersion": "16.1",
                    "ValidUpgradeTarget": [],
                }
            ]
        }

        cluster = ClusterInfo(
            cluster_id="latest-cluster",
            engine_version="16.1",
            region="us-east-1",
            environment=Environment.DEV,
            account_id="123456",
            instance_count=2,
            storage_gb=100,
        )

        target = find_available_upgrades(cluster)
        assert target is None
