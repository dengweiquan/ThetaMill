"""The coverage gate is a safety mechanism, so its own failure modes are pinned.

A gate that cannot fail is worse than no gate: it reports "PASSED" forever and
every later degradation looks green.  These tests drive ``scripts/coverage_gate.py``
through its real ``main()`` and check the exit code of each way it can go wrong:
a violated module floor (1), a missing / malformed / wrong-shaped report (2),
and a module that silently stopped being measured (1).
"""
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "coverage_gate.py"


def _load_gate():
    """Import the script by path: it is not a package module."""
    spec = importlib.util.spec_from_file_location("coverage_gate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_counters(cov_lines=100, num_lines=100, cov_branches=100, num_branches=100, percent_covered=None):
    pct = percent_covered if percent_covered is not None else (
        round((cov_lines + cov_branches) / (num_lines + num_branches) * 100.0, 2)
        if (num_lines + num_branches) > 0 else 100.0
    )
    return {
        "covered_lines": cov_lines, "num_statements": num_lines, "missing_lines": num_lines - cov_lines,
        "covered_branches": cov_branches, "num_branches": num_branches, "missing_branches": num_branches - cov_branches,
        "percent_covered": pct,
    }

class CoverageGateTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.gate = _load_gate()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _report(self, *, total=95.0, stmt_total=None, branch_total=90.0,
                overrides=None, drops=(), branch_meta=True):
        """A report where every guarded module is comfortably above its floors."""
        if stmt_total is None:
            stmt_total = total
        totals = _make_counters(
            cov_lines=round(1000 * stmt_total / 100), num_lines=1000,
            cov_branches=round(1000 * branch_total / 100), num_branches=1000,
        )
        files = {name: {"summary": _make_counters(100, 100, 100, 100)}
                 for name in self.gate.FLOORS}
        for name in drops:
            files.pop(name, None)
        files.update(overrides or {})
        rep = {"totals": totals, "files": files}
        if branch_meta is not None:
            rep["meta"] = {"branch_coverage": branch_meta}
        return rep

    def _run(self, report=None, *, raw=None, name="coverage.json"):
        path = self.root / name
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
        elif report is not None:
            path.write_text(json.dumps(report), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.gate.main(["coverage_gate.py", str(path)])
        return rc, out.getvalue() + err.getvalue()

    def test_a_healthy_report_passes(self):
        rc, out = self._run(self._report())
        self.assertEqual(rc, 0, out)
        self.assertIn("COVERAGE GATE PASSED", out)

    def test_a_module_below_its_statement_floor_fails(self):
        name = "thetamill/resolve.py"
        floor = self.gate.FLOORS[name]
        rc, out = self._run(self._report(overrides={
            name: {"summary": _make_counters(cov_lines=floor - 1, num_lines=100, cov_branches=100, num_branches=100)}}))
        self.assertEqual(rc, 1, out)
        self.assertIn(name, out)
        self.assertIn("COVERAGE GATE FAILED", out)

    def test_a_module_below_its_branch_floor_fails(self):
        name = "thetamill/resolve.py"
        b_floor = self.gate.BRANCH_FLOORS[name]
        rc, out = self._run(self._report(overrides={
            name: {"summary": _make_counters(cov_lines=100, num_lines=100, cov_branches=b_floor - 1, num_branches=100)}}))
        self.assertEqual(rc, 1, out)
        self.assertIn(name, out)
        self.assertIn("COVERAGE GATE FAILED", out)

    def test_a_module_exactly_on_its_floors_passes(self):
        """Floors are inclusive: no off-by-one failures on exact measurements."""
        name = "thetamill/state.py"
        rc, out = self._run(self._report(overrides={
            name: {"summary": _make_counters(cov_lines=self.gate.FLOORS[name], num_lines=100,
                                            cov_branches=self.gate.BRANCH_FLOORS[name], num_branches=100)}}))
        self.assertEqual(rc, 0, out)

    def test_a_global_statement_total_below_the_floor_fails(self):
        rc, out = self._run(self._report(stmt_total=self.gate.GLOBAL_FLOOR - 0.5))
        self.assertEqual(rc, 1, out)
        self.assertIn("TOTAL", out)

    def test_a_global_branch_total_below_the_floor_fails(self):
        rc, out = self._run(self._report(branch_total=self.gate.GLOBAL_BRANCH_FLOOR - 0.5))
        self.assertEqual(rc, 1, out)
        self.assertIn("TOTAL", out)

    def test_mixed_overall_percent_covered_cannot_bypass_gate(self):
        name = "thetamill/resolve.py"
        bad_branch = _make_counters(cov_lines=100, num_lines=100, cov_branches=98, num_branches=100, percent_covered=99.0)
        rc, out = self._run(self._report(overrides={name: {"summary": bad_branch}}))
        self.assertEqual(rc, 1, out)
        self.assertIn("branch 98.0% < 99%", out)

    def test_a_module_that_is_no_longer_measured_fails(self):
        """A rename or an import change must not silently drop a floor."""
        name = "thetamill/risk.py"
        rc, out = self._run(self._report(drops=(name,)))
        self.assertEqual(rc, 1, out)
        self.assertIn("not measured", out)

    def test_a_missing_report_file_is_not_a_pass(self):
        rc, out = self._run(name="absent.json")
        self.assertEqual(rc, 2, out)
        self.assertIn("no coverage report", out)

    def test_malformed_json_is_explained_not_a_traceback(self):
        rc, out = self._run(raw="{ truncated")
        self.assertEqual(rc, 2, out)
        self.assertIn("cannot read", out)

    def test_json_of_another_shape_is_refused(self):
        rc, out = self._run(raw='{"files": {}}')
        self.assertEqual(rc, 2, out)
        self.assertIn("not a coverage.json report", out)

    def test_report_without_branch_metadata_is_refused(self):
        rc, out = self._run(self._report(branch_meta=None))
        self.assertEqual(rc, 2, out)
        self.assertIn("not branch-enabled", out)

    def test_report_with_branch_metadata_false_is_refused(self):
        rc, out = self._run(self._report(branch_meta=False))
        self.assertEqual(rc, 2, out)
        self.assertIn("not branch-enabled", out)

    def test_counter_validation_failures(self):
        """Counters must be strict, non-bool, integral, non-negative, and consistent."""
        name = "thetamill/audit.py"
        cases = [
            ("missing", {}),
            ("bool", {"num_statements": True, "covered_lines": 0, "missing_lines": 0,
                      "num_branches": 0, "covered_branches": 0, "missing_branches": 0}),
            ("string", {"num_statements": "100", "covered_lines": 100, "missing_lines": 0,
                        "num_branches": 0, "covered_branches": 0, "missing_branches": 0}),
            ("nan", {"num_statements": float("nan"), "covered_lines": 0, "missing_lines": 0,
                     "num_branches": 0, "covered_branches": 0, "missing_branches": 0}),
            ("negative", {"num_statements": 100, "covered_lines": -1, "missing_lines": 101,
                          "num_branches": 0, "covered_branches": 0, "missing_branches": 0}),
            ("above denom", {"num_statements": 100, "covered_lines": 105, "missing_lines": 0,
                             "num_branches": 0, "covered_branches": 0, "missing_branches": 0}),
            ("inconsistent", {"num_statements": 100, "covered_lines": 80, "missing_lines": 10,
                              "num_branches": 0, "covered_branches": 0, "missing_branches": 0}),
        ]
        for label, summary in cases:
            with self.subTest(case=label):
                rc, out = self._run(self._report(overrides={name: {"summary": summary}}))
                self.assertEqual(rc, 2, out)
                self.assertIn("invalid counters", out)

    def test_zero_branches_valid_vs_invalid(self):
        name = "thetamill/resolve.py"
        valid_zero = {"num_statements": 100, "covered_lines": 100, "missing_lines": 0,
                      "num_branches": 0, "covered_branches": 0, "missing_branches": 0}
        rc, out = self._run(self._report(overrides={name: {"summary": valid_zero}}))
        self.assertEqual(rc, 0, out)
        self.assertIn("COVERAGE GATE PASSED", out)

        invalid_zero = {"num_statements": 100, "covered_lines": 100, "missing_lines": 0,
                        "num_branches": 0, "covered_branches": 1, "missing_branches": 0}
        rc, out = self._run(self._report(overrides={name: {"summary": invalid_zero}}))
        self.assertEqual(rc, 2, out)
        self.assertIn("invalid counters", out)

    def test_usage_without_a_report_argument(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.gate.main(["coverage_gate.py"])
        self.assertEqual(rc, 2)
        self.assertIn("usage", err.getvalue())

    def test_the_guarded_modules_still_exist(self):
        """A floor on a deleted file would never be enforced again."""
        for name in self.gate.FLOORS:
            with self.subTest(module=name):
                self.assertTrue((ROOT / name).is_file(), name)
        self.assertEqual(set(self.gate.FLOORS.keys()), set(self.gate.BRANCH_FLOORS.keys()))


if __name__ == "__main__":
    unittest.main()
