# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Data models shared across all agents."""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Environment(str, Enum):
    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"


class ActionMode(str, Enum):
    """Controls whether agents execute actions or recommend them for approval.

    RECOMMEND: Agent analyzes, reasons, and proposes actions — user must approve.
    EXECUTE:   Agent carries out approved actions (only after user says "go").
    """
    RECOMMEND = "recommend"
    EXECUTE = "execute"


class UpgradeStatus(str, Enum):
    PENDING = "pending"
    ASSESSING = "assessing"
    READY = "ready"
    RECOMMENDED = "recommended"        # Agent recommends upgrade — awaiting user decision
    APPROVED = "approved"              # User approved — ready for execution
    EXECUTING = "executing"
    VALIDATING = "validating"
    COMPLETE = "complete"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    AWAITING_APPROVAL = "awaiting_approval"
    ROLLBACK_RECOMMENDED = "rollback_recommended"  # Agent recommends rollback — user decides


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ClusterType(str, Enum):
    PRIMARY = "primary"
    BLUE_GREEN = "blue_green"
    OLD_PRIMARY = "old_primary"
    GLOBAL_SECONDARY = "global_secondary"


class EngineType(str, Enum):
    POSTGRESQL = "aurora-postgresql"
    MYSQL = "aurora-mysql"


@dataclass
class Recommendation:
    """A proposed action the agent recommends but does not execute.

    The agent surfaces these to the user with full context and reasoning.
    Nothing happens until the user explicitly approves.
    """
    id: str                            # Unique recommendation ID
    action: str                        # What the agent proposes (e.g., "upgrade", "rollback", "hold")
    cluster_id: str                    # Target cluster
    reasoning: str                     # WHY the agent recommends this (plain English)
    risk_level: RiskLevel = RiskLevel.LOW
    confidence: float = 0.0            # 0-100, how confident the agent is
    alternatives: list[str] = field(default_factory=list)  # Other options the user could choose
    context: dict = field(default_factory=dict)            # Supporting data for the recommendation
    created_at: str = ""               # ISO timestamp
    status: str = "pending"            # pending | approved | rejected | expired
    approved_by: str = ""              # Who approved (for audit)
    approved_at: str = ""              # When approved


@dataclass
class ClusterInfo:
    cluster_id: str
    engine_version: str
    region: str
    environment: Environment
    account_id: str
    instance_count: int
    storage_gb: float
    extensions: list[str] = field(default_factory=list)
    parameter_group: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    cluster_type: ClusterType = ClusterType.PRIMARY
    source_cluster: Optional[str] = None
    engine_type: EngineType = EngineType.POSTGRESQL


@dataclass
class UpgradeTarget:
    cluster: ClusterInfo
    target_version: str
    available_versions: list[str] = field(default_factory=list)
    risk_score: float = 0.0
    risk_level: RiskLevel = RiskLevel.LOW
    risk_factors: list[str] = field(default_factory=list)
    status: UpgradeStatus = UpgradeStatus.PENDING
    blue_green_deployment_id: Optional[str] = None


@dataclass
class HealthCheck:
    cluster_id: str
    connections_ok: bool = False
    replication_lag_ms: float = 0.0
    p99_latency_ms: float = 0.0
    baseline_p99_ms: float = 0.0
    extensions_loaded: bool = False
    error_rate_pct: float = 0.0
    passed: bool = False
    details: dict = field(default_factory=dict)
