"""Deterministic unit tests for thetamill.broker_requests.

Tests run completely offline against real IB and Contract objects with a fake
transport, avoiding all live network and broker connectivity.
"""
from __future__ import annotations

import asyncio
import sys
import time
import unittest
from typing import Any

from ib_async import Client, Contract, IB

from thetamill.broker_requests import (
    DEFAULT_REQUEST_TIMEOUT,
    Deadline,
    DeadlineExceededError,
    resolve_bounded_timeout,
    safe_req_tickers,
    safe_req_tickers_async,
)


class DeadlineTests(unittest.TestCase):
    """Tests for monotonic Deadline budgeting, clock validation, and bounds."""

    def test_deadline_basic_and_remaining(self):
        default_dl = Deadline(10.0)
        self.assertGreater(default_dl.remaining(), 0.0)

        current = 100.0
        dl = Deadline(10.0, clock=lambda: current)
        self.assertEqual(dl.budget, 10.0)
        self.assertEqual(dl.remaining(), 10.0)

        current = 106.5
        self.assertEqual(dl.remaining(), 3.5)
        dl.check()

        current = 110.0
        with self.assertRaises(DeadlineExceededError):
            dl.remaining()
        with self.assertRaises(DeadlineExceededError):
            dl.check()

    def test_deadline_sanitized_clock_exception(self):
        def bad_clock():
            raise RuntimeError("secret_key_data_12345")

        with self.assertRaises(ValueError) as ctx:
            Deadline(5.0, clock=bad_clock)
        self.assertNotIn("secret_key_data_12345", str(ctx.exception))
        self.assertIn("RuntimeError", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)

        curr = 100.0

        def fail_later():
            if curr > 100.0:
                raise KeyError("private_account_token")
            return curr

        dl = Deadline(5.0, clock=fail_later)
        curr = 101.0
        with self.assertRaises(ValueError) as ctx2:
            dl.remaining()
        self.assertNotIn("private_account_token", str(ctx2.exception))
        self.assertIn("KeyError", str(ctx2.exception))
        self.assertIsNone(ctx2.exception.__cause__)

    def test_deadline_budget_and_clock_validations(self):
        for bad_budget in (True, False, -1.0, 0, float("nan"), float("inf")):
            with self.subTest(bad_budget=bad_budget):
                with self.assertRaises(ValueError):
                    Deadline(bad_budget, clock=lambda: 100.0)

        with self.assertRaises(ValueError):
            Deadline(10**400, clock=lambda: 100.0)

        for bad_clock_val in (True, False, float("nan"), float("inf")):
            with self.subTest(bad_clock_val=bad_clock_val):
                with self.assertRaises(ValueError):
                    Deadline(5.0, clock=lambda: bad_clock_val)

        max_float = sys.float_info.max
        with self.assertRaises(ValueError):
            Deadline(max_float, clock=lambda: max_float)

    def test_deadline_bounded_timeout_never_widens(self):
        current = 100.0
        dl = Deadline(10.0, clock=lambda: current)

        self.assertEqual(dl.bounded_timeout(4.0), 4.0)
        self.assertEqual(dl.bounded_timeout(15.0), 10.0)
        self.assertEqual(dl.bounded_timeout(None), 10.0)

        current = 108.0
        self.assertEqual(dl.bounded_timeout(4.0), 2.0)

        for bad in (True, -1.0, 0, float("nan")):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    dl.bounded_timeout(bad)


