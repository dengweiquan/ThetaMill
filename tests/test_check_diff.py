"""Regression and safety tests for the whitespace diff-checking gate."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_diff.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_diff", SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load check_diff from {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CheckDiffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gate = _load_gate()

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.repo_dir = self.root / "repo"
        self.repo_dir.mkdir()
        self.home_dir = self.root / "home"
        self.home_dir.mkdir()

        # Hermetic environment for the entire test lifetime
        env_updates = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(self.home_dir),
            "GIT_AUTHOR_NAME": "Test Committer",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test Committer",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        patcher = patch.dict(os.environ, env_updates, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Neutralize / strip ambient Git and GitHub environment variables
        strip_prefixes = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        strip_keys = {
            "GITHUB_EVENT_PATH",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_COUNT",
            "GIT_DIR",
            "GIT_WORK_TREE",
            "XDG_CONFIG_HOME",
            "GIT_CONFIG",
        }
        for k in list(os.environ):
            if k in strip_keys or any(k.startswith(p) for p in strip_prefixes):
                os.environ.pop(k, None)

        # Initialize repository with hooks and gpgsign disabled
        self._git_cmd(["init", "-b", "main"])
        self._git_cmd(["config", "core.hooksPath", "/dev/null"])
        self._git_cmd(["config", "commit.gpgsign", "false"])
        self._git_cmd(["config", "user.name", "Test Committer"])
        self._git_cmd(["config", "user.email", "test@example.com"])

    def _git_cmd(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        cmd = [
            "git",
            "-c", "core.hooksPath=/dev/null",
            "-c", "commit.gpgsign=false",
            *args,
        ]
        res = subprocess.run(
            cmd,
            cwd=self.repo_dir,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"Git command failed ({res.returncode}): {' '.join(cmd)}\n"
                f"Stdout: {res.stdout}\nStderr: {res.stderr}"
            )
        return res

    def _commit_file(self, rel_path: str, content: str, msg: str = "commit") -> str:
        file_path = self.repo_dir / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        self._git_cmd(["add", rel_path])
        self._git_cmd(["commit", "-m", msg])
        return self._git_cmd(["rev-parse", "HEAD"]).stdout.strip()

    def _write_event(self, data: object, name: str = "event.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def _run_gate(
        self,
        argv: list[str] | None = None,
        *,
        env_event_path: Path | None = None,
    ) -> tuple[int, str]:
        if env_event_path is not None:
            os.environ["GITHUB_EVENT_PATH"] = str(env_event_path)
        try:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = self.gate.main(argv, cwd=self.repo_dir)
            return rc, out.getvalue() + err.getvalue()
        finally:
            if env_event_path is not None:
                os.environ.pop("GITHUB_EVENT_PATH", None)

    def test_initial_push_clean_passes(self):
        head = self._commit_file("main.py", "print('clean')\n")
        event = self._write_event({"before": "0" * 40, "after": head})
        rc, out = self._run_gate(["check_diff.py", "--event-path", str(event)])
        self.assertEqual(rc, 0, out)
        self.assertIn("WHITESPACE GATE PASSED", out)

    def test_initial_push_whitespace_violation_fails(self):
        head = self._commit_file("main.py", "print('bad')   \n")
        event = self._write_event({"before": "0" * 40, "after": head})
        rc, out = self._run_gate(["check_diff.py", "--event-path", str(event)])
        self.assertEqual(rc, 1, out)
        self.assertIn("trailing whitespace", out)
        self.assertIn("WHITESPACE GATE FAILED", out)

    def test_initial_push_sha256_all_zeros_supported(self):
        head = self._commit_file("main.py", "print('clean')\n")
        event = self._write_event({"before": "0" * 64, "after": head})
        rc, out = self._run_gate(["check_diff.py", "--event-path", str(event)])
        self.assertEqual(rc, 0, out)
        self.assertIn("WHITESPACE GATE PASSED", out)

    def test_multi_commit_push_catches_earlier_bad_commit(self):
        c1 = self._commit_file("base.py", "print('base')\n")
        self._commit_file("bad.py", "print('bad')   \n")
        c3 = self._commit_file("clean.py", "print('clean')\n")
        event = self._write_event({"before": c1, "after": c3})
        rc, out = self._run_gate(["check_diff.py", "--event-path", str(event)])
        self.assertEqual(rc, 1, out)
        self.assertIn("bad.py", out)
        self.assertIn("trailing whitespace", out)

    def test_multi_commit_push_reverted_violation_in_final_tree_passes(self):
        c1 = self._commit_file("f.py", "print('base')\n")
        self._commit_file("f.py", "print('bad')   \n")
        c3 = self._commit_file("f.py", "print('clean')\n")
        event = self._write_event({"before": c1, "after": c3})
        rc, out = self._run_gate(["check_diff.py", "--event-path", str(event)])
        self.assertEqual(rc, 0, out)
        self.assertIn("WHITESPACE GATE PASSED", out)

    def test_pr_full_base_range_catches_violation(self):
        c1 = self._commit_file("base.py", "print('base')\n")
        self._git_cmd(["checkout", "-b", "feature"])
        self._commit_file("bad.py", "print('bad')   \n")
        c3 = self._commit_file("f2.py", "print('f2')\n")
        event = self._write_event({"pull_request": {"base": {"sha": c1}, "head": {"sha": c3}}})
        rc, out = self._run_gate(["check_diff.py", "--event-path", str(event)])
        self.assertEqual(rc, 1, out)
        self.assertIn("bad.py", out)

    def test_pr_diverged_base_ignores_base_only_changes(self):
        self._commit_file("common.py", "print('common')\n")
        self._git_cmd(["checkout", "-b", "feature"])
        feat_head = self._commit_file("feat.py", "print('clean feature')\n")
        self._git_cmd(["checkout", "main"])
        main_head = self._commit_file("main_only.py", "print('bad main')   \n")

        event = self._write_event({
            "pull_request": {
                "base": {"sha": main_head},
                "head": {"sha": feat_head},
            }
        })
        rc, out = self._run_gate(["check_diff.py", "--event-path", str(event)])
        self.assertEqual(rc, 0, out)
        self.assertIn("WHITESPACE GATE PASSED", out)

    def test_missing_base_failure(self):
        head_commit = self._commit_file("base.py", "print('base')\n")
        missing_sha = "deadbeef" * 5
        event = self._write_event({"before": missing_sha, "after": head_commit})
        rc, out = self._run_gate(["check_diff.py", "--event-path", str(event)])
        self.assertEqual(rc, 2, out)
        self.assertIn("CHECK DIFF FAILED", out)

    def test_missing_head_failure(self):
        base_commit = self._commit_file("base.py", "print('base')\n")
        missing_sha = "deadbeef" * 5
        event = self._write_event({"before": base_commit, "after": missing_sha})
        rc, out = self._run_gate(["check_diff.py", "--event-path", str(event)])
        self.assertEqual(rc, 2, out)
        self.assertIn("CHECK DIFF FAILED", out)

    def test_working_tree_unrelated_cleanliness(self):
        c1 = self._commit_file("base.py", "print('c1')\n")
        c2 = self._commit_file("feature.py", "print('c2')\n")

        untracked = self.repo_dir / "untracked.py"
        untracked.write_text("untracked = 3   \n", encoding="utf-8")

        tracked = self.repo_dir / "base.py"
        with open(tracked, "a", encoding="utf-8") as f:
            f.write("dirty_line = 4   \n")

        event = self._write_event({"before": c1, "after": c2})
        rc, out = self._run_gate(["check_diff.py", "--event-path", str(event)])
        self.assertEqual(rc, 0, out)
        self.assertIn("WHITESPACE GATE PASSED", out)

    def test_push_missing_before_fails(self):
        head = self._commit_file("a.py", "print('ok')\n")
        event = self._write_event({"after": head})
        rc, out = self._run_gate(["check_diff.py", "--event-path", str(event)])
        self.assertEqual(rc, 2, out)
        self.assertIn("missing required 'before' field", out)

    def test_push_missing_after_fails(self):
        event = self._write_event({"before": "0" * 40})
        rc, out = self._run_gate(["check_diff.py", "--event-path", str(event)])
        self.assertEqual(rc, 2, out)
        self.assertIn("missing required 'after' field", out)

    def test_push_all_zero_after_fails(self):
        event = self._write_event({"before": "0" * 40, "after": "0" * 40})
        rc, out = self._run_gate(["check_diff.py", "--event-path", str(event)])
        self.assertEqual(rc, 2, out)
        self.assertIn("cannot be an all-zero SHA", out)

    def test_push_non_hex_sha_fails(self):
        head = self._commit_file("a.py", "print('ok')\n")
        event = self._write_event({"before": "not-a-hex-sha", "after": head})
        rc, out = self._run_gate(["check_diff.py", "--event-path", str(event)])
        self.assertEqual(rc, 2, out)
        self.assertIn("CHECK DIFF FAILED", out)

    def test_pr_nested_type_validation_fails(self):
        c1 = self._commit_file("a.py", "print('ok')\n")
        invalid_events = [
            {"pull_request": "not_an_object"},
            {"pull_request": {"base": "not_an_object", "head": {"sha": c1}}},
            {"pull_request": {"base": {"sha": c1}, "head": None}},
            {"pull_request": {"base": {"sha": "--upload-pack"}, "head": {"sha": c1}}},
            {"pull_request": {"base": {"sha": "0" * 40}, "head": {"sha": c1}}},
            {"pull_request": {"base": {"sha": c1}, "head": {"sha": "0" * 40}}},
            {"pull_request": {"base": {"sha": 12345}, "head": {"sha": c1}}},
        ]
        for bad_data in invalid_events:
            with self.subTest(bad_data=bad_data):
                event = self._write_event(bad_data)
                rc, out = self._run_gate(["check_diff.py", "--event-path", str(event)])
                self.assertEqual(rc, 2, out)
                self.assertIn("CHECK DIFF FAILED", out)

    def test_cli_refs_resolution_and_security(self):
        self._commit_file("a.py", "print('c1')\n")
        self._commit_file("b.py", "print('c2')\n")

        # Explicit valid range
        rc, out = self._run_gate(["check_diff.py", "HEAD~1", "HEAD"])
        self.assertEqual(rc, 0, out)

        # Explicit single ref (diffs against parent)
        rc, out = self._run_gate(["check_diff.py", "HEAD"])
        self.assertEqual(rc, 0, out)

        # Attempted option injection via CLI ref string is blocked
        with self.assertRaises(self.gate.DiffCheckError):
            self.gate._resolve_cli_ref("--upload-pack=evil", cwd=self.repo_dir)

        rc, out = self._run_gate(["check_diff.py", "--upload-pack=evil", "HEAD"])
        self.assertEqual(rc, 2, out)

        # Non-existent ref fails
        rc, out = self._run_gate(["check_diff.py", "non_existent_branch", "HEAD"])
        self.assertEqual(rc, 2, out)

    def test_single_ref_root_commit_passes(self):
        root = self._commit_file("a.py", "print('root')\n")
        rc, out = self._run_gate(["check_diff.py", root])
        self.assertEqual(rc, 0, out)
        self.assertIn("WHITESPACE GATE PASSED", out)

    def test_single_ref_shallow_nonroot_fails(self):
        c1 = self._commit_file("a.py", "print('c1')\n")
        c2 = self._commit_file("b.py", "print('c2')\n")
        parent_obj = self.repo_dir / ".git" / "objects" / c1[:2] / c1[2:]
        self.assertTrue(parent_obj.exists(), f"Object {parent_obj} not found")
        parent_obj.unlink()

        rc, out = self._run_gate(["check_diff.py", c2])
        self.assertEqual(rc, 2, out)
        self.assertIn("CHECK DIFF FAILED", out)

    def test_subprocess_oserror_clean_exit_code_2(self):
        with patch("subprocess.run", side_effect=OSError("Simulated spawn error")):
            rc, out = self._run_gate(["check_diff.py", "HEAD"])
        self.assertEqual(rc, 2)
        self.assertIn("CHECK DIFF FAILED", out)

    def test_missing_event_file_or_empty_invocation_fails(self):
        rc, out = self._run_gate(["check_diff.py", "--event-path", str(self.root / "absent.json")])
        self.assertEqual(rc, 2, out)
        self.assertIn("event file not found", out)

        rc, out = self._run_gate(["check_diff.py"])
        self.assertEqual(rc, 2, out)
        self.assertIn("no event path provided", out)

        bad_json = self.root / "bad.json"
        bad_json.write_text("{ unclosed", encoding="utf-8")
        rc, out = self._run_gate(["check_diff.py", "--event-path", str(bad_json)])
        self.assertEqual(rc, 2, out)
        self.assertIn("malformed event JSON", out)

        list_json = self.root / "list.json"
        list_json.write_text("[1, 2, 3]", encoding="utf-8")
        rc, out = self._run_gate(["check_diff.py", "--event-path", str(list_json)])
        self.assertEqual(rc, 2, out)
        self.assertIn("event JSON root must be an object", out)

    def test_empty_tree_object_creation(self):
        tree_sha = self.gate._get_empty_tree(cwd=self.repo_dir)
        self.assertTrue(self.gate._is_hex_sha(tree_sha))
        res = self._git_cmd(["cat-file", "-e", tree_sha])
        self.assertEqual(res.returncode, 0)

    def test_environment_variable_detection_and_isolation(self):
        head = self._commit_file("a.py", "print('ok')\n")
        event = self._write_event({"before": "0" * 40, "after": head})
        rc, out = self._run_gate(["check_diff.py"], env_event_path=event)
        self.assertEqual(rc, 0, out)
        self.assertIn("WHITESPACE GATE PASSED", out)


if __name__ == "__main__":
    unittest.main()
