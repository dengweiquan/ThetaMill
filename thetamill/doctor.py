"""Stage-D1 offline read-only health diagnostics for ThetaMill.

Provides strict offline diagnostics for configuration, the SQLite submission
ledger, Python dependencies, and deliverable evidence files.

Caveat: offline stopped-copy only; no concurrent consistency guarantee.
This module is strictly read-only: it performs no socket connections, no broker
calls, no file creation, no state mutations, no audit writes, and no commit
operations.
"""
from __future__ import annotations

import importlib.metadata
import json
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from thetamill.delivery_evidence import (
    DeliverableEvidenceError,
    load_delivery_evidence,
)
from thetamill.risk import RiskConfigError, validate_risk_config
from thetamill.state import (
    SchemaError,
    SubmissionLedger,
    _REQUIRED_TABLES,
)

DOCTOR_SCHEMA = "thetamill.doctor.v1"
DOCTOR_CAVEAT = "offline stopped-copy only; no concurrent consistency guarantee"

_REQUIRED_TABLE_COLUMNS: Mapping[str, frozenset[str]] = {
    "meta": frozenset({"key", "value"}),
    "intents": frozenset({
        "intent_id", "scope", "account", "environment", "full_digest",
        "approval_code", "payload", "config", "config_digest", "state",
        "created_at", "expires_at", "claimed_at", "terminal_at", "consumed",
    }),
    "legacy_codes": frozenset({"code", "scope", "source", "imported_at"}),
    "day_budget": frozenset({
        "scope", "day_key", "limit_count", "reserved_count",
        "submitted_count", "migration_frozen",
    }),
    "resources": frozenset({"scope", "currency", "resource_key", "amount"}),
    "reservations": frozenset({
        "reservation_id", "intent_id", "scope", "day_key", "kind",
        "state", "created_at", "day_submission_slots",
    }),
    "reservation_items": frozenset({
        "reservation_id", "scope", "currency", "resource_key", "amount",
    }),
    "legs": frozenset({
        "intent_id", "reservation_id", "order_ref", "leg_index",
        "contract_id", "action", "quantity", "state", "order_id",
        "perm_id", "created_at", "submitted_at", "updated_at",
    }),
    "reconciliation": frozenset({
        "observation_id", "intent_id", "order_ref", "perm_id",
        "order_id", "status", "broker_status", "note", "observed_at",
    }),
}


