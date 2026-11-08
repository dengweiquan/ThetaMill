#!/usr/bin/env python3
"""Coverage gate: a global floor plus per-module floors for money-path code.

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
    coverage json -o coverage.json
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
    try:
        total = report["totals"]["percent_covered"]
        files = report["files"]
    except (KeyError, TypeError) as exc:
        # Wrong JSON (someone's report of another shape) is a broken gate too.
        print(f"COVERAGE GATE FAILED: {path} is not a coverage.json report "
              f"({exc!r})", file=sys.stderr)
        return 2
    failures: list[str] = []

    if total < GLOBAL_FLOOR:
        failures.append(f"TOTAL {total:.1f}% < {GLOBAL_FLOOR}%")
    print(f"{'module':44s} {'cover':>7s}  floor  status")
    print(f"{'TOTAL':44s} {total:6.1f}%  {GLOBAL_FLOOR:4d}%  "
          f"{'FAIL' if total < GLOBAL_FLOOR else 'ok'}")
    for name, floor in FLOORS.items():
        entry = files.get(name)
        if entry is None or "summary" not in entry:
            failures.append(f"{name}: not measured (module renamed or not imported)")
            print(f"{name:44s} {'--':>7s}  {floor:4d}%  FAIL (not measured)")
            continue
        cover = entry["summary"].get("percent_covered")
        if not isinstance(cover, (int, float)):
            failures.append(f"{name}: report has no usable percentage")
            print(f"{name:44s} {'--':>7s}  {floor:4d}%  FAIL (no percentage)")
            continue
        ok = cover >= floor
        if not ok:
            failures.append(f"{name} {cover:.1f}% < {floor}%")
        print(f"{name:44s} {cover:6.1f}%  {floor:4d}%  {'ok' if ok else 'FAIL'}")

    if failures:
        print("\nCOVERAGE GATE FAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("\nCOVERAGE GATE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
