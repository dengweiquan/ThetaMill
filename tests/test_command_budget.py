"""Deterministic tests for monotonic command budgeting, IB._run wrapping, and failure paths."""
import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from ib_async import IB, Client, Stock
from thetamill import cli, command_budget as cb, execution, risk, state, submission_cli as sc
from thetamill.broker_state import Instrument, Snapshot


class InjectedClock:
    def __init__(self, start: float = 1000.0):
        self.current = float(start)

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += float(seconds)


class StrictFakeIB:
    def __init__(self, request_timeout: float = 30.0, step_advance: float = 0.0, clock=None):
        self._request_timeout = float(request_timeout)
        self.step_advance = float(step_advance)
        self.clock = clock
        self.run_calls = []
        self.timeouts_during_run = []
        self.place_calls = []
        self.cancel_calls = []
        self.sleep_calls = []
        self.disconnected = False

    @property
    def RequestTimeout(self):
        return self._request_timeout

    @RequestTimeout.setter
    def RequestTimeout(self, val):
        self._request_timeout = float(val)

    def _run(self, *awaitables):
        self.run_calls.append(awaitables)
        self.timeouts_during_run.append(self.RequestTimeout)
        if self.step_advance and self.clock:
            self.clock.advance(self.step_advance)
        return "success"

    def sleep(self, secs: float = 0.02):
        self.sleep_calls.append(secs)
        if self.step_advance and self.clock:
            self.clock.advance(self.step_advance)
        elif self.clock:
            self.clock.advance(secs)
        return True

    def managedAccounts(self):
        return ["U1"]

    def connect(self, *a, **k):
        return self._run()

    def accountSummary(self, *a, **k):
        return []

    def disconnect(self):
        self.disconnected = True

    def placeOrder(self, contract, order):
        self.place_calls.append((contract, order))
        trade = MagicMock()
        trade.orderStatus.status = "PendingSubmit"
        trade.order.orderId = 101
        trade.order.permId = 202
        return trade

    def cancelOrder(self, order):
        self.cancel_calls.append(order)


class TestCommandBudgetPrimitives(unittest.TestCase):
    def test_validate_command_timeout_fallback(self):
        self.assertEqual(cb.validate_command_timeout(120), 120.0)
        self.assertEqual(cb.validate_command_timeout(15.5), 15.5)
        for bad in (0, -1, float("nan"), float("inf"), True, False, "120", None, []):
            self.assertEqual(cb.validate_command_timeout(bad, default=120.0), 120.0)
        huge = 10**1000
        self.assertEqual(cb.validate_command_timeout(huge), cb.DEFAULT_COMMAND_TIMEOUT)
        self.assertEqual(cb.validate_command_timeout(huge, default=60.0), 60.0)
        self.assertEqual(cb.validate_command_timeout(45.0, default=huge), 45.0)
        self.assertEqual(cb.validate_command_timeout(huge, default=huge), cb.DEFAULT_COMMAND_TIMEOUT)

    def test_deadline_api_methods(self):
        clock = InjectedClock(100.0)
        d = cb.Deadline(budget=50.0, clock=clock)
        self.assertAlmostEqual(d.remaining(), 50.0)
        d.check()

        clock.advance(30.0)
        self.assertAlmostEqual(d.remaining(), 20.0)
        d.check()

        clock.advance(20.0)
        with self.assertRaises(cb.DeadlineExceededError):
            d.remaining()
        with self.assertRaises(cb.DeadlineExceededError):
            d.check()

    def test_command_budget_context_nesting(self):
        clock = InjectedClock(0.0)
        self.assertIsNone(cb.get_active_budget())
        with cb.command_budget(60.0, clock=clock) as outer:
            self.assertIs(cb.get_active_budget(), outer)
            clock.advance(10.0)
            with cb.command_budget(60.0, clock=clock) as inner:
                self.assertIs(inner, outer)
                self.assertAlmostEqual(inner.remaining(), 50.0)
        self.assertIsNone(cb.get_active_budget())

    def test_sentinel_avoids_magicmock_truthiness_trap(self):
        mock = MagicMock()
        self.assertIsNone(cb.get_instance_budget(mock))

        clock = InjectedClock(0.0)
        d = cb.Deadline(60.0, clock=clock)
        cb.attach_budget(mock, d)
        self.assertIs(cb.get_instance_budget(mock), d)

    def test_attach_budget_fails_closed(self):
        with self.assertRaises(TypeError):
            cb.attach_budget(StrictFakeIB(), "not-a-deadline")  # type: ignore


