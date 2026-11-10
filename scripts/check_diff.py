#!/usr/bin/env python3
"""Check git diff ranges for whitespace violations and conflict markers.

Evaluates net diff semantics against a base commit, merge-base, or the empty tree
for initial commits. Driven either by GitHub event data (GITHUB_EVENT_PATH) or
explicit commit references.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HEX_CHARS = frozenset("0123456789abcdefABCDEF")


class DiffCheckError(Exception):
    """Operational or validation error in diff check gate."""


class DiffCheckOSError(DiffCheckError):
    """Subprocess OS-level error."""


def _is_hex_sha(s: object) -> bool:
    """Validate that `s` is a hex string of length 40 or 64."""
    if not isinstance(s, str):
        return False
    if len(s) not in (40, 64):
        return False
    return set(s).issubset(HEX_CHARS)


def _is_zero_sha(s: object) -> bool:
    """Validate that `s` is an all-zero SHA of length 40 or 64."""
    if not isinstance(s, str):
        return False
    if len(s) not in (40, 64):
        return False
    return set(s) == {"0"}


def _git(
    args: list[str],
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute git with hooks disabled."""
    cmd = ["git", "-c", "core.hooksPath=/dev/null", *args]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
        )
    except OSError as exc:
        raise DiffCheckOSError(f"git command execution failed ({exc})") from exc


def _resolve_cli_ref(ref_str: str, cwd: Path | str | None = None) -> str:
    """Resolve a user-supplied CLI ref to an immutable hex commit SHA."""
    if not isinstance(ref_str, str) or not ref_str.strip():
        raise DiffCheckError("commit ref cannot be empty")
    if "\x00" in ref_str:
        raise DiffCheckError("commit ref cannot contain null bytes")
    res = _git(
        ["rev-parse", "--verify", "--end-of-options", f"{ref_str}^{{commit}}"],
        cwd=cwd,
    )
    if res.returncode != 0:
        err = res.stderr.strip() or "not a valid commit"
        raise DiffCheckError(f"cannot resolve commit ref {ref_str!r}: {err}")
    sha = res.stdout.strip()
    if not _is_hex_sha(sha):
        raise DiffCheckError(f"resolved ref {ref_str!r} is not a valid hex SHA: {sha!r}")
    return sha


def _verify_commit_exists(sha: str, cwd: Path | str | None = None) -> None:
    """Verify that a hex commit SHA exists in the local repository."""
    res = _git(
        ["rev-parse", "--verify", "--end-of-options", f"{sha}^{{commit}}"],
        cwd=cwd,
    )
    if res.returncode != 0:
        raise DiffCheckError(f"commit {sha} does not exist in repository")


def _get_empty_tree(cwd: Path | str | None = None) -> str:
    """Ensure the empty tree object physically exists in git and return its SHA."""
    res = _git(["hash-object", "-w", "-t", "tree", "/dev/null"], cwd=cwd)
    if res.returncode != 0:
        err = res.stderr.strip() or "failed to create empty tree"
        raise DiffCheckError(f"cannot create empty tree object: {err}")
    sha = res.stdout.strip()
    if not _is_hex_sha(sha):
        raise DiffCheckError(f"empty tree SHA is invalid: {sha!r}")
    return sha


def _run_diff_check(
    base_tree_or_commit: str,
    head_commit: str,
    cwd: Path | str | None = None,
) -> int:
    """Run `git diff --check` evaluating the net diff between base and head."""
    diff_res = _git(
        ["diff", "--check", "--end-of-options", base_tree_or_commit, head_commit],
        cwd=cwd,
    )
    if diff_res.returncode == 0:
        print("WHITESPACE GATE PASSED")
        return 0
    if diff_res.stdout.strip():
        print("WHITESPACE GATE FAILED: whitespace or formatting violations found:")
        print(diff_res.stdout.rstrip())
        return 1
    print(f"WHITESPACE GATE FAILED: git diff --check error: {diff_res.stderr.strip()}", file=sys.stderr)
    return 2


def _handle_pr_event(pr_data: object, cwd: Path | str | None = None) -> int:
    """Process pull_request event comparing merge-base(base, head) to head."""
    if not isinstance(pr_data, dict):
        raise DiffCheckError(f"pull_request must be an object, got {type(pr_data).__name__}")
    base = pr_data.get("base")
    head = pr_data.get("head")
    if not isinstance(base, dict):
        raise DiffCheckError(f"pull_request.base must be an object, got {type(base).__name__}")
    if not isinstance(head, dict):
        raise DiffCheckError(f"pull_request.head must be an object, got {type(head).__name__}")

    base_sha = base.get("sha")
    head_sha = head.get("sha")
    if not _is_hex_sha(base_sha) or _is_zero_sha(base_sha):
        raise DiffCheckError(f"pull_request.base.sha is not a valid non-zero hex SHA: {base_sha!r}")
    if not _is_hex_sha(head_sha) or _is_zero_sha(head_sha):
        raise DiffCheckError(f"pull_request.head.sha is not a valid non-zero hex SHA: {head_sha!r}")

    _verify_commit_exists(base_sha, cwd=cwd)
    _verify_commit_exists(head_sha, cwd=cwd)

    mb_res = _git(["merge-base", "--end-of-options", base_sha, head_sha], cwd=cwd)
    if mb_res.returncode != 0:
        raise DiffCheckError(f"no common ancestor between PR base {base_sha} and head {head_sha}")
    merge_base = mb_res.stdout.strip()
    return _run_diff_check(merge_base, head_sha, cwd=cwd)


