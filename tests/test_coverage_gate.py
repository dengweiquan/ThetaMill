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


class CoverageGateTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.gate = _load_gate()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _report(self, *, total=95.0, overrides=None, drops=()):
        """A report where every guarded module is comfortably above its floor."""
        files = {name: {"summary": {"percent_covered": 99.0}}
                 for name in self.gate.FLOORS}
        for name in drops:
            files.pop(name)
        files.update(overrides or {})
        return {"totals": {"percent_covered": total}, "files": files}

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

    def test_a_module_below_its_floor_fails(self):
        name = "thetamill/resolve.py"
        floor = self.gate.FLOORS[name]
        rc, out = self._run(self._report(overrides={
            name: {"summary": {"percent_covered": floor - 1}}}))
        self.assertEqual(rc, 1, out)
        self.assertIn(name, out)
        self.assertIn("COVERAGE GATE FAILED", out)

    def test_a_module_exactly_on_its_floor_passes(self):
        """Floors are inclusive: no off-by-one failures on exact measurements."""
        name = "thetamill/state.py"
        rc, out = self._run(self._report(overrides={
            name: {"summary": {"percent_covered": self.gate.FLOORS[name]}}}))
        self.assertEqual(rc, 0, out)

    def test_a_global_total_below_the_floor_fails(self):
        rc, out = self._run(self._report(total=self.gate.GLOBAL_FLOOR - 0.5))
        self.assertEqual(rc, 1, out)
        self.assertIn("TOTAL", out)

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

    def test_a_module_entry_without_a_percentage_fails(self):
        name = "thetamill/audit.py"
        rc, out = self._run(self._report(overrides={name: {"summary": {}}}))
        self.assertEqual(rc, 1, out)
        self.assertIn("no usable percentage", out)

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


if __name__ == "__main__":
    unittest.main()
