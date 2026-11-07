"""Stage A execution regressions: real ib_async contracts, no broker connections."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

try:
    from ib_async import Option, Stock
except ImportError:
    from thetamill.cli import Option, Stock
from thetamill import cli


def option(right="P", multiplier="100", con_id=101):
    contract = Option("AAPL", "20261218", 40, right, "SMART", currency="USD",
                      multiplier=multiplier, tradingClass="AAPL")
    contract.conId = con_id
    return contract


def position(contract, quantity):
    return SimpleNamespace(account="DU12345", contract=contract, position=quantity, averageCost=30, marketPrice=30)


def pending(contract, action="SELL", total=1, remaining=1, filled=0,
            status="Submitted"):
    return SimpleNamespace(contract=contract,
                           order=SimpleNamespace(account="DU12345", action=action, totalQuantity=total),
                           orderStatus=SimpleNamespace(status=status, remaining=remaining,
                                                       filled=filled))


class ExecutionSafetyTests(unittest.TestCase):
    @contextmanager
    def broker(self, contract=None, shares=0, positions=(), trades=(), cash=1000000,
               delivery_confirmed=True):
        ib = MagicMock()
        ib.managedAccounts.return_value = ["DU12345"]
        stock = Stock("AAPL", "SMART", "USD")
        stock.conId = 1
        contract = contract if contract is not None else option()
        ib.portfolio.return_value = [position(stock, shares), *positions]
        ib.openTrades.return_value = list(trades)
        ib.trades.return_value = list(trades)
        ib.accountValues.return_value = [
            SimpleNamespace(account="DU12345", tag="TotalCashValue", currency="USD", value=str(cash)),
            SimpleNamespace(account="DU12345", tag="Currency", currency="BASE", value="USD"),
            SimpleNamespace(account="DU12345", tag="NetLiquidation", currency="USD", value=str(cash)),
        ]
        ib.reqAllOpenOrders.return_value = []
        ib.reqTickers.side_effect = lambda c: [SimpleNamespace(
            marketDataType=1,
            contract=c,
            time=datetime.now(timezone.utc),
            bid=30.0,
            ask=30.0,
        )]
        ib.qualifyContracts.side_effect = lambda c: [c]

        ib.reqContractDetails.side_effect = lambda c: (
            ib.reqContractDetails.return_value
            if ib.reqContractDetails.return_value is not None
            else [SimpleNamespace(contract=c, underConId=1, underSecType="STK")]
        )
        ib.reqContractDetails.return_value = None
        ib.sleep = lambda _: None

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            evidence_path = data_dir / "evidence.json"
            ev_records = []
            if delivery_confirmed:
                now_iso = datetime.now(timezone.utc).isoformat()
                exp_iso = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
                con_ids = {getattr(contract, "conId", 101)}
                for p in positions:
                    c = getattr(p, "contract", None)
                    if c and getattr(c, "secType", "") == "OPT" and getattr(c, "conId", None):
                        con_ids.add(c.conId)
                for tr in trades:
                    c = getattr(tr, "contract", None)
                    if c and getattr(c, "secType", "") == "OPT" and getattr(c, "conId", None):
                        con_ids.add(c.conId)
                for cid in con_ids:
                    ev_records.append({
                        "instrument": {
                            "con_id": cid, "underlying_id": 1, "symbol": "AAPL",
                            "currency": "USD", "sec_type": "OPT", "exchange": "SMART",
                            "multiplier": str(getattr(contract, "multiplier", "100") or "100"),
                            "right": getattr(contract, "right", "P") or "P",
                            "strike": str(getattr(contract, "strike", "40") or "40"),
                            "expiry": getattr(contract, "lastTradeDateOrContractMonth", "20261218") or "20261218",
                            "trading_class": getattr(contract, "tradingClass", "AAPL") or "AAPL",
                        },
                        "stock_only": True,
                        "source": "human review",
                        "verified_at": now_iso,
                        "expires_at": exp_iso,
                    })
            evidence_path.write_text(json.dumps({
                "schema": "thetamill.deliverables.v1",
                "records": ev_records,
            }), encoding="utf-8")
            self.current_evidence_path = evidence_path

            with patch.object(cli, "CFG_PATH", Path(__file__)), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                 patch.object(cli, "connect", return_value=ib), \
                 patch.object(cli, "finish"), \
                 patch.object(cli, "_opt", return_value=(contract, "SMART")), \
                 patch.object(cli, "_stock", return_value=stock), \
                 patch("sys.stdout", new_callable=io.StringIO):
                yield ib, stock

    def order_args(self, **overrides):
        values = dict(symbol="AAPL", opt=True, action="SELL", qty=1, type="LMT",
                      limit=1.0, stop=None, expiry="20261218", strike=40.0, right="P",
                      currency="USD", exchange="SMART", tif="DAY", outside_rth=False,
                      whatif=False, code="", intent="", live_ok=False, paper=True, port=4002,
                      delivery_evidence=str(getattr(self, "current_evidence_path", "")))
        values.update(overrides)
        return argparse.Namespace(**values)

    def combo_args(self, **overrides):
        values = dict(symbol="AAPL", preset="cash-put", qty=1, k1=40.0,
                      expiry="20261218", currency="USD", exchange="SMART", net=1.0,
                      stock_px=None, code="", intent="", live_ok=False, paper=True, port=4002,
                      delivery_evidence=str(getattr(self, "current_evidence_path", "")))
        values.update(overrides)
        return argparse.Namespace(**values)

    def rejected(self, ib, args, combo=False):
        with self.assertRaises(SystemExit):
            (cli.cmd_combo if combo else cli.cmd_order)(cli.DEFAULT_CFG, args)
        ib.placeOrder.assert_not_called()

    def preflight(self, ib, args, combo=False):
        self.assertEqual((cli.cmd_combo if combo else cli.cmd_order)(cli.DEFAULT_CFG, args), 2)
        ib.placeOrder.assert_not_called()

    def confirm_run(self, ib, args, combo=True):
        import sqlite3
        pre_args = argparse.Namespace(**vars(args))
        pre_args.intent = ""
        pre_args.code = ""
        rc = (cli.cmd_combo if combo else cli.cmd_order)(cli.DEFAULT_CFG, pre_args)
        self.assertEqual(rc, 2)
        ledger_path = Path(cli._data_dir()) / "state.sqlite3"
        with sqlite3.connect(ledger_path) as conn:
            row = conn.execute("SELECT intent_id, approval_code FROM intents ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        intent_id, approval_code = row[0], row[1]
        confirm_args = argparse.Namespace(**vars(args))
        confirm_args.intent = intent_id
        confirm_args.code = approval_code
        return (cli.cmd_combo if combo else cli.cmd_order)(cli.DEFAULT_CFG, confirm_args)

    # The four independently reproduced preflight gaps.
    def test_stock_sell_cannot_uncover_held_short_call(self):
        with self.broker(shares=100, positions=[position(option("C"), -1)]) as (ib, _):
            self.rejected(ib, self.order_args(opt=False, qty=100))

    def test_pending_stock_sell_reserves_call_cover(self):
        with self.broker(option("C"), shares=100) as (ib, stock):
            ib.openTrades.return_value = [pending(stock, total=100, remaining=100)]
            self.rejected(ib, self.order_args(right="C"))

    def test_zero_quantity_short_put_is_rejected(self):
        with self.broker() as (ib, _):
            self.rejected(ib, self.order_args(qty=0))

    def test_open_order_sync_failure_blocks_new_risk(self):
        for combo in (False, True):
            with self.subTest(combo=combo), self.broker() as (ib, _):
                ib.reqAllOpenOrders.side_effect = RuntimeError("offline sync failure")
                self.rejected(ib, self.combo_args() if combo else self.order_args(), combo)

    def test_invalid_option_quantities_are_not_truncated_or_accepted(self):
        for qty in (-1, 0.5, True, "1", None, float("nan"), float("inf")):
            for combo in (False, True):
                with self.subTest(qty=qty, combo=combo), self.broker() as (ib, _):
                    args = self.combo_args(qty=qty) if combo else self.order_args(qty=qty)
                    self.rejected(ib, args, combo)

    def test_invalid_strikes_are_rejected(self):
        for strike in (0, -1, True, "40", None, float("nan"), float("inf")):
            for combo in (False, True):
                with self.subTest(strike=strike, combo=combo), self.broker() as (ib, _):
                    args = self.combo_args(k1=strike) if combo else self.order_args(strike=strike)
                    self.rejected(ib, args, combo)

    def test_invalid_limit_and_stop_prices_are_rejected(self):
        for order_type, key in (("LMT", "limit"), ("STP", "stop")):
            for price in (0, -1, True, "1", float("nan"), float("inf")):
                with self.subTest(type=order_type, price=price), self.broker() as (ib, _):
                    self.rejected(ib, self.order_args(**{"type": order_type, key: price}))

    def test_option_buy_requires_a_short_position_with_same_con_id(self):
        with self.broker(positions=[position(option(con_id=999), -2)]) as (ib, _):
            self.rejected(ib, self.order_args(action="BUY"))

    def test_option_buy_cannot_exceed_confirmed_short_quantity(self):
        with self.broker(positions=[position(option(), -1)]) as (ib, _):
            self.rejected(ib, self.order_args(action="BUY", qty=2))

    def test_pending_buyback_consumes_only_matching_con_id_remaining(self):
        for pending_id, requested, accepted in ((101, 2, False), (101, 1, True),
                                                (999, 2, True)):
            with self.subTest(con_id=pending_id, requested=requested), \
                 self.broker(positions=[position(option(), -3)],
                             trades=[pending(option(con_id=pending_id), action="BUY",
                                             total=3, remaining=2, filled=1)]) as (ib, _):
                args = self.order_args(action="BUY", qty=requested)
                (self.preflight if accepted else self.rejected)(ib, args)

    def test_stock_sell_respects_pending_remaining_not_total(self):
        for qty, accepted in ((100, True), (101, False)):
            with self.subTest(qty=qty), \
                 self.broker(shares=250, positions=[position(option("C"), -1)]) as (ib, stock):
                ib.openTrades.return_value = [pending(stock, total=100, remaining=50, filled=50)]
                (self.preflight if accepted else self.rejected)(ib, self.order_args(opt=False, qty=qty))

    def test_pending_buyback_does_not_release_call_cover_before_fill(self):
        call = option("C")
        with self.broker(shares=100, positions=[position(call, -1)],
                         trades=[pending(call, action="BUY")]) as (ib, _):
            self.rejected(ib, self.order_args(opt=False, qty=1))

    def test_uncertain_pending_remaining_blocks_new_risk(self):
        for remaining in (float("nan"), float("inf")):
            with self.subTest(remaining=remaining), \
                 self.broker(trades=[pending(option(), remaining=remaining)]) as (ib, _):
                self.rejected(ib, self.order_args())

    def test_put_collateral_exact_and_one_cent_short_for_100_200_500(self):
        for multiplier in (100, 200, 500):
            required = 40 * 2 * multiplier
            for cash, accepted in ((required, True), (required - 0.01, False)):
                for combo in (False, True):
                    with self.subTest(multiplier=multiplier, cash=cash, combo=combo), \
                         self.broker(option(multiplier=str(multiplier)), cash=cash) as (ib, _):
                        args = self.combo_args(qty=2) if combo else self.order_args(qty=2)
                        (self.preflight if accepted else self.rejected)(ib, args, combo)

    def test_call_cover_exact_and_one_share_short_for_100_200_500(self):
        for multiplier in (100, 200, 500):
            for shares, accepted in ((2 * multiplier, True), (2 * multiplier - 1, False)):
                for combo in (False, True):
                    with self.subTest(multiplier=multiplier, shares=shares, combo=combo), \
                         self.broker(option("C", str(multiplier)), shares=shares) as (ib, _):
                        args = (self.combo_args(preset="sell-call", qty=2) if combo
                                else self.order_args(right="C", qty=2))
                        (self.preflight if accepted else self.rejected)(ib, args, combo)

    def test_fractional_stock_quantity_is_rejected_without_broker_support(self):
        with self.broker(shares=100) as (ib, _):
            self.rejected(ib, self.order_args(opt=False, qty=0.5))

    def test_mixed_multiplier_puts_deduct_held_and_pending_remaining(self):
        positions = [position(option("P", "100", 101), -1)]
        trades = [pending(option("P", "200", 102), total=3, remaining=1, filled=2),
                  pending(option("P", "500", 103), status="Cancelled")]
        # 4,000 held + 8,000 pending + 20,000 new, all USD.
        for cash, accepted in ((32000, True), (31999.99, False)):
            with self.subTest(cash=cash), \
                 self.broker(option("P", "500", 104), cash=cash,
                             positions=positions, trades=trades) as (ib, _):
                (self.preflight if accepted else self.rejected)(ib, self.order_args())

    def test_mixed_multiplier_partial_call_orders_use_remaining(self):
        positions = [position(option("C", "100", 101), -1)]
        trades = [pending(option("C", "200", 102), total=3, remaining=1, filled=2),
                  pending(option("C", "500", 103), total=1, remaining=1, status="Cancelled")]
        # 100 held + 200 pending + 500 new = 800 shares; cancelled orders reserve none.
        for shares, accepted in ((800, True), (799, False)):
            with self.subTest(shares=shares), \
                 self.broker(option("C", "500", 104), shares=shares,
                             positions=positions, trades=trades) as (ib, _):
                (self.preflight if accepted else self.rejected)(ib, self.order_args(right="C"))

    def test_cross_currency_obligations_share_one_cash_pool(self):
        ib = MagicMock()
        ib.accountValues.return_value = [SimpleNamespace(tag="TotalCashValue", currency="USD", value="10000"),
                                         SimpleNamespace(tag="TotalCashValue", currency="BASE", value="10000")]
        hkd = option("P")
        hkd.currency, hkd.strike = "HKD", 40
        jpy = option("P")
        jpy.currency, jpy.strike = "JPY", 5000
        ib.portfolio.return_value = [position(hkd, -1)]  # 4,000 HKD
        ib.openTrades.return_value = [pending(jpy)]       # 500,000 JPY
        with patch.object(cli, "_fx", side_effect=lambda _ib, _base, cur: {"HKD": 8, "JPY": 100}[cur]):
            ok, _ = cli._coverage(ib, "HKD", 36000)
            self.assertTrue(ok)  # (10,000 - 500 - 5,000) * 8 = 36,000
            ok, _ = cli._coverage(ib, "HKD", 36000.01)
            self.assertFalse(ok)

    def test_base_aggregate_does_not_duplicate_detailed_cash(self):
        ib = MagicMock()
        ib.accountValues.return_value = [SimpleNamespace(tag="TotalCashValue", currency="USD", value="8000"),
                                         SimpleNamespace(tag="TotalCashValue", currency="BASE", value="8000")]
        ib.portfolio.return_value = []
        ib.openTrades.return_value = []
        with patch.object(cli, "_fx", return_value=8):
            self.assertFalse(cli._coverage(ib, "HKD", 64000.01)[0])

    def test_invalid_cash_and_missing_fx_do_not_inflate_collateral(self):
        for cash in ("nan", "inf", "-1"):
            with self.subTest(cash=cash), self.broker(cash=cash) as (ib, _):
                self.rejected(ib, self.order_args())
        ib = MagicMock()
        ib.accountValues.return_value = [SimpleNamespace(tag="TotalCashValue", currency="USD", value="10000")]
        ib.portfolio.return_value = []
        ib.openTrades.return_value = []
        with patch.object(cli, "_fx", return_value=None):
            self.assertFalse(cli._coverage(ib, "HKD", 1)[0])

    def test_multiple_accounts_or_mismatched_ownership_block_preflight(self):
        with self.broker() as (ib, _):
            ib.managedAccounts.return_value = ["DU1", "DU2"]
            self.rejected(ib, self.order_args())
        with self.broker() as (ib, _):
            ib.managedAccounts.return_value = ["DU1"]
            ib.portfolio.return_value[0].account = "DU2"
            self.rejected(ib, self.order_args())

    def test_known_complex_delivery_blocks_risk_increase(self):
        contract = option()
        contract.comboLegsDescrip = "cash and shares"
        with self.broker(contract) as (ib, _):
            self.rejected(ib, self.order_args())
        contract.comboLegsDescrip = ""
        with self.broker(contract) as (ib, _):
            ib.reqContractDetails.return_value = [SimpleNamespace(contract=contract, underSecType="BAG", underConId=1)]
            self.rejected(ib, self.order_args())

    def test_covered_call_refreshes_after_stock_fill(self):
        call = option("C")
        with self.broker(call, shares=0) as (ib, stock):
            def submit(contract, order):
                trade = SimpleNamespace(order=order,
                                        orderStatus=SimpleNamespace(status="Filled", filled=100,
                                                                    avgFillPrice=30), log=[])
                ib.trades.return_value.append(trade)
                return trade
            ib.placeOrder.side_effect = submit
            rc = self.confirm_run(ib, self.combo_args(preset="covered-call", stock_px=30.0))
            self.assertEqual(rc, 1)
            ib.placeOrder.assert_called_once()
            self.assertIs(ib.placeOrder.call_args.args[0], stock)

    def test_covered_call_second_leg_uses_refreshed_portfolio(self):
        call = option("C")
        with self.broker(call, shares=0) as (ib, stock):
            def submit(contract, order):
                if contract.secType == "STK":
                    trade = SimpleNamespace(order=order,
                                            orderStatus=SimpleNamespace(status="Filled", filled=100,
                                                                        avgFillPrice=30), log=[])
                    ib.trades.return_value.append(trade)
                    ib.portfolio.return_value = [position(stock, 100)]
                    return trade
                trade = SimpleNamespace(order=order,
                                        orderStatus=SimpleNamespace(status="Filled", filled=1,
                                                                    avgFillPrice=1), log=[])
                ib.trades.return_value.append(trade)
                return trade

            ib.placeOrder.side_effect = submit
            rc = self.confirm_run(ib, self.combo_args(preset="covered-call", stock_px=30.0))
            self.assertEqual(rc, 0)
            self.assertEqual(ib.placeOrder.call_count, 2)

    def test_malformed_existing_obligation_blocks_preflight(self):
        bad = option("C")
        bad.currency = ""
        with self.broker(positions=[position(bad, -1)]) as (ib, _):
            self.rejected(ib, self.order_args())
        with self.broker(trades=[pending(option(), remaining=float("nan"))]) as (ib, _):
            self.rejected(ib, self.order_args(action="BUY"))

    def test_source_currency_cash_deducts_foreign_put_liability(self):
        foreign = option()
        foreign.currency, foreign.strike = "HKD", 800
        with self.broker(positions=[position(foreign, -1)], cash=10000) as (ib, _):
            with patch.object(cli, "_fx", return_value=8):
                self.rejected(ib, self.order_args())

    def test_call_known_underlying_mismatch_blocks_both_entries(self):
        for combo in (False, True):
            with self.broker(option("C"), shares=100) as (ib, _):
                ib.reqContractDetails.return_value = [SimpleNamespace(contract=option("C"),
                                                                      underConId=2, underSecType="STK")]
                self.rejected(ib, self.combo_args(preset="sell-call") if combo else self.order_args(right="C"), combo)

    def test_stock_sell_must_match_qualified_con_id(self):
        with self.broker(shares=100) as (ib, _):
            qualified = Stock("AAPL", "SMART", "USD")
            qualified.conId = 2
            with patch.object(cli, "_stock", return_value=qualified):
                self.rejected(ib, self.order_args(opt=False, qty=100))

    def test_inconsistent_zero_remaining_cannot_erase_pending_order(self):
        with self.broker(option("C"), shares=100) as (ib, stock):
            ib.openTrades.return_value = [pending(stock, total=100, remaining=0, filled=0)]
            self.rejected(ib, self.order_args(right="C"))

    def test_covered_call_nan_fill_stops_before_option(self):
        with self.broker(option("C"), shares=0) as (ib, stock):
            def submit(contract, order):
                trade = SimpleNamespace(order=order,
                                        orderStatus=SimpleNamespace(status="Filled", filled=float("nan"),
                                                                    avgFillPrice=30), log=[])
                ib.trades.return_value.append(trade)
                return trade
            ib.placeOrder.side_effect = submit
            rc = self.confirm_run(ib, self.combo_args(preset="covered-call", stock_px=30.0))
            self.assertEqual(rc, 1)
            ib.placeOrder.assert_called_once()

    def test_stock_fill_followed_by_external_sale_cannot_cover_call(self):
        with self.broker(option("C"), shares=0) as (ib, stock):
            def submit(contract, order):
                trade = SimpleNamespace(order=order,
                                        orderStatus=SimpleNamespace(status="Filled", filled=100,
                                                                    avgFillPrice=30), log=[])
                ib.trades.return_value.append(trade)
                ib.portfolio.return_value = [position(stock, 100)]
                ib.openTrades.return_value = [pending(stock, total=100, remaining=100)]
                return trade

            ib.placeOrder.side_effect = submit
            rc = self.confirm_run(ib, self.combo_args(preset="covered-call", stock_px=30.0))
            self.assertEqual(rc, 1)
            ib.placeOrder.assert_called_once()

    def test_held_put_nan_and_fractional_stock_shortage_rejected(self):
        with self.broker(positions=[position(option(), float("nan"))]) as (ib, _):
            self.rejected(ib, self.order_args())
        with self.broker(option("C"), shares=99.5) as (ib, _):
            self.rejected(ib, self.combo_args(preset="covered-call"), combo=True)

    def test_same_symbol_distinct_stock_contracts_cannot_combine_cover(self):
        with self.broker(option("C"), shares=100) as (ib, _):
            other = Stock("AAPL", "SMART", "USD")
            other.conId = 2
            ib.portfolio.return_value.append(position(other, -100))
            self.rejected(ib, self.order_args(right="C"))


if __name__ == "__main__":
    unittest.main()