def _handle_push_event(event_data: dict, cwd: Path | str | None = None) -> int:
    """Process push event comparing before to after (or empty tree on initial push)."""
    if "before" not in event_data:
        raise DiffCheckError("push event missing required 'before' field")
    if "after" not in event_data:
        raise DiffCheckError("push event missing required 'after' field")

    before = event_data["before"]
    after = event_data["after"]

    if not _is_hex_sha(after):
        raise DiffCheckError(f"push 'after' is not a valid hex SHA: {after!r}")
    if _is_zero_sha(after):
        raise DiffCheckError(f"push 'after' cannot be an all-zero SHA: {after!r}")

    if _is_zero_sha(before):
        _verify_commit_exists(after, cwd=cwd)
        base_sha = _get_empty_tree(cwd=cwd)
    else:
        if not _is_hex_sha(before):
            raise DiffCheckError(f"push 'before' is not a valid hex SHA or initial-push zero SHA: {before!r}")
        _verify_commit_exists(before, cwd=cwd)
        _verify_commit_exists(after, cwd=cwd)
        base_sha = before

    return _run_diff_check(base_sha, after, cwd=cwd)


def main(argv: list[str] | None = None, *, cwd: Path | str | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    elif argv and (argv[0].endswith(".py") or argv[0] in ("check_diff.py", "scripts/check_diff.py")):
        argv = argv[1:]

    parser = argparse.ArgumentParser(
        description="Check git diff ranges for whitespace violations.",
        exit_on_error=False,
    )
    parser.add_argument(
        "--event-path",
        type=Path,
        default=None,
        help="Path to GitHub Actions event.json file",
    )
    parser.add_argument(
        "--base",
        type=str,
        default=None,
        help="Explicit base commit or reference",
    )
    parser.add_argument(
        "--head",
        type=str,
        default=None,
        help="Explicit head commit or reference",
    )
    parser.add_argument(
        "refs",
        nargs="*",
        help="Optional [base] head commit/ref",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 0
    except argparse.ArgumentError as exc:
        print(f"CHECK DIFF FAILED: invalid arguments: {exc}", file=sys.stderr)
        return 2

    event_path = args.event_path
    if event_path is None and not args.base and not args.head and not args.refs:
        env_val = os.environ.get("GITHUB_EVENT_PATH")
        if env_val and env_val.strip():
            event_path = Path(env_val.strip())

    if event_path is not None:
        if not event_path.is_file():
            print(f"CHECK DIFF FAILED: event file not found: {event_path}", file=sys.stderr)
            return 2
        try:
            content = event_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"CHECK DIFF FAILED: cannot read event file {event_path}: {exc}", file=sys.stderr)
            return 2
        try:
            event_data = json.loads(content)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"CHECK DIFF FAILED: malformed event JSON in {event_path}: {exc}", file=sys.stderr)
            return 2
        if not isinstance(event_data, dict):
            print(
                f"CHECK DIFF FAILED: event JSON root must be an object, got {type(event_data).__name__}",
                file=sys.stderr,
            )
            return 2

        try:
            if "pull_request" in event_data:
                return _handle_pr_event(event_data["pull_request"], cwd=cwd)
            elif "before" in event_data or "after" in event_data:
                return _handle_push_event(event_data, cwd=cwd)
            else:
                print(
                    "CHECK DIFF FAILED: event JSON contains neither 'pull_request' nor push ('before'/'after') fields",
                    file=sys.stderr,
                )
                return 2
        except DiffCheckOSError as exc:
            print(f"CHECK DIFF FAILED: OS error: {exc}", file=sys.stderr)
            return 2
        except DiffCheckError as exc:
            print(f"CHECK DIFF FAILED: {exc}", file=sys.stderr)
            return 2

    base_ref = args.base
    head_ref = args.head
    if args.refs:
        if len(args.refs) == 1:
            if head_ref is None:
                head_ref = args.refs[0]
            elif base_ref is None:
                base_ref = args.refs[0]
            else:
                print("CHECK DIFF FAILED: too many refs specified", file=sys.stderr)
                return 2
        elif len(args.refs) == 2:
            if base_ref is None and head_ref is None:
                base_ref, head_ref = args.refs[0], args.refs[1]
            else:
                print("CHECK DIFF FAILED: base/head specified via both flags and positional arguments", file=sys.stderr)
                return 2
        else:
            print("CHECK DIFF FAILED: too many refs specified (expected at most 2)", file=sys.stderr)
            return 2

    if head_ref is None:
        print(
            "CHECK DIFF FAILED: no event path provided (GITHUB_EVENT_PATH not set) and no commit refs supplied",
            file=sys.stderr,
        )
        return 2

    try:
        head_sha = _resolve_cli_ref(head_ref, cwd=cwd)
        if base_ref is not None:
            base_sha = _resolve_cli_ref(base_ref, cwd=cwd)
        else:
            cat_res = _git(["cat-file", "-p", head_sha], cwd=cwd)
            if cat_res.returncode != 0:
                raise DiffCheckError(f"cannot inspect commit object {head_sha}")
            parents: list[str] = []
            for line in cat_res.stdout.splitlines():
                if not line:
                    break
                if line.startswith("parent "):
                    parts = line.split()
                    if len(parts) >= 2 and _is_hex_sha(parts[1]):
                        parents.append(parts[1])
            if not parents:
                base_sha = _get_empty_tree(cwd=cwd)
            else:
                _verify_commit_exists(parents[0], cwd=cwd)
                base_sha = parents[0]
        return _run_diff_check(base_sha, head_sha, cwd=cwd)
    except DiffCheckOSError as exc:
        print(f"CHECK DIFF FAILED: OS error: {exc}", file=sys.stderr)
        return 2
    except DiffCheckError as exc:
        print(f"CHECK DIFF FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
