"""Deterministic unit tests for thetamill.broker_session."""
from __future__ import annotations

import io
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from ib_async import Client, Contract, IB

from thetamill.broker_session import (
    private_diagnostics,
    risk_snapshot,
    select_account,
    timestamped_mid,
)
from thetamill.broker_state import Instrument, Position, Snapshot, SnapshotError


class SelectAccountTests(unittest.TestCase):
    """Tests for account selection delegation and argument resolution."""

    def test_select_account_resolutions(self):
        ib = MagicMock()
        ib.managedAccounts.return_value = ["DU1001", "DU1002"]

        # 1. requested kwarg
        self.assertEqual(select_account(ib, requested="DU1001"), "DU1001")

        # 2. CLI args
        args = SimpleNamespace(account="DU1002")
        self.assertEqual(select_account(ib, {}, args), "DU1002")

        # 3. Config dict
        cfg = {"account": "DU1001"}
        self.assertEqual(select_account(ib, cfg, SimpleNamespace(account=None)), "DU1001")

        # 4. String cfg fallback
        self.assertEqual(select_account(ib, "DU1002"), "DU1002")

        # 5. Invalid / unmanaged requested account raises SnapshotError
        with self.assertRaises(SnapshotError):
            select_account(ib, requested="DU9999")

        # 6. Ambiguous accounts without explicit selection raises SnapshotError
        with self.assertRaises(SnapshotError):
            select_account(ib, {}, SimpleNamespace(account=None))

        # 7. Single account auto-resolution
        ib.managedAccounts.return_value = ["DU5555"]
        self.assertEqual(select_account(ib, {}, SimpleNamespace(account=None)), "DU5555")


class PrivateDiagnosticsTests(unittest.TestCase):
    """Tests for privacy suppression of broker diagnostics."""

    def test_suppresses_stdout_and_stderr(self):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with private_diagnostics():
                print("sensitive account info DU12345", file=sys.stdout)
                print("sensitive error DU12345", file=sys.stderr)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_propagates_exceptions(self):
        with self.assertRaises(ValueError):
            with private_diagnostics():
                raise ValueError("operational failure inside privacy context")