class TestIBRunWrapping(unittest.TestCase):
    def test_clamped_timeout_min_current_ceiling_remaining_and_shared_budget(self):
        clock = InjectedClock(0.0)
        deadline = cb.Deadline(budget=100.0, clock=clock)
        ib = StrictFakeIB(request_timeout=30.0, clock=clock)
        cb.wrap_ib_instance(ib, deadline, configured_timeout=40.0)

        ib._run()
        self.assertEqual(len(ib.timeouts_during_run), 1)
        self.assertEqual(ib.timeouts_during_run[-1], 30.0)
        self.assertEqual(ib.RequestTimeout, 30.0)

        clock.advance(40.0)
        ib.RequestTimeout = 50.0
        ib._run()
        self.assertEqual(len(ib.timeouts_during_run), 2)
        self.assertEqual(ib.timeouts_during_run[-1], 40.0)
        self.assertEqual(ib.RequestTimeout, 50.0)

        clock.advance(45.0)
        ib._run()
        self.assertEqual(len(ib.timeouts_during_run), 3)
        self.assertEqual(ib.timeouts_during_run[-1], 15.0)
        self.assertEqual(ib.RequestTimeout, 50.0)

        clock.advance(20.0)
        with self.assertRaises(cb.DeadlineExceededError):
            ib._run()

    def test_coroutine_closed_when_deadline_expired_pre_run(self):
        clock = InjectedClock(0.0)
        deadline = cb.Deadline(budget=10.0, clock=clock)
        ib = StrictFakeIB()
        cb.wrap_ib_instance(ib, deadline)
        clock.advance(15.0)

        async def coro_fn():
            return 1

        coro = coro_fn()
        with self.assertRaises(cb.DeadlineExceededError):
            ib._run(coro)
        with self.assertRaises(RuntimeError):
            asyncio.run(coro)

    def test_future_cancelled_when_deadline_expired_pre_run(self):
        clock = InjectedClock(0.0)
        deadline = cb.Deadline(budget=10.0, clock=clock)
        ib = StrictFakeIB()
        cb.wrap_ib_instance(ib, deadline)
        clock.advance(15.0)

        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            with self.assertRaises(cb.DeadlineExceededError):
                ib._run(fut)
            self.assertTrue(fut.cancelled())
        finally:
            loop.close()

    def test_timeout_setter_failure_restores_previous_timeout(self):
        class PartialSetterIB(StrictFakeIB):
            def __init__(self):
                super().__init__(request_timeout=30.0)
                self.set_attempts = 0

            @property
            def RequestTimeout(self):
                return self._request_timeout

            @RequestTimeout.setter
            def RequestTimeout(self, val):
                self.set_attempts += 1
                self._request_timeout = float(val)
                if self.set_attempts == 1:
                    raise ValueError("simulated setter failure")

        clock = InjectedClock(0.0)
        deadline = cb.Deadline(budget=50.0, clock=clock)
        ib = PartialSetterIB()
        cb.wrap_ib_instance(ib, deadline)

        with self.assertRaises(RuntimeError):
            ib._run()
        self.assertEqual(ib.RequestTimeout, 30.0)

    def test_wrap_ib_instance_fails_closed_on_readonly_run(self):
        class ReadOnlyRunIB:
            @property
            def _run(self):
                return lambda: None

        clock = InjectedClock(0.0)
        deadline = cb.Deadline(10.0, clock=clock)
        with self.assertRaises(Exception):
            cb.wrap_ib_instance(ReadOnlyRunIB(), deadline)

    def test_arbitrary_magicmock_phantom_run_not_wrapped(self):
        mock_ib = MagicMock()
        clock = InjectedClock(0.0)
        deadline = cb.Deadline(10.0, clock=clock)
        cb.wrap_ib_instance(mock_ib, deadline)
        self.assertNotIn("_thetamill_budget_wrapped", mock_ib.__dict__)
        self.assertNotIn("_run", mock_ib.__dict__)
        self.assertNotIn("sleep", mock_ib.__dict__)

    def test_reattach_reuse_updates_deadline_avoids_stale(self):
        clock = InjectedClock(0.0)
        d1 = cb.Deadline(budget=10.0, clock=clock)
        ib = StrictFakeIB(clock=clock)
        cb.wrap_ib_instance(ib, d1)

        ib._run()
        clock.advance(15.0)
        with self.assertRaises(cb.DeadlineExceededError):
            ib._run()

        d2 = cb.Deadline(budget=50.0, clock=clock)
        cb.wrap_ib_instance(ib, d2)
        ib._run()
        self.assertAlmostEqual(d2.remaining(), 50.0)

    def test_actual_ib_run_with_fake_transport(self):
        clock = InjectedClock(0.0)
        deadline = cb.Deadline(budget=30.0, clock=clock)
        ib = IB()

        class FakeTransport:
            def __init__(self):
                self._closing = False
                self.messages = []

            def write(self, data: bytes) -> None:
                self.messages.append(data)

            def is_closing(self) -> bool:
                return self._closing

            def close(self) -> None:
                self._closing = True

            def write_eof(self) -> None:
                pass

        transport = FakeTransport()
        ib.client.conn.transport = transport
        ib.client.connState = Client.CONNECTED
        ib.client._apiReady = True
        ib.client._serverVersion = ib.client.MaxClientVersion
        ib.RequestTimeout = 20.0

        try:
            cb.wrap_ib_instance(ib, deadline, configured_timeout=15.0)
            self.assertTrue(getattr(ib, "_thetamill_budget_wrapped", False))

            async def sample_coro():
                return 123

            res = ib._run(sample_coro())
            self.assertEqual(res, 123)
            self.assertEqual(ib.RequestTimeout, 20.0)

            clock.advance(35.0)
            fut = asyncio.get_event_loop().create_future()
            with self.assertRaises(cb.DeadlineExceededError):
                ib._run(fut)
            self.assertTrue(fut.cancelled())
        finally:
            ib.disconnect()

    def test_real_ib_offline_sleep_contract_bounded_wall_time(self):
        ib = IB()
        try:
            deadline = cb.Deadline(budget=0.02)
            cb.wrap_ib_instance(ib, deadline)
            t0 = time.monotonic()
            with self.assertRaises(cb.DeadlineExceededError):
                ib.sleep(2.0)
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 0.5)
        finally:
            ib.disconnect()

    def test_sleep_exact_clamping_fake_clock(self):
        clock = InjectedClock(0.0)
        deadline = cb.Deadline(budget=50.0, clock=clock)
        ib = StrictFakeIB(clock=clock)
        cb.wrap_ib_instance(ib, deadline)

        ib.sleep(10.0)
        self.assertEqual(ib.sleep_calls, [10.0])

        clock.advance(20.0)
        with self.assertRaises(cb.DeadlineExceededError):
            ib.sleep(35.0)
        self.assertEqual(ib.sleep_calls, [10.0, 20.0])

    def test_sleep_checks_deadline_before_and_after(self):
        clock = InjectedClock(0.0)
        deadline = cb.Deadline(budget=10.0, clock=clock)
        ib = StrictFakeIB(clock=clock)
        cb.wrap_ib_instance(ib, deadline)

        clock.advance(15.0)
        with self.assertRaises(cb.DeadlineExceededError):
            ib.sleep(1.0)
        self.assertEqual(ib.sleep_calls, [])

    def test_sleep_reattach_reuse_updates_deadline_avoids_stale(self):
        clock = InjectedClock(0.0)
        d1 = cb.Deadline(budget=10.0, clock=clock)
        ib = StrictFakeIB(clock=clock)
        cb.wrap_ib_instance(ib, d1)

        ib.sleep(5.0)
        self.assertEqual(ib.sleep_calls, [5.0])

        clock.advance(10.0)
        with self.assertRaises(cb.DeadlineExceededError):
            ib.sleep(1.0)

        d2 = cb.Deadline(budget=60.0, clock=clock)
        cb.wrap_ib_instance(ib, d2)
        ib.sleep(15.0)
        self.assertEqual(ib.sleep_calls, [5.0, 15.0])
        self.assertAlmostEqual(d2.remaining(), 45.0)

    def test_sleep_fails_closed_on_bad_inputs(self):
        clock = InjectedClock(0.0)
        deadline = cb.Deadline(budget=50.0, clock=clock)
        ib = StrictFakeIB(clock=clock)
        cb.wrap_ib_instance(ib, deadline)

        for bad in (-1, -0.01, float("nan"), float("inf"), True, False, "10", None, []):
            with self.assertRaises((TypeError, ValueError)):
                ib.sleep(bad)
        self.assertEqual(ib.sleep_calls, [])

    def test_wrap_ib_instance_install_failure_cleans_up_all_wrappers(self):
        class ReadOnlySleepIB(StrictFakeIB):
            @property
            def sleep(self):
                return lambda s: None

        clock = InjectedClock(0.0)
        deadline = cb.Deadline(10.0, clock=clock)
        ib = ReadOnlySleepIB()
        original_run = ib._run

        with self.assertRaises(Exception):
            cb.wrap_ib_instance(ib, deadline)

        self.assertEqual(ib._run, original_run)
        self.assertNotIn("_thetamill_budget_wrapped", ib.__dict__)
        self.assertNotIn(cb.BUDGET_ATTR, ib.__dict__)
        self.assertNotIn(cb.SENTINEL_ATTR, ib.__dict__)
        self.assertIsNone(cb.get_instance_budget(ib))

    def test_wrap_ib_instance_readback_mismatch_cleans_up(self):
        class ReadbackFailingSleepIB(StrictFakeIB):
            def sleep(self, secs=0.02):
                pass

            def __setattr__(self, name, value):
                if name == "sleep":
                    return
                super().__setattr__(name, value)

        clock = InjectedClock(0.0)
        deadline = cb.Deadline(10.0, clock=clock)
        ib = ReadbackFailingSleepIB()
        with self.assertRaises(RuntimeError) as ctx:
            cb.wrap_ib_instance(ib, deadline)
        self.assertIn("Failed to install budget sleep wrapper", str(ctx.exception))
        self.assertNotIn("_thetamill_budget_wrapped", ib.__dict__)
        self.assertIsNone(cb.get_instance_budget(ib))

    def test_timeout_setter_restoration_failure_raises_and_not_swallowed(self):
        class FailingRestoreIB(StrictFakeIB):
            def __init__(self):
                super().__init__(request_timeout=30.0)
                self.set_count = 0

            @property
            def RequestTimeout(self):
                return self._request_timeout

            @RequestTimeout.setter
            def RequestTimeout(self, val):
                self.set_count += 1
                if self.set_count > 1:
                    raise ValueError("restoration failed")
                self._request_timeout = float(val)

        clock = InjectedClock(0.0)
        deadline = cb.Deadline(budget=50.0, clock=clock)
        ib = FailingRestoreIB()
        cb.wrap_ib_instance(ib, deadline)

        with self.assertRaises(RuntimeError) as ctx:
            ib._run()
        self.assertIn("restoration failed", str(ctx.exception))

    def test_has_real_class_run_requires_callable(self):
        class NonCallableRunIB:
            _run = "not-callable"
            sleep = "not-callable"

        clock = InjectedClock(0.0)
        deadline = cb.Deadline(10.0, clock=clock)
        ib = NonCallableRunIB()
        cb.wrap_ib_instance(ib, deadline)
        self.assertNotIn("_thetamill_budget_wrapped", ib.__dict__)
        self.assertEqual(ib._run, "not-callable")

    def test_safe_req_tickers_async_cleanup_on_budget_timeout(self):
        from thetamill.broker_requests import safe_req_tickers_async
        ib = IB()
        ib.client._apiReady = True
        contract = Stock("AAPL", "SMART", "USD", conId=1001)
        clock = InjectedClock(0.0)
        deadline = cb.Deadline(budget=1.0, clock=clock)
        cb.wrap_ib_instance(ib, deadline)
        ib.RequestTimeout = 0.05

        loop = asyncio.new_event_loop()
        try:
            async def run_test():
                with patch.object(ib.client, "reqMktData"), \
                     patch.object(ib.client, "isConnected", return_value=True), \
                     patch.object(ib.client, "cancelMktData") as mock_cancel:
                    with self.assertRaises((asyncio.TimeoutError, TimeoutError)):
                        await safe_req_tickers_async(ib, contract, timeout=0.01)
                    mock_cancel.assert_called_once()
                    self.assertEqual(len(ib.wrapper._futures), 0)
                    self.assertEqual(len(ib.wrapper.reqId2Ticker), 0)

            loop.run_until_complete(run_test())
        finally:
            loop.close()
            ib.disconnect()

    def test_wrapped_run_fake_clock_timeout_when_deadline_expired_converts_to_deadline_exceeded(self):
        clock = InjectedClock(0.0)
        deadline = cb.Deadline(budget=10.0, clock=clock)

        class TimingOutFakeIB(StrictFakeIB):
            def __init__(self, exc_to_raise=None, advance_on_run=0.0, **kwargs):
                super().__init__(**kwargs)
                self.exc_to_raise = exc_to_raise if exc_to_raise is not None else asyncio.TimeoutError()
                self.advance_on_run = float(advance_on_run)

            def _run(self, *awaitables):
                self.run_calls.append(awaitables)
                self.timeouts_during_run.append(self.RequestTimeout)
                if self.advance_on_run and self.clock:
                    self.clock.advance(self.advance_on_run)
                raise self.exc_to_raise

        # Test with asyncio.TimeoutError
        ib_asyncio = TimingOutFakeIB(
            exc_to_raise=asyncio.TimeoutError(),
            advance_on_run=10.0,
            request_timeout=30.0,
            clock=clock,
        )
        cb.wrap_ib_instance(ib_asyncio, deadline)

        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            with self.assertRaises(cb.DeadlineExceededError) as ctx:
                ib_asyncio._run(fut)
            self.assertIn("Command deadline exceeded", str(ctx.exception))
            self.assertIsInstance(ctx.exception.__cause__, (asyncio.TimeoutError, TimeoutError))
            self.assertTrue(fut.cancelled())
            self.assertEqual(ib_asyncio.RequestTimeout, 30.0)
        finally:
            loop.close()

        # Test with builtin TimeoutError
        clock2 = InjectedClock(0.0)
        deadline2 = cb.Deadline(budget=10.0, clock=clock2)
        ib_builtin = TimingOutFakeIB(
            exc_to_raise=TimeoutError("builtin timeout"),
            advance_on_run=10.0,
            request_timeout=25.0,
            clock=clock2,
        )
        cb.wrap_ib_instance(ib_builtin, deadline2)

        loop2 = asyncio.new_event_loop()
        try:
            fut2 = loop2.create_future()
            with self.assertRaises(cb.DeadlineExceededError) as ctx2:
                ib_builtin._run(fut2)
            self.assertIn("Command deadline exceeded", str(ctx2.exception))
            self.assertIsInstance(ctx2.exception.__cause__, TimeoutError)
            self.assertTrue(fut2.cancelled())
            self.assertEqual(ib_builtin.RequestTimeout, 25.0)
        finally:
            loop2.close()

    def test_wrapped_run_fake_clock_per_request_timeout_not_expired_retains_type(self):
        clock = InjectedClock(0.0)
        deadline = cb.Deadline(budget=100.0, clock=clock)

        class TimingOutFakeIB(StrictFakeIB):
            def __init__(self, exc_to_raise=None, advance_on_run=0.0, **kwargs):
                super().__init__(**kwargs)
                self.exc_to_raise = exc_to_raise if exc_to_raise is not None else asyncio.TimeoutError()
                self.advance_on_run = float(advance_on_run)

            def _run(self, *awaitables):
                self.run_calls.append(awaitables)
                self.timeouts_during_run.append(self.RequestTimeout)
                if self.advance_on_run and self.clock:
                    self.clock.advance(self.advance_on_run)
                raise self.exc_to_raise

        # Test with asyncio.TimeoutError
        ib_asyncio = TimingOutFakeIB(
            exc_to_raise=asyncio.TimeoutError("per-request timeout"),
            advance_on_run=5.0,
            request_timeout=5.0,
            clock=clock,
        )
        cb.wrap_ib_instance(ib_asyncio, deadline)

        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            with self.assertRaises(asyncio.TimeoutError) as ctx:
                ib_asyncio._run(fut)
            self.assertNotIsInstance(ctx.exception, cb.DeadlineExceededError)
            self.assertEqual(str(ctx.exception), "per-request timeout")
            self.assertTrue(fut.cancelled())
            self.assertEqual(ib_asyncio.RequestTimeout, 5.0)
        finally:
            loop.close()

        # Test with builtin TimeoutError
        clock2 = InjectedClock(0.0)
        deadline2 = cb.Deadline(budget=100.0, clock=clock2)
        ib_builtin = TimingOutFakeIB(
            exc_to_raise=TimeoutError("per-request builtin timeout"),
            advance_on_run=5.0,
            request_timeout=8.0,
            clock=clock2,
        )
        cb.wrap_ib_instance(ib_builtin, deadline2)

        loop2 = asyncio.new_event_loop()
        try:
            fut2 = loop2.create_future()
            with self.assertRaises(TimeoutError) as ctx2:
                ib_builtin._run(fut2)
            self.assertNotIsInstance(ctx2.exception, cb.DeadlineExceededError)
            self.assertEqual(str(ctx2.exception), "per-request builtin timeout")
            self.assertTrue(fut2.cancelled())
            self.assertEqual(ib_builtin.RequestTimeout, 8.0)
        finally:
            loop2.close()

    def test_wrapped_run_real_ib_offline_never_completing_awaitable_expired(self):
        ib = IB()
        try:
            ib.RequestTimeout = 10.0
            deadline = cb.Deadline(budget=0.03)
            cb.wrap_ib_instance(ib, deadline)

            loop = asyncio.get_event_loop()
            fut = loop.create_future()
            with self.assertRaises(cb.DeadlineExceededError) as ctx:
                ib._run(fut)
            self.assertIn("Command deadline exceeded", str(ctx.exception))
            self.assertIsInstance(ctx.exception.__cause__, (asyncio.TimeoutError, TimeoutError))
            self.assertTrue(fut.cancelled())
            self.assertEqual(ib.RequestTimeout, 10.0)
        finally:
            ib.disconnect()

    def test_wrapped_run_real_ib_offline_never_completing_awaitable_per_request_timeout(self):
        ib = IB()
        try:
            ib.RequestTimeout = 0.03
            deadline = cb.Deadline(budget=30.0)
            cb.wrap_ib_instance(ib, deadline)

            loop = asyncio.get_event_loop()
            fut = loop.create_future()
            with self.assertRaises((asyncio.TimeoutError, TimeoutError)) as ctx:
                ib._run(fut)
            self.assertNotIsInstance(ctx.exception, cb.DeadlineExceededError)
            self.assertTrue(fut.cancelled())
            self.assertEqual(ib.RequestTimeout, 0.03)
        finally:
            ib.disconnect()



