"""Safe bounded market-data requests and deadline primitives for IB.

Contract verified only against ib_async 2.1.0.

Provides:
- Deadline: monotonic execution budget with injected clock and overflow protection.
- resolve_bounded_timeout: combines caller timeout, ib.RequestTimeout, and deadline.
- safe_req_tickers_async / safe_req_tickers: snapshot requester with strict cleanup
  on success, timeout, cancellation, or wire failure.
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from ib_async import IB, Contract, Ticker

DEFAULT_REQUEST_TIMEOUT: float = 20.0

__all__ = [
    "DEFAULT_REQUEST_TIMEOUT",
    "DeadlineExceededError",
    "Deadline",
    "resolve_bounded_timeout",
    "safe_req_tickers_async",
    "safe_req_tickers",
]


class DeadlineExceededError(TimeoutError):
    """Raised when an operation's monotonic execution budget has expired."""


class Deadline:
    """Monotonic execution deadline with an injected clock."""

    def __init__(
        self,
        budget: float | int,
        *,
        clock: Any = None,
    ) -> None:
        if isinstance(budget, bool) or not isinstance(budget, (int, float)):
            raise ValueError("Budget must be a finite positive non-bool number")
        try:
            budget_val = float(budget)
        except (OverflowError, ValueError):
            raise ValueError("Budget is too large or invalid") from None

        if not math.isfinite(budget_val) or budget_val <= 0:
            raise ValueError("Budget must be a finite positive non-bool number")

        if clock is None:
            clock = time.monotonic
        self._clock = clock
        try:
            start = clock()
        except Exception as exc:
            raise ValueError(f"Clock invocation failed ({type(exc).__name__})") from None

        if isinstance(start, bool) or not isinstance(start, (int, float)):
            raise ValueError("Clock must return a finite number")
        try:
            start_val = float(start)
        except (OverflowError, ValueError):
            raise ValueError("Clock return value overflowed") from None

        if not math.isfinite(start_val):
            raise ValueError("Clock must return a finite number")

        try:
            end = start_val + budget_val
        except (OverflowError, ValueError):
            raise ValueError("start + budget overflowed") from None

        if not math.isfinite(end):
            raise ValueError("start + budget cannot overflow to inf")

        self._start = start_val
        self._budget = budget_val
        self._end = end

    @property
    def budget(self) -> float:
        return self._budget

    def remaining(self) -> float:
        """Return remaining seconds. Raises DeadlineExceededError if expired."""
        try:
            now = self._clock()
        except Exception as exc:
            raise ValueError(f"Clock invocation failed ({type(exc).__name__})") from None

        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise ValueError("Clock must return a finite number")
        try:
            now_val = float(now)
        except (OverflowError, ValueError):
            raise ValueError("Clock return value overflowed") from None

        if not math.isfinite(now_val):
            raise ValueError("Clock must return a finite number")

        rem = self._end - now_val
        if rem <= 0:
            raise DeadlineExceededError("Command deadline exceeded")
        return rem

    def check(self) -> None:
        """Verify the deadline has not expired; raises DeadlineExceededError if it has."""
        self.remaining()

    def bounded_timeout(self, timeout: float | int | None = None) -> float:
        """Return min(timeout, remaining). Never widens caller's timeout or deadline."""
        rem = self.remaining()
        if timeout is None:
            return rem
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("Timeout must be a finite positive non-bool number")
        try:
            t_val = float(timeout)
        except (OverflowError, ValueError):
            raise ValueError("Timeout overflowed") from None
        if not math.isfinite(t_val) or t_val <= 0:
            raise ValueError("Timeout must be a finite positive non-bool number")
        return min(t_val, rem)


def _get_instance_budget(ib: Any) -> Deadline | None:
    if ib is None:
        return None
    from .command_budget import get_instance_budget
    return get_instance_budget(ib)


def _get_active_budget() -> Deadline | None:
    from .command_budget import get_active_budget
    return get_active_budget()