class TimestampedMidTests(unittest.TestCase):
    """Tests for timestamped_mid pre-I/O validation, clock injection, and tick parsing."""

    def test_pre_io_contract_validation(self):
        # Requester must never be invoked for non-positive or non-int conIds
        mock_requester = MagicMock()
        ib = MagicMock()

        bad_contracts = [
            None,
            Contract(),  # conId == 0
            Contract(conId=0),
            Contract(conId=-1),
            Contract(conId=True),  # bool must not pass int check
            Contract(conId=False),
            SimpleNamespace(conId="1001"),  # string
            SimpleNamespace(conId=1001.0),  # float
            SimpleNamespace(),  # no conId attribute
        ]

        for bad in bad_contracts:
            with self.subTest(bad=bad):
                result = timestamped_mid(ib, bad, requester=mock_requester)
                self.assertIsNone(result)
                mock_requester.assert_not_called()

    def test_timestamped_mid_valid_tick_and_sampling_now(self):
        ib = MagicMock()
        c = Contract(conId=1001, symbol="AAPL")
        now = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)

        tick = SimpleNamespace(
            contract=c,
            time=now - timedelta(seconds=2),
            bid=Decimal("150.00"),
            ask=Decimal("150.50"),
            marketDataType=1,
        )

        requester = MagicMock(return_value=[tick])

        # Test with fixed now datetime
        res = timestamped_mid(ib, c, now=now, requester=requester)
        self.assertIsNotNone(res)
        mid, stamp, src = res
        self.assertEqual(mid, Decimal("150.25"))
        self.assertEqual(stamp, tick.time)
        self.assertEqual(src, "IB timestamped bid/ask midpoint")

        # Test with now callable
        clock_calls = 0
        def get_now():
            nonlocal clock_calls
            clock_calls += 1
            return now

        res2 = timestamped_mid(ib, c, now=get_now, requester=requester)
        self.assertIsNotNone(res2)
        self.assertGreaterEqual(clock_calls, 1)

    def test_timestamped_mid_invalid_ticks(self):
        ib = MagicMock()
        c = Contract(conId=1001, symbol="AAPL")
        now = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)

        for name, tick in [
            ("wrong_data_type", SimpleNamespace(contract=c, time=now, bid=100, ask=101, marketDataType=3)),
            ("crossed_quotes", SimpleNamespace(contract=c, time=now, bid=102, ask=100, marketDataType=1)),
            ("negative_bid", SimpleNamespace(contract=c, time=now, bid=-1, ask=100, marketDataType=1)),
            ("zero_ask", SimpleNamespace(contract=c, time=now, bid=0, ask=0, marketDataType=1)),
            ("stale_quote", SimpleNamespace(contract=c, time=now - timedelta(seconds=35), bid=100, ask=101, marketDataType=1)),
            ("future_skew", SimpleNamespace(contract=c, time=now + timedelta(seconds=10), bid=100, ask=101, marketDataType=1)),
            ("mismatched_con_id", SimpleNamespace(contract=Contract(conId=9999), time=now, bid=100, ask=101, marketDataType=1)),
            ("non_finite_bid", SimpleNamespace(contract=c, time=now, bid=float("nan"), ask=101, marketDataType=1)),
            ("empty_time", SimpleNamespace(contract=c, time=None, bid=100, ask=101, marketDataType=1)),
            ("naive_time", SimpleNamespace(contract=c, time=datetime(2026, 11, 9, 12, 0, 0), bid=100, ask=101, marketDataType=1)),
        ]:
            with self.subTest(name=name):
                res = timestamped_mid(ib, c, now=now, requester=lambda *args: [tick])
                self.assertIsNone(res)

    def test_requester_failure_fails_closed(self):
        ib = MagicMock()
        c = Contract(conId=1001, symbol="AAPL")

        def failing_req(*args, **kwargs):
            raise OSError("Network drop during snapshot")

        self.assertIsNone(timestamped_mid(ib, c, requester=failing_req))
        self.assertIsNone(timestamped_mid(ib, c, requester=lambda *args: []))
        self.assertIsNone(timestamped_mid(ib, c, requester=lambda *args: [1, 2]))


class TimestampedMidFakeTransportTests(unittest.TestCase):
    """Integration test executing timestamped_mid with real IB and fake transport."""

    def test_safe_req_tickers_with_fake_transport(self):
        ib = IB()
        written: list[bytes] = []

        class FakeTransport:
            def write(self, data: bytes) -> None:
                written.append(data)
            def is_closing(self) -> bool:
                return False
            def close(self) -> None:
                pass
            def write_eof(self) -> None:
                pass

        ib.client.conn.transport = FakeTransport()
        ib.client.connState = Client.CONNECTED
        ib.client._apiReady = True
        ib.client._startTime = time.time()
        ib.client._serverVersion = ib.client.MaxClientVersion

        c = Contract(conId=1001, symbol="AAPL", secType="STK", exchange="SMART", currency="USD")
        now = datetime.now(timezone.utc)

        real_start_req = ib.wrapper.startReq
        def auto_complete(key: int, contract: Any = None, container: Any = None) -> Any:
            fut = real_start_req(key, contract, container)
            fut.set_result([])
            return fut

        real_start_ticker = ib.wrapper.startTicker
        def on_start_ticker(req_id: int, contract: Any, tick_type: Any) -> Any:
            ticker = real_start_ticker(req_id, contract, tick_type)
            ticker.bid = Decimal("150.00")
            ticker.ask = Decimal("150.20")
            ticker.time = now
            ticker.marketDataType = 1
            return ticker

        ib.wrapper.startReq = auto_complete
        ib.wrapper.startTicker = on_start_ticker

        try:
            res = timestamped_mid(ib, c, now=now)
            self.assertIsNotNone(res)
            self.assertEqual(res[0], Decimal("150.10"))
            self.assertEqual(res[1], now)
            self.assertEqual(len(written), 2)  # reqMktData + cancelMktData
        finally:
            ib.disconnect()