class TestCliIntegration(unittest.TestCase):
    def test_cmd_status_ports_share_single_budget(self):
        clock = InjectedClock(0.0)
        created = []

        def make_ib(*args, **kwargs):
            fake = StrictFakeIB(request_timeout=30.0)
            created.append(fake)
            clock.advance(25.0)
            return fake

        cfg = dict(cli.DEFAULT_CFG, command_timeout=60.0)
        args = MagicMock()
        args.account = "U1"

        with patch.object(cli, "IB", side_effect=make_ib), \
              patch.object(cli, "finish"), \
              patch.object(cli, "CFG_PATH") as p, \
              patch("time.monotonic", clock):
            p.exists.return_value = True
            with self.assertRaises(cb.DeadlineExceededError):
                cli.cmd_status(cfg, args)
        self.assertEqual(len(created), 3)

    def test_cmd_status_propagates_deadline_from_inner_account_summary(self):
        clock = InjectedClock(0.0)
        cfg = dict(cli.DEFAULT_CFG, command_timeout=60.0)
        args = MagicMock()
        args.account = "U1"
        fake_ib = StrictFakeIB(request_timeout=30.0)

        def expiring_summary(account):
            clock.advance(70.0)
            raise cb.DeadlineExceededError("summary timeout")

        fake_ib.accountSummary = expiring_summary

        with patch.object(cli, "IB", return_value=fake_ib), \
              patch.object(cli, "_selected_account", return_value="U1"), \
              patch.object(cli, "finish"), \
              patch.object(cli, "CFG_PATH") as p, \
              patch("time.monotonic", clock):
            p.exists.return_value = True
            with self.assertRaises(cb.DeadlineExceededError):
                cli.cmd_status(cfg, args)

    def test_connect_error_reporting_class_only_and_disconnects(self):
        class FailingSetterIB(StrictFakeIB):
            @property
            def RequestTimeout(self):
                return 0

            @RequestTimeout.setter
            def RequestTimeout(self, val):
                raise AttributeError("private account U1234567 secret host 10.0.0.1")

        ib = FailingSetterIB()
        with patch.object(cli, "IB", return_value=ib):
            with self.assertRaises(RuntimeError) as ctx:
                cli.connect(cli.DEFAULT_CFG, 4002, args=MagicMock())
        self.assertTrue(ib.disconnected)
        self.assertNotIn("U1234567", str(ctx.exception))
        self.assertNotIn("10.0.0.1", str(ctx.exception))

    def test_connect_socket_failure_class_only_no_leak(self):
        class FailingConnectIB(StrictFakeIB):
            def connect(self, host, port, **kw):
                raise OSError(f"cannot reach secret host {host}:{port}")

        ib = FailingConnectIB()
        cfg = dict(cli.DEFAULT_CFG, host="192.168.1.99")
        with patch.object(cli, "IB", return_value=ib):
            with self.assertRaises(SystemExit) as ctx:
                cli.connect(cfg, 4002, args=MagicMock())
        self.assertTrue(ib.disconnected)
        self.assertNotIn("192.168.1.99", str(ctx.exception))
        self.assertIn("OSError", str(ctx.exception))

    def test_main_wraps_command_budget_and_reports_class_only_no_traceback(self):
        clock = InjectedClock(0.0)

        def expiring_cmd(cfg, args):
            clock.advance(150.0)
            raise cb.DeadlineExceededError("main deadline reached")

        mock_parser = MagicMock()
        mock_args = MagicMock()
        mock_args.func = expiring_cmd
        mock_parser.parse_args.return_value = mock_args

        with patch("time.monotonic", clock), \
              patch.object(cli, "load_cfg", return_value=cli.DEFAULT_CFG), \
              patch.object(cli, "build_parser", return_value=mock_parser):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
            self.assertIn("DeadlineExceededError", str(ctx.exception))
            self.assertNotIn("Traceback", str(ctx.exception))

    def test_connect_deadline_exceeded_disconnects_and_propagates(self):
        class ExpiringConnectIB(StrictFakeIB):
            def connect(self, host, port, **kw):
                raise cb.DeadlineExceededError("connect deadline exceeded")

        ib = ExpiringConnectIB()
        with patch.object(cli, "IB", return_value=ib):
            with self.assertRaises(cb.DeadlineExceededError):
                cli.connect(cli.DEFAULT_CFG, 4002, args=MagicMock())
        self.assertTrue(ib.disconnected)