def _validate_timeout_val(value: Any) -> bool:
    """Validate a timeout value: non-bool, positive finite number, huge-int overflow safe."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        f = float(value)
        return math.isfinite(f) and f > 0
    except (OverflowError, ValueError, TypeError):
        return False


def diagnose_environment() -> dict[str, Any]:
    """Diagnose Python runtime and required package dependencies."""
    deps: dict[str, Any] = {}
    ib_status = "missing"
    try:
        _ = importlib.metadata.version("ib_async")
        deps["ib_async"] = {"status": "present"}
        ib_status = "present"
    except importlib.metadata.PackageNotFoundError:
        deps["ib_async"] = {"status": "missing"}
    except Exception:
        deps["ib_async"] = {"status": "error"}

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return {
        "status": "ok" if ib_status == "present" else "fail",
        "python_version": py_ver,
        "dependencies": deps,
    }


def diagnose_config(config_path: str | os.PathLike) -> dict[str, Any]:
    """Diagnose a ThetaMill configuration file strictly read-only."""
    path = Path(config_path)
    if not path.is_file():
        return {
            "status": "missing",
            "issues": ["file_not_found"],
        }

    try:
        raw = path.read_text(encoding="utf-8")
        doc = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            "status": "malformed",
            "issues": ["json_decode_error"],
        }

    if not isinstance(doc, Mapping):
        return {
            "status": "malformed",
            "issues": ["root_not_a_mapping"],
        }

    issues: list[str] = []

    if "account" in doc:
        acc = doc["account"]
        if isinstance(acc, bool) or not isinstance(acc, str) or not acc.strip():
            issues.append("invalid_account")

    for timeout_key in ("connect_timeout", "request_timeout", "command_timeout"):
        if timeout_key in doc:
            if not _validate_timeout_val(doc[timeout_key]):
                issues.append(f"invalid_{timeout_key}")

    try:
        limits = validate_risk_config(doc)
        _ = limits.enabled
        _ = limits.day_timezone
        _ = limits.max_daily_submissions
        _ = limits.max_snapshot_age_seconds
    except RiskConfigError:
        issues.append("invalid_risk_config")
    except Exception:
        issues.append("invalid_risk_config")

    status = "ok" if not issues else "invalid"
    return {
        "status": status,
        "issues": issues,
    }


def diagnose_ledger(ledger_path: str | os.PathLike) -> dict[str, Any]:
    """Diagnose SQLite submission ledger using a single immutable read-only connection."""
    path = Path(ledger_path)
    if not path.is_file():
        return {
            "status": "missing",
            "issues": ["file_not_found"],
        }

    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            return {
                "status": "sidecar_present_refused",
                "issues": ["sidecar_present_refused"],
            }

    resolved = path.resolve()
    uri = f"file:{quote(str(resolved))}?mode=ro&immutable=1"
    conn: sqlite3.Connection | None = None
    try:
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
        except (sqlite3.Error, OSError):
            return {
                "status": "unreadable",
                "issues": ["connection_failed"],
            }

        try:
            SubmissionLedger._assert_schema(None, conn, initialize=False, write_probe=False)
        except (SchemaError, sqlite3.DatabaseError, TypeError):
            return {
                "status": "corrupt",
                "issues": ["schema_error"],
            }

        for table in sorted(_REQUIRED_TABLES):
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            req_cols = _REQUIRED_TABLE_COLUMNS.get(table, frozenset())
            if not req_cols.issubset(cols):
                return {
                    "status": "foreign_schema",
                    "issues": ["missing_columns"],
                }

        try:
            row = conn.execute("SELECT value FROM meta WHERE key='day_timezone'").fetchone()
            if row is None or not row["value"]:
                return {
                    "status": "invalid_metadata",
                    "issues": ["missing_day_timezone"],
                }
            tz_str = str(row["value"])
            ZoneInfo(tz_str)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            return {
                "status": "invalid_metadata",
                "issues": ["invalid_day_timezone"],
            }
        except (sqlite3.Error, TypeError):
            return {
                "status": "corrupt",
                "issues": ["schema_error"],
            }

        return {
            "status": "ok",
            "issues": [],
        }
    finally:
        if conn is not None:
            conn.close()


def diagnose_delivery_evidence(
    evidence_path: str | os.PathLike,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Diagnose delivery evidence file strictly structurally using the neutral delivery_evidence API."""
    path = Path(evidence_path)
    if not path.is_file():
        return {
            "status": "missing",
            "validation": "not_checked",
            "record_count": 0,
            "issues": ["file_not_found"],
        }

    try:
        raw = path.read_text(encoding="utf-8")
        doc = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            "status": "malformed",
            "validation": "not_checked",
            "record_count": 0,
            "issues": ["json_decode_error"],
        }

    if not isinstance(doc, dict):
        return {
            "status": "malformed",
            "validation": "not_checked",
            "record_count": 0,
            "issues": ["delivery_evidence_error"],
        }

    record_count = len(doc.get("records", [])) if isinstance(doc.get("records"), list) else 0

    try:
        load_delivery_evidence(path, instruments=(), now=now)
    except DeliverableEvidenceError:
        return {
            "status": "malformed",
            "validation": "not_checked",
            "record_count": record_count,
            "issues": ["delivery_evidence_error"],
        }
    except Exception:
        return {
            "status": "malformed",
            "validation": "not_checked",
            "record_count": 0,
            "issues": ["delivery_evidence_error"],
        }

    return {
        "status": "structural_only",
        "validation": "not_checked",
        "record_count": record_count,
        "issues": [],
    }


