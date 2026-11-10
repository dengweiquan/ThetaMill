"""Tests for the verify_offline verification pipeline."""
from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
VERIFY_SCRIPT = ROOT / "scripts" / "verify_offline.py"
GATE_SCRIPT = ROOT / "scripts" / "coverage_gate.py"


def _load_script(path: Path, mod_name: str):
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class VerifyOfflinePipelineTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.verify = _load_script(VERIFY_SCRIPT, "verify_offline")
        cls.gate = _load_script(GATE_SCRIPT, "coverage_gate")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work_dir = Path(self._tmp.name)
        self.repo_root = ROOT
        self.tests_dir = ROOT / "tests"

    def test_missing_coverage_tool_returns_code_2(self):
        """When coverage cannot be executed, OSError is caught and returns 2."""
        def fake_run(cmd, *args, **kwargs):
            if "coverage" in cmd:
                raise FileNotFoundError("No coverage executable found")
            return subprocess.CompletedProcess(cmd, 0)

        err = io.StringIO()
        with patch("subprocess.run", side_effect=fake_run), redirect_stderr(err):
            rc = self.verify.run_pipeline(
                self.repo_root, self.tests_dir, self.work_dir, verbose=False
            )
        self.assertEqual(rc, 2)
        self.assertIn("failed", err.getvalue().lower())

    def test_test_step_nonzero_exit_propagates(self):
        """A failure in offline_tests under coverage immediately propagates nonzero rc."""
        def fake_run(cmd, *args, **kwargs):
            if "coverage" in cmd and "run" in cmd:
                return subprocess.CompletedProcess(cmd, 1)
            return subprocess.CompletedProcess(cmd, 0)

        with patch("subprocess.run", side_effect=fake_run):
            rc = self.verify.run_pipeline(
                self.repo_root, self.tests_dir, self.work_dir, verbose=False
            )
        self.assertEqual(rc, 1)

    def test_missing_fresh_data_even_when_stale_data_preexisting(self):
        """Stale .coverage must be purged; failure to produce fresh data returns 2."""
        stale_data = self.work_dir / ".coverage"
        stale_data.write_text("stale coverage bytes", encoding="utf-8")
        self.assertTrue(stale_data.exists())

        def fake_run(cmd, *args, **kwargs):
            # Returns 0 without generating fresh .coverage
            return subprocess.CompletedProcess(cmd, 0)

        err = io.StringIO()
        with patch("subprocess.run", side_effect=fake_run), redirect_stderr(err):
            rc = self.verify.run_pipeline(
                self.repo_root, self.tests_dir, self.work_dir, verbose=False
            )
        self.assertEqual(rc, 2)
        self.assertIn("missing or empty", err.getvalue())
        self.assertFalse(stale_data.exists())

    def test_missing_fresh_report_even_when_stale_report_preexisting(self):
        """Stale coverage.json must be purged; failure to produce fresh report returns 2."""
        stale_json = self.work_dir / "coverage.json"
        stale_json.write_text("{}", encoding="utf-8")

        cov_data = self.work_dir / ".coverage"

        def fake_run(cmd, *args, **kwargs):
            if "coverage" in cmd and "run" in cmd:
                cov_data.write_text("fresh_data", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0)
            if "coverage" in cmd and "json" in cmd:
                # Does not write fresh coverage.json
                return subprocess.CompletedProcess(cmd, 0)
            return subprocess.CompletedProcess(cmd, 0)

        err = io.StringIO()
        with patch("subprocess.run", side_effect=fake_run), redirect_stderr(err):
            rc = self.verify.run_pipeline(
                self.repo_root, self.tests_dir, self.work_dir, verbose=False
            )
        self.assertEqual(rc, 2)
        self.assertIn("coverage.json report is missing or empty", err.getvalue())
        self.assertFalse(stale_json.exists())

    def test_malformed_gate_failure_propagates_exit_code(self):
        """Coverage gate failure (e.g. malformed report or violated floors) propagates."""
        cov_data = self.work_dir / ".coverage"
        cov_json = self.work_dir / "coverage.json"

        def fake_run(cmd, *args, **kwargs):
            if "coverage" in cmd and "run" in cmd:
                cov_data.write_text("fresh_data", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0)
            if "coverage" in cmd and "json" in cmd:
                cov_json.write_text("malformed json {", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0)
            if "coverage_gate.py" in str(cmd):
                return subprocess.CompletedProcess(cmd, 2)
            return subprocess.CompletedProcess(cmd, 0)

        with patch("subprocess.run", side_effect=fake_run):
            rc = self.verify.run_pipeline(
                self.repo_root, self.tests_dir, self.work_dir, verbose=False
            )
        self.assertEqual(rc, 2)

    def test_lint_failure_propagates_exit_code(self):
        """Ruff static check failure propagates nonzero exit code."""
        cov_data = self.work_dir / ".coverage"
        cov_json = self.work_dir / "coverage.json"

        def fake_run(cmd, *args, **kwargs):
            if "coverage" in cmd and "run" in cmd:
                cov_data.write_text("fresh_data", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0)
            if "coverage" in cmd and "json" in cmd:
                cov_json.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0)
            if "coverage_gate.py" in str(cmd):
                return subprocess.CompletedProcess(cmd, 0)
            if "ruff" in cmd:
                return subprocess.CompletedProcess(cmd, 1)
            return subprocess.CompletedProcess(cmd, 0)

        with patch("subprocess.run", side_effect=fake_run):
            rc = self.verify.run_pipeline(
                self.repo_root, self.tests_dir, self.work_dir, verbose=False
            )
        self.assertEqual(rc, 1)

    def test_pipeline_validates_actual_report_using_coverage_gate(self):
        """Validate that real coverage_gate rejects an incomplete/corrupt report with exit code 2."""
        cov_data = self.work_dir / ".coverage"
        cov_json = self.work_dir / "coverage.json"

        def fake_run(cmd, *args, **kwargs):
            if "coverage" in cmd and "run" in cmd:
                cov_data.write_text("fresh_data", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0)
            if "coverage" in cmd and "json" in cmd:
                # Report missing meta/totals/files
                cov_json.write_text(json.dumps({"invalid": "report"}), encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0)
            if "coverage_gate.py" in str(cmd):
                gate_rc = self.gate.main([str(cmd[-2]), str(cmd[-1])])
                return subprocess.CompletedProcess(cmd, gate_rc)
            return subprocess.CompletedProcess(cmd, 0)

        err = io.StringIO()
        with patch("subprocess.run", side_effect=fake_run), redirect_stderr(err):
            rc = self.verify.run_pipeline(
                self.repo_root, self.tests_dir, self.work_dir, verbose=False
            )
        self.assertEqual(rc, 2)
        self.assertIn("not a coverage.json report", err.getvalue())

    def test_pipeline_success_executes_all_narrow_steps(self):
        """All steps succeed cleanly when artifacts and exit codes are healthy."""
        cov_data = self.work_dir / ".coverage"
        cov_json = self.work_dir / "coverage.json"
        calls = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            if "coverage" in cmd and "run" in cmd:
                cov_data.write_text("fresh_data", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0)
            if "coverage" in cmd and "json" in cmd:
                cov_json.write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0)
            return subprocess.CompletedProcess(cmd, 0)

        with patch("subprocess.run", side_effect=fake_run):
            rc = self.verify.run_pipeline(
                self.repo_root, self.tests_dir, self.work_dir, verbose=False
            )
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 4)
        self.assertIn(f"--source={self.repo_root / 'thetamill'}", calls[0])
        self.assertIn("ruff", calls[3])

    def test_retain_artifacts_prints_clear_location(self):
        """--retain-artifacts creates and prints retained directory location."""
        def fake_run(cmd, *args, **kwargs):
            for arg in cmd:
                if str(arg).startswith("--data-file="):
                    Path(str(arg).split("=", 1)[1]).write_text("data", encoding="utf-8")
                elif str(arg) == "-o":
                    idx = cmd.index(arg)
                    Path(str(cmd[idx + 1])).write_text("json", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0)

        out = io.StringIO()
        parent_tmp = self.work_dir / "parent"
        parent_tmp.mkdir()
        with patch("subprocess.run", side_effect=fake_run), redirect_stdout(out):
            rc = self.verify.main([
                "--retain-artifacts",
                f"--output-dir={parent_tmp}",
                f"--source-root={self.repo_root}",
                f"--tests-dir={self.tests_dir}",
            ])
        self.assertEqual(rc, 0)
        output_text = out.getvalue()
        self.assertIn("Retained verification artifacts in:", output_text)
        retained_dir = output_text.split("Retained verification artifacts in:")[1].strip()
        self.assertTrue(Path(retained_dir).is_dir())


if __name__ == "__main__":
    unittest.main()
