# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Scanner Agent — CVE monitoring, version intelligence, and upgrade urgency scoring.

This agent goes beyond simple version diff. It:
1. Checks Aurora release notes for security fixes (CVEs)
2. Identifies LTS vs non-LTS version paths
3. Scores urgency based on exposure window and data sensitivity
4. Monitors deprecation timelines (Aurora end-of-life dates)

WHY AI IS NEEDED HERE:
- Aurora release notes are semi-structured text; extracting CVE severity
  and affected component requires reasoning, not regex
- Correlating a CVE to actual cluster exposure (does this cluster USE
  the affected auth module?) requires understanding cluster configuration
- Prioritizing 50 clusters across different urgency dimensions requires
  multi-factor reasoning that changes as new CVEs appear
"""
import json
import logging
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

from shared.aws_clients import rds_client, cloudwatch_client, bedrock_client, extract_bedrock_text
from shared.config import BEDROCK_MODEL_ID

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Aurora PostgreSQL end-of-life dates (major versions)
AURORA_PG_EOL = {
    "13": "2026-06-30",
    "14": "2027-02-28",
    "15": "2027-11-30",
    "16": "2028-11-30",
    "17": "2029-11-30",
    "18": "2030-11-30",
}

# Aurora PostgreSQL LTS (Long-Term Support) versions
# LTS versions stay on the same minor for 3+ years with critical patches only.
# Recommended for production workloads that need stability over latest features.
AURORA_PG_LTS_VERSIONS = {
    "13.16": {"lts_end": "2026-06-30", "note": "Final PG13 LTS"},
    "14.12": {"lts_end": "2027-02-28", "note": "PG14 LTS release"},
    "15.8":  {"lts_end": "2027-11-30", "note": "PG15 LTS release"},
    "16.4":  {"lts_end": "2028-11-30", "note": "PG16 LTS release"},
    "17.4":  {"lts_end": "2029-11-30", "note": "PG17 LTS release"},
}

# Known CVE patterns affecting Aurora PostgreSQL
KNOWN_CVES = {
    "CVE-2024-10976": {"severity": "HIGH", "affected": "<=15.8,<=16.4", "component": "row security policies", "fixed_in": ["15.10", "16.6"]},
    "CVE-2024-10977": {"severity": "MEDIUM", "affected": "<=15.8,<=16.4", "component": "libpq SSL certificate validation", "fixed_in": ["15.10", "16.6"]},
    "CVE-2024-10978": {"severity": "MEDIUM", "affected": "<=15.8,<=16.4", "component": "SET ROLE session authorization", "fixed_in": ["15.10", "16.6"]},
    "CVE-2024-7348": {"severity": "HIGH", "affected": "<=15.7,<=16.3", "component": "pg_dump race condition", "fixed_in": ["15.8", "16.4"]},
    "CVE-2025-1094": {"severity": "HIGH", "affected": "<=15.11,<=16.7,<=17.3", "component": "PL/pgSQL SQL injection in quoting functions", "fixed_in": ["15.12", "16.8", "17.4"]},
}

# Aurora MySQL end-of-life dates
# Aurora MySQL v2 = MySQL 5.7 compat, v3 = MySQL 8.0 compat
AURORA_MYSQL_EOL = {
    "5.7": "2024-10-31",  # Past EOL — extended support only (charges apply)
    "8.0": "2027-05-31",  # Aurora MySQL v3 standard support
}

# Aurora MySQL LTS versions
# LTS clusters stay on the same minor for 3+ years with critical patches only.
AURORA_MYSQL_LTS_VERSIONS = {
    "2.11.6": {"lts_end": "2024-10-31", "note": "MySQL 5.7 LTS (v2) — now in extended support"},
    "3.04.3": {"lts_end": "2027-05-31", "note": "MySQL 8.0 LTS (v3, compatible with MySQL 8.0.28)"},
}

# Known CVE patterns affecting Aurora MySQL
KNOWN_MYSQL_CVES = {
    "CVE-2024-21009": {"severity": "MEDIUM", "affected": "<=3.07.0", "component": "MySQL Server Optimizer DoS", "fixed_in": ["3.07.1"]},
    "CVE-2024-20977": {"severity": "MEDIUM", "affected": "<=3.06.0", "component": "MySQL Server Optimizer hang", "fixed_in": ["3.06.1"]},
    "CVE-2024-20963": {"severity": "MEDIUM", "affected": "<=3.05.2", "component": "MySQL Server Security privilege escalation", "fixed_in": ["3.06.0"]},
    "CVE-2024-21171": {"severity": "MEDIUM", "affected": "<=3.07.0", "component": "MySQL Server Optimizer crash", "fixed_in": ["3.07.1"]},
    "CVE-2025-50094": {"severity": "MEDIUM", "affected": "<=3.08.0", "component": "MySQL Server Optimizer DoS via stored procedures", "fixed_in": ["3.08.1"]},
    "CVE-2025-50078": {"severity": "MEDIUM", "affected": "<=3.08.0", "component": "MySQL Server DoS vulnerability", "fixed_in": ["3.08.1"]},
    "CVE-2025-50084": {"severity": "MEDIUM", "affected": "<=3.08.0", "component": "MySQL Server Optimizer repeated crash", "fixed_in": ["3.08.1"]},
    "CVE-2024-37371": {"severity": "HIGH", "affected": "<=3.07.1", "component": "Kerberos GSS-API token handling", "fixed_in": ["3.08.0"]},
}

# Module-level cache for live CVE data
_live_cve_cache: dict = {}
_live_cve_cache_timestamp: float = 0.0
_LIVE_CVE_CACHE_TTL_SECONDS: float = 6 * 60 * 60  # 6 hours


def _get_mysql_major_version(engine_version: str) -> str:
    """Extract MySQL compatibility version from Aurora MySQL engine version.

    Aurora MySQL versions can be:
    - "5.7.mysql_aurora.2.12.3" (v2 = MySQL 5.7)
    - "8.0.mysql_aurora.3.07.1" (v3 = MySQL 8.0)
    - Or just the Aurora version like "3.07.1"
    """
    if engine_version.startswith("5.7"):
        return "5.7"
    elif engine_version.startswith("8.0"):
        return "8.0"
    elif engine_version.startswith("2."):
        return "5.7"  # Aurora MySQL v2 = MySQL 5.7
    elif engine_version.startswith("3."):
        return "8.0"  # Aurora MySQL v3 = MySQL 8.0
    return "8.0"  # Default to 8.0


def _fetch_live_cves() -> dict:
    """Fetch live PostgreSQL CVEs from the NVD API v2.

    Returns a dict in the same format as KNOWN_CVES. Results are cached for 6 hours.
    On any failure (network, parsing, timeout), returns an empty dict so the caller
    falls back to the static KNOWN_CVES data.
    """
    global _live_cve_cache, _live_cve_cache_timestamp

    # Return cached results if still fresh
    if _live_cve_cache and (time.time() - _live_cve_cache_timestamp) < _LIVE_CVE_CACHE_TTL_SECONDS:
        return _live_cve_cache

    try:
        url = "https://services.nvd.nist.gov/rest/json/cves/2.0?keywordSearch=postgresql&resultsPerPage=50"
        # Audit: url is a fixed constant above, never derived from request input,
        # but assert the scheme explicitly so this can't silently start accepting
        # file:// or other unexpected schemes if the URL is ever parameterized later.
        if not url.startswith("https://"):
            raise ValueError(f"Refusing to fetch non-HTTPS URL: {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "aurora-patch-agent/1.0"})
        with urllib.request.urlopen(req, timeout=10) as response:  # noqa: S310 # nosec B310 - scheme validated above, url is a fixed https constant
            data = json.loads(response.read().decode("utf-8"))

        results = {}
        for vuln in data.get("vulnerabilities", []):
            cve_item = vuln.get("cve", {})
            cve_id = cve_item.get("id", "")

            # Extract description
            descriptions = cve_item.get("descriptions", [])
            description = ""
            for desc in descriptions:
                if desc.get("lang") == "en":
                    description = desc.get("value", "").lower()
                    break

            # Filter: must mention postgresql in description
            if "postgresql" not in description:
                continue

            # Extract CVSS v3.1 score
            cvss_score = 0.0
            metrics = cve_item.get("metrics", {})
            cvss_v31_list = metrics.get("cvssMetricV31", [])
            if cvss_v31_list:
                cvss_data = cvss_v31_list[0].get("cvssData", {})
                cvss_score = cvss_data.get("baseScore", 0.0)
            else:
                # Fall back to cvssMetricV30 if v3.1 not available
                cvss_v30_list = metrics.get("cvssMetricV30", [])
                if cvss_v30_list:
                    cvss_data = cvss_v30_list[0].get("cvssData", {})
                    cvss_score = cvss_data.get("baseScore", 0.0)

            # Filter: only include CVEs with CVSS >= 4.0
            if cvss_score < 4.0:
                continue

            # Determine severity from CVSS score
            if cvss_score >= 9.0:
                severity = "CRITICAL"
            elif cvss_score >= 7.0:
                severity = "HIGH"
            else:
                severity = "MEDIUM"

            # Extract affected version ranges from CPE match strings
            affected_versions = []
            fixed_in = []
            configurations = cve_item.get("configurations", [])
            for config in configurations:
                for node in config.get("nodes", []):
                    for cpe_match in node.get("cpeMatch", []):
                        cpe_uri = cpe_match.get("criteria", "")
                        if "postgresql" not in cpe_uri:
                            continue
                        version_end = cpe_match.get("versionEndIncluding", "")
                        version_end_excl = cpe_match.get("versionEndExcluding", "")
                        if version_end:
                            affected_versions.append(f"<={version_end}")
                        elif version_end_excl:
                            affected_versions.append(f"<{version_end_excl}")
                            fixed_in.append(version_end_excl)

            # Build affected spec string
            affected_spec = ",".join(affected_versions) if affected_versions else "unknown"

            # Extract component from description (first sentence, truncated)
            component = description[:80].split(".")[0] if description else "postgresql"

            results[cve_id] = {
                "severity": severity,
                "affected": affected_spec,
                "component": component,
                "fixed_in": fixed_in if fixed_in else [],
            }

        # Update cache
        _live_cve_cache = results
        _live_cve_cache_timestamp = time.time()
        logger.info(f"Fetched {len(results)} live CVEs from NVD API")
        return results

    except Exception as e:
        logger.warning(f"Failed to fetch live CVEs from NVD: {e}")
        return {}


def _fetch_aws_security_bulletins(region: str = "") -> dict:
    """Fetch security-related upgrade information from AWS RDS and Health APIs.

    Checks describe_db_engine_versions for aurora-postgresql to identify
    security-motivated releases via ValidUpgradeTarget, and pulls active
    RDS security advisories from the AWS Health API if available.

    Returns supplementary CVE/advisory information as a dict of advisories.
    """
    advisories = {}

    try:
        client = rds_client(region=region)
        response = client.describe_db_engine_versions(
            Engine="aurora-postgresql",
            IncludeAll=False,
        )

        for version_info in response.get("DBEngineVersions", []):
            engine_version = version_info.get("EngineVersion", "")
            valid_targets = version_info.get("ValidUpgradeTarget", [])

            for target in valid_targets:
                # Security-motivated releases are often marked as auto-upgrade eligible
                # or have IsMajorVersionUpgrade=False with AutoUpgrade=True
                if target.get("AutoUpgrade", False) and not target.get("IsMajorVersionUpgrade", False):
                    target_version = target.get("EngineVersion", "")
                    advisory_id = f"AWS-RDS-UPGRADE-{engine_version}-to-{target_version}"
                    advisories[advisory_id] = {
                        "type": "security_upgrade",
                        "source_version": engine_version,
                        "target_version": target_version,
                        "description": target.get("Description", "Security-motivated auto-upgrade available"),
                        "auto_upgrade": True,
                    }

    except Exception as e:
        logger.warning(f"Failed to fetch RDS engine versions: {e}")

    # Also check AWS Health API for active RDS security events
    try:
        import boto3
        health_client = boto3.client("health", region_name="us-east-1")  # Health API is global (us-east-1)
        health_response = health_client.describe_events(
            filter={
                "services": ["RDS"],
                "eventTypeCategories": ["accountNotification", "scheduledChange"],
                "eventStatusCodes": ["open", "upcoming"],
            }
        )

        for event in health_response.get("events", []):
            event_arn = event.get("arn", "")
            event_type = event.get("eventTypeCode", "")
            if "security" in event_type.lower() or "vulnerability" in event_type.lower():
                advisories[event_arn] = {
                    "type": "health_advisory",
                    "event_type": event_type,
                    "service": event.get("service", "RDS"),
                    "status": event.get("statusCode", "unknown"),
                    "start_time": event.get("startTime", "").isoformat() if event.get("startTime") else None,
                    "description": event.get("eventTypeCode", ""),
                    "region": event.get("region", "global"),
                }

    except Exception as e:
        logger.warning(f"Failed to fetch AWS Health events: {e}")

    return advisories


def _check_aws_security_notifications(region: str = "") -> list[dict]:
    """Check for active RDS security advisories via the AWS Health API.

    Uses the boto3 health client to query for any open or upcoming security-related
    events affecting the RDS service in the specified region.

    Returns a list of active security advisory dicts, or an empty list on failure.
    """
    notifications = []

    try:
        import boto3
        # AWS Health API is a global service, always in us-east-1
        health_client = boto3.client("health", region_name="us-east-1")

        response = health_client.describe_events(
            filter={
                "services": ["RDS"],
                "regions": [region],
                "eventTypeCategories": ["accountNotification", "scheduledChange"],
                "eventStatusCodes": ["open", "upcoming"],
            }
        )

        for event in response.get("events", []):
            event_type_code = event.get("eventTypeCode", "")
            # Include security-related and maintenance notifications
            if any(kw in event_type_code.lower() for kw in ["security", "vulnerability", "patch", "maintenance"]):
                # Fetch detailed description for this event
                detail_description = ""
                try:
                    detail_response = health_client.describe_event_details(
                        eventArns=[event.get("arn", "")]
                    )
                    for detail in detail_response.get("successfulSet", []):
                        detail_description = detail.get("eventDescription", {}).get("latestDescription", "")
                except Exception:
                    pass

                notifications.append({
                    "arn": event.get("arn", ""),
                    "event_type": event_type_code,
                    "status": event.get("statusCode", "unknown"),
                    "region": event.get("region", region),
                    "start_time": event.get("startTime", "").isoformat() if event.get("startTime") else None,
                    "end_time": event.get("endTime", "").isoformat() if event.get("endTime") else None,
                    "description": detail_description or event_type_code,
                    "service": "RDS",
                })

    except Exception as e:
        logger.warning(f"Failed to check AWS security notifications for region {region}: {e}")

    return notifications


def scan_fleet_urgency(clusters: list[dict], region: str = "") -> dict:
    """Scan entire fleet and produce urgency-scored upgrade candidates.

    Returns a prioritized list with reasons for each upgrade urgency.
    Fetches live CVE data from NVD and merges with static KNOWN_CVES (live takes priority).
    Uses persistent DynamoDB cache to survive Lambda cold starts and NVD outages.
    """
    # Callers usually don't pass a Region; an empty string would make boto3
    # build "https://rds..amazonaws.com" and the Health filter reject [""].
    if not region:
        from shared.config import DEPLOYMENT_REGION
        region = DEPLOYMENT_REGION

    # Use persistent cache with full fallback chain
    from shared.cve_cache import get_cves_with_fallback
    merged_cves = get_cves_with_fallback(_fetch_live_cves, KNOWN_CVES)
    live_cves = _fetch_live_cves()

    # MySQL CVEs use static database (no live NVD fetch for now)
    merged_mysql_cves = KNOWN_MYSQL_CVES.copy()

    # Fetch AWS security bulletins for additional context
    aws_bulletins = _fetch_aws_security_bulletins(region=region)

    # Check for active security notifications
    security_notifications = _check_aws_security_notifications(region=region)

    results = []

    for cluster in clusters:
        engine = cluster.get("engine_type", "aurora-postgresql")
        if engine == "aurora-mysql":
            urgency = _assess_urgency(cluster, cve_database=merged_mysql_cves, engine_type=engine)
        else:
            urgency = _assess_urgency(cluster, cve_database=merged_cves, engine_type=engine)
        results.append(urgency)

    # Sort by urgency score (highest = most urgent)
    results.sort(key=lambda r: r["urgency_score"], reverse=True)

    # Use AI to produce fleet-wide summary and recommendations
    summary = _generate_fleet_summary(results)

    return {
        "scan_time": datetime.now(timezone.utc).isoformat(),
        "total_clusters": len(results),
        "critical_count": sum(1 for r in results if r["urgency_level"] == "critical"),
        "high_count": sum(1 for r in results if r["urgency_level"] == "high"),
        "cve_sources": {
            "static_count": len(KNOWN_CVES),
            "live_count": len(live_cves),
            "merged_count": len(merged_cves),
        },
        "aws_security_bulletins": aws_bulletins,
        "active_security_notifications": security_notifications,
        "clusters": results,
        "fleet_summary": summary,
    }


def _assess_urgency(cluster: dict, cve_database: dict | None = None, engine_type: str = "aurora-postgresql") -> dict:
    """Assess upgrade urgency for a single cluster."""
    cluster_id = cluster["cluster_id"]
    version = cluster["engine_version"]
    environment = cluster.get("environment", "dev")

    # Determine major version based on engine type
    if engine_type == "aurora-mysql":
        major = _get_mysql_major_version(version)
    else:
        major = version.split(".")[0]

    # Use provided CVE database or fall back to appropriate static database
    if cve_database is not None:
        cves_to_check = cve_database
    elif engine_type == "aurora-mysql":
        cves_to_check = KNOWN_MYSQL_CVES
    else:
        cves_to_check = KNOWN_CVES

    urgency_score = 0
    reasons = []
    cve_exposure = []

    # MySQL CVE ranges use the Aurora version ("3.08.0"), not the full engine
    # version ("8.0.mysql_aurora.3.08.0"); compare like with like.
    cve_version = version
    if engine_type == "aurora-mysql" and "mysql_aurora." in version:
        cve_version = version.split("mysql_aurora.")[-1]

    # Check CVE exposure
    for cve_id, cve_info in cves_to_check.items():
        if _version_affected(cve_version, cve_info["affected"]):
            severity_score = {"CRITICAL": 40, "HIGH": 30, "MEDIUM": 15, "LOW": 5}
            score = severity_score.get(cve_info["severity"], 10)
            # Production clusters get amplified score
            if environment == "production":
                score *= 2
            urgency_score += score
            cve_exposure.append({
                "cve_id": cve_id,
                "severity": cve_info["severity"],
                "component": cve_info["component"],
                "fixed_in": cve_info["fixed_in"],
            })
            reasons.append(f"{cve_id} ({cve_info['severity']}): {cve_info['component']}")

    # Check end-of-life proximity — use appropriate EOL map
    eol_map = AURORA_MYSQL_EOL if engine_type == "aurora-mysql" else AURORA_PG_EOL
    eol_date_str = eol_map.get(major)
    if eol_date_str:
        eol_date = datetime.strptime(eol_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days_until_eol = (eol_date - datetime.now(timezone.utc)).days
        engine_label = "MySQL" if engine_type == "aurora-mysql" else "PG"
        if days_until_eol < 0:
            urgency_score += 50
            reasons.append(f"PAST END-OF-LIFE: {engine_label} {major} EOL was {eol_date_str}")
        elif days_until_eol < 90:
            urgency_score += 35
            reasons.append(f"EOL in {days_until_eol} days ({eol_date_str}) — major version upgrade needed")
        elif days_until_eol < 180:
            urgency_score += 15
            reasons.append(f"EOL approaching in {days_until_eol} days — plan major upgrade")

    # Check version staleness (minor versions behind latest)
    available = cluster.get("available_versions", [])
    if available:
        same_major_available = [v for v in available if v.split(".")[0] == major]
        if len(same_major_available) >= 3:
            urgency_score += 10
            reasons.append(f"{len(same_major_available)} minor versions behind latest")

    # Classify urgency level
    if urgency_score >= 60:
        urgency_level = "critical"
    elif urgency_score >= 35:
        urgency_level = "high"
    elif urgency_score >= 15:
        urgency_level = "medium"
    else:
        urgency_level = "low"

    # Determine recommended upgrade type
    target_version = cluster.get("target_version")
    upgrade_type = "none"
    if target_version:
        target_major = target_version.split(".")[0]
        if target_major != major:
            upgrade_type = "major"
        else:
            upgrade_type = "minor"

    # LTS tagging — identify if current/target versions are LTS
    current_lts = _get_lts_info(version, engine_type)
    target_lts = _get_lts_info(target_version, engine_type) if target_version else None
    recommended_lts = _get_recommended_lts(version, engine_type)

    return {
        "cluster_id": cluster_id,
        "current_version": version,
        "target_version": target_version,
        "environment": environment,
        "urgency_score": min(urgency_score, 100),
        "urgency_level": urgency_level,
        "upgrade_type": upgrade_type,
        "cve_exposure": cve_exposure,
        "eol_date": eol_date_str,
        "reasons": reasons,
        "lts": {
            "current_is_lts": current_lts is not None,
            "current_lts_info": current_lts,
            "target_is_lts": target_lts is not None,
            "target_lts_info": target_lts,
            "recommended_lts_target": recommended_lts,
        },
    }


def _version_affected(current: str, affected_spec: str) -> bool:
    """Check if current version falls within affected range."""
    for spec in affected_spec.split(","):
        spec = spec.strip()
        if spec.startswith("<="):
            max_affected = spec[2:]
            if _version_in_major(current, max_affected) and _version_lte(current, max_affected):
                return True
    return False


def _version_in_major(v1: str, v2: str) -> bool:
    return v1.split(".")[0] == v2.split(".")[0]


def _get_lts_info(version: str, engine_type: str = "aurora-postgresql") -> dict | None:
    """Check if a version is an LTS release. Returns LTS info or None."""
    if not version:
        return None
    if engine_type == "aurora-mysql":
        # For MySQL, check the Aurora version part (e.g., "3.04.3" from "8.0.mysql_aurora.3.04.3")
        aurora_ver = version
        if "mysql_aurora." in version:
            aurora_ver = version.split("mysql_aurora.")[-1]
        return AURORA_MYSQL_LTS_VERSIONS.get(aurora_ver)
    else:
        return AURORA_PG_LTS_VERSIONS.get(version)


def _get_recommended_lts(current_version: str, engine_type: str = "aurora-postgresql") -> dict | None:
    """Find the recommended LTS target for an upgrade.

    Returns the next LTS version that is:
    1. In the same major version (for minor upgrades)
    2. Or the LTS version of the next major (for major upgrades)
    """
    if not current_version:
        return None

    if engine_type == "aurora-mysql":
        # For MySQL, recommend the v3 LTS if on v2, or latest v3 LTS
        mysql_major = _get_mysql_major_version(current_version)
        if mysql_major == "5.7":
            return {"version": "3.04.3", "note": "MySQL 8.0 LTS — recommended upgrade from 5.7", "is_major": True}
        # Already on 8.0 — recommend the LTS minor
        aurora_ver = current_version
        if "mysql_aurora." in current_version:
            aurora_ver = current_version.split("mysql_aurora.")[-1]
        if _version_tuple(aurora_ver) < _version_tuple("3.04.3"):
            return {"version": "3.04.3", "note": "MySQL 8.0 LTS release (8.0.28 compatible)", "is_major": False}
        return None  # Already at or past LTS
    else:
        # PostgreSQL
        current_major = current_version.split(".")[0]
        # Find LTS in same major
        for lts_ver, info in AURORA_PG_LTS_VERSIONS.items():
            lts_major = lts_ver.split(".")[0]
            # Compare numerically: as strings, "15.8" > "15.12" and a cluster on
            # 15.12 was offered 15.8 (a downgrade) as its LTS target.
            if lts_major == current_major and _version_tuple(lts_ver) > _version_tuple(current_version):
                return {"version": lts_ver, "note": info["note"], "is_major": False}
        # If no same-major LTS ahead, recommend next major's LTS
        next_major = str(int(current_major) + 1)
        for lts_ver, info in AURORA_PG_LTS_VERSIONS.items():
            if lts_ver.startswith(next_major + "."):
                return {"version": lts_ver, "note": info["note"], "is_major": True}
        return None


def _version_tuple(version: str) -> tuple:
    """'15.12' -> (15, 12); non-numeric parts sort as 0."""
    return tuple(int(p) if p.isdigit() else 0 for p in version.split("."))


def _version_lte(v1: str, v2: str) -> bool:
    """Compare version strings (e.g., '15.7' <= '15.8')."""
    parts1 = [int(x) for x in v1.split(".")]
    parts2 = [int(x) for x in v2.split(".")]
    return parts1 <= parts2


def _generate_fleet_summary(results: list[dict]) -> dict:
    """Use AI to generate fleet-wide upgrade summary and recommendations."""
    critical = [r for r in results if r["urgency_level"] == "critical"]
    high = [r for r in results if r["urgency_level"] == "high"]

    if not critical and not high:
        return {
            "headline": "Fleet is in good shape — no urgent upgrades required",
            "action_needed": False,
            "recommendations": ["Continue monitoring for new CVEs and version releases"],
        }

    prompt = f"""You are a fleet database operations advisor analyzing Aurora PostgreSQL upgrade urgency.

