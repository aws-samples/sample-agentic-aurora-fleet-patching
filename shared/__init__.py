# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared utilities for Aurora Patch Agent.

Modules:
    config              - Environment-driven configuration
    aws_clients         - Cached boto3 client initialization
    models              - Data models shared across agents
    multi_account       - Cross-account session management
    iam_auth            - IAM database authentication
    cve_cache           - Persistent CVE data caching
    recommendations     - Recommendation engine (agent suggests, user decides)
    outcome_learning    - Learning from outcomes (agent gets smarter over time)
"""
