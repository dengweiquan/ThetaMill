"""ThetaMill — Monotonic command-level total budget and deadline enforcement.

Provides deterministic, monotonic command budgets across broker requests,
per-instance IB._run wrapping with RequestTimeout clamping, and explicit
attribute sentinel tagging for safe broker requester inspection.
"""
from __future__ import annotations

import asyncio
import inspect
import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Optional

from .broker_requests import Deadline, DeadlineExceededError

DEFAULT_COMMAND_TIMEOUT: float = 120.0
BUDGET_ATTR = "_thetamill_command_budget"
SENTINEL_ATTR = "_thetamill_command_budget_sentinel"
_SENTINEL = object()

__all__ = [
    "DEFAULT_COMMAND_TIMEOUT",
    "BUDGET_ATTR",
    "SENTINEL_ATTR",
    "Deadline",
    "DeadlineExceededError",
    "validate_command_timeout",
    "get_active_budget",
    "command_budget",
    "attach_budget",
    "get_instance_budget",
    "wrap_ib_instance",
]


def validate_command_timeout(value: Any, default: float = DEFAULT_COMMAND_TIMEOUT) -> float:
    """Validate and return a finite positive non-bool command timeout in seconds."""
    fallback = DEFAULT_COMMAND_TIMEOUT
    if (
        default is not None
        and not isinstance(default, bool)
        and isinstance(default, (int, float))
    ):
        try:
            def_val = float(default)
            if math.isfinite(def_val) and def_val > 0:
                fallback = def_val
        except (TypeError, ValueError, OverflowError):
            fallback = DEFAULT_COMMAND_TIMEOUT

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    try:
        val = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if not math.isfinite(val) or val <= 0:
        return fallback
    return val


_ACTIVE_BUDGET: ContextVar[Optional[Deadline]] = ContextVar("_active_budget", default=None)


def get_active_budget() -> Optional[Deadline]:
    """Return the currently active command budget deadline in this context, if any."""
    return _ACTIVE_BUDGET.get()


@contextmanager
def command_budget(
    budget_or_cfg: Any = DEFAULT_COMMAND_TIMEOUT,
    clock: Optional[Callable[[], float]] = None,
):
    """Context manager that establishes a command-level monotonic budget.

    If an active budget already exists in this context, it is reused.
    Otherwise, a new Deadline is created and made active.
    """
    existing = get_active_budget()
    if existing is not None:
        yield existing
        return

    c = clock if clock is not None else time.monotonic

    if isinstance(budget_or_cfg, Deadline):
        deadline = budget_or_cfg
    elif isinstance(budget_or_cfg, dict):
        raw = budget_or_cfg.get("command_timeout", DEFAULT_COMMAND_TIMEOUT)
        valid_timeout = validate_command_timeout(raw, default=DEFAULT_COMMAND_TIMEOUT)
        deadline = Deadline(valid_timeout, clock=c)
    else:
        valid_timeout = validate_command_timeout(budget_or_cfg, default=DEFAULT_COMMAND_TIMEOUT)
        deadline = Deadline(valid_timeout, clock=c)

    token = _ACTIVE_BUDGET.set(deadline)
    try:
        yield deadline
    finally:
        _ACTIVE_BUDGET.reset(token)


def attach_budget(ib: Any, deadline: Deadline) -> None:
    """Mark budget on an IB instance with an explicit attribute sentinel."""
    if not isinstance(deadline, Deadline):
        raise TypeError("deadline must be a Deadline instance")
    setattr(ib, BUDGET_ATTR, deadline)
    setattr(ib, SENTINEL_ATTR, _SENTINEL)
    if getattr(ib, BUDGET_ATTR, None) is not deadline or inspect.getattr_static(ib, SENTINEL_ATTR, None) is not _SENTINEL:
        raise RuntimeError("Failed to attach budget attributes to IB instance")


def get_instance_budget(ib: Any) -> Optional[Deadline]:
    """Fetch budget attached to ib without MagicMock truthiness."""
    try:
        if inspect.getattr_static(ib, SENTINEL_ATTR, None) is _SENTINEL:
            val = getattr(ib, BUDGET_ATTR, None)
            if isinstance(val, Deadline):
                return val
    except Exception:
        pass
    return None


