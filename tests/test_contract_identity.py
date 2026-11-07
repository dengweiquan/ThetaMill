"""Offline regressions for option identity and multiplier-based collateral."""
from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from thetamill import cli


EXPIRY = (date.today() + timedelta(days=30)).strftime("%Y%m%d")


def option(multiplier="100", con_id=101, right="C", strike=100, **overrides):
    contract = cli.Option("AAPL", EXPIRY, strike, right, "SMART",
                          multiplier=multiplier, currency="USD", tradingClass="AAPL")
    contract.conId = con_id
    for key, value in overrides.items():
        setattr(contract, key, value)
    return contract


def pending(contract, remaining=1, status="Submitted"):
    return SimpleNamespace(contract=contract,
                           order=SimpleNamespace(action="SELL", totalQuantity=remaining),
                           orderStatus=SimpleNamespace(status=status, remaining=remaining, filled=0))


class MultiplierTests(unittest.TestCase):
    def test_valid_values_preserve_noninteger_multipliers(self):
        ib = MagicMock()
        for value, expected in [(10, 10), (100.0, 100), ("500", 500), ("100.0", 100),
                                ("12.5", 12.5), (0.5, 0.5)]:
            with self.subTest(value=value):
                self.assertEqual(cli._multiplier(ib, option(value)), expected)
        ib.reqContractDetails.assert_not_called()

    def test_unknown_invalid_and_nonfinite_values_never_default_to_100(self):
        ib = MagicMock()
        ib.reqContractDetails.return_value = []
        for value in [None, "", "invalid", 0, -10, True, False, "nan", "inf", "-inf",
                      float("nan"), float("inf"), "1e999", object()]:
            with self.subTest(value=value), self.assertRaisesRegex(SystemExit, "无法确认.*乘数"):
                cli._multiplier(ib, option(value))

    def test_details_must_match_the_requested_con_id(self):
        ib = MagicMock()
        ib.reqContractDetails.return_value = [
            SimpleNamespace(contract=option("100", con_id=999)),
            SimpleNamespace(contract=option("12.5")),
        ]
        self.assertEqual(cli._multiplier(ib, option("")), 12.5)

    def test_missing_conflicting_or_invalid_details_are_rejected(self):
        ib = MagicMock()
        for contracts in [[], [option("100", con_id=999)],
                          [option("10"), option("100")], [option("nan")],
                          [option("100"), option("")]]:
            ib.reqContractDetails.return_value = [SimpleNamespace(contract=c) for c in contracts]
            with self.subTest(contracts=contracts), self.assertRaises(SystemExit):
                cli._multiplier(ib, option(""))
        ib.reqContractDetails.side_effect = RuntimeError("offline")
        with self.assertRaises(SystemExit):
            cli._multiplier(ib, option(""))

    def test_mixed_existing_and_pending_obligations_use_each_contract_multiplier(self):
        ib = MagicMock()
        ib.portfolio.return_value = [
            SimpleNamespace(contract=option("10"), position=-2),
            SimpleNamespace(contract=option("500", con_id=102), position=-1),
            SimpleNamespace(contract=option("12.5", right="P"), position=-2),
        ]
        ib.openTrades.return_value = [pending(option("12.5"), remaining=2),
                                     pending(option("500", right="P"), remaining=3),
                                     pending(option("", right="P"), status="Cancelled")]
        self.assertEqual(cli._short_calls_shares(ib, "AAPL", "USD"), 520)
        self.assertEqual(cli._pending_short_calls_shares(ib, "AAPL", "USD"), 25)
        self.assertEqual(cli._short_puts_collateral(ib, "USD"), 2500)
        self.assertEqual(cli._pending_cash_puts(ib, "USD"), 150000)

    def test_all_obligation_helpers_reject_unknown_multipliers(self):
        for helper, right, is_pending in [
            (cli._short_calls_shares, "C", False),
            (cli._pending_short_calls_shares, "C", True),
            (cli._short_puts_collateral, "P", False),
            (cli._pending_cash_puts, "P", True),
        ]:
            ib = MagicMock()
            contract = option("", right=right)
            ib.reqContractDetails.return_value = []
            ib.portfolio.return_value = [SimpleNamespace(contract=contract, position=-1)]
            ib.openTrades.return_value = [pending(contract)] if is_pending else []
            args = (ib, "AAPL", "USD") if right == "C" else (ib, "USD")
            with self.subTest(helper=helper.__name__), self.assertRaises(SystemExit):
                helper(*args)