def _collect_deadlines(
    *,
    ib: Any = None,
    deadline: Deadline | None = None,
    deadlines: list[Deadline] | tuple[Deadline, ...] | None = None,
) -> list[Deadline]:
    result: list[Deadline] = []
    seen: set[int] = set()

    def _add(d: Any) -> None:
        if isinstance(d, Deadline) and id(d) not in seen:
            seen.add(id(d))
            result.append(d)

    _add(deadline)
    if deadlines:
        for d in deadlines:
            _add(d)
    _add(_get_instance_budget(ib))
    _add(_get_active_budget())
    return result


def resolve_bounded_timeout(
    timeout: float | int | None = None,
    *,
    ib: Any = None,
    deadline: Deadline | None = None,
    deadlines: list[Deadline] | tuple[Deadline, ...] | None = None,
    default: float | int | None = DEFAULT_REQUEST_TIMEOUT,
) -> float | None:
    """Resolve effective request timeout adhering strictly to caller and deadline bounds.

    Combines min(default/explicit, valid IB.RequestTimeout, deadline) without widening
    either the caller bound or the deadline. Ignores non-finite/overflowing IB values.
    """
    caller_timeout: float | None = None
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("Timeout must be a finite positive non-bool number")
        try:
            t_val = float(timeout)
        except (OverflowError, ValueError):
            raise ValueError("Timeout overflowed") from None
        if not math.isfinite(t_val) or t_val <= 0:
            raise ValueError("Timeout must be a finite positive non-bool number")
        caller_timeout = t_val
    elif default is not None:
        if isinstance(default, bool) or not isinstance(default, (int, float)):
            raise ValueError("Default must be a finite positive non-bool number")
        try:
            d_val = float(default)
        except (OverflowError, ValueError):
            raise ValueError("Default overflowed") from None
        if not math.isfinite(d_val) or d_val <= 0:
            raise ValueError("Default must be a finite positive non-bool number")
        caller_timeout = d_val

    ib_timeout: float | None = None
    if ib is not None:
        raw_ib = getattr(ib, "RequestTimeout", None)
        if not isinstance(raw_ib, bool) and isinstance(raw_ib, (int, float)):
            try:
                val = float(raw_ib)
                if math.isfinite(val) and val > 0:
                    ib_timeout = val
            except (OverflowError, ValueError, TypeError):
                ib_timeout = None

    if caller_timeout is not None and ib_timeout is not None:
        base_timeout = min(caller_timeout, ib_timeout)
    elif caller_timeout is not None:
        base_timeout = caller_timeout
    elif ib_timeout is not None:
        base_timeout = ib_timeout
    else:
        base_timeout = None

    all_dls = _collect_deadlines(ib=ib, deadline=deadline, deadlines=deadlines)

    min_rem: float | None = None
    for d in all_dls:
        rem = d.remaining()
        if min_rem is None or rem < min_rem:
            min_rem = rem

    if min_rem is not None:
        if base_timeout is not None:
            return min(base_timeout, min_rem)
        return min_rem

    return base_timeout