class TestSubmissionCliAndEngineIntegration(unittest.TestCase):
    def test_no_order_or_cancel_for_expiry_pre_send(self):
        clock = InjectedClock(0.0)
        deadline = cb.Deadline(budget=10.0, clock=clock)
        clock.advance(15.0)

        ib = StrictFakeIB()
        cb.attach_budget(ib, deadline)

        with self.assertRaises(cb.DeadlineExceededError):
            sc._bounded_receipt(ib, MagicMock(), MagicMock(), clock=clock)

        self.assertEqual(ib.place_calls, [])
        self.assertEqual(ib.cancel_calls, [])

    def test_after_send_deadline_in_wait_loop(self):
        clock = InjectedClock(0.0)
        deadline = cb.Deadline(budget=10.0, clock=clock)

        class DelayedWaitIB(StrictFakeIB):
            def sleep(self, dur):
                clock.advance(15.0)

        ib = DelayedWaitIB()
        cb.attach_budget(ib, deadline)

        with self.assertRaises(cb.DeadlineExceededError):
            sc._bounded_receipt(ib, MagicMock(), MagicMock(), clock=clock)

        self.assertEqual(len(ib.place_calls), 1)
        self.assertEqual(ib.cancel_calls, [])

    def test_receipt_monotonic_polling_bounds_and_injectable_clock(self):
        clock = InjectedClock(100.0)
        sleep_calls = []

        class SleepTrackingIB(StrictFakeIB):
            def sleep(self, dur):
                sleep_calls.append(dur)
                clock.advance(dur)

        ib = SleepTrackingIB()
        deadline = cb.Deadline(budget=1.0, clock=clock)
        cb.attach_budget(ib, deadline)

        with self.assertRaises(cb.DeadlineExceededError):
            sc._bounded_receipt(ib, MagicMock(), MagicMock(), max_wait_seconds=5.0, clock=clock)

        self.assertTrue(all(s <= 1.0 for s in sleep_calls))

    def test_after_send_unknown_receipt_preserves_reservation_no_unreserve(self):
        clock = InjectedClock(0.0)
        deadline = cb.Deadline(budget=60.0, clock=clock)

        class UnknownReceiptIB(StrictFakeIB):
            def sleep(self, dur):
                clock.advance(dur)

        ib = UnknownReceiptIB()
        cb.attach_budget(ib, deadline)

        now_dt = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
        account = "U12345"
        env = "paper"

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            lock_dir = Path(tmpdir) / "locks"
            lock_dir.mkdir()
            ledger = state.SubmissionLedger(db_path, "UTC")
            limits = risk.validate_risk_config({"risk": {"enabled": False, "day_timezone": "UTC"}})

            inst = Instrument(con_id=1001, symbol="AAPL", currency="USD",
                              sec_type="STK", exchange="SMART", underlying_id=1001)
            leg = execution.PreparedLeg(
                instrument=inst, action="BUY", quantity=Decimal("2"), order_type="LMT",
                limit_price=Decimal("180"), contract=MagicMock(), tif="DAY",
                exchange="SMART", outside_rth=False, strategy="test",
                account=account, deliverable_verified=True,
            )

            def snap_provider():
                return Snapshot(
                    account=account, environment=env,
                    captured_at=now_dt,
                    base_currency="USD", cash={"USD": Decimal("100000")},
                    net_liquidation=Decimal("100000"),
                    positions=(), orders=(), fx={"USD": Decimal(1)},
                    issues=(),
                )

            def plan_fn(snapshot, plan_legs, aggregate):
                return execution.ResourcePlan(
                    resources={
                        ("USD", "cash_base"): Decimal("100000"),
                        ("USD", "exposure:1001:USD"): Decimal("100000"),
                    },
                    items=[
                        state.RiskReservationSpec("USD", "cash_base", "360"),
                        state.RiskReservationSpec("USD", "exposure:1001:USD", "360"),
                    ],
                    overrides={},
                )

            def place_fn(broker, p_leg, order_ref):
                return sc._broker_submit(broker, p_leg, order_ref, account, {}, clock=clock)

            engine = execution.ExecutionEngine(
                ledger=ledger,
                snapshot_provider=snap_provider,
                broker=ib,
                place_order=place_fn,
                resource_plan=plan_fn,
                config={"risk": {"enabled": False, "day_timezone": "UTC"}},
                limits=limits,
                lock_dir=lock_dir,
                clock=lambda: now_dt,
            )

            with patch.object(sc, "verify_contract_identity", return_value=[]):
                pre = engine.preflight([leg], account=account, environment=env)
                res = engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                                     legs=[leg], account=account, environment=env)

            self.assertEqual(len(res.legs), 1)
            self.assertEqual(res.legs[0].state, "unknown")
            self.assertEqual(len(ib.place_calls), 1)
            self.assertEqual(len(ib.cancel_calls), 0)

            self.assertTrue(ledger.has_unresolved_submissions(account=account, environment=env))
            outstanding = ledger.outstanding_reservations(account=account, environment=env)
            self.assertEqual(len(outstanding), 1)
            self.assertEqual(outstanding[0].intent_id, pre.intent_id)

            leg_rows = ledger.list_legs(account=account, environment=env, intent_id=pre.intent_id)
            self.assertEqual(len(leg_rows), 1)
            self.assertEqual(leg_rows[0].state, "unknown")

    def test_after_send_execution_engine_records_unknown_and_preserves_reservation(self):
        clock = InjectedClock(0.0)
        deadline = cb.Deadline(budget=10.0, clock=clock)

        class DelayedWaitIB(StrictFakeIB):
            def sleep(self, dur):
                clock.advance(15.0)

        ib = DelayedWaitIB()
        cb.attach_budget(ib, deadline)

        now_dt = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
        account = "U12345"
        env = "paper"

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            lock_dir = Path(tmpdir) / "locks"
            lock_dir.mkdir()
            ledger = state.SubmissionLedger(db_path, "UTC")
            limits = risk.validate_risk_config({"risk": {"enabled": False, "day_timezone": "UTC"}})

            inst = Instrument(con_id=1001, symbol="AAPL", currency="USD",
                              sec_type="STK", exchange="SMART", underlying_id=1001)
            leg = execution.PreparedLeg(
                instrument=inst, action="BUY", quantity=Decimal("2"), order_type="LMT",
                limit_price=Decimal("180"), contract=MagicMock(), tif="DAY",
                exchange="SMART", outside_rth=False, strategy="test",
                account=account, deliverable_verified=True,
            )

            def snap_provider():
                return Snapshot(
                    account=account, environment=env,
                    captured_at=now_dt,
                    base_currency="USD", cash={"USD": Decimal("100000")},
                    net_liquidation=Decimal("100000"),
                    positions=(), orders=(), fx={"USD": Decimal(1)},
                    issues=(),
                )

            def plan_fn(snapshot, plan_legs, aggregate):
                return execution.ResourcePlan(
                    resources={
                        ("USD", "cash_base"): Decimal("100000"),
                        ("USD", "exposure:1001:USD"): Decimal("100000"),
                    },
                    items=[
                        state.RiskReservationSpec("USD", "cash_base", "360"),
                        state.RiskReservationSpec("USD", "exposure:1001:USD", "360"),
                    ],
                    overrides={},
                )

            def place_fn(broker, p_leg, order_ref):
                return sc._broker_submit(broker, p_leg, order_ref, account, {}, clock=clock)

            engine = execution.ExecutionEngine(
                ledger=ledger,
                snapshot_provider=snap_provider,
                broker=ib,
                place_order=place_fn,
                resource_plan=plan_fn,
                config={"risk": {"enabled": False, "day_timezone": "UTC"}},
                limits=limits,
                lock_dir=lock_dir,
                clock=lambda: now_dt,
            )

            with patch.object(sc, "verify_contract_identity", return_value=[]):
                pre = engine.preflight([leg], account=account, environment=env)
                with self.assertRaises(execution.ExecutionError) as ctx:
                    engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                                   legs=[leg], account=account, environment=env)

            self.assertIn("submission interrupted after a send", str(ctx.exception))
            self.assertIn("Command deadline exceeded", str(ctx.exception))
            self.assertIsInstance(ctx.exception.__cause__, cb.DeadlineExceededError)
            self.assertEqual(len(ib.place_calls), 1)
            self.assertEqual(len(ib.cancel_calls), 0)

            self.assertTrue(ledger.has_unresolved_submissions(account=account, environment=env))
            outstanding = ledger.outstanding_reservations(account=account, environment=env)
            self.assertEqual(len(outstanding), 1)
            self.assertEqual(outstanding[0].intent_id, pre.intent_id)

            leg_rows = ledger.list_legs(account=account, environment=env, intent_id=pre.intent_id)
            self.assertEqual(len(leg_rows), 1)
            self.assertEqual(leg_rows[0].state, "submitting")

            day_key = state.day_key_for("UTC", now=now_dt)
            budget_row = ledger.get_day_budget(account=account, environment=env, day_key=day_key)
            self.assertEqual(budget_row["submitted_count"], 1)


if __name__ == "__main__":
    unittest.main()