def _classify_class_attr(cls: type, name: str) -> str:
    """Classify class attribute as 'missing', 'property', 'callable', or 'other'."""
    attr = inspect.getattr_static(cls, name, None)
    if attr is None:
        return "missing"
    if isinstance(attr, property):
        return "property"
    if callable(attr):
        return "callable"
    if isinstance(attr, (staticmethod, classmethod)):
        return "callable" if callable(attr.__func__) else "other"
    return "other"


def _has_real_class_run(ib: Any) -> bool:
    """Check if ib's class defines a real callable _run (not a MagicMock phantom)."""
    return _classify_class_attr(type(ib), "_run") == "callable"


def _has_real_class_sleep(ib: Any) -> bool:
    """Check if ib's class defines a real callable sleep (not a MagicMock phantom)."""
    return _classify_class_attr(type(ib), "sleep") == "callable"


def _close_awaitables(awaitables: tuple[Any, ...]) -> None:
    for a in awaitables:
        if inspect.iscoroutine(a):
            try:
                a.close()
            except Exception:
                pass
        elif hasattr(a, "cancel") and callable(getattr(a, "cancel", None)):
            try:
                if hasattr(a, "done") and callable(getattr(a, "done", None)):
                    if not a.done():
                        a.cancel()
                else:
                    a.cancel()
            except Exception:
                pass