class ResolveBoundedTimeoutTests(unittest.TestCase):
    """Tests for resolve_bounded_timeout combining explicit, ib, and deadline bounds."""

    def test_resolve_bounded_timeout_defaults_and_overflow(self):
        class MockIB:
            RequestTimeout = 0

        ib = MockIB()
        # Default finite 20s when no explicit timeout and RequestTimeout is 0
        self.assertEqual(resolve_bounded_timeout(timeout=None, ib=ib), DEFAULT_REQUEST_TIMEOUT)

        # Huge float conversion overflow in ib.RequestTimeout falls back cleanly
        ib.RequestTimeout = 10**400
        self.assertEqual(resolve_bounded_timeout(timeout=None, ib=ib), DEFAULT_REQUEST_TIMEOUT)

        # Stricter IB RequestTimeout is respected
        ib.RequestTimeout = 5.0
        self.assertEqual(resolve_bounded_timeout(timeout=10.0, ib=ib), 5.0)

        # Stricter caller timeout is respected
        ib.RequestTimeout = 30.0
        self.assertEqual(resolve_bounded_timeout(timeout=10.0, ib=ib), 10.0)

        # Mutable clock advancing tests deadline expiration
        t = [100.0]
        dl = Deadline(1.0, clock=lambda: t[0])
        t[0] = 105.0  # clock advances past budget
        with self.assertRaises(DeadlineExceededError):
            resolve_bounded_timeout(timeout=10.0, deadline=dl)


