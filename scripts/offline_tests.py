#!/usr/bin/env python3
"""Accidental-network-regression, subprocess, and multiprocessing guard for offline tests.

Guards against unintended network calls, DNS queries, and external process
execution during test discovery and execution. This runner provides an
accidental-network-regression guard; it is not an arbitrary-code security
sandbox against malicious Python or native code. Spawned multiprocessing
workers are intercepted before importing test modules to inherit equivalent
offline socket/DNS/process guards and isolated environments.
"""
from __future__ import annotations

import argparse
import contextlib
import multiprocessing.spawn as mp_spawn
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import weakref
from pathlib import Path
from typing import Any, Iterator, Sequence

# Canonical module registration to share state across imports
sys.modules.setdefault("offline_tests", sys.modules[__name__])

# Fixed real git binary resolved once.
_RAW_GIT = shutil.which("git")
_REAL_GIT: str | None = os.path.realpath(_RAW_GIT) if _RAW_GIT else None

_ALLOWED_SUBCOMMANDS = {
    "init", "config", "add", "commit", "diff", "rev-parse",
    "merge-base", "show", "cat-file", "hash-object", "checkout",
    "clone", "status", "log",
}

_HERMETIC_GIT_FLAGS = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "commit.gpgSign=false",
    "-c", "tag.gpgSign=false",
    "-c", "diff.external=",
    "-c", "diff.textconv=false",
)

_SAFE_GIT_C_CONFIGS = {
    "core.hooksPath=/dev/null",
    "commit.gpgSign=false",
    "commit.gpgsign=false",
    "tag.gpgSign=false",
    "tag.gpgsign=false",
    "core.fsmonitor=false",
    "diff.external=",
    "diff.textconv=false",
}

_SAFE_GIT_CONFIG_WRITES = {
    ("core.hooksPath", "/dev/null"),
    ("commit.gpgSign", "false"),
    ("commit.gpgsign", "false"),
    ("tag.gpgSign", "false"),
    ("tag.gpgsign", "false"),
}

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = Path(__file__).resolve().parent
REAL_USER_HOME = os.path.realpath(os.path.expanduser("~"))
_REAL_USER_HOME = REAL_USER_HOME

_SOCK_FN_NAMES = (
    "socketpair", "create_connection", "getaddrinfo", "gethostbyname",
    "gethostbyname_ex", "gethostbyaddr", "getnameinfo", "getfqdn",
)
_SOCK_METH_NAMES = (
    "connect", "connect_ex", "bind", "listen", "send", "sendall", "sendto", "sendmsg",
)
_OS_PROC_NAMES = (
    "system", "popen", "execl", "execle", "execlp", "execlpe",
    "execv", "execve", "execvp", "execvpe", "spawnl", "spawnle",
    "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "posix_spawn", "posix_spawnp",
)
_ENV_VAR_NAMES = ("HOME", "THETAMILL_DATA", "THETAMILL_CONFIG")


class NetworkBlockedError(RuntimeError, OSError):
    """Raised when network operations or DNS resolutions are attempted."""


class SubprocessBlockedError(PermissionError):
    """Raised when forbidden subprocess execution is attempted."""


_allowed_sockets: weakref.WeakSet[socket.socket] = weakref.WeakSet()
_guard_depth = 0
_saved_state: dict[str, Any] = {}
_current_sandbox_dir: str | None = None


def _is_under_temp(p: str | Path, temp_roots: Sequence[str]) -> bool:
    try:
        resolved = os.path.realpath(str(p))
        return any(resolved == r or resolved.startswith(r + os.sep) for r in temp_roots)
    except Exception:
        return False


def _is_repo_or_real_home(p: str | Path, sandbox_dir: str | None) -> bool:
    try:
        resolved = os.path.realpath(str(p))
        if sandbox_dir:
            sb_res = os.path.realpath(sandbox_dir)
            if resolved == sb_res or resolved.startswith(sb_res + os.sep):
                return False
        repo_res = os.path.realpath(str(_REPO_ROOT))
        if repo_res != "/" and (resolved == repo_res or resolved.startswith(repo_res + os.sep)):
            return True
        home_res = os.path.realpath(_REAL_USER_HOME)
        if home_res != "/" and (resolved == home_res or resolved.startswith(home_res + os.sep)):
            return True
        return False
    except Exception:
        return False