class QualificationIdentityTests(unittest.TestCase):
    def test_ambiguous_matches_are_not_selected_by_list_position(self):
        ib = MagicMock()
        ib.qualifyContracts.return_value = [option("10"), option("100", con_id=102)]
        self.assertEqual(cli._opt(ib, "AAPL", EXPIRY, 100, "C", "USD"), (None, None))

    def test_explicit_identity_disambiguates_and_is_sent_to_ib(self):
        ib = MagicMock()
        mini = option("10", tradingClass="AAPL7")
        ib.qualifyContracts.return_value = [option("100", con_id=102), mini]
        result, exchange = cli._opt(ib, "AAPL", EXPIRY, 100, "C", "USD",
                                    multiplier="10.0", trading_class="AAPL7", con_id=101)
        self.assertIs(result, mini)
        self.assertEqual(exchange, "SMART")
        request = ib.qualifyContracts.call_args.args[0]
        self.assertEqual((request.multiplier, request.tradingClass, request.conId),
                         ("10", "AAPL7", 101))

    def test_mismatched_qualification_is_rejected_on_every_identity_field(self):
        mismatches = {"symbol": "MSFT", "currency": "HKD", "right": "P", "strike": 105,
                      "lastTradeDateOrContractMonth": "20991231", "secType": "STK",
                      "multiplier": "500", "tradingClass": "AAPL7", "conId": 102}
        for key, value in mismatches.items():
            ib = MagicMock()
            ib.qualifyContracts.return_value = [option(**{key: value})]
            with self.subTest(field=key):
                self.assertEqual(cli._opt(ib, "AAPL", EXPIRY, 100, "C", "USD",
                                          multiplier=100, trading_class="AAPL", con_id=101),
                                 (None, None))

    def test_exchange_fallback_preserves_requested_identity(self):
        ib = MagicMock()
        qualified = option("500", symbol="700", currency="HKD", tradingClass="TCH")
        ib.qualifyContracts.side_effect = [[], [qualified]]
        result, exchange = cli._opt(ib, "700", EXPIRY, 100, "C", "HKD",
                                    multiplier=500, trading_class="TCH", con_id=101)
        self.assertIs(result, qualified)
        self.assertEqual(exchange, "SMART")
        requests = [c.args[0] for c in ib.qualifyContracts.call_args_list]
        self.assertEqual([c.exchange for c in requests], ["SEHK", "SMART"])
        for request in requests:
            self.assertEqual((request.multiplier, request.tradingClass, request.conId),
                             ("500", "TCH", 101))

    def test_invalid_explicit_multiplier_stops_before_qualification(self):
        ib = MagicMock()
        with self.assertRaises(SystemExit):
            cli._opt(ib, "AAPL", EXPIRY, 100, "C", "USD", multiplier="nan")
        ib.qualifyContracts.assert_not_called()

    def test_missing_candidate_does_not_shift_strike_to_next_qualified_contract(self):
        ib = MagicMock()
        qualified = option("10", strike=110)
        ib.qualifyContracts.side_effect = [[], [qualified]]
        chain = SimpleNamespace(exchange="SMART", tradingClass="AAPL", multiplier="10")
        result, strike = cli._resolve_option(ib, "AAPL", EXPIRY, [100, 110], 100,
                                             "C", "USD", chain)
        self.assertIs(result, qualified)
        self.assertEqual(strike, qualified.strike)
        self.assertEqual(strike, 110)
        self.assertEqual([c.args[0].strike for c in ib.qualifyContracts.call_args_list], [100, 110])

    def test_unrequested_strike_is_not_used_even_when_it_has_a_con_id(self):
        ib = MagicMock()
        ib.qualifyContracts.return_value = [option("10", strike=150)]
        chain = SimpleNamespace(exchange="SMART", tradingClass="AAPL", multiplier="10")
        self.assertEqual(cli._resolve_option(ib, "AAPL", EXPIRY, [100, 110], 100,
                                             "C", "USD", chain), (None, None))


