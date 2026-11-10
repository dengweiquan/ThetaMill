#!/usr/bin/env python3
"""Coverage gate: statement and branch floors for money-path code.

A single global percentage hides the thing this repository actually cares
about.  83% overall can mean "the risk engine is well tested and the argparse
help text is not", or the reverse.  The per-module table below names the files
that can lose real money or leave a real order unattended, and gives each one a
floor of its own so a regression there cannot hide behind an unrelated gain.

Every floor is a REGRESSION guard at today's measured value (rounded down with
a little headroom), not an aspiration.  Raising one is always welcome; lowering
one is a deliberate decision that has to be argued in the commit message.

Usage:
    coverage run -m unittest discover -s tests
    coverage json --fail-under=0 -o coverage.json
    python scripts/coverage_gate.py coverage.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# (module, floor).  Keep this ordered by blast radius, not alphabetically.
FLOORS = {
    # Derives and validates every leg's reserved cash / shares / close capacity.
    "thetamill/execution.py": 80,
    # The adapter: preflight, freshness gate, quota, resource plan, submit.
    "thetamill/submission_cli.py": 80,
    # The only sanctioned way to clear a locked account.
    "thetamill/resolve.py": 90,
    # Authoritative ledger: quota, idempotency, recovery.
    "thetamill/state.py": 87,
    # Collateral / coverage / exposure math.
    "thetamill/risk.py": 88,
    # Strict-typed audit trail (the only writer of orders.jsonl).
    "thetamill/audit.py": 88,
    # Snapshot collection: bad data here becomes bad risk decisions.
    "thetamill/broker_state.py": 90,
    # Shared evidence / budget primitives used by every entry point.
    "thetamill/planning.py": 87,
    # Command wiring: account/environment selection, the operator exits.
    "thetamill/cli.py": 70,
}

GLOBAL_FLOOR = 83

GLOBAL_BRANCH_FLOOR = 77

BRANCH_FLOORS = {
    "thetamill/execution.py": 70,
    "thetamill/submission_cli.py": 73,
    "thetamill/resolve.py": 99,
    "thetamill/state.py": 76,
    "thetamill/risk.py": 83,
    "thetamill/audit.py": 79,
    "thetamill/broker_state.py": 81,
    "thetamill/planning.py": 84,
    "thetamill/cli.py": 66,
}


def _extract_counters(data: object, context: str) -> tuple[float, float] | str:
    """Validate non-bool integer counters and return (stmt_pct, branch_pct)."""
    if not isinstance(data, dict):
        return f"{context} summary is not a dict"
    keys = (
        "num_statements", "covered_lines", "missing_lines",
        "num_branches", "covered_branches", "missing_branches",
    )
    for k in keys:
        if k not in data:
            return f"{context} missing counter '{k}'"
        val = data[k]
        if isinstance(val, bool) or not isinstance(val, int):
            return f"{context} counter '{k}' is not an integer ({val!r})"
        if val < 0:
            return f"{context} counter '{k}' is negative ({val})"

    n_stmt, c_stmt, m_stmt = data["num_statements"], data["covered_lines"], data["missing_lines"]
    if c_stmt > n_stmt:
        return f"{context} covered_lines ({c_stmt}) > num_statements ({n_stmt})"
    if c_stmt + m_stmt != n_stmt:
        return f"{context} statement counters inconsistent: covered+missing!=num"

    n_br, c_br, m_br = data["num_branches"], data["covered_branches"], data["missing_branches"]
    if c_br > n_br:
        return f"{context} covered_branches ({c_br}) > num_branches ({n_br})"
    if c_br + m_br != n_br:
        return f"{context} branch counters inconsistent: covered+missing!=num"

    stmt_pct = 100.0 if n_stmt == 0 else (c_stmt / n_stmt) * 100.0
    branch_pct = 100.0 if n_br == 0 else (c_br / n_br) * 100.0
    return stmt_pct, branch_pct


def _load(path: Path) -> dict | None:
    """Parse the JSON report, or ``None`` after explaining why it is unusable.

    A truncated or malformed report is a broken gate, not a passing one: an
    uncaught ``JSONDecodeError`` would still exit non-zero, but the operator
    would have to read a traceback to find out what happened.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"COVERAGE GATE FAILED: cannot read {path}: {exc}", file=sys.stderr)
        return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: coverage_gate.py <coverage.json>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        # An unreadable report must never read as "no coverage problems".
        print(f"COVERAGE GATE FAILED: no coverage report at {path}", file=sys.stderr)
        return 2
    report = _load(path)
    if report is None:
        return 2

    # Structural shape validation: report, totals, files must be dicts.
    if (
        not isinstance(report, dict)
        or not isinstance(report.get("totals"), dict)
        or not isinstance(report.get("files"), dict)
    ):
        print(f"COVERAGE GATE FAILED: {path} is not a coverage.json report "
              "(missing or invalid 'totals'/'files')", file=sys.stderr)
        return 2

    # Enforce branch coverage metadata.
    meta = report.get("meta")
    if not isinstance(meta, dict) or meta.get("branch_coverage") is not True:
        print(f"COVERAGE GATE FAILED: {path} is not branch-enabled "
              "(meta.branch_coverage must be True)", file=sys.stderr)
        return 2

    tot_res = _extract_counters(report["totals"], "totals")
    if isinstance(tot_res, str):
        print(f"COVERAGE GATE FAILED: {path} has invalid counters: {tot_res}", file=sys.stderr)
        return 2
    total_stmt, total_branch = tot_res

    files = report["files"]
    module_data: dict[str, tuple[float, float]] = {}
    for name in FLOORS:
        entry = files.get(name)
        if entry is None or not isinstance(entry, dict) or "summary" not in entry:
            continue
        res = _extract_counters(entry["summary"], name)
        if isinstance(res, str):
            print(f"COVERAGE GATE FAILED: {path} has invalid counters: {res}", file=sys.stderr)
            return 2
        module_data[name] = res

    failures: list[str] = []
    if total_stmt < GLOBAL_FLOOR:
        failures.append(f"TOTAL stmt {total_stmt:.1f}% < {GLOBAL_FLOOR}%")
    if total_branch < GLOBAL_BRANCH_FLOOR:
        failures.append(f"TOTAL branch {total_branch:.1f}% < {GLOBAL_BRANCH_FLOOR}%")

    tot_ok = total_stmt >= GLOBAL_FLOOR and total_branch >= GLOBAL_BRANCH_FLOOR
    print(f"{'module':32s} {'stmt':>7s}  floor {'branch':>7s}  floor  status")
    print(f"{'TOTAL':32s} {total_stmt:6.1f}%  {GLOBAL_FLOOR:4d}% {total_branch:6.1f}%  "
          f"{GLOBAL_BRANCH_FLOOR:4d}%  {'ok' if tot_ok else 'FAIL'}")

    for name, s_floor in FLOORS.items():
        b_floor = BRANCH_FLOORS[name]
        if name not in module_data:
            failures.append(f"{name}: not measured (module renamed or not imported)")
            print(f"{name:32s} {'--':>7s}  {s_floor:4d}% {'--':>7s}  {b_floor:4d}%  FAIL (not measured)")
            continue
        s_pct, b_pct = module_data[name]
        s_ok, b_ok = s_pct >= s_floor, b_pct >= b_floor
        if not s_ok:
            failures.append(f"{name} stmt {s_pct:.1f}% < {s_floor}%")
        if not b_ok:
            failures.append(f"{name} branch {b_pct:.1f}% < {b_floor}%")
        ok = s_ok and b_ok
        print(f"{name:32s} {s_pct:6.1f}%  {s_floor:4d}% {b_pct:6.1f}%  {b_floor:4d}%  {'ok' if ok else 'FAIL'}")

    if failures:
        print("\nCOVERAGE GATE FAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("\nCOVERAGE GATE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
