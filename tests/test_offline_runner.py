#!/usr/bin/env python3
"""Tests for the offline runner, network guards, and verification pipeline."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_script(mod_name: str, file_path: Path):
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


offline_tests = _load_script("offline_tests", REPO_ROOT / "scripts" / "offline_tests.py")
verify_offline = _load_script("verify_offline", REPO_ROOT / "scripts" / "verify_offline.py")


class NetworkAndEnvGuardTests(unittest.TestCase):
    def test_network_and_dns_blocked(self):
        with offline_tests.offline_guard():
            with self.assertRaises(offline_tests.NetworkBlockedError):
                socket.getaddrinfo("localhost", 80)
            with self.assertRaises(offline_tests.NetworkBlockedError):
                socket.gethostbyname("localhost")
            with self.assertRaises(offline_tests.NetworkBlockedError):
                socket.create_connection(("127.0.0.1", 80))

            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                with self.assertRaises(offline_tests.NetworkBlockedError):
                    s.connect(("127.0.0.1", 80))
                with self.assertRaises(offline_tests.NetworkBlockedError):
                    s.connect_ex(("127.0.0.1", 80))
                with self.assertRaises(offline_tests.NetworkBlockedError):
                    s.bind(("127.0.0.1", 0))
                with self.assertRaises(offline_tests.NetworkBlockedError):
                    s.listen(1)
                with self.assertRaises(offline_tests.NetworkBlockedError):
                    s.send(b"data")
                with self.assertRaises(offline_tests.NetworkBlockedError):
                    s.sendall(b"data")
                with self.assertRaises(offline_tests.NetworkBlockedError):
                    s.sendto(b"data", ("127.0.0.1", 80))
            finally:
                s.close()

            if hasattr(socket, "AF_UNIX"):
                s_unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    with self.assertRaises(offline_tests.NetworkBlockedError):
                        s_unix.connect("/tmp/nonexistent.sock")
                    with self.assertRaises(offline_tests.NetworkBlockedError):
                        s_unix.send(b"data")
                finally:
                    s_unix.close()

    def test_socketpair_and_asyncio_allowed(self):
        with offline_tests.offline_guard():
            s1, s2 = socket.socketpair()
            try:
                s1.sendall(b"ping")
                data = s2.recv(4)
                self.assertEqual(data, b"ping")
            finally:
                s1.close()
                s2.close()

            async def dummy_coro():
                await asyncio.sleep(0)
                return "completed"

            res = asyncio.run(dummy_coro())
            self.assertEqual(res, "completed")

    def test_nested_guard_and_restoration(self):
        depth_before = offline_tests._guard_depth
        orig_gai = socket.getaddrinfo
        orig_connect = socket.socket.connect
        unpatched_gai = offline_tests._saved_state.get("fn_getaddrinfo", orig_gai)
        unpatched_connect = offline_tests._saved_state.get("meth_connect", orig_connect)

        with offline_tests.offline_guard():
            self.assertEqual(offline_tests._guard_depth, depth_before + 1)
            self.assertNotEqual(socket.getaddrinfo, unpatched_gai)
            self.assertNotEqual(socket.socket.connect, unpatched_connect)
            with offline_tests.offline_guard():
                self.assertEqual(offline_tests._guard_depth, depth_before + 2)
                self.assertNotEqual(socket.getaddrinfo, unpatched_gai)
            self.assertEqual(offline_tests._guard_depth, depth_before + 1)
            with self.assertRaises(offline_tests.NetworkBlockedError):
                socket.getaddrinfo("localhost", 80)

        self.assertEqual(offline_tests._guard_depth, depth_before)
        self.assertEqual(socket.getaddrinfo, orig_gai)
        self.assertEqual(socket.socket.connect, orig_connect)
        if depth_before == 0:
            self.assertEqual(socket.getaddrinfo, unpatched_gai)
            self.assertEqual(socket.socket.connect, unpatched_connect)

    def test_env_isolation_and_restoration(self):
        orig_home = os.environ.get("HOME")
        orig_data = os.environ.get("THETAMILL_DATA")
        orig_config = os.environ.get("THETAMILL_CONFIG")

        with offline_tests.offline_guard():
            sandbox_home = os.environ.get("HOME")
            self.assertIsNotNone(sandbox_home)
            self.assertTrue(Path(sandbox_home).is_dir())
            cfg_path = Path(os.environ["THETAMILL_CONFIG"])
            self.assertFalse(cfg_path.exists())

        self.assertEqual(os.environ.get("HOME"), orig_home)
        self.assertEqual(os.environ.get("THETAMILL_DATA"), orig_data)
        self.assertEqual(os.environ.get("THETAMILL_CONFIG"), orig_config)


class InstalledModeConfigTests(unittest.TestCase):
    def test_installed_mode_success(self):
        old_path = list(sys.path)
        old_modules = dict(sys.modules)
        try:
            with tempfile.TemporaryDirectory() as td:
                site_pkg = Path(td) / "site-packages"
                pkg_dir = site_pkg / "thetamill"
                pkg_dir.mkdir(parents=True)
                (pkg_dir / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
                sys.path.insert(0, str(site_pkg))
                sys.modules.pop("thetamill", None)

                with offline_tests.offline_guard():
                    path_before = list(sys.path)
                    offline_tests.configure_environment(
                        tests_dir=Path(td),
                        source_root=None,
                        require_installed=True,
                    )
                    self.assertIn("thetamill", sys.modules)
                    self.assertEqual(sys.path, path_before)
        finally:
            sys.path[:] = old_path
            sys.modules.clear()
            sys.modules.update(old_modules)

    def test_installed_mode_fails_outside_site_packages(self):
        old_path = list(sys.path)
        old_modules = dict(sys.modules)
        try:
            with tempfile.TemporaryDirectory() as td:
                pkg_dir = Path(td) / "thetamill"
                pkg_dir.mkdir()
                (pkg_dir / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
                sys.path.insert(0, str(td))
                sys.modules.pop("thetamill", None)

                with offline_tests.offline_guard():
                    with self.assertRaises(RuntimeError):
                        offline_tests.configure_environment(
                            tests_dir=Path(td),
                            source_root=None,
                            require_installed=True,
                        )
        finally:
            sys.path[:] = old_path
            sys.modules.clear()
            sys.modules.update(old_modules)

    def test_mutually_exclusive_flags(self):
        rc = offline_tests.main(["--require-installed", "--source-root", str(REPO_ROOT)])
        self.assertEqual(rc, 2)


class SubprocessAdversarialNegativeTests(unittest.TestCase):
    def test_subprocess_adversarial_rejections(self):
        real_home = offline_tests.REAL_USER_HOME
        with offline_tests.offline_guard() as sandbox_dir:
            with self.assertRaises(PermissionError):
                subprocess.Popen(["python", "-c", "pass"], cwd=sandbox_dir)
            with self.assertRaises(PermissionError):
                subprocess.Popen(["sh", "-c", "echo 1"], cwd=sandbox_dir)

            with self.assertRaises(PermissionError):
                subprocess.Popen(["git", "status"], shell=True, cwd=sandbox_dir)

            with self.assertRaises(PermissionError):
                subprocess.Popen(["git", "status"], executable="/bin/sh", cwd=sandbox_dir)

            with self.assertRaises(PermissionError):
                subprocess.Popen(["git", "status"], cwd=str(REPO_ROOT))
            with self.assertRaises(PermissionError):
                subprocess.Popen(["git", "status"], cwd=real_home)

            with self.assertRaises(PermissionError):
                subprocess.Popen(["git", "-c", "core.editor=sh", "status"], cwd=sandbox_dir)

            with self.assertRaises(PermissionError):
                subprocess.Popen(["git", "--exec-path=/tmp", "status"], cwd=sandbox_dir)

            with self.assertRaises(PermissionError):
                subprocess.Popen(["git", "diff", "--textconv"], cwd=sandbox_dir)
            with self.assertRaises(PermissionError):
                subprocess.Popen(["git", "diff", "--ext-diff"], cwd=sandbox_dir)

            with self.assertRaises(PermissionError):
                subprocess.Popen(["git", "clone", "https://github.com/foo/bar.git"], cwd=sandbox_dir)
            with self.assertRaises(PermissionError):
                subprocess.Popen(["git", "clone", "ssh://git@github.com/foo/bar.git"], cwd=sandbox_dir)

            with self.assertRaises(PermissionError):
                subprocess.Popen(["git", "config", "alias.st", "status"], cwd=sandbox_dir)
            with self.assertRaises(PermissionError):
                subprocess.Popen(["git", "config", "core.hooksPath", "/tmp"], cwd=sandbox_dir)

            with self.assertRaises(PermissionError):
                os.system("echo 1")
            with self.assertRaises(PermissionError):
                os.popen("echo 1")
            with self.assertRaises(PermissionError):
                os.execv("/bin/ls", ["ls"])
            with self.assertRaises(PermissionError):
                os.spawnv(os.P_WAIT, "/bin/ls", ["ls"])

    def test_allowed_git_shapes(self):
        with tempfile.TemporaryDirectory() as td:
            repo_dir = Path(td) / "repo"
            repo_dir.mkdir()
            with offline_tests.offline_guard():
                p = subprocess.Popen(["git", "init", "-b", "main", str(repo_dir)], cwd=td)
                self.assertEqual(p.wait(), 0)

                p = subprocess.Popen(["git", "config", "user.name", "Tester"], cwd=str(repo_dir))
                self.assertEqual(p.wait(), 0)
                p = subprocess.Popen(["git", "config", "user.email", "t@example.com"], cwd=str(repo_dir))
                self.assertEqual(p.wait(), 0)
                p = subprocess.Popen(["git", "config", "core.hooksPath", "/dev/null"], cwd=str(repo_dir))
                self.assertEqual(p.wait(), 0)
                p = subprocess.Popen(["git", "config", "commit.gpgsign", "false"], cwd=str(repo_dir))
                self.assertEqual(p.wait(), 0)

                p = subprocess.Popen(
                    ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "init", "--end-of-options"],
                    cwd=str(repo_dir),
                )
                self.assertEqual(p.wait(), 0)

                test_file = repo_dir / "test.txt"
                test_file.write_text("content", encoding="utf-8")
                p = subprocess.Popen(["git", "hash-object", "-w", "test.txt"], cwd=str(repo_dir))
                self.assertEqual(p.wait(), 0)

                clone_dir = Path(td) / "clone"
                p = subprocess.Popen(["git", "clone", "--depth", "1", f"file://{repo_dir}", str(clone_dir)], cwd=td)
                self.assertEqual(p.wait(), 0)

    def test_build_git_env_strips_poison_and_preserves_state(self):
        caller_env = {
            "HOME": "/tmp/fake_home",
            "TMPDIR": "/tmp/fake_tmp",
            "GIT_DIR": "/tmp/fake_git_dir",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "true",
            "CUSTOM_VAR": "retained",
        }
        orig_os_env = dict(os.environ)
        sandbox = "/tmp/thetamill_sandbox_test"
        built = offline_tests._build_git_env(caller_env, sandbox)

        self.assertEqual(built["HOME"], sandbox)
        self.assertEqual(built["TMPDIR"], sandbox)
        self.assertNotIn("GIT_DIR", built)
        self.assertNotIn("GIT_CONFIG_COUNT", built)
        self.assertNotIn("GIT_CONFIG_KEY_0", built)
        self.assertNotIn("GIT_CONFIG_VALUE_0", built)
        self.assertEqual(built.get("CUSTOM_VAR"), "retained")
        self.assertEqual(built["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(built["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(built["GIT_CONFIG_SYSTEM"], "/dev/null")
        self.assertEqual(os.environ, orig_os_env)
        self.assertIn("GIT_DIR", caller_env)


class VerificationPipelineNegativeTests(unittest.TestCase):
    def test_missing_tool_oserror_returns_2(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("missing coverage")):
            rc = verify_offline.main(["--source-root", str(REPO_ROOT)])
            self.assertEqual(rc, 2)

    def test_missing_coverage_data_returns_2(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            rc = verify_offline.main(["--source-root", str(REPO_ROOT)])
            self.assertEqual(rc, 2)

    def test_missing_coverage_json_returns_2(self):
        def fake_run(cmd, *args, **kwargs):
            for arg in cmd:
                if arg.startswith("--data-file="):
                    Path(arg.split("=", 1)[1]).write_text("dummy cov", encoding="utf-8")
            return MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=fake_run):
            rc = verify_offline.main(["--source-root", str(REPO_ROOT)])
            self.assertEqual(rc, 2)

    def test_stale_artifact_purge(self):
        with tempfile.TemporaryDirectory() as td:
            child = Path(td) / "work"
            child.mkdir()
            cov_file = child / ".coverage"
            json_file = child / "coverage.json"
            cov_file.write_text("stale", encoding="utf-8")
            json_file.write_text("stale", encoding="utf-8")

            with patch("subprocess.run", side_effect=FileNotFoundError("abort")):
                rc = verify_offline.run_pipeline(REPO_ROOT, REPO_ROOT / "tests", child)
                self.assertEqual(rc, 2)

            self.assertFalse(cov_file.exists())
            self.assertFalse(json_file.exists())


class RetainedArtifactsTests(unittest.TestCase):
    def test_retained_artifacts_and_caller_dir_integrity(self):
        with tempfile.TemporaryDirectory() as td:
            caller_dir = Path(td) / "my_output"
            caller_dir.mkdir()
            sentinel = caller_dir / "user_file.txt"
            sentinel.write_text("important user data", encoding="utf-8")

            def fake_run(cmd, *args, **kwargs):
                for arg in cmd:
                    if arg.startswith("--data-file="):
                        Path(arg.split("=", 1)[1]).write_text("cov data", encoding="utf-8")
                    elif arg == "-o":
                        idx = cmd.index("-o")
                        Path(cmd[idx + 1]).write_text("{}", encoding="utf-8")
                return MagicMock(returncode=0)

            with patch("subprocess.run", side_effect=fake_run):
                rc = verify_offline.main([
                    "--source-root", str(REPO_ROOT),
                    "--output-dir", str(caller_dir),
                    "--retain-artifacts",
                ])
                self.assertEqual(rc, 0)

            self.assertTrue(sentinel.is_file())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "important user data")

            children = [p for p in caller_dir.iterdir() if p.is_dir() and p.name.startswith("thetamill_verify_")]
            self.assertEqual(len(children), 1)
            child = children[0]
            self.assertTrue((child / ".coverage").is_file())
            self.assertTrue((child / "coverage.json").is_file())


def _asyncio_socketpair_worker(out_queue):
    import asyncio
    import socket

    s1, s2 = socket.socketpair()
    try:
        s1.sendall(b"worker_ping")
        received = s2.recv(11)
    finally:
        s1.close()
        s2.close()

    async def coro():
        await asyncio.sleep(0)
        return "async_ok"

    async_res = asyncio.run(coro())
    out_queue.put({"socketpair": received.decode("utf-8"), "asyncio": async_res})


def _state_concurrency_worker(db_path, tz, account, env, out_queue):
    from thetamill.state import SubmissionLedger
    ledger = SubmissionLedger(db_path, tz)
    intent = ledger.create_intent(
        account=account,
        environment=env,
        payload={"action": "SELL", "conId": 12345},
        config={"risk.enabled": True, "max_daily_submissions": 5, "day_timezone": tz},
        ttl_seconds=600,
    )
    reservation_id = ledger.claim_intent(
        account=account,
        environment=env,
        intent_id=intent["intent_id"],
        full_digest=intent["full_digest"],
        code=intent["approval_code"],
    )
    claimed = ledger.get_intent(reservation_id)
    out_queue.put({"intent_id": intent["intent_id"], "state": claimed["state"] if claimed else None})


class SpawnWorkerOfflineGuardTests(unittest.TestCase):
    def test_spawned_worker_blocked_before_test_module_import(self):
        import multiprocessing as mp

        probe_module_code = (
            "import os\n"
            "import socket\n"
            "import subprocess\n"
            "\n"
            "_import_probe_results = {}\n"
            "\n"
            "try:\n"
            "    socket.getaddrinfo('localhost', 80)\n"
            "    _import_probe_results['dns'] = 'ALLOWED'\n"
            "except Exception as exc:\n"
            "    _import_probe_results['dns'] = type(exc).__name__\n"
            "\n"
            "try:\n"
            "    s_tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "    try:\n"
            "        s_tcp.connect(('127.0.0.1', 80))\n"
            "        _import_probe_results['tcp'] = 'ALLOWED'\n"
            "    finally:\n"
            "        s_tcp.close()\n"
            "except Exception as exc:\n"
            "    _import_probe_results['tcp'] = type(exc).__name__\n"
            "\n"
            "try:\n"
            "    s_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
            "    try:\n"
            "        s_udp.sendto(b'ping', ('127.0.0.1', 9999))\n"
            "        _import_probe_results['udp'] = 'ALLOWED'\n"
            "    finally:\n"
            "        s_udp.close()\n"
            "except Exception as exc:\n"
            "    _import_probe_results['udp'] = type(exc).__name__\n"
            "\n"
            "try:\n"
            "    subprocess.Popen(['python', '-c', 'pass'])\n"
            "    _import_probe_results['proc'] = 'ALLOWED'\n"
            "except Exception as exc:\n"
            "    _import_probe_results['proc'] = type(exc).__name__\n"
            "\n"
            "try:\n"
            "    os.system('echo 1')\n"
            "    _import_probe_results['os_system'] = 'ALLOWED'\n"
            "except Exception as exc:\n"
            "    _import_probe_results['os_system'] = type(exc).__name__\n"
            "\n"
            "\n"
            "def probe_worker_fn(out_queue, real_home_str):\n"
            "    worker_dns = None\n"
            "    worker_proc = None\n"
            "    try:\n"
            "        socket.gethostbyname('localhost')\n"
            "        worker_dns = 'ALLOWED'\n"
            "    except Exception as exc:\n"
            "        worker_dns = type(exc).__name__\n"
            "    try:\n"
            "        subprocess.Popen(['sh', '-c', 'echo 1'])\n"
            "        worker_proc = 'ALLOWED'\n"
            "    except Exception as exc:\n"
            "        worker_proc = type(exc).__name__\n"
            "    out_queue.put({\n"
            "        'import_results': _import_probe_results,\n"
            "        'worker_dns': worker_dns,\n"
            "        'worker_proc': worker_proc,\n"
            "        'home': os.environ.get('HOME'),\n"
            "        'data': os.environ.get('THETAMILL_DATA'),\n"
            "        'config': os.environ.get('THETAMILL_CONFIG'),\n"
            "        'is_not_real_home': os.environ.get('HOME') != real_home_str,\n"
            "    })\n"
        )

        with tempfile.TemporaryDirectory() as td:
            probe_file = Path(td) / "probe_import_module.py"
            probe_file.write_text(probe_module_code, encoding="utf-8")
            old_sys_path = list(sys.path)
            sys.path.insert(0, td)
            try:
                with offline_tests.offline_guard() as sandbox_dir:
                    import probe_import_module

                    ctx = mp.get_context("spawn")
                    out_q = ctx.Queue()
                    proc = ctx.Process(
                        target=probe_import_module.probe_worker_fn,
                        args=(out_q, offline_tests.REAL_USER_HOME),
                    )
                    try:
                        proc.start()
                        data = out_q.get(timeout=30)
                        proc.join(timeout=30)
                        self.assertEqual(proc.exitcode, 0)
                    finally:
                        if proc.is_alive():
                            proc.terminate()
                        proc.join(timeout=30)
                        out_q.close()
                        out_q.join_thread()

                    imp = data["import_results"]
                    self.assertEqual(imp.get("dns"), "NetworkBlockedError")
                    self.assertEqual(imp.get("tcp"), "NetworkBlockedError")
                    self.assertEqual(imp.get("udp"), "NetworkBlockedError")
                    self.assertEqual(imp.get("proc"), "SubprocessBlockedError")
                    self.assertEqual(imp.get("os_system"), "SubprocessBlockedError")

                    self.assertEqual(data.get("worker_dns"), "NetworkBlockedError")
                    self.assertEqual(data.get("worker_proc"), "SubprocessBlockedError")
                    self.assertEqual(data.get("home"), sandbox_dir)
                    self.assertEqual(data.get("data"), os.path.join(sandbox_dir, "data"))
                    self.assertEqual(data.get("config"), os.path.join(sandbox_dir, "config.json"))
                    self.assertTrue(data.get("is_not_real_home"))
            finally:
                sys.path[:] = old_sys_path
                sys.modules.pop("probe_import_module", None)

    def test_spawned_worker_asyncio_and_socketpair_allowed(self):
        import multiprocessing as mp

        with offline_tests.offline_guard():
            ctx = mp.get_context("spawn")
            out_q = ctx.Queue()
            proc = ctx.Process(target=_asyncio_socketpair_worker, args=(out_q,))
            try:
                proc.start()
                res = out_q.get(timeout=30)
                proc.join(timeout=30)
                self.assertEqual(proc.exitcode, 0)
            finally:
                if proc.is_alive():
                    proc.terminate()
                proc.join(timeout=30)
                out_q.close()
                out_q.join_thread()
            self.assertEqual(res.get("socketpair"), "worker_ping")
            self.assertEqual(res.get("asyncio"), "async_ok")

    def test_spawned_worker_state_ledger_concurrency(self):
        import multiprocessing as mp

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "test_state.sqlite3"
            tz = "America/New_York"
            with offline_tests.offline_guard():
                ctx = mp.get_context("spawn")
                out_q = ctx.Queue()
                workers = [
                    ctx.Process(
                        target=_state_concurrency_worker,
                        args=(str(db_path), tz, f"DU123456{i}", "paper", out_q),
                    )
                    for i in range(2)
                ]
                try:
                    for p in workers:
                        p.start()
                    results = [out_q.get(timeout=30) for _ in workers]
                    for p in workers:
                        p.join(timeout=30)
                        self.assertEqual(p.exitcode, 0)
                finally:
                    for p in workers:
                        if p.is_alive():
                            p.terminate()
                        p.join(timeout=30)
                    out_q.close()
                    out_q.join_thread()
                self.assertEqual(len(results), 2)
                for r in results:
                    self.assertEqual(r.get("state"), "claimed")


if __name__ == "__main__":
    unittest.main()