class SafeReqTickersAsyncTests(unittest.IsolatedAsyncioTestCase):
    """Async integration tests using real IB and Contract objects with fake transport."""

    def setUp(self):
        self.ib = IB()
        self.written_messages: list[bytes] = []

        class FakeTransport:
            def __init__(self, target_list: list[bytes]):
                self._target = target_list
                self._closing = False

            def write(self, data: bytes) -> None:
                self._target.append(data)

            def is_closing(self) -> bool:
                return self._closing

            def close(self) -> None:
                self._closing = True

            def write_eof(self) -> None:
                pass

        self.transport = FakeTransport(self.written_messages)
        self.ib.client.conn.transport = self.transport
        self.ib.client.connState = Client.CONNECTED
        self.ib.client._apiReady = True
        self.ib.client._startTime = time.time()
        self.ib.client._serverVersion = self.ib.client.MaxClientVersion

    def tearDown(self):
        try:
            self.ib.disconnect()
        except Exception:
            pass

    async def test_protocol_version_and_wire_req_ids(self):
        self.assertEqual(self.ib.client.serverVersion(), Client.MaxClientVersion)
        self.assertTrue(Client.MinClientVersion <= self.ib.client.serverVersion() <= Client.MaxClientVersion)

        c = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
        real_start_req = self.ib.wrapper.startReq

        def auto_complete(key: int, contract: Any = None, container: Any = None) -> Any:
            fut = real_start_req(key, contract, container)
            fut.set_result([])
            return fut

        self.ib.wrapper.startReq = auto_complete

        tickers = await safe_req_tickers_async(self.ib, c, timeout=5.0)
        self.assertEqual(len(tickers), 1)
        self.assertEqual(len(self.written_messages), 2)  # reqMktData + cancelMktData cleanup
        self.assertIn(b"\x000\x001001\x00AAPL\x00", self.written_messages[0])
        self.assertEqual(len(self.ib.wrapper._futures), 0)
        self.assertEqual(len(self.ib.wrapper.reqId2Ticker), 0)
        self.assertEqual(len(self.ib.wrapper._reqId2Contract), 0)
        self.assertEqual(len(self.ib.wrapper.ticker2ReqId.get("snapshot", {})), 0)

    async def test_empty_contracts_returns_immediately(self):
        res = await safe_req_tickers_async(self.ib)
        self.assertEqual(res, [])
        self.assertEqual(len(self.written_messages), 0)

    async def test_timeout_cancels_wire_and_cleans_own_records(self):
        c = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
        cancelled_req_ids: list[int] = []
        original_cancel = self.ib.client.cancelMktData

        def spy_cancel(req_id: int) -> None:
            cancelled_req_ids.append(req_id)
            original_cancel(req_id)

        self.ib.client.cancelMktData = spy_cancel

        with self.assertRaises((asyncio.TimeoutError, TimeoutError)):
            await safe_req_tickers_async(self.ib, c, timeout=0.02)

        self.assertEqual(cancelled_req_ids, [0])
        self.assertEqual(len(self.ib.wrapper._futures), 0)
        self.assertEqual(len(self.ib.wrapper.reqId2Ticker), 0)
        self.assertEqual(len(self.ib.wrapper._reqId2Contract), 0)
        self.assertEqual(len(self.ib.wrapper._results), 0)
        self.assertEqual(len(self.ib.wrapper.ticker2ReqId.get("snapshot", {})), 0)

    async def test_explicit_task_cancellation_cleans_resources_and_cancels_wire(self):
        c = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
        cancelled_req_ids: list[int] = []
        original_cancel = self.ib.client.cancelMktData

        def spy_cancel(req_id: int) -> None:
            cancelled_req_ids.append(req_id)
            original_cancel(req_id)

        self.ib.client.cancelMktData = spy_cancel

        task = asyncio.create_task(safe_req_tickers_async(self.ib, c, timeout=10.0))
        await asyncio.sleep(0.005)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(cancelled_req_ids, [0])
        self.assertEqual(len(self.ib.wrapper._futures), 0)
        self.assertEqual(len(self.ib.wrapper.reqId2Ticker), 0)
        self.assertEqual(len(self.ib.wrapper._reqId2Contract), 0)
        self.assertEqual(len(self.ib.wrapper._results), 0)
        self.assertEqual(len(self.ib.wrapper.ticker2ReqId.get("snapshot", {})), 0)

    async def test_partial_start_req_failure_cleans_all_bookkeeping(self):
        c1 = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
        c2 = Contract(conId=1002, symbol="MSFT", secType="STK", exchange="SMART", currency="USD")
        real_start_req = self.ib.wrapper.startReq
        call_count = 0

        def failing_start_req(key: int, contract: Any = None, container: Any = None) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("startReq failed on second contract")
            return real_start_req(key, contract, container)

        self.ib.wrapper.startReq = failing_start_req

        with self.assertRaises(RuntimeError) as ctx:
            await safe_req_tickers_async(self.ib, c1, c2, timeout=5.0)

        self.assertIn("startReq failed on second contract", str(ctx.exception))
        self.assertEqual(len(self.ib.wrapper._futures), 0)
        self.assertEqual(len(self.ib.wrapper.reqId2Ticker), 0)
        self.assertEqual(len(self.ib.wrapper._reqId2Contract), 0)
        self.assertEqual(len(self.ib.wrapper._results), 0)
        self.assertEqual(len(self.ib.wrapper.ticker2ReqId.get("snapshot", {})), 0)

    async def test_partial_start_ticker_failure_cleans_all_bookkeeping(self):
        c1 = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
        c2 = Contract(conId=1002, symbol="MSFT", secType="STK", exchange="SMART", currency="USD")
        real_start_ticker = self.ib.wrapper.startTicker
        call_count = 0

        def failing_start_ticker(req_id: int, contract: Any, tick_type: Any) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("startTicker failed on second contract")
            return real_start_ticker(req_id, contract, tick_type)

        self.ib.wrapper.startTicker = failing_start_ticker

        with self.assertRaises(RuntimeError) as ctx:
            await safe_req_tickers_async(self.ib, c1, c2, timeout=5.0)

        self.assertIn("startTicker failed on second contract", str(ctx.exception))
        self.assertEqual(len(self.ib.wrapper._futures), 0)
        self.assertEqual(len(self.ib.wrapper.reqId2Ticker), 0)
        self.assertEqual(len(self.ib.wrapper._reqId2Contract), 0)
        self.assertEqual(len(self.ib.wrapper._results), 0)
        self.assertEqual(len(self.ib.wrapper.ticker2ReqId.get("snapshot", {})), 0)

    async def test_start_ticker_mutate_then_throw_cleans_snapshot_map(self):
        unrelated_c = Contract(conId=8888, symbol="IBM", secType="STK", exchange="SMART", currency="USD")
        unrelated_ticker = self.ib.wrapper.startTicker(888, unrelated_c, "snapshot")

        c = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
        real_start_ticker = self.ib.wrapper.startTicker

        def mutate_then_throw(req_id: int, contract: Any, tick_type: Any) -> Any:
            real_start_ticker(req_id, contract, tick_type)
            raise RuntimeError("simulated error after startTicker mutation")

        self.ib.wrapper.startTicker = mutate_then_throw

        with self.assertRaises(RuntimeError) as ctx:
            await safe_req_tickers_async(self.ib, c, timeout=5.0)

        self.assertIn("simulated error after startTicker mutation", str(ctx.exception))
        snapshot_map = self.ib.wrapper.ticker2ReqId.get("snapshot", {})
        self.assertNotIn(0, snapshot_map.values())
        self.assertEqual(snapshot_map.get(unrelated_ticker), 888)
        self.assertNotIn(0, self.ib.wrapper.reqId2Ticker)
        self.assertIn(888, self.ib.wrapper.reqId2Ticker)
        self.assertIs(self.ib.wrapper.reqId2Ticker[888], unrelated_ticker)
        self.assertEqual(len(self.ib.wrapper._futures), 0)

    async def test_preserve_concurrent_same_contract_request(self):
        c = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
        newer_req_id = 999
        newer_fut: Any = None
        newer_ticker: Any = None

        original_req_mkt_data = self.ib.client.reqMktData

        def on_req_mkt_data(req_id: int, contract: Any, *args: Any) -> None:
            nonlocal newer_fut, newer_ticker
            original_req_mkt_data(req_id, contract, *args)
            newer_fut = self.ib.wrapper.startReq(newer_req_id, contract)
            newer_ticker = self.ib.wrapper.startTicker(newer_req_id, contract, "snapshot")

        self.ib.client.reqMktData = on_req_mkt_data

        with self.assertRaises((asyncio.TimeoutError, TimeoutError)):
            await safe_req_tickers_async(self.ib, c, timeout=0.02)

        self.assertNotIn(0, self.ib.wrapper._futures)
        self.assertNotIn(0, self.ib.wrapper.reqId2Ticker)
        self.assertNotIn(0, self.ib.wrapper._reqId2Contract)

        self.assertIn(newer_req_id, self.ib.wrapper._futures)
        self.assertIs(self.ib.wrapper._futures[newer_req_id], newer_fut)
        self.assertIn(newer_req_id, self.ib.wrapper.reqId2Ticker)
        self.assertIs(self.ib.wrapper.reqId2Ticker[newer_req_id], newer_ticker)
        self.assertIn(newer_req_id, self.ib.wrapper._reqId2Contract)
        self.assertIs(self.ib.wrapper._reqId2Contract[newer_req_id], c)
        self.assertEqual(self.ib.wrapper.ticker2ReqId["snapshot"][newer_ticker], newer_req_id)

    async def test_preserve_concurrent_different_contract_request(self):
        other_c = Contract(conId=9999, symbol="GOOG", secType="STK", exchange="SMART", currency="USD")
        other_fut = self.ib.wrapper.startReq(999, other_c)
        other_ticker = self.ib.wrapper.startTicker(999, other_c, "snapshot")

        c = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
        with self.assertRaises((asyncio.TimeoutError, TimeoutError)):
            await safe_req_tickers_async(self.ib, c, timeout=0.02)

        self.assertNotIn(0, self.ib.wrapper._futures)
        self.assertNotIn(0, self.ib.wrapper.reqId2Ticker)
        self.assertNotIn(0, self.ib.wrapper._reqId2Contract)

        self.assertIn(999, self.ib.wrapper._futures)
        self.assertIs(self.ib.wrapper._futures[999], other_fut)
        self.assertIn(999, self.ib.wrapper.reqId2Ticker)
        self.assertIs(self.ib.wrapper.reqId2Ticker[999], other_ticker)
        self.assertIn(999, self.ib.wrapper._reqId2Contract)
        self.assertIs(self.ib.wrapper._reqId2Contract[999], other_c)
        self.assertEqual(self.ib.wrapper.ticker2ReqId["snapshot"][other_ticker], 999)

    async def test_deadline_expired_raises_deadline_exceeded_error(self):
        t = [100.0]
        dl = Deadline(1.0, clock=lambda: t[0])
        c = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
        t[0] = 105.0
        with self.assertRaises(DeadlineExceededError):
            await safe_req_tickers_async(self.ib, c, timeout=10.0, deadline=dl)

    async def test_transport_writes_then_raises_cancels_all_attempted_wire_req_ids(self):
        c1 = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
        c2 = Contract(conId=1002, symbol="MSFT", secType="STK", exchange="SMART", currency="USD")
        cancelled_req_ids: list[int] = []
        original_cancel = self.ib.client.cancelMktData

        def spy_cancel(req_id: int) -> None:
            cancelled_req_ids.append(req_id)
            original_cancel(req_id)

        self.ib.client.cancelMktData = spy_cancel

        original_write = self.transport.write

        def write_side_effect(data: bytes) -> None:
            original_write(data)
            if len(self.written_messages) == 2:
                raise OSError("Socket error after transmitting second request bytes")

        self.transport.write = write_side_effect

        with self.assertRaises(OSError) as ctx:
            await safe_req_tickers_async(self.ib, c1, c2, timeout=5.0)

        self.assertIn("Socket error after transmitting second request bytes", str(ctx.exception))
        self.assertEqual(cancelled_req_ids, [0, 1])
        self.assertEqual(len(self.ib.wrapper._futures), 0)
        self.assertEqual(len(self.ib.wrapper.reqId2Ticker), 0)
        self.assertEqual(len(self.ib.wrapper._reqId2Contract), 0)

    def test_sync_safe_req_tickers(self):
        c = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
        with self.assertRaises((asyncio.TimeoutError, TimeoutError)):
            safe_req_tickers(self.ib, c, timeout=0.03)

        self.assertEqual(len(self.ib.wrapper._futures), 0)
        self.assertEqual(len(self.ib.wrapper.reqId2Ticker), 0)
        self.assertEqual(len(self.ib.wrapper._reqId2Contract), 0)
        self.assertEqual(len(self.ib.wrapper._results), 0)
        self.assertEqual(len(self.ib.wrapper.ticker2ReqId.get("snapshot", {})), 0)

    def test_sync_safe_req_tickers_attached_budget_bounds_timeout(self):
        from thetamill.command_budget import attach_budget

        c = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
        dl = Deadline(0.02)
        attach_budget(self.ib, dl)
        self.ib.RequestTimeout = 30.0

        cancelled_req_ids: list[int] = []
        original_cancel = self.ib.client.cancelMktData

        def spy_cancel(req_id: int) -> None:
            cancelled_req_ids.append(req_id)
            original_cancel(req_id)

        self.ib.client.cancelMktData = spy_cancel

        start_time = time.monotonic()
        with self.assertRaises(DeadlineExceededError):
            safe_req_tickers(self.ib, c, timeout=1.0)
        elapsed = time.monotonic() - start_time

        self.assertLess(elapsed, 0.5)
        self.assertEqual(cancelled_req_ids, [0])
        self.assertEqual(len(self.ib.wrapper._futures), 0)
        self.assertEqual(len(self.ib.wrapper.reqId2Ticker), 0)
        self.assertEqual(len(self.ib.wrapper._reqId2Contract), 0)
        self.assertEqual(len(self.ib.wrapper._results), 0)
        self.assertEqual(len(self.ib.wrapper.ticker2ReqId.get("snapshot", {})), 0)
        self.assertEqual(len(self.ib.trades()), 0)
        self.assertEqual(len(self.ib.orders()), 0)

    def test_sync_safe_req_tickers_tighter_explicit_than_attached(self):
        from thetamill.command_budget import attach_budget

        c = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
        attached_dl = Deadline(10.0)
        attach_budget(self.ib, attached_dl)
        explicit_dl = Deadline(0.02)
        self.ib.RequestTimeout = 30.0

        cancelled_req_ids: list[int] = []
        original_cancel = self.ib.client.cancelMktData

        def spy_cancel(req_id: int) -> None:
            cancelled_req_ids.append(req_id)
            original_cancel(req_id)

        self.ib.client.cancelMktData = spy_cancel

        start_time = time.monotonic()
        with self.assertRaises(DeadlineExceededError):
            safe_req_tickers(self.ib, c, timeout=1.0, deadline=explicit_dl)
        elapsed = time.monotonic() - start_time

        self.assertLess(elapsed, 0.5)
        self.assertGreater(attached_dl.remaining(), 5.0)
        self.assertEqual(cancelled_req_ids, [0])
        self.assertEqual(len(self.ib.wrapper._futures), 0)
        self.assertEqual(len(self.ib.wrapper.reqId2Ticker), 0)
        self.assertEqual(len(self.ib.wrapper._reqId2Contract), 0)
        self.assertEqual(len(self.ib.wrapper._results), 0)
        self.assertEqual(len(self.ib.wrapper.ticker2ReqId.get("snapshot", {})), 0)

    async def test_successful_response_crossing_injected_deadline_postawait_rejected_with_cleanup(self):
        curr_time = 100.0

        def injected_clock() -> float:
            return curr_time

        dl = Deadline(5.0, clock=injected_clock)
        c = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")

        real_start_req = self.ib.wrapper.startReq

        def complete_and_advance_clock(key: int, contract: Any = None, container: Any = None) -> Any:
            fut = real_start_req(key, contract, container)
            fut.set_result([])
            nonlocal curr_time
            curr_time = 110.0
            return fut

        self.ib.wrapper.startReq = complete_and_advance_clock

        cancelled_req_ids: list[int] = []
        original_cancel = self.ib.client.cancelMktData

        def spy_cancel(req_id: int) -> None:
            cancelled_req_ids.append(req_id)
            original_cancel(req_id)

        self.ib.client.cancelMktData = spy_cancel

        with self.assertRaises(DeadlineExceededError):
            await safe_req_tickers_async(self.ib, c, timeout=10.0, deadline=dl)

        self.assertEqual(cancelled_req_ids, [0])
        self.assertEqual(len(self.ib.wrapper._futures), 0)
        self.assertEqual(len(self.ib.wrapper.reqId2Ticker), 0)
        self.assertEqual(len(self.ib.wrapper._reqId2Contract), 0)
        self.assertEqual(len(self.ib.wrapper._results), 0)
        self.assertEqual(len(self.ib.wrapper.ticker2ReqId.get("snapshot", {})), 0)

    def test_no_magicmock_phantom_budgets(self):
        from unittest.mock import MagicMock
        from thetamill.broker_requests import _get_instance_budget

        mock_ib = MagicMock()
        self.assertIsNone(_get_instance_budget(mock_ib))

        resolved = resolve_bounded_timeout(timeout=10.0, ib=mock_ib)
        self.assertEqual(resolved, 10.0)

    async def test_preexpired_deadline_zero_wire_allocation(self):
        t = [100.0]
        dl = Deadline(1.0, clock=lambda: t[0])
        t[0] = 105.0

        c = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")

        req_id_calls = 0
        orig_get_req_id = self.ib.client.getReqId

        def spy_get_req_id() -> int:
            nonlocal req_id_calls
            req_id_calls += 1
            return orig_get_req_id()

        self.ib.client.getReqId = spy_get_req_id

        with self.assertRaises(DeadlineExceededError):
            await safe_req_tickers_async(self.ib, c, timeout=10.0, deadline=dl)

        self.assertEqual(req_id_calls, 0)
        self.assertEqual(len(self.written_messages), 0)
        self.assertEqual(len(self.ib.wrapper._futures), 0)
        self.assertEqual(len(self.ib.wrapper.reqId2Ticker), 0)
        self.assertEqual(len(self.ib.wrapper._reqId2Contract), 0)


if __name__ == "__main__":
    unittest.main()