## Fleet Status
- Total clusters: {len(results)}
- Critical urgency: {len(critical)}
- High urgency: {len(high)}

## Critical Clusters:
{json.dumps(critical, indent=2, default=str)}

## High Priority Clusters:
{json.dumps(high[:5], indent=2, default=str)}

## Your Task
Generate a concise fleet upgrade summary:
1. What is the most pressing risk across the fleet?
2. Which clusters should be upgraded THIS WEEK?
3. What is the recommended upgrade sequence considering dependencies?
4. Are there any clusters that share the same vulnerability and can be batch-upgraded?

Respond with JSON:
{{
  "headline": "One-sentence fleet status",
  "action_needed": true,
  "most_pressing_risk": "...",
  "upgrade_this_week": ["cluster_id1", "cluster_id2"],
  "batch_groups": [{{"reason": "same CVE/version", "clusters": ["id1","id2"]}}],
  "recommendations": ["rec1", "rec2", "rec3"]
}}"""

    try:
        client = bedrock_client(region="")
        response = client.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        body = json.loads(response["body"].read())
        text = extract_bedrock_text(body)
        if text and "{" in text:
            return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except Exception as e:
        logger.warning(f"Fleet summary generation failed: {e}")

    return {
        "headline": f"{len(critical)} critical, {len(high)} high priority clusters need upgrades",
        "action_needed": True,
        "recommendations": [r["reasons"][0] for r in critical[:3]] if critical else [],
    }
