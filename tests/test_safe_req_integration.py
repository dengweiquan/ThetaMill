"""Integration tests for safe_req_tickers across production entry points.

Verified offline against real IB and Contract objects with fake transport,
exercising actual ib_async 2.1.0 client/wrapper event loops and timeout cleanup.
Zero live network sockets, zero broker mutations (no placeOrder/cancelOrder).
"""
from __future__ import annotations

import asyncio
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from ib_async import Client, Contract, IB

from thetamill import cli, plan_cli, roll_cli

NOW = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)


class FakeTransport:
    """In-memory transport capturing all written wire bytes without socket I/O."""

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


class SafeReqIntegrationTests(unittest.TestCase):
    """Production integration test suite for safe_req_tickers replacement."""

    def setUp(self):
        self.ib = IB()
        self.written_messages: list[bytes] = []
        self.transport = FakeTransport(self.written_messages)
        self.ib.client.conn.transport = self.transport
        self.ib.client.connState = Client.CONNECTED
        self.ib.client._apiReady = True
        self.ib.client._startTime = time.time()
        self.ib.client._serverVersion = self.ib.client.MaxClientVersion

        self._guard_patchers = [
            patch.object(Client, "connect", side_effect=AssertionError),
            patch.object(Client, "connectAsync", side_effect=AssertionError),
            patch.object(IB, "placeOrder", side_effect=AssertionError),
            patch.object(IB, "cancelOrder", side_effect=AssertionError),
            patch.object(Client, "placeOrder", side_effect=AssertionError),
            patch.object(Client, "cancelOrder", side_effect=AssertionError),
        ]
        for p in self._guard_patchers:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        try:
            self.ib.disconnect()
        except Exception:
            pass

    def test_plan_cli_fresh_stock_mid_timeout_cleanup(self):
        c = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
        self.ib.RequestTimeout = 0.02
        res = plan_cli.fresh_stock_mid(self.ib, c)
        self.assertIsNone(res)
        # Verify wire cancel was transmitted
        self.assertGreater(len(self.written_messages), 0)
        # Verify wrapper cleanup
        self.assertEqual(len(self.ib.wrapper._futures), 0)
        self.assertEqual(len(self.ib.wrapper.reqId2Ticker), 0)
        self.assertEqual(len(self.ib.wrapper._reqId2Contract), 0)
        self.assertEqual(len(self.ib.wrapper._results), 0)
        self.assertEqual(len(self.ib.wrapper.ticker2ReqId.get("snapshot", {})), 0)

    def test_plan_cli_fresh_stock_mid_success_and_cleanup(self):
        c = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
        real_start_req = self.ib.wrapper.startReq
        real_start_ticker = self.ib.wrapper.startTicker

        def auto_complete(key, contract=None, container=None):
            fut = real_start_req(key, contract, container)
            fut.set_result([])
            return fut

        def setup_ticker(req_id, contract, tick_type):
            t = real_start_ticker(req_id, contract, tick_type)
            t.bid = 150.0
            t.ask = 150.10
            t.time = NOW
            t.marketDataType = 1
            return t

        self.ib.wrapper.startReq = auto_complete
        self.ib.wrapper.startTicker = setup_ticker

        res = plan_cli.fresh_stock_mid(self.ib, c, now=NOW)
        self.assertIsNotNone(res)
        self.assertEqual(res, Decimal("150.05"))
        self.assertEqual(res.con_id, 1001)
        self.assertEqual(res.bid, Decimal("150.0"))
        self.assertEqual(res.ask, Decimal("150.10"))
        # Verify wrapper cleanup on success
        self.assertEqual(len(self.ib.wrapper._futures), 0)
        self.assertEqual(len(self.ib.wrapper.reqId2Ticker), 0)
        self.assertEqual(len(self.ib.wrapper._reqId2Contract), 0)
        self.assertEqual(len(self.ib.wrapper.ticker2ReqId.get("snapshot", {})), 0)

    def test_roll_cli_fetch_tickers_timeout_cleanup_and_restoration(self):
        c = Contract(conId=2001, symbol="AAPL", secType="OPT", exchange="SMART", currency="USD")
        self.ib.RequestTimeout = 50.0
        notes = []
        with patch.object(roll_cli, "SNAPSHOT_TIMEOUT_SECONDS", 0.02):
            raw = roll_cli._fetch_tickers(self.ib, {2001: c}, notes)
        self.assertEqual(raw, {})
        self.assertTrue(any("TimeoutError" in n for n in notes))
        self.assertEqual(self.ib.RequestTimeout, 50.0)
        # Verify wrapper cleanup
        self.assertEqual(len(self.ib.wrapper._futures), 0)
        self.assertEqual(len(self.ib.wrapper.reqId2Ticker), 0)
        self.assertEqual(len(self.ib.wrapper._reqId2Contract), 0)
        self.assertEqual(len(self.ib.wrapper._results), 0)
        self.assertEqual(len(self.ib.wrapper.ticker2ReqId.get("snapshot", {})), 0)

    def test_roll_cli_fetch_tickers_success_and_restoration(self):
        c = Contract(conId=2001, symbol="AAPL", secType="OPT", exchange="SMART", currency="USD")
        self.ib.RequestTimeout = 45.0
        real_start_req = self.ib.wrapper.startReq
        real_start_ticker = self.ib.wrapper.startTicker

        def auto_complete(key, contract=None, container=None):
            fut = real_start_req(key, contract, container)
            fut.set_result([])
            return fut

        def setup_ticker(req_id, contract, tick_type):
            t = real_start_ticker(req_id, contract, tick_type)
            t.bid = 3.20
            t.ask = 3.30
            t.time = NOW
            t.marketDataType = 1
            return t

        self.ib.wrapper.startReq = auto_complete
        self.ib.wrapper.startTicker = setup_ticker

        notes = []
        raw = roll_cli._fetch_tickers(self.ib, {2001: c}, notes)
        self.assertIn(2001, raw)
        self.assertEqual(self.ib.RequestTimeout, 45.0)
        # Verify wrapper cleanup
        self.assertEqual(len(self.ib.wrapper._futures), 0)
        self.assertEqual(len(self.ib.wrapper.reqId2Ticker), 0)
        self.assertEqual(len(self.ib.wrapper._reqId2Contract), 0)
        self.assertEqual(len(self.ib.wrapper.ticker2ReqId.get("snapshot", {})), 0)

    def test_cli_quote_premium_timeout_cleanup(self):
        c = Contract(conId=2001, symbol="AAPL", secType="OPT", exchange="SMART", currency="USD")
        self.ib.RequestTimeout = 0.02
        with self.assertRaises((asyncio.TimeoutError, TimeoutError)):
            cli._quote_premium(self.ib, c)
        # Verify wrapper cleanup
        self.assertEqual(len(self.ib.wrapper._futures), 0)
        self.assertEqual(len(self.ib.wrapper.reqId2Ticker), 0)
        self.assertEqual(len(self.ib.wrapper._reqId2Contract), 0)
        self.assertEqual(len(self.ib.wrapper.ticker2ReqId.get("snapshot", {})), 0)

    def test_cli_quote_premium_success(self):
        c = Contract(conId=2001, symbol="AAPL", secType="OPT", exchange="SMART", currency="USD")
        real_start_req = self.ib.wrapper.startReq
        real_start_ticker = self.ib.wrapper.startTicker

        def auto_complete(key, contract=None, container=None):
            fut = real_start_req(key, contract, container)
            fut.set_result([])
            return fut

        def setup_ticker(req_id, contract, tick_type):
            t = real_start_ticker(req_id, contract, tick_type)
            t.bid = 2.50
            t.last = 2.45
            t.close = 2.40
            return t

        self.ib.wrapper.startReq = auto_complete
        self.ib.wrapper.startTicker = setup_ticker

        with patch("ib_async.IB.sleep"):
            px, label = cli._quote_premium(self.ib, c)
        self.assertEqual(px, 2.50)
        self.assertEqual(label, "买价")
        # Verify wrapper cleanup
        self.assertEqual(len(self.ib.wrapper._futures), 0)
        self.assertEqual(len(self.ib.wrapper.reqId2Ticker), 0)
        self.assertEqual(len(self.ib.wrapper._reqId2Contract), 0)

    def test_no_fake_only_fallback_when_safe_requester_fails(self):
        fake_ib = MagicMock()
        fake_ib.reqTickers = MagicMock(return_value=["should_never_be_called"])
        c = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")

        with patch.object(plan_cli, "safe_req_tickers", side_effect=RuntimeError("transport failure")):
            res = plan_cli.fresh_stock_mid(fake_ib, c)
            self.assertIsNone(res)
            fake_ib.reqTickers.assert_not_called()

        with patch.object(roll_cli, "safe_req_tickers", side_effect=RuntimeError("transport failure")):
            notes = []
            res = roll_cli._fetch_tickers(fake_ib, {1001: c}, notes)
            self.assertEqual(res, {})
            fake_ib.reqTickers.assert_not_called()

        with patch.object(cli, "safe_req_tickers", side_effect=RuntimeError("transport failure")):
            with self.assertRaises(RuntimeError):
                cli._quote_premium(fake_ib, c)
            fake_ib.reqTickers.assert_not_called()

    def test_zero_real_broker_connect_order_cancel(self):
        c = Contract(conId=1001)
        self.ib.RequestTimeout = 0.02
        res = plan_cli.fresh_stock_mid(self.ib, c)
        self.assertIsNone(res)
        self.assertTrue(len(self.written_messages) > 0)
        msg_types = [msg[4:].split(b"\x00")[0] for msg in self.written_messages]
        self.assertIn(b"1", msg_types)
        self.assertIn(b"2", msg_types)
        self.assertNotIn(b"3", msg_types)
        self.assertNotIn(b"4", msg_types)


if __name__ == "__main__":
    unittest.main()