class PlanRunIdentityTests(unittest.TestCase):
    @contextmanager
    def environment(self, multiplier="100", shares=0, positions=(), trades=()):
        ib = MagicMock()
        stock = cli.Stock("AAPL", "SMART", "USD")
        stock.conId = 1
        ib.portfolio.return_value = [SimpleNamespace(contract=stock, position=shares,
                                                    averageCost=80)] + list(positions)
        ib.openTrades.return_value = list(trades)
        ib.reqContractDetails.return_value = []
        ib.accountValues.return_value = [SimpleNamespace(tag="TotalCashValue", value="1000000",
                                                        currency="USD")]
        chain = SimpleNamespace(exchange="SMART", tradingClass="AAPL", multiplier=multiplier,
                                expirations={EXPIRY}, strikes={90, 100, 110})

        def qualify(contract):
            contract.conId = contract.conId or (101 if contract.right == "C" else 102)
            contract.multiplier = contract.multiplier or multiplier
            contract.tradingClass = contract.tradingClass or "AAPL"
            return [contract]

        ib.qualifyContracts.side_effect = qualify
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(cli, "CFG_PATH", Path(__file__)), \
             patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
             patch.object(cli, "connect", return_value=ib), \
             patch.object(cli, "finish"), \
             patch.object(cli, "_chain_safe", return_value=(stock, chain)), \
             patch.object(cli, "_spot", return_value=100), \
             patch.object(cli, "_quote_premium", return_value=(2, "bid")), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.plan_path = Path(directory) / "plan.json"
            yield ib

    def plan(self, preset="auto", qty=1):
        args = argparse.Namespace(symbol="AAPL", symbols=None, currency="USD", preset=preset,
                                  dte_min=20, dte_max=40, target_dte=30, otm=0.05, qty=qty,
                                  json_out=str(self.plan_path), paper=True, delayed=False,
                                  port=None, universe=None)
        self.assertEqual(cli.cmd_plan(cli.DEFAULT_CFG, args), 0)
        return json.loads(self.plan_path.read_text())["items"][0]

    def run_args(self, **overrides):
        args = argparse.Namespace(file=str(self.plan_path), i=1, net=None, stock_px=None,
                                  code="", live_ok=False, paper=True, port=None, delayed=False)
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_auto_preset_uses_resolved_multiplier_not_a_100_share_threshold(self):
        for mult, shares, expected in [("10", 10, "sell-call"), ("100", 100, "sell-call"),
                                       ("500", 100, "cash-put"), ("500", 500, "sell-call"),
                                       ("12.5", 12.5, "sell-call"), ("100", 0, "cash-put")]:
            with self.subTest(mult=mult, shares=shares), self.environment(mult, shares) as ib:
                item = self.plan()
                self.assertEqual(item["preset"], expected)
                self.assertEqual(item["mult"], float(mult))
                self.assertEqual(item["strike"], 110 if expected == "sell-call" else 90)
                ib.placeOrder.assert_not_called()

    def test_auto_falls_back_to_put_when_no_call_qualifies(self):
        for shares in (10, 50, 100):
            with self.subTest(shares=shares), self.environment(shares=shares) as ib:
                qualify = ib.qualifyContracts.side_effect
                ib.qualifyContracts.side_effect = lambda c: [] if c.right == "C" else qualify(c)
                item = self.plan()
                self.assertEqual(item["preset"], "cash-put")
                self.assertEqual((item["strike"], item["mult"], item["conId"]), (90, 100, 102))
                self.assertEqual(item["collateral"], 9000)
                self.assertIn("无可用 Call", item["note"])
                ib.placeOrder.assert_not_called()

    def test_explicit_sell_call_does_not_fall_back_when_no_call_qualifies(self):
        with self.environment(shares=100) as ib:
            ib.qualifyContracts.return_value = []
            ib.qualifyContracts.side_effect = None
            args = argparse.Namespace(symbol="AAPL", symbols=None, currency="USD",
                                      preset="sell-call", dte_min=20, dte_max=40, target_dte=30,
                                      otm=0.05, qty=1, json_out=str(self.plan_path), paper=True,
                                      delayed=False, port=None, universe=None)
            self.assertEqual(cli.cmd_plan(cli.DEFAULT_CFG, args), 0)
            self.assertFalse(self.plan_path.exists())
            self.assertTrue(all(c.args[0].right == "C"
                                for c in ib.qualifyContracts.call_args_list))
            ib.placeOrder.assert_not_called()

    def test_call_quantity_and_collateral_use_actual_net_available_shares(self):
        positions = [SimpleNamespace(contract=option("10"), position=-2)]
        trades = [pending(option("500"), remaining=1)]
        with self.environment("10", shares=550, positions=positions, trades=trades):
            item = self.plan(qty=10)
            self.assertEqual(item["preset"], "sell-call")
            self.assertEqual(item["qty"], 3)
            self.assertEqual(item["collateral_txt"], "30 股")
            self.assertEqual(item["collateral"], 2400)

    def test_auto_switches_to_put_after_deducting_mixed_call_obligations(self):
        positions = [SimpleNamespace(contract=option("500"), position=-1)]
        trades = [pending(option("100"), remaining=1)]
        with self.environment("10", shares=605, positions=positions, trades=trades):
            item = self.plan()
            self.assertEqual(item["preset"], "cash-put")
            self.assertEqual(item["collateral"], 900)

    def test_put_collateral_and_saved_identity_match_qualified_contract(self):
        with self.environment("500") as ib:
            item = self.plan(preset="cash-put", qty=2)
            qualified = ib.qualifyContracts.call_args.args[0]
            self.assertEqual(item["collateral"], 90000)
            self.assertEqual((item["strike"], item["mult"], item["conId"], item["tradingClass"],
                              item["exchange"]),
                             (qualified.strike, 500, qualified.conId, qualified.tradingClass,
                              qualified.exchange))

    def test_unknown_resolved_multiplier_aborts_plan_without_saving(self):
        with self.environment("") as ib, self.assertRaises(SystemExit):
            cli.cmd_plan(cli.DEFAULT_CFG, argparse.Namespace(
                symbol="AAPL", symbols=None, currency="USD", preset="cash-put", dte_min=20,
                dte_max=40, target_dte=30, otm=0.05, qty=1, json_out=str(self.plan_path),
                paper=True, delayed=False, port=None, universe=None))
        self.assertFalse(self.plan_path.exists())
        ib.placeOrder.assert_not_called()

    def test_run_forwards_all_saved_identity_fields(self):
        with self.environment("10", shares=10):
            item = self.plan()
            with patch.object(cli, "cmd_combo", return_value=2) as combo:
                self.assertEqual(cli.cmd_run(cli.DEFAULT_CFG, self.run_args()), 2)
            forwarded = combo.call_args.args[1]
            self.assertEqual((forwarded.con_id, forwarded.multiplier, forwarded.trading_class,
                              forwarded.exchange, forwarded.k1),
                             (item["conId"], item["mult"], item["tradingClass"], item["exchange"],
                              item["strike"]))

    def test_legacy_or_invalid_identity_plans_require_regeneration(self):
        with self.environment("10", shares=10):
            self.plan()
            original = json.loads(self.plan_path.read_text())
            for field, invalid in [("conId", None), ("conId", True), ("conId", 0),
                                   ("mult", None), ("mult", "nan"), ("tradingClass", ""),
                                   ("exchange", "")]:
                doc = json.loads(json.dumps(original))
                if invalid is None:
                    doc["items"][0].pop(field)
                else:
                    doc["items"][0][field] = invalid
                self.plan_path.write_text(json.dumps(doc))
                with self.subTest(field=field, invalid=invalid), \
                     patch.object(cli, "cmd_combo") as combo, \
                     self.assertRaisesRegex(SystemExit, "重新运行 plan"):
                    cli.cmd_run(cli.DEFAULT_CFG, self.run_args())
                combo.assert_not_called()

    def test_run_refuses_changed_con_id_multiplier_class_or_strike_before_approval(self):
        for changes in [{"conId": 999}, {"multiplier": "100"}, {"tradingClass": "AAPL7"},
                        {"strike": 120}]:
            with self.subTest(changes=changes), self.environment("10", shares=10) as ib:
                self.plan()
                ib.qualifyContracts.side_effect = None
                changed = option("10", strike=110)
                for key, value in changes.items():
                    setattr(changed, key, value)
                ib.qualifyContracts.return_value = [changed]
                with patch.object(cli, "_code") as code, self.assertRaises(SystemExit):
                    cli.cmd_run(cli.DEFAULT_CFG, self.run_args(code="APPROVED"))
                code.assert_not_called()
                ib.placeOrder.assert_not_called()

    def test_run_preflight_and_approved_order_keep_exact_plan_identity(self):
        with self.environment("10", shares=10) as ib:
            item = self.plan()
            with patch.object(cli, "_code", wraps=cli._code) as code:
                self.assertEqual(cli.cmd_run(cli.DEFAULT_CFG, self.run_args()), 2)
                approval = cli._code(code.call_args.args[0])
            ib.placeOrder.assert_not_called()
            trade = SimpleNamespace(order=SimpleNamespace(orderId=123), log=[],
                                    orderStatus=SimpleNamespace(status="Filled", filled=1,
                                                                avgFillPrice=2))
            ib.placeOrder.return_value = trade
            with patch.object(cli, "_is_code_consumed", return_value=False), \
                 patch.object(cli, "_consume_code"), patch.object(cli, "_write_audit"):
                self.assertEqual(cli.cmd_run(cli.DEFAULT_CFG, self.run_args(code=approval)), 0)
            ib.placeOrder.assert_called_once()
            contract, order = ib.placeOrder.call_args.args
            self.assertEqual((contract.conId, contract.multiplier, contract.tradingClass,
                              contract.strike, order.totalQuantity),
                             (item["conId"], "10", item["tradingClass"], item["strike"], 1))


if __name__ == "__main__":
    unittest.main()