async def safe_req_tickers_async(
    ib: IB,
    *contracts: Contract,
    regulatory_snapshot: bool = False,
    timeout: float | int | None = None,
    deadline: Deadline | None = None,
) -> list[Ticker]:
    """Request snapshot tickers with bounded timeout and guaranteed cleanup.

    Follows actual ib_async 2.1.0 reqTickersAsync semantics with safe cleanup
    on success, timeout, cancellation, or wire failure.
    """
    if not contracts:
        return []

    active_deadlines = _collect_deadlines(ib=ib, deadline=deadline)

    # Pre-check all deadlines before any request, getReqId, or wire allocation
    for d in active_deadlines:
        d.check()

    effective_timeout = resolve_bounded_timeout(
        timeout=timeout,
        ib=ib,
        deadlines=active_deadlines,
        default=DEFAULT_REQUEST_TIMEOUT,
    )

    req_ids: list[int] = []
    req_tickers: list[tuple[int, Ticker]] = []
    futures: list[asyncio.Future] = []
    attempted_wire_req_ids: list[int] = []

    gather_fut: asyncio.Future[Any] | None = None
    original_error: BaseException | None = None

    try:
        for contract in contracts:
            req_id = ib.client.getReqId()
            req_ids.append(req_id)

            future = ib.wrapper.startReq(req_id, contract)
            futures.append(future)

            ticker = ib.wrapper.startTicker(req_id, contract, "snapshot")
            req_tickers.append((req_id, ticker))

            attempted_wire_req_ids.append(req_id)
            ib.client.reqMktData(req_id, contract, "", True, regulatory_snapshot, [])

        gather_fut = asyncio.gather(*futures)
        try:
            if effective_timeout is not None:
                await asyncio.wait_for(gather_fut, timeout=effective_timeout)
            else:
                await gather_fut
        except (asyncio.TimeoutError, TimeoutError) as exc:
            for d in active_deadlines:
                try:
                    d.check()
                except DeadlineExceededError as d_exc:
                    raise d_exc from exc
            raise
        except asyncio.CancelledError:
            if gather_fut is not None and not gather_fut.done():
                gather_fut.cancel()
            raise

        # Check deadlines post-await even on success
        for d in active_deadlines:
            d.check()

        return [t for _, t in req_tickers]

    except BaseException as exc:
        original_error = exc
        raise

    finally:
        cleanup_errors: list[BaseException] = []

        # 1. Wire cancellation for all attempted request IDs
        for req_id in attempted_wire_req_ids:
            try:
                if ib.client.isConnected():
                    ib.client.cancelMktData(req_id)
            except Exception as e:
                cleanup_errors.append(e)

        # 2. End tickers only when the snapshot map still maps this ticker to our owned reqId,
        # preserving newer concurrent snapshot requests on the same contract.
        snapshot_map = ib.wrapper.ticker2ReqId.get("snapshot")
        for req_id, ticker in req_tickers:
            try:
                if snapshot_map is not None and snapshot_map.get(ticker) == req_id:
                    ib.wrapper.endTicker(ticker, "snapshot")
            except Exception as e:
                cleanup_errors.append(e)

        if snapshot_map is not None:
            owned_req_ids = set(req_ids)
            for ticker, req_id in list(snapshot_map.items()):
                if req_id in owned_req_ids:
                    try:
                        ib.wrapper.endTicker(ticker, "snapshot")
                    except Exception as e:
                        cleanup_errors.append(e)

        # 3. Clean up all owned wrapper mappings for our reqIds only
        for req_id in req_ids:
            try:
                ib.wrapper.reqId2Ticker.pop(req_id, None)
            except Exception as e:
                cleanup_errors.append(e)
            try:
                ib.wrapper._reqId2Contract.pop(req_id, None)
            except Exception as e:
                cleanup_errors.append(e)
            try:
                fut = ib.wrapper._futures.pop(req_id, None)
                if fut is not None and not fut.done():
                    fut.cancel()
            except Exception as e:
                cleanup_errors.append(e)
            try:
                ib.wrapper._results.pop(req_id, None)
            except Exception as e:
                cleanup_errors.append(e)

        # 4. Clean up gathering future state to prevent unhandled exception warnings
        if gather_fut is not None:
            if not gather_fut.done():
                gather_fut.cancel()
            try:
                if gather_fut.done() and not gather_fut.cancelled():
                    gather_fut.exception()
            except (asyncio.CancelledError, Exception):
                pass

        if original_error is None and cleanup_errors:
            raise cleanup_errors[0]


def safe_req_tickers(
    ib: IB,
    *contracts: Contract,
    regulatory_snapshot: bool = False,
    timeout: float | int | None = None,
    deadline: Deadline | None = None,
) -> list[Ticker]:
    """Synchronous snapshot request wrapping safe_req_tickers_async via util.run."""
    from ib_async import util

    return util.run(
        safe_req_tickers_async(
            ib,
            *contracts,
            regulatory_snapshot=regulatory_snapshot,
            timeout=timeout,
            deadline=deadline,
        )
    )
