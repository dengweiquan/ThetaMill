#!/usr/bin/env python3
"""Offline verification pipeline for ThetaMill.

Deterministic offline verification steps:
  1. Purges stale artifacts within the unique child output directory.
  2. Runs offline unit tests under coverage via sys.executable -I -m coverage run.
  3. Verifies that a non-empty .coverage data file is generated.
  4. Generates a coverage.json report via sys.executable -I -m coverage json --fail-under=0.
  5. Verifies that a non-empty coverage.json report is generated.
  6. Evaluates money-path coverage floors via scripts/coverage_gate.py.
  7. Runs static linting checks via sys.executable -I -m ruff check --select E9,F.
"""
from __future__ import annotations

import argparse
import shutil
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence


def _run_step(cmd: Sequence[str], cwd: Path) -> int:
    """Execute a subprocess command, capturing OSError into a clean return code 2."""
    try:
        proc = subprocess.run(cmd, cwd=cwd)
        return proc.returncode
    except OSError as exc:
        print(f"ERROR: Execution of '{cmd[0]}' failed: {exc}", file=sys.stderr)
        return 2


def run_pipeline(
    repo_root: Path,
    tests_dir: Path,
    work_dir: Path,
    verbose: bool = False,
) -> int:
    """Execute the end-to-end offline verification pipeline."""
    cov_data = work_dir / ".coverage"
    cov_json = work_dir / "coverage.json"
    rcfile = repo_root / "pyproject.toml"
    offline_tests_script = repo_root / "scripts" / "offline_tests.py"
    coverage_gate_script = repo_root / "scripts" / "coverage_gate.py"
    thetamill_pkg = repo_root / "thetamill"

    def _clean_path(p: Path) -> None:
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()

    _clean_path(cov_data)
    _clean_path(cov_json)

    cov_run_cmd = [
        sys.executable, "-I", "-m", "coverage", "run",
        f"--rcfile={rcfile}",
        f"--data-file={cov_data}",
        f"--source={thetamill_pkg}",
        str(offline_tests_script),
        f"--tests-dir={tests_dir}",
        f"--source-root={repo_root}",
    ]
    if verbose:
        cov_run_cmd.append("-v")

    rc = _run_step(cov_run_cmd, repo_root)
    if rc != 0:
        return rc

    if not cov_data.is_file() or cov_data.stat().st_size == 0:
        print("ERROR: Fresh .coverage data file is missing or empty", file=sys.stderr)
        return 2

    _clean_path(cov_json)
    cov_json_cmd = [
        sys.executable, "-I", "-m", "coverage", "json",
        f"--rcfile={rcfile}",
        f"--data-file={cov_data}",
        "--fail-under=0",
        "-o", str(cov_json),
    ]
    rc = _run_step(cov_json_cmd, repo_root)
    if rc != 0:
        return rc

    if not cov_json.is_file() or cov_json.stat().st_size == 0:
        print("ERROR: Fresh coverage.json report is missing or empty", file=sys.stderr)
        return 2

    gate_cmd = [sys.executable, "-I", str(coverage_gate_script), str(cov_json)]
    rc = _run_step(gate_cmd, repo_root)
    if rc != 0:
        return rc

    ruff_cmd = [
        sys.executable, "-I", "-m", "ruff", "check",
        "--select", "E9,F",
        "thetamill", "tests", "scripts",
    ]
    rc = _run_step(ruff_cmd, repo_root)
    if rc != 0:
        return rc

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for verify_offline."""
    parser = argparse.ArgumentParser(description="Run offline verification pipeline.")
    parser.add_argument("--source-root", type=str, default=None, help="Path to repository source root.")
    parser.add_argument("--tests-dir", type=str, default=None, help="Path to tests directory.")
    parser.add_argument("--output-dir", type=str, default=None, help="Directory to place coverage artifacts.")
    parser.add_argument("--retain-artifacts", action="store_true", help="Retain coverage artifacts after execution.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose test execution.")
    args = parser.parse_args(argv)

    repo_root = Path(args.source_root).resolve() if args.source_root else Path(__file__).resolve().parent.parent
    tests_dir = Path(args.tests_dir).resolve() if args.tests_dir else (repo_root / "tests").resolve()
    parent_dir = str(Path(args.output_dir).resolve()) if args.output_dir else None
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    if args.retain_artifacts:
        work_dir_str = tempfile.mkdtemp(prefix="thetamill_verify_", dir=parent_dir)
        work_dir = Path(work_dir_str)
        print(f"Retained verification artifacts in: {work_dir}")
        return run_pipeline(repo_root, tests_dir, work_dir, verbose=args.verbose)
    else:
        with tempfile.TemporaryDirectory(prefix="thetamill_verify_", dir=parent_dir) as tmp_str:
            work_dir = Path(tmp_str)
            return run_pipeline(repo_root, tests_dir, work_dir, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