def wrap_ib_instance(
    ib: Any,
    deadline: Deadline,
    configured_timeout: Optional[float] = None,
) -> None:
    """Per-instance wrapper for IB._run and IB.sleep respecting command deadline.

    Clamps RequestTimeout to min(current valid at call, configured ceiling, remaining())
    for EVERY blocking call, verifies setter readback, restores in finally on all paths,
    checks deadline before and after.
    Clamps sleep duration to remaining deadline, preserving strict caller max,
    failing closed on bad inputs, checking deadline before and after.
    Does NOT globally monkeypatch the IB class, and does NOT wrap in a proxy object.
    Does NOT wrap arbitrary MagicMock phantom _run or sleep.
    Dynamically looks up attached or active deadline to avoid stale deadline capture
    on reattach/reuse.
    """
    attach_budget(ib, deadline)
    if configured_timeout is not None:
        setattr(ib, "_thetamill_configured_timeout", configured_timeout)

    cls = type(ib)
    run_kind = _classify_class_attr(cls, "_run")
    sleep_kind = _classify_class_attr(cls, "sleep")

    if run_kind in ("missing", "other") and sleep_kind in ("missing", "other"):
        return

    if getattr(ib, "_thetamill_budget_wrapped", False) is True:
        return

    installed_run = False
    installed_sleep = False
    had_instance_run = "_run" in ib.__dict__
    had_instance_sleep = "sleep" in ib.__dict__
    original_run = getattr(ib, "_run", None)
    original_sleep = getattr(ib, "sleep", None)

    try:
        if run_kind == "property":
            raise TypeError("Cannot safely wrap property '_run' on IB instance")
        elif run_kind == "callable":
            def _wrapped_run(*awaitables: Any) -> Any:
                current_deadline = get_instance_budget(ib)
                if current_deadline is None:
                    current_deadline = get_active_budget()
                if current_deadline is None:
                    current_deadline = deadline

                cfg_timeout = getattr(ib, "_thetamill_configured_timeout", configured_timeout)
                has_timeout_attr = hasattr(ib, "RequestTimeout")
                prev_timeout = getattr(ib, "RequestTimeout", None) if has_timeout_attr else None
                should_restore = False

                try:
                    current_deadline.check()
                    rem = float(current_deadline.remaining())

                    candidates = [rem]
                    if (
                        cfg_timeout is not None
                        and not isinstance(cfg_timeout, bool)
                        and isinstance(cfg_timeout, (int, float))
                    ):
                        try:
                            cfg_f = float(cfg_timeout)
                            if math.isfinite(cfg_f) and cfg_f > 0:
                                candidates.append(cfg_f)
                        except (TypeError, ValueError, OverflowError):
                            pass

                    if (
                        prev_timeout is not None
                        and not isinstance(prev_timeout, bool)
                        and isinstance(prev_timeout, (int, float))
                    ):
                        try:
                            prev_f = float(prev_timeout)
                            if math.isfinite(prev_f) and prev_f > 0:
                                candidates.append(prev_f)
                        except (TypeError, ValueError, OverflowError):
                            pass

                    clamped = min(candidates)

                    if has_timeout_attr:
                        should_restore = True
                        try:
                            ib.RequestTimeout = float(clamped)
                            actual = getattr(ib, "RequestTimeout", None)
                            if (
                                isinstance(actual, bool)
                                or not isinstance(actual, (int, float))
                                or not math.isfinite(float(actual))
                                or float(actual) <= 0
                                or abs(float(actual) - float(clamped)) > 1e-6
                            ):
                                raise RuntimeError("RequestTimeout setter readback mismatch")
                        except Exception as exc:
                            raise RuntimeError(f"RequestTimeout assignment failed: {type(exc).__name__}") from None

                    result = original_run(*awaitables)
                    current_deadline.check()
                    return result
                except DeadlineExceededError:
                    _close_awaitables(awaitables)
                    raise
                except (asyncio.TimeoutError, TimeoutError) as exc:
                    _close_awaitables(awaitables)
                    for d in (current_deadline, get_active_budget()):
                        if d is not None:
                            try:
                                d.check()
                            except DeadlineExceededError as d_exc:
                                raise d_exc from exc
                    raise
                except BaseException:
                    _close_awaitables(awaitables)
                    raise
                finally:
                    if should_restore and has_timeout_attr:
                        try:
                            ib.RequestTimeout = prev_timeout
                            actual = getattr(ib, "RequestTimeout", None)
                            if isinstance(prev_timeout, (int, float)) and not isinstance(prev_timeout, bool):
                                if (
                                    isinstance(actual, bool)
                                    or not isinstance(actual, (int, float))
                                    or abs(float(actual) - float(prev_timeout)) > 1e-6
                                ):
                                    raise RuntimeError("RequestTimeout restoration readback mismatch")
                            elif actual != prev_timeout:
                                raise RuntimeError("RequestTimeout restoration readback mismatch")
                        except Exception as exc:
                            raise RuntimeError(f"RequestTimeout restoration failed: {type(exc).__name__}") from None

            ib._run = _wrapped_run
            installed_run = True
            if getattr(ib, "_run", None) is not _wrapped_run:
                raise RuntimeError("Failed to install budget _run wrapper on IB instance")

        if sleep_kind == "property":
            raise TypeError("Cannot safely wrap property 'sleep' on IB instance")
        elif sleep_kind == "callable":
            def _wrapped_sleep(secs: float = 0.02, *args: Any, **kwargs: Any) -> Any:
                if isinstance(secs, bool) or not isinstance(secs, (int, float)):
                    raise TypeError("sleep duration must be a finite non-negative number")
                try:
                    secs_val = float(secs)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(f"sleep duration is invalid ({type(exc).__name__})") from None
                if not math.isfinite(secs_val) or secs_val < 0:
                    raise ValueError("sleep duration must be a finite non-negative number")

                current_deadline = get_instance_budget(ib)
                if current_deadline is None:
                    current_deadline = get_active_budget()
                if current_deadline is None:
                    current_deadline = deadline

                current_deadline.check()
                rem = float(current_deadline.remaining())
                clamped = min(secs_val, rem)

                res = original_sleep(clamped, *args, **kwargs)
                current_deadline.check()
                return res

            ib.sleep = _wrapped_sleep
            installed_sleep = True
            if getattr(ib, "sleep", None) is not _wrapped_sleep:
                raise RuntimeError("Failed to install budget sleep wrapper on IB instance")

        setattr(ib, "_thetamill_budget_wrapped", True)
        if getattr(ib, "_thetamill_budget_wrapped", None) is not True:
            raise RuntimeError("Failed to install budget wrapper sentinel on IB instance")

    except BaseException:
        if installed_run:
            try:
                if had_instance_run:
                    ib._run = original_run
                else:
                    if "_run" in ib.__dict__:
                        delattr(ib, "_run")
            except Exception:
                pass
        if installed_sleep:
            try:
                if had_instance_sleep:
                    ib.sleep = original_sleep
                else:
                    if "sleep" in ib.__dict__:
                        delattr(ib, "sleep")
            except Exception:
                pass
        for attr in ("_thetamill_budget_wrapped", "_thetamill_configured_timeout", BUDGET_ATTR, SENTINEL_ATTR):
            try:
                if hasattr(ib, attr):
                    delattr(ib, attr)
            except Exception:
                pass
        raise