def diagnose(
    *,
    config_path: str | os.PathLike | None = None,
    ledger_path: str | os.PathLike | None = None,
    delivery_evidence_path: str | os.PathLike | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run all offline health checks with explicit paths only."""
    if now is not None:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be an aware datetime")
        moment = now
    else:
        moment = datetime.now(timezone.utc)

    env_diag = diagnose_environment()

    if config_path is not None:
        cfg_diag = diagnose_config(config_path)
    else:
        cfg_diag = {"status": "not_provided", "issues": []}

    if ledger_path is not None:
        ledger_diag = diagnose_ledger(ledger_path)
    else:
        ledger_diag = {"status": "not_provided", "issues": []}

    if delivery_evidence_path is not None:
        ev_diag = diagnose_delivery_evidence(delivery_evidence_path, now=moment)
    else:
        ev_diag = {
            "status": "not_provided",
            "validation": "not_checked",
            "record_count": 0,
            "issues": [],
        }

    overall_status = "pass"
    if env_diag.get("status") != "ok":
        overall_status = "fail"
    if config_path is not None and cfg_diag.get("status") != "ok":
        overall_status = "fail"
    if ledger_path is not None and ledger_diag.get("status") != "ok":
        overall_status = "fail"
    if delivery_evidence_path is not None and (
        ev_diag.get("status") != "structural_only" or ev_diag.get("issues")
    ):
        overall_status = "fail"

    return {
        "doctor": DOCTOR_SCHEMA,
        "status": overall_status,
        "caveat": DOCTOR_CAVEAT,
        "environment": env_diag,
        "config": cfg_diag,
        "ledger": ledger_diag,
        "delivery_evidence": ev_diag,
    }


def format_text(report: Mapping[str, Any]) -> str:
    """Format doctor diagnostics as concise text without leaking secrets or paths."""
    status = report.get("status", "unknown")
    if status == "pass":
        status_line = "PASS (no detected errors)"
    else:
        status_line = status.upper()

    lines = [
        "ThetaMill Doctor (Stage-D1 Offline Health Report)",
        "=================================================",
        f"Status: {status_line}",
        f"Caveat: {report.get('caveat', DOCTOR_CAVEAT)}",
        "",
        "Components:",
    ]
    env = report.get("environment", {})
    deps = env.get("dependencies", {})
    ib_dep = deps.get("ib_async", {}).get("status", "unknown")
    lines.append(f"  Environment: python={env.get('python_version', 'unknown')}, ib_async={ib_dep}")

    cfg = report.get("config", {})
    cfg_status = cfg.get("status", "not_provided")
    lines.append(f"  Config: {cfg_status}")
    for issue in cfg.get("issues", []):
        lines.append(f"    - issue: {issue}")

    ledger = report.get("ledger", {})
    ledger_status = ledger.get("status", "not_provided")
    if ledger_status == "ok":
        lines.append("  Ledger: ok (offline stopped-copy schema verified)")
    else:
        lines.append(f"  Ledger: {ledger_status}")
    for issue in ledger.get("issues", []):
        lines.append(f"    - issue: {issue}")

    ev = report.get("delivery_evidence", {})
    ev_status = ev.get("status", "not_provided")
    lines.append(
        f"  Delivery Evidence: {ev_status} "
        f"(validation={ev.get('validation', 'not_checked')}, "
        f"records={ev.get('record_count', 0)})"
    )
    for issue in ev.get("issues", []):
        lines.append(f"    - issue: {issue}")

    return "\n".join(lines)


def run_doctor_cli(args: Any) -> int:
    """CLI dispatcher for doctor command."""
    fmt = getattr(args, "format", "text")
    config_path = getattr(args, "config", None)
    ledger_path = getattr(args, "ledger", None)
    delivery_evidence_path = getattr(args, "delivery_evidence", None)

    report = diagnose(
        config_path=config_path,
        ledger_path=ledger_path,
        delivery_evidence_path=delivery_evidence_path,
    )
    if fmt == "json":
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_text(report))
    return 0 if report.get("status") == "pass" else 1