class RiskSnapshotTests(unittest.TestCase):
    """Tests for risk_snapshot collection, FX resolution, and position enrichment."""

    def setUp(self):
        self.ib = MagicMock()
        self.now = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)

    def test_risk_snapshot_currencies_and_fx_enrichment(self):
        inst_stk = Instrument(
            con_id=1001,
            symbol="AAPL",
            currency="USD",
            sec_type="STK",
            exchange="SMART",
            underlying_id=1001,
        )
        inst_eur = Instrument(
            con_id=2001,
            symbol="SAP",
            currency="EUR",
            sec_type="STK",
            exchange="SMART",
            underlying_id=2001,
        )

        base_snap = Snapshot(
            account="DU12345",
            environment="paper",
            captured_at=self.now,
            base_currency="USD",
            cash={"USD": Decimal("10000"), "EUR": Decimal("5000")},
            net_liquidation=Decimal("25000"),
            positions=(
                Position(instrument=inst_stk, quantity=Decimal("100"), price=None, average_cost=Decimal("140.00")),
                Position(instrument=inst_eur, quantity=Decimal("50"), price=None, average_cost=Decimal("110.00")),
            ),
            orders=(),
            fx={"USD": Decimal(1)},
        )

        with patch("thetamill.broker_session.select_account", return_value="DU12345"), \
             patch("thetamill.broker_session.collect_snapshot", return_value=base_snap) as mock_collect:

            def qualify(contract):
                # Returns qualified contract with matching conId
                if contract.symbol == "EUR" and contract.currency == "USD":
                    return [Contract(conId=9901, secType="CASH", symbol="EUR", currency="USD", exchange="IDEALPRO")]
                return []

            self.ib.qualifyContracts.side_effect = qualify

            def fake_timestamped_mid(ib, contract, *, now=None, requester=None):
                if contract.conId == 9901:
                    # EUR.USD FX rate = 1.08
                    return (Decimal("1.08"), self.now, "IB timestamped bid/ask midpoint")
                elif contract.conId == 1001:
                    # AAPL = 155.00
                    return (Decimal("155.00"), self.now, "IB timestamped bid/ask midpoint")
                elif contract.conId == 2001:
                    # SAP = 120.00
                    return (Decimal("120.00"), self.now, "IB timestamped bid/ask midpoint")
                return None

            with patch("thetamill.broker_session.timestamped_mid", side_effect=fake_timestamped_mid):
                snap = risk_snapshot(self.ib, {}, SimpleNamespace(account="DU12345"), "paper", now=self.now)

            mock_collect.assert_called_once_with(self.ib, account="DU12345", environment="paper")
            self.assertEqual(snap.fx["USD"], Decimal(1))
            self.assertEqual(snap.fx["EUR"], Decimal("1.08"))
            self.assertEqual(snap.fx_quotes["EUR"].as_of, self.now)
            self.assertEqual(snap.positions[0].price, Decimal("155.00"))
            self.assertEqual(snap.positions[1].price, Decimal("120.00"))
            self.assertEqual(snap.price_quotes[1001].price, Decimal("155.00"))
            self.assertEqual(snap.price_quotes[2001].price, Decimal("120.00"))

    def test_required_currencies_validation(self):
        with patch("thetamill.broker_session.select_account", return_value="DU12345"), \
             patch("thetamill.broker_session.collect_snapshot", return_value=Snapshot(
                 account="DU12345",
                 environment="paper",
                 captured_at=self.now,
                 base_currency="USD",
                 cash={"USD": Decimal("1000")},
                 net_liquidation=Decimal("1000"),
                 positions=(),
                 orders=(),
                 fx={"USD": Decimal(1)},
             )):
            for bad_currency in ["US", "USDD", "123", "US$", True, None, 12]:
                with self.subTest(bad_currency=bad_currency):
                    with self.assertRaises(ValueError):
                        risk_snapshot(self.ib, {}, SimpleNamespace(account="DU12345"), "paper", required_currencies=[bad_currency])


if __name__ == "__main__":
    unittest.main()