def _build_git_env(caller_env: dict[str, str] | None, sandbox_dir: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    if caller_env:
        for k, v in caller_env.items():
            if not k.startswith("GIT_"):
                env[k] = v
    env["HOME"] = sandbox_dir
    env["TMPDIR"] = sandbox_dir
    for k in list(env):
        if k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(k, None)
    for blocked_var in (
        "GIT_CONFIG_COUNT", "GIT_DIR", "GIT_WORK_TREE", "GIT_EXEC_PATH",
        "GIT_CONFIG_PARAMETERS", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR",
        "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
    ):
        env.pop(blocked_var, None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["GIT_AUTHOR_NAME"] = "ThetaMill Hermetic"
    env["GIT_AUTHOR_EMAIL"] = "offline@thetamill.local"
    env["GIT_COMMITTER_NAME"] = "ThetaMill Hermetic"
    env["GIT_COMMITTER_EMAIL"] = "offline@thetamill.local"
    return env


def validate_git_cmd(
    cmd: Sequence[str],
    temp_roots: Sequence[str],
    cwd: str | Path | None = None,
    sandbox_dir: str | None = None,
) -> None:
    if isinstance(cmd, (str, bytes)) or not cmd:
        raise SubprocessBlockedError("Command must be a non-empty sequence of arguments")

    if _REAL_GIT is None:
        raise SubprocessBlockedError("Real git executable not found on host")

    raw_bin = cmd[0]
    resolved_bin = shutil.which(raw_bin) or raw_bin
    if os.path.realpath(resolved_bin) != _REAL_GIT:
        raise SubprocessBlockedError(f"Process '{raw_bin}' is blocked; only real git is permitted")

    cwd_to_check = cwd if cwd is not None else os.getcwd()
    if not _is_under_temp(cwd_to_check, temp_roots) or _is_repo_or_real_home(cwd_to_check, sandbox_dir):
        raise SubprocessBlockedError(f"CWD '{cwd_to_check}' is not inside an allowed temporary directory")

    forbidden_prefixes = ("--textconv", "--ext-diff", "--upload-pack", "--receive-pack", "--exec-path")
    for arg in cmd[1:]:
        if arg == "--config" or arg.startswith("--config=") or any(arg.startswith(fp) for fp in forbidden_prefixes):
            raise SubprocessBlockedError(f"Option '{arg}' is blocked")
        if any(arg.startswith(p) for p in ("http://", "https://", "ssh://", "git://", "ftp://")):
            raise SubprocessBlockedError(f"Network URL '{arg}' is blocked")

    idx = 1
    while idx < len(cmd):
        tok = cmd[idx]
        if tok == "--no-pager":
            idx += 1
        elif tok == "-c":
            if idx + 1 >= len(cmd) or cmd[idx + 1] not in _SAFE_GIT_C_CONFIGS:
                val = cmd[idx + 1] if idx + 1 < len(cmd) else ""
                raise SubprocessBlockedError(f"git -c configuration '{val}' is blocked")
            idx += 2
        elif tok.startswith("-c"):
            val = tok[3:] if tok.startswith("-c=") else tok[2:]
            if val not in _SAFE_GIT_C_CONFIGS:
                raise SubprocessBlockedError(f"git -c configuration '{val}' is blocked")
            idx += 1
        elif tok == "-C":
            if idx + 1 >= len(cmd) or not _is_under_temp(cmd[idx + 1], temp_roots):
                raise SubprocessBlockedError("git -C must target an allowed temporary directory")
            idx += 2
        elif tok.startswith("-C"):
            target = tok[2:]
            if not _is_under_temp(target, temp_roots):
                raise SubprocessBlockedError("git -C must target an allowed temporary directory")
            idx += 1
        elif tok == "--end-of-options":
            idx += 1
        elif tok.startswith("-"):
            raise SubprocessBlockedError(f"Global git option '{tok}' is blocked")
        else:
            break

    if idx >= len(cmd):
        raise SubprocessBlockedError("Missing git subcommand")

    subcmd = cmd[idx]
    if subcmd not in _ALLOWED_SUBCOMMANDS:
        raise SubprocessBlockedError(f"git subcommand '{subcmd}' is blocked")

    sub_args = cmd[idx + 1:]
    if subcmd == "config":
        pos_args = [a for a in sub_args if not a.startswith("-")]
        if len(pos_args) >= 2:
            key, val = pos_args[0], pos_args[1]
            if key not in ("user.name", "user.email", "init.defaultBranch") and (key, val) not in _SAFE_GIT_CONFIG_WRITES:
                raise SubprocessBlockedError(f"git config write for '{key}' is blocked")
        elif len(pos_args) == 1:
            key = pos_args[0]
            if any(k in key for k in ("textconv", "ext-diff", "diff.", "alias.", "include.", "fsmonitor", "gpg.sign", "filter.", "hooks")):
                raise SubprocessBlockedError(f"git config key '{key}' is blocked")
    elif subcmd == "clone":
        file_urls = [a for a in sub_args if a.startswith("file://")]
        if not file_urls:
            raise SubprocessBlockedError("git clone allows only local file:// URLs")
        for u in file_urls:
            src_path = u[7:]
            if not _is_under_temp(src_path, temp_roots):
                raise SubprocessBlockedError(f"Clone source '{src_path}' is not under temporary roots")
        dest_args = []
        skip = False
        for a in sub_args:
            if skip:
                skip = False
                continue
            if a in ("--depth", "-b", "--branch"):
                skip = True
                continue
            if not a.startswith("-") and not a.startswith("file://") and a != "--end-of-options":
                dest_args.append(a)
        for d in dest_args:
            if not _is_under_temp(d, temp_roots):
                raise SubprocessBlockedError(f"Clone destination '{d}' is not under temporary roots")


def _guarded_socketpair(*args: Any, **kwargs: Any) -> tuple[socket.socket, socket.socket]:
    s1, s2 = _saved_state["fn_socketpair"](*args, **kwargs)
    _allowed_sockets.add(s1)
    _allowed_sockets.add(s2)
    return s1, s2


def _guarded_send(self: socket.socket, *args: Any, **kwargs: Any) -> Any:
    if self in _allowed_sockets and getattr(self, "family", None) == getattr(socket, "AF_UNIX", None):
        return _saved_state["meth_send"](self, *args, **kwargs)
    raise NetworkBlockedError("Outbound socket send is blocked in offline test mode")


def _guarded_sendall(self: socket.socket, *args: Any, **kwargs: Any) -> Any:
    if self in _allowed_sockets and getattr(self, "family", None) == getattr(socket, "AF_UNIX", None):
        return _saved_state["meth_sendall"](self, *args, **kwargs)
    raise NetworkBlockedError("Outbound socket sendall is blocked in offline test mode")


def _blocked_network(*args: Any, **kwargs: Any) -> Any:
    raise NetworkBlockedError("Network and DNS operations are blocked in offline test mode")


def _blocked_os_proc(*args: Any, **kwargs: Any) -> Any:
    raise SubprocessBlockedError("Direct process execution via os module is blocked")


def _guarded_get_command_line(**kwargs: Any) -> list[str]:
    """Wraps multiprocessing.spawn.get_command_line to inject worker guard bootstrap."""
    orig_fn = _saved_state.get("mp_get_command_line", mp_spawn.get_command_line)
    cmd = list(orig_fn(**kwargs))
    if "-c" not in cmd:
        raise SubprocessBlockedError("Multiprocessing spawn command lacks expected -c argument; frozen executables not supported")
    idx = cmd.index("-c")
    if idx + 1 >= len(cmd):
        raise SubprocessBlockedError("Multiprocessing spawn command lacks code after -c")
    prog = cmd[idx + 1]
    sandbox_repr = repr(_current_sandbox_dir)
    real_home_repr = repr(_REAL_USER_HOME)
    scripts_repr = repr(str(_SCRIPTS_DIR))
    bootstrap = (
        f"import sys; "
        f"sys.path.insert(0, {scripts_repr}); "
        f"import offline_tests; "
        f"offline_tests.install_worker_guard({sandbox_repr}, {real_home_repr}); "
    )
    cmd[idx + 1] = bootstrap + prog
    return cmd


def install_worker_guard(sandbox_dir: str | None, real_user_home: str | None = None) -> None:
    """Installs offline guards and verifies isolation in a spawned worker process."""
    global _current_sandbox_dir, _guard_depth, _REAL_USER_HOME, REAL_USER_HOME
    _guard_depth = 1
    _current_sandbox_dir = sandbox_dir

    if not sandbox_dir or not os.path.isabs(sandbox_dir) or not os.path.isdir(sandbox_dir):
        raise NetworkBlockedError("Worker sandbox directory must be an existing absolute directory")

    temp_roots = [os.path.realpath(tempfile.gettempdir()), "/tmp", "/private/tmp", "/var/tmp"]
    if not _is_under_temp(sandbox_dir, temp_roots):
        raise NetworkBlockedError(f"Worker sandbox directory '{sandbox_dir}' is not under a temporary directory")

    if not os.environ.get("HOME") or os.path.realpath(os.environ["HOME"]) != os.path.realpath(sandbox_dir):
        raise NetworkBlockedError("Worker process inherited HOME does not match sandbox directory")

    if real_user_home:
        _REAL_USER_HOME = os.path.realpath(real_user_home)
        REAL_USER_HOME = _REAL_USER_HOME
        if os.path.realpath(sandbox_dir) == _REAL_USER_HOME:
            raise NetworkBlockedError("Worker process detected real user HOME; isolation failed")

    os.environ["HOME"] = sandbox_dir
    os.environ["THETAMILL_DATA"] = os.path.join(sandbox_dir, "data")
    os.environ["THETAMILL_CONFIG"] = os.path.join(sandbox_dir, "config.json")

    for name in _SOCK_FN_NAMES:
        _saved_state.setdefault(f"fn_{name}", getattr(socket, name))
    for name in _SOCK_METH_NAMES:
        _saved_state.setdefault(f"meth_{name}", getattr(socket.socket, name))

    _saved_state.setdefault("popen", subprocess.Popen)
    for name in _OS_PROC_NAMES:
        if hasattr(os, name):
            _saved_state.setdefault(f"os_{name}", getattr(os, name))

    socket.socketpair = _guarded_socketpair
    for name in _SOCK_FN_NAMES[1:]:
        setattr(socket, name, _blocked_network)
    for name in ("connect", "connect_ex", "bind", "listen", "sendto", "sendmsg"):
        setattr(socket.socket, name, _blocked_network)
    socket.socket.send = _guarded_send
    socket.socket.sendall = _guarded_sendall

    subprocess.Popen = _guarded_popen
    for name in _OS_PROC_NAMES:
        if hasattr(os, name):
            setattr(os, name, _blocked_os_proc)

    _saved_state.setdefault("mp_get_command_line", mp_spawn.get_command_line)
    mp_spawn.get_command_line = _guarded_get_command_line


def _guarded_popen(cmd: Sequence[str], *args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
    if kwargs.get("shell"):
        raise SubprocessBlockedError("subprocess execution with shell=True is blocked")
    executable = kwargs.get("executable")
    if executable is not None:
        resolved_exe = shutil.which(executable) or executable
        if _REAL_GIT is None or os.path.realpath(resolved_exe) != _REAL_GIT:
            raise SubprocessBlockedError(f"Executable override '{executable}' is blocked")

    cwd = kwargs.get("cwd")
    temp_roots = [
        os.path.realpath(_current_sandbox_dir) if _current_sandbox_dir else tempfile.gettempdir(),
        os.path.realpath(tempfile.gettempdir()),
    ]
    validate_git_cmd(cmd, temp_roots, cwd, _current_sandbox_dir)

    caller_env = kwargs.get("env")
    kwargs["env"] = _build_git_env(caller_env, _current_sandbox_dir or tempfile.gettempdir())
    new_cmd = [_REAL_GIT, *_HERMETIC_GIT_FLAGS, *list(cmd)[1:]]
    return _saved_state["popen"](new_cmd, *args, **kwargs)


@contextlib.contextmanager
def offline_guard() -> Iterator[str]:
    """Accidental regression guard context for sockets, DNS, subprocesses, and spawn workers.

    Installs in parent process and hooks multiprocessing.spawn to ensure worker bootstrap
    executes in child processes before test modules are imported.
    """
    global _guard_depth, _current_sandbox_dir
    if _guard_depth == 0:
        for name in _SOCK_FN_NAMES:
            _saved_state[f"fn_{name}"] = getattr(socket, name)
        for name in _SOCK_METH_NAMES:
            _saved_state[f"meth_{name}"] = getattr(socket.socket, name)

        _saved_state["popen"] = subprocess.Popen
        for name in _OS_PROC_NAMES:
            if hasattr(os, name):
                _saved_state[f"os_{name}"] = getattr(os, name)

        for var in _ENV_VAR_NAMES:
            _saved_state[f"env_{var}"] = os.environ.get(var)

        _saved_state["mp_get_command_line"] = mp_spawn.get_command_line

        sandbox = tempfile.TemporaryDirectory(prefix="thetamill_sandbox_")
        _saved_state["sandbox"] = sandbox
        _current_sandbox_dir = sandbox.name

        os.environ["HOME"] = _current_sandbox_dir
        os.environ["THETAMILL_DATA"] = os.path.join(_current_sandbox_dir, "data")
        os.environ["THETAMILL_CONFIG"] = os.path.join(_current_sandbox_dir, "config.json")

        socket.socketpair = _guarded_socketpair
        for name in _SOCK_FN_NAMES[1:]:
            setattr(socket, name, _blocked_network)
        for name in ("connect", "connect_ex", "bind", "listen", "sendto", "sendmsg"):
            setattr(socket.socket, name, _blocked_network)
        socket.socket.send = _guarded_send
        socket.socket.sendall = _guarded_sendall

        subprocess.Popen = _guarded_popen
        for name in _OS_PROC_NAMES:
            if hasattr(os, name):
                setattr(os, name, _blocked_os_proc)

        mp_spawn.get_command_line = _guarded_get_command_line

    _guard_depth += 1
    try:
        yield _current_sandbox_dir or ""
    finally:
        _guard_depth -= 1
        if _guard_depth == 0:
            for name in _SOCK_FN_NAMES:
                setattr(socket, name, _saved_state[f"fn_{name}"])
            for name in _SOCK_METH_NAMES:
                setattr(socket.socket, name, _saved_state[f"meth_{name}"])

            subprocess.Popen = _saved_state["popen"]
            for name in _OS_PROC_NAMES:
                if hasattr(os, name):
                    setattr(os, name, _saved_state[f"os_{name}"])

            if "mp_get_command_line" in _saved_state:
                mp_spawn.get_command_line = _saved_state["mp_get_command_line"]

            for var in _ENV_VAR_NAMES:
                val = _saved_state.get(f"env_{var}")
                if val is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = val

            _saved_state["sandbox"].cleanup()
            _saved_state.clear()
            _allowed_sockets.clear()
            _current_sandbox_dir = None


def configure_environment(
    tests_dir: Path,
    source_root: Path | None,
    require_installed: bool,
) -> None:
    """Configure sys.path and verify installation boundaries."""
    if require_installed:
        import thetamill
        pkg_file = getattr(thetamill, "__file__", "") or ""
        resolved = str(Path(pkg_file).resolve())
        if "site-packages" not in resolved and "dist-packages" not in resolved:
            raise RuntimeError(f"Expected thetamill to be imported from site-packages, got: {resolved}")
    else:
        root = source_root if source_root is not None else _REPO_ROOT
        root_str = str(root.resolve())
        if root_str not in sys.path:
            sys.path.insert(0, root_str)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Wraps configuration and test discovery under offline_guard."""
    parser = argparse.ArgumentParser(description="Run ThetaMill tests under offline guards.")
    parser.add_argument("--tests-dir", type=str, default=None, help="Directory containing tests.")
    parser.add_argument("--source-root", type=str, default=None, help="Path to repository source root.")
    parser.add_argument("--require-installed", action="store_true", help="Assert thetamill is installed in site-packages.")
    parser.add_argument("-v", "--verbose", action="count", default=1, help="Verbose output.")
    args = parser.parse_args(argv)

    if args.require_installed and args.source_root:
        print("ERROR: --require-installed and --source-root are mutually exclusive", file=sys.stderr)
        return 2

    tests_dir = Path(args.tests_dir).resolve() if args.tests_dir else (_REPO_ROOT / "tests").resolve()
    if not tests_dir.is_dir():
        print(f"ERROR: tests directory does not exist: {tests_dir}", file=sys.stderr)
        return 2

    source_root = Path(args.source_root).resolve() if args.source_root else None

    with offline_guard():
        try:
            configure_environment(
                tests_dir=tests_dir,
                source_root=source_root,
                require_installed=args.require_installed,
            )
        except Exception as exc:
            print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
            return 2

        suite = unittest.defaultTestLoader.discover(
            start_dir=str(tests_dir),
            pattern="test_*.py",
            top_level_dir=str(tests_dir),
        )
        runner = unittest.TextTestRunner(verbosity=args.verbose)
        result = runner.run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
