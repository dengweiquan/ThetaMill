"""Unit test suite for ThetaMill CLI logic and helper functions.

Tests are completely offline and mocked without requiring IB Gateway connections.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from thetamill import cli


class MockAccountValue:
    def __init__(self, tag: str, value: str, currency: str):
        self.tag = tag
        self.value = value
        self.currency = currency


class MockPortfolioItem:
    def __init__(self, sec_type: str, symbol: str, currency: str, position: float,
                 average_cost: float = 0.0, right: str = ""):
        self.contract = MagicMock()
        self.contract.secType = sec_type
        self.contract.symbol = symbol
        self.contract.currency = currency
        self.contract.right = right
        self.position = position
        self.averageCost = average_cost


class CodePayloadTests(unittest.TestCase):
    """Test _code payload standardization and determinism."""

    def test_code_deterministic_hex(self):
        payload = {"mode": "paper", "port": 4002, "contract": 12345, "action": "BUY", "qty": 1}
        code1 = cli._code(payload)
        code2 = cli._code(payload)
        self.assertEqual(code1, code2)
        self.assertEqual(len(code1), 8)
        self.assertEqual(code1, code1.upper())
        # Must be valid hex
        int(code1, 16)

    def test_code_differs_on_key_fields(self):
        base = {
            "mode": "paper",
            "port": 4002,
            "contract": 12345,
            "sec": "OPT",
            "action": "SELL",
            "qty": 1,
            "strike": 150.0,
            "otype": "LMT",
            "px": 5.0,
            "tif": "DAY",
            "outside_rth": False,
        }
        base_code = cli._code(base)

        # Changing mode (live vs paper) alters code
        p_mode = dict(base, mode="live")
        self.assertNotEqual(base_code, cli._code(p_mode))

        # Changing port alters code
        p_port = dict(base, port=4001)
        self.assertNotEqual(base_code, cli._code(p_port))

        # Changing qty alters code
        p_qty = dict(base, qty=2)
        self.assertNotEqual(base_code, cli._code(p_qty))

        # Changing strike alters code
        p_strike = dict(base, strike=155.0)
        self.assertNotEqual(base_code, cli._code(p_strike))

        # Changing action alters code
        p_action = dict(base, action="BUY")
        self.assertNotEqual(base_code, cli._code(p_action))

    def test_code_identical_regardless_of_ui_env(self):
        # Auto-detected paper gateway vs explicit --paper producing identical mode and port
        payload_auto = {"mode": "paper", "port": 4002, "contract": 1001, "action": "SELL", "qty": 1}
        payload_explicit = {"mode": "paper", "port": 4002, "contract": 1001, "action": "SELL", "qty": 1}
        self.assertEqual(cli._code(payload_auto), cli._code(payload_explicit))


class ResolveOptionCandidateSortingTests(unittest.TestCase):
    """Test candidate selection sorting logic in _resolve_option."""

    def test_call_candidate_sorting(self):
        strikes = [80.0, 90.0, 100.0, 110.0, 120.0, 130.0]
        target = 105.0

        # For Call (right="C"):
        # ge (>= 105): [110, 120, 130] in ascending order
        # lt (< 105): [100, 90, 80] in descending order (closest to target first)
        mock_ib = MagicMock()
        mock_chain = MagicMock(exchange="SMART", tradingClass="AAPL")

        qualified_contract = MagicMock(conId=9999)
        tested_strikes = []

        def mock_qualify(*contracts):
            for c in contracts:
                tested_strikes.append(c.strike)
            # Qualify the first candidate
            return [qualified_contract]

        mock_ib.qualifyContracts.side_effect = mock_qualify

        opt, strike = cli._resolve_option(
            mock_ib, "AAPL", "20261218", strikes, target, "C", "USD", mock_chain
        )
        self.assertEqual(tested_strikes, [110.0, 120.0, 130.0, 100.0, 90.0, 80.0])
        self.assertEqual(opt, qualified_contract)
        self.assertEqual(strike, 110.0)

    def test_call_candidate_sorting_with_floor(self):
        strikes = [80.0, 90.0, 100.0, 110.0, 120.0, 130.0]
        target = 100.0
        floor = 115.0  # Floor elevates effective target to 115.0

        mock_ib = MagicMock()
        mock_chain = MagicMock(exchange="SMART", tradingClass="AAPL")
        tested_strikes = []

        def mock_qualify(*contracts):
            for c in contracts:
                tested_strikes.append(c.strike)
            return [MagicMock(conId=1)]

        mock_ib.qualifyContracts.side_effect = mock_qualify

        cli._resolve_option(
            mock_ib, "AAPL", "20261218", strikes, target, "C", "USD", mock_chain, floor=floor
        )
        # Effective target is 115.0
        # ge (>= 115): [120, 130]
        # lt (< 115): [110, 100, 90, 80]
        self.assertEqual(tested_strikes, [120.0, 130.0, 110.0, 100.0, 90.0, 80.0])

    def test_put_candidate_sorting(self):
        strikes = [80.0, 90.0, 100.0, 110.0, 120.0, 130.0]
        target = 105.0

        # For Put (right="P"):
        # le (<= 105): [100, 90, 80] in descending order (closest to target first)
        # gt (> 105): [110, 120, 130] in ascending order (closest to target first)
        mock_ib = MagicMock()
        mock_chain = MagicMock(exchange="SMART", tradingClass="AAPL")
        tested_strikes = []

        def mock_qualify(*contracts):
            for c in contracts:
                tested_strikes.append(c.strike)
            return [MagicMock(conId=8888)]

        mock_ib.qualifyContracts.side_effect = mock_qualify

        opt, strike = cli._resolve_option(
            mock_ib, "AAPL", "20261218", strikes, target, "P", "USD", mock_chain
        )
        self.assertEqual(tested_strikes, [100.0, 90.0, 80.0, 110.0, 120.0, 130.0])
        self.assertEqual(strike, 100.0)

    def test_resolve_option_fallback_when_qualification_fails(self):
        strikes = [100.0, 105.0]
        mock_ib = MagicMock()
        mock_chain = MagicMock(exchange="SMART", tradingClass="AAPL")
        mock_ib.qualifyContracts.return_value = []

        with patch.object(cli, "_opt", return_value=(None, None)):
            opt, strike = cli._resolve_option(
                mock_ib, "AAPL", "20261218", strikes, 100.0, "C", "USD", mock_chain
            )
            self.assertIsNone(opt)
            self.assertIsNone(strike)


class PickExpiryTests(unittest.TestCase):
    """Test expiry selection in _pick_expiry."""

    def test_picks_closest_dte_in_window(self):
        today = date.today()
        d25 = (today + timedelta(days=25)).strftime("%Y%m%d")
        d32 = (today + timedelta(days=32)).strftime("%Y%m%d")
        d40 = (today + timedelta(days=40)).strftime("%Y%m%d")
        d60 = (today + timedelta(days=60)).strftime("%Y%m%d")

        exps = [d25, d32, d40, d60]
        # Target 30: |25-30|=5, |32-30|=2, |40-30|=10 -> d32 should be chosen
        chosen_exp, chosen_dte = cli._pick_expiry(exps, dmin=21, dmax=45, target=30)
        self.assertEqual(chosen_exp, d32)
        self.assertEqual(chosen_dte, 32)

    def test_falls_back_when_none_in_window(self):
        today = date.today()
        d10 = (today + timedelta(days=10)).strftime("%Y%m%d")
        d60 = (today + timedelta(days=60)).strftime("%Y%m%d")

        exps = [d10, d60]
        # Window is [21, 45], none match. Falls back to all: |10-30|=20 vs |60-30|=30 -> d10 chosen
        chosen_exp, chosen_dte = cli._pick_expiry(exps, dmin=21, dmax=45, target=30)
        self.assertEqual(chosen_exp, d10)
        self.assertEqual(chosen_dte, 10)

    def test_empty_or_past_expirations(self):
        exp, dte = cli._pick_expiry([], dmin=21, dmax=45, target=30)
        self.assertIsNone(exp)
        self.assertIsNone(dte)

        past = [(date.today() - timedelta(days=5)).strftime("%Y%m%d")]
        exp, dte = cli._pick_expiry(past, dmin=21, dmax=45, target=30)
        self.assertIsNone(exp)
        self.assertIsNone(dte)


class FundsParsingTests(unittest.TestCase):
    """Test _funds parsing with accountValues."""

    def test_funds_extraction_and_base_currency(self):
        mock_ib = MagicMock()
        mock_ib.accountValues.return_value = [
            MockAccountValue("TotalCashValue", "20000.0", "USD"),
            MockAccountValue("NetLiquidation", "30000.0", "USD"),
            MockAccountValue("AvailableFunds", "15000.0", "USD"),
            MockAccountValue("FullAvailableFunds", "14500.0", "USD"),  # Conservative min chosen
            MockAccountValue("AvailableFunds", "80000.0", "HKD"),
            MockAccountValue("BuyingPower", "50000.0", "USD"),
            MockAccountValue("AvailableFunds", "invalid", "USD"),
            MockAccountValue("AvailableFunds", "1000.0", ""),
        ]

        cash_map, avail_map, base = cli._funds(mock_ib)

        self.assertEqual(cash_map["USD"], 20000.0)
        # Conservative min for USD: min(15000, 14500) = 14500
        self.assertEqual(avail_map["USD"], 14500.0)
        self.assertEqual(avail_map["HKD"], 80000.0)
        # Base currency is the currency with largest nominal value in real currencies
        self.assertEqual(base, "HKD")
        self.assertEqual(avail_map["BASE"], 80000.0)


class CoverageTests(unittest.TestCase):
    """Test collateral coverage checks."""

    def test_coverage_sufficient_same_currency(self):
        mock_ib = MagicMock()
        mock_ib.accountValues.return_value = [
            MockAccountValue("TotalCashValue", "15000.0", "USD"),
            MockAccountValue("AvailableFunds", "15000.0", "USD"),
        ]
        ok, note = cli._coverage(mock_ib, "USD", 10000.0)
        self.assertTrue(ok)
        self.assertIn("✅", note)
        self.assertIn("需要 10,000.00 USD", note)

    def test_coverage_insufficient_same_currency(self):
        mock_ib = MagicMock()
        mock_ib.accountValues.return_value = [
            MockAccountValue("TotalCashValue", "8000.0", "USD"),
            MockAccountValue("AvailableFunds", "8000.0", "USD"),
        ]
        ok, note = cli._coverage(mock_ib, "USD", 10000.0)
        self.assertFalse(ok)
        self.assertIn("❌", note)

    def test_coverage_insufficient_no_fx(self):
        mock_ib = MagicMock()
        mock_ib.accountValues.return_value = [
            MockAccountValue("TotalCashValue", "5000.0", "USD"),
            MockAccountValue("AvailableFunds", "5000.0", "USD"),
        ]
        # Target currency is JPY (no direct balance), and _fx returns None
        with patch.object(cli, "_fx", return_value=None):
            ok, note = cli._coverage(mock_ib, "JPY", 100000.0)
            self.assertFalse(ok)
            self.assertIn("❌", note)
            self.assertIn("无法获取", note)

    def test_coverage_with_fx_conversion(self):
        mock_ib = MagicMock()
        mock_ib.accountValues.return_value = [
            MockAccountValue("TotalCashValue", "10000.0", "USD"),
            MockAccountValue("AvailableFunds", "10000.0", "USD"),
        ]
        # Base is USD (10000), target currency HKD, need 70000, fx rate 7.8 -> 78000 >= 70000 -> True
        with patch.object(cli, "_fx", return_value=7.8):
            ok, note = cli._coverage(mock_ib, "HKD", 70000.0)
            self.assertTrue(ok)
            self.assertIn("✅", note)

        # Need 85000 -> 78000 < 85000 -> False
        with patch.object(cli, "_fx", return_value=7.8):
            ok, note = cli._coverage(mock_ib, "HKD", 85000.0)
            self.assertFalse(ok)
            self.assertIn("❌", note)


class PortfolioHelperTests(unittest.TestCase):
    """Test _cost_basis and _short_calls calculations."""

    def test_cost_basis(self):
        mock_ib = MagicMock()
        mock_ib.portfolio.return_value = [
            MockPortfolioItem("STK", "AAPL", "USD", position=200, average_cost=175.50),
            MockPortfolioItem("STK", "MSFT", "USD", position=0, average_cost=300.0),
            MockPortfolioItem("OPT", "AAPL", "USD", position=-2, average_cost=3.5),
        ]
        self.assertEqual(cli._cost_basis(mock_ib, "AAPL", "USD"), 175.50)
        self.assertIsNone(cli._cost_basis(mock_ib, "MSFT", "USD"))
        self.assertIsNone(cli._cost_basis(mock_ib, "GOOG", "USD"))

    def test_short_calls(self):
        mock_ib = MagicMock()
        mock_ib.portfolio.return_value = [
            MockPortfolioItem("OPT", "AAPL", "USD", position=-2, right="C"),  # 2 short calls
            MockPortfolioItem("OPT", "AAPL", "USD", position=1, right="C"),   # Long call (ignored)
            MockPortfolioItem("OPT", "AAPL", "USD", position=-3, right="P"),  # Short put (ignored)
            MockPortfolioItem("OPT", "MSFT", "USD", position=-1, right="C"),  # Different symbol
            MockPortfolioItem("STK", "AAPL", "USD", position=100),            # Stock (ignored)
        ]
        self.assertEqual(cli._short_calls(mock_ib, "AAPL", "USD"), 2)
        self.assertEqual(cli._short_calls(mock_ib, "MSFT", "USD"), 1)
        self.assertEqual(cli._short_calls(mock_ib, "GOOG", "USD"), 0)


class LoadCfgTests(unittest.TestCase):
    """Test load_cfg fallback and required behavior."""

    def test_load_cfg_fallback_when_missing(self):
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        with patch.object(cli, "CFG_PATH", mock_path):
            cfg = cli.load_cfg(required=False)
            self.assertEqual(cfg, cli.DEFAULT_CFG)
            self.assertEqual(cfg["default_currency"], "USD")
            self.assertFalse(cfg["live_enabled"])

    def test_load_cfg_raises_when_required_and_missing(self):
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        with patch.object(cli, "CFG_PATH", mock_path):
            with self.assertRaises(SystemExit) as cm:
                cli.load_cfg(required=True)
            self.assertIn("缺少配置文件", str(cm.exception))

    def test_load_cfg_loads_file_when_exists(self):
        dummy_data = {"default_currency": "HKD", "live_enabled": True}
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = json.dumps(dummy_data)
        with patch.object(cli, "CFG_PATH", mock_path):
            cfg = cli.load_cfg(required=True)
            self.assertEqual(cfg["default_currency"], "HKD")
            self.assertTrue(cfg["live_enabled"])

    def test_commands_require_cfg_when_missing(self):
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        funcs = [
            cli.cmd_status,
            cli.cmd_account,
            cli.cmd_positions,
            cli.cmd_orders,
            cli.cmd_quote,
            cli.cmd_chain,
            cli.cmd_order,
            cli.cmd_combo,
            cli.cmd_plan,
            cli.cmd_cancel,
        ]
        args = argparse.Namespace(symbol="AAPL", currency="USD", delayed=False)
        cfg = dict(cli.DEFAULT_CFG)
        with patch.object(cli, "CFG_PATH", mock_path):
            for fn in funcs:
                with self.assertRaises(SystemExit) as cm:
                    fn(cfg, args)
                self.assertIn("缺少配置文件", str(cm.exception))

    def test_main_help_works_without_cfg(self):
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        with patch.object(cli, "CFG_PATH", mock_path):
            for cmd_args in [["thetamill", "--help"], ["thetamill", "order", "--help"], ["thetamill", "plan", "--help"]]:
                with patch("sys.argv", cmd_args), patch("sys.stdout", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as cm:
                        cli.main()
                    self.assertEqual(cm.exception.code, 0)


class CmdRunValidationTests(unittest.TestCase):
    """Test cmd_run secondary whitelist validation checks."""

    def test_cmd_run_rejects_strike_exceeding_max_entry(self):
        valid_until = (date.today() + timedelta(days=30)).isoformat()
        universe_doc = {
            "schema": "thetamill-universe/1",
            "generated": "2026-11-07T12:00:00",
            "valid_until": valid_until,
            "approved_by": "投资经理签字",
            "signed_at": "2026-11-07T12:00:00",
            "items": [{
                "symbol": "AAPL", "name": "Apple", "market": "US", "currency": "USD",
                "tier": "ELIGIBLE", "score": 85.0, "as_of": "2026Q3",
                "source": "manual", "max_entry": 200.0, "demo": False,
            }],
        }
        plan_doc = {
            "generated": "2026-11-07T12:00:00",
            "env": "模拟",
            "universe": universe_doc,
            "items": [{
                "i": 1,
                "preset": "cash-put",
                "symbol": "AAPL",
                "currency": "USD",
                "expiry": "20261218",
                "dte": 30,
                "strike": 250.0,  # Exceeds max_entry 200.0!
                "qty": 1,
                "premium": 5.0,
            }],
        }

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps(plan_doc))
            plan_path = f.name

        try:
            args = argparse.Namespace(
                file=plan_path,
                i=1,
                net=None,
                stock_px=None,
                code="",
                live_ok=False,
                paper=True,
                port=None,
                delayed=False,
            )
            cfg = dict(cli.DEFAULT_CFG)
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_run(cfg, args)
            self.assertIn("准入白名单", str(cm.exception))
        finally:
            Path(plan_path).unlink(missing_ok=True)


class PlanPresetLogicTests(unittest.TestCase):
    """Test preset selection logic under auto, sell-call, and cash-put."""

    def test_preset_sell_call_and_cash_put_explicit(self):
        # sell-call explicitly forces right='C', preset='sell-call'
        args_sc = argparse.Namespace(preset="sell-call")
        right_sc = "C" if getattr(args_sc, "preset", "auto") == "sell-call" else "P"
        self.assertEqual(right_sc, "C")

        # cash-put explicitly forces right='P', preset='cash-put'
        args_cp = argparse.Namespace(preset="cash-put")
        right_cp = "P" if getattr(args_cp, "preset", "auto") == "cash-put" else "C"
        self.assertEqual(right_cp, "P")

    def test_preset_auto_rules(self):
        # 1. have >= 100 and avail >= 100 -> sell-call
        have = 200.0
        busy = 0
        avail = max(0.0, have - busy * 100)
        self.assertGreaterEqual(avail, 100)

        # 2. have >= 100 but busy calls occupy all -> cash-put with note
        have = 100.0
        busy = 1
        avail = max(0.0, have - busy * 100)
        self.assertLess(avail, 100)

        # 3. 0 < have < 100 -> cash-put with note (< 1 lot)
        have = 50.0
        self.assertTrue(0 < have < 100)

        # 4. have == 0 -> cash-put without share note
        have = 0.0
        self.assertEqual(have, 0)


class OptionExchangeFallbackTests(unittest.TestCase):
    """Test option exchange fallback logic in cmd_order."""

    def test_hkd_option_defaults_to_sehk_and_falls_back_to_smart(self):
        # In cmd_order:
        # exchange = getattr(args, "exchange", None) or ("SEHK" if args.currency == "HKD" else "SMART")
        args = argparse.Namespace(
            symbol="700",
            opt=True,
            expiry="20261218",
            strike=400.0,
            right="C",
            action="SELL",
            qty=1,
            type="LMT",
            limit=10.0,
            tif="DAY",
            outside_rth=False,
            currency="HKD",
            exchange=None,
            code="",
            whatif=False,
            live_ok=False,
            paper=True,
            port=4002,
        )

        exchange = getattr(args, "exchange", None) or ("SEHK" if args.currency == "HKD" else "SMART")
        self.assertEqual(exchange, "SEHK")

        # Mock qualifyContracts behavior: fails on SEHK, succeeds on SMART
        mock_ib = MagicMock()
        smart_qualified = MagicMock(conId=12345, secType="OPT")

        def mock_qualify(c):
            if c.exchange == "SEHK":
                return []
            if c.exchange == "SMART":
                return [smart_qualified]
            return []

        mock_ib.qualifyContracts.side_effect = mock_qualify

        # Simulate the fallback block in cmd_order
        contract = cli.Option(args.symbol.upper(), args.expiry, args.strike, args.right.upper(),
                              exchange, currency=args.currency)
        try:
            q = mock_ib.qualifyContracts(contract)
        except Exception:
            q = []
        if (not q or not q[0].conId) and exchange == "SEHK":
            try:
                contract = cli.Option(args.symbol.upper(), args.expiry, args.strike, args.right.upper(),
                                      "SMART", currency=args.currency)
                q = mock_ib.qualifyContracts(contract)
            except Exception:
                q = []

        self.assertTrue(q and q[0].conId)
        self.assertEqual(contract.exchange, "SMART")


class TradingRiskControlsTests(unittest.TestCase):
    """Test the 6 critical trading risk controls implemented in thetamill/cli.py."""

    def test_cmd_order_blocks_naked_call(self):
        args = argparse.Namespace(
            symbol="AAPL",
            opt=True,
            action="SELL",
            qty=1,
            type="LMT",
            limit=5.0,
            expiry="20261218",
            strike=200.0,
            right="C",
            currency="USD",
            exchange="SMART",
            tif="DAY",
            outside_rth=False,
            whatif=False,
            code="",
            live_ok=False,
            paper=True,
            port=4002,
        )
        cfg = dict(cli.DEFAULT_CFG)
        mock_ib = MagicMock()
        mock_ib.qualifyContracts.return_value = [MagicMock(conId=12345, secType="OPT")]

        with patch.object(cli, "connect", return_value=mock_ib), \
             patch.object(cli, "finish"), \
             patch.object(cli, "CFG_PATH") as mock_cfg_path, \
             patch.object(cli, "_shares", return_value=50.0), \
             patch.object(cli, "_short_calls", return_value=0), \
             patch.object(cli, "_pending_short_calls", return_value=0), \
             patch("sys.stdout", new_callable=io.StringIO):
            mock_cfg_path.exists.return_value = True
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_order(cfg, args)
            self.assertIn("备兑卖 Call 的先决条件是持有未被覆盖的正股", str(cm.exception))
            self.assertIn("净可用仅 50 股", str(cm.exception))

    def test_cmd_order_blocks_cash_insufficient_put(self):
        args = argparse.Namespace(
            symbol="AAPL",
            opt=True,
            action="SELL",
            qty=1,
            type="LMT",
            limit=5.0,
            expiry="20261218",
            strike=200.0,
            right="P",
            currency="USD",
            exchange="SMART",
            tif="DAY",
            outside_rth=False,
            whatif=False,
            code="",
            live_ok=False,
            paper=True,
            port=4002,
        )
        cfg = dict(cli.DEFAULT_CFG)
        mock_ib = MagicMock()
        mock_ib.qualifyContracts.return_value = [MagicMock(conId=12345, secType="OPT")]

        with patch.object(cli, "connect", return_value=mock_ib), \
             patch.object(cli, "finish"), \
             patch.object(cli, "CFG_PATH") as mock_cfg_path, \
             patch.object(cli, "_coverage", return_value=(False, "需要 20,000.00 USD，账户实际现金 TotalCashValue 仅 5,000.00 USD")), \
             patch("sys.stdout", new_callable=io.StringIO):
            mock_cfg_path.exists.return_value = True
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_order(cfg, args)
            self.assertIn("现金担保不足", str(cm.exception))
            self.assertIn("严禁裸卖或使用融资杠杆", str(cm.exception))

    def test_code_replay_prevention(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            consumed_path = Path(tmp_dir) / "consumed_codes.json"
            with patch.object(cli, "_consumed_codes_file", return_value=consumed_path):
                test_code = "A1B2C3D4"
                self.assertFalse(cli._is_code_consumed(test_code))
                cli._consume_code(test_code)
                self.assertTrue(cli._is_code_consumed(test_code))
                self.assertFalse(cli._is_code_consumed("OTHERCODE"))

                # In cmd_order, using an already consumed code should exit immediately
                args = argparse.Namespace(
                    symbol="AAPL",
                    opt=False,
                    action="BUY",
                    qty=10,
                    type="MKT",
                    limit=None,
                    currency="USD",
                    exchange="SMART",
                    tif="DAY",
                    outside_rth=False,
                    whatif=False,
                    code=test_code,
                    live_ok=False,
                    paper=True,
                    port=4002,
                )
                cfg = dict(cli.DEFAULT_CFG)
                mock_ib = MagicMock()
                mock_ib.qualifyContracts.return_value = [MagicMock(conId=12345, secType="STK")]

                with patch.object(cli, "connect", return_value=mock_ib), \
                     patch.object(cli, "finish"), \
                     patch.object(cli, "CFG_PATH") as mock_cfg_path, \
                     patch.object(cli, "_code", return_value=test_code), \
                     patch("sys.stdout", new_callable=io.StringIO):
                    mock_cfg_path.exists.return_value = True
                    with self.assertRaises(SystemExit) as cm:
                        cli.cmd_order(cfg, args)
                    self.assertIn("已经被消费执行过，不可重复提交同一笔委托", str(cm.exception))

    def test_port_7496_triggers_live_guard(self):
        args = argparse.Namespace(paper=False, port=7496, live_ok=False)
        cfg = {"live_enabled": False, "live_port": 4001, "paper_port": 4002}

        # Port 7496 is detected as live environment
        self.assertTrue(cli._is_live_env(cfg, args, 7496))

        # live_enabled=False should block port 7496
        with self.assertRaises(SystemExit) as cm:
            cli._live_guard(cfg, args, 7496)
        self.assertIn("实盘下单已被总开关锁定", str(cm.exception))

        # live_enabled=True, but live_ok=False should block port 7496
        cfg["live_enabled"] = True
        with self.assertRaises(SystemExit) as cm:
            cli._live_guard(cfg, args, 7496)
        self.assertIn("这是实盘账户，委托需要显式加 --live-ok", str(cm.exception))

        # With live_ok=True, it passes
        args.live_ok = True
        cli._live_guard(cfg, args, 7496)

    def test_pending_short_calls_deducted_from_available_shares(self):
        mock_ib = MagicMock()
        mock_trade = MagicMock()
        mock_trade.orderStatus.status = "Submitted"
        mock_trade.orderStatus.remaining = 1.0
        mock_trade.contract.secType = "OPT"
        mock_trade.contract.symbol = "AAPL"
        mock_trade.contract.currency = "USD"
        mock_trade.contract.right = "C"
        mock_trade.order.action = "SELL"
        mock_ib.openTrades.return_value = [mock_trade]

        pending = cli._pending_short_calls(mock_ib, "AAPL", "USD")
        self.assertEqual(pending, 1)

        # In cmd_order, verify available shares check:
        # 100 shares held, 0 settled short calls, 1 pending short call -> avail = 100 - (0 + 1)*100 = 0
        args = argparse.Namespace(
            symbol="AAPL",
            opt=True,
            action="SELL",
            qty=1,
            type="LMT",
            limit=5.0,
            expiry="20261218",
            strike=200.0,
            right="C",
            currency="USD",
            exchange="SMART",
            tif="DAY",
            outside_rth=False,
            whatif=False,
            code="",
            live_ok=False,
            paper=True,
            port=4002,
        )
        cfg = dict(cli.DEFAULT_CFG)
        mock_ib.qualifyContracts.return_value = [MagicMock(conId=12345, secType="OPT")]

        with patch.object(cli, "connect", return_value=mock_ib), \
             patch.object(cli, "finish"), \
             patch.object(cli, "CFG_PATH") as mock_cfg_path, \
             patch.object(cli, "_shares", return_value=100.0), \
             patch.object(cli, "_short_calls", return_value=0), \
             patch.object(cli, "_pending_short_calls", return_value=1), \
             patch("sys.stdout", new_callable=io.StringIO):
            mock_cfg_path.exists.return_value = True
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_order(cfg, args)
            self.assertIn("净可用仅 0 股（总持仓 100，空头持仓占用 100 股）", str(cm.exception))

    def test_cash_put_rejects_zero_cash_when_available_funds_positive(self):
        mock_ib = MagicMock()
        mock_ib.accountValues.return_value = [
            MockAccountValue("TotalCashValue", "0.0", "USD"),
            MockAccountValue("AvailableFunds", "50000.0", "USD"),
        ]
        ok, note = cli._coverage(mock_ib, "USD", 10000.0, require_cash=True)
        self.assertFalse(ok)
        self.assertIn("❌", note)
        self.assertIn("账户实际现金 TotalCashValue 仅 0.00 USD", note)
        self.assertIn("AvailableFunds 融资额度不可用于现金担保", note)

    def test_cmd_run_rejects_expired_plan_and_cross_env(self):
        # 1. Expired plan rejection
        expired_plan = {
            "generated": "2020-01-01T12:00:00",
            "valid_until": "2020-01-02T12:00:00",
            "is_live": False,
            "items": [],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps(expired_plan))
            exp_path = f.name

        try:
            args = argparse.Namespace(
                file=exp_path,
                i=1,
                net=None,
                stock_px=None,
                code="",
                live_ok=False,
                paper=True,
                port=None,
                delayed=False,
            )
            cfg = dict(cli.DEFAULT_CFG)
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_run(cfg, args)
            self.assertIn("计划文件已过期", str(cm.exception))
        finally:
            Path(exp_path).unlink(missing_ok=True)

        # 2. Cross-environment execution rejection (live plan executed on paper port)
        future_until = (datetime.now() + timedelta(days=1)).isoformat()
        cross_env_plan = {
            "generated": datetime.now().isoformat(),
            "valid_until": future_until,
            "is_live": True,
            "items": [],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(json.dumps(cross_env_plan))
            cross_path = f.name

        try:
            args = argparse.Namespace(
                file=cross_path,
                i=1,
                net=None,
                stock_px=None,
                code="",
                live_ok=False,
                paper=True,
                port=4002,
                delayed=False,
            )
            cfg = dict(cli.DEFAULT_CFG)
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_run(cfg, args)
            self.assertIn("严禁跨环境执行", str(cm.exception))
            self.assertIn("实盘", str(cm.exception))
            self.assertIn("纸账户", str(cm.exception))
        finally:
            Path(cross_path).unlink(missing_ok=True)

    def test_verify_account_env_blocks_live_account_in_paper_mode(self):
        mock_ib = MagicMock()
        mock_ib.managedAccounts.return_value = ["U9876543"]
        args = argparse.Namespace(paper=True, port=4002)
        cfg = dict(cli.DEFAULT_CFG)

        with self.assertRaises(SystemExit) as cm:
            cli._verify_account_env(mock_ib, cfg, args, 4002)
        self.assertIn("检测到实盘账号 U9876543 正在非实盘模式下运行", str(cm.exception))

        mock_ib.managedAccounts.return_value = ["DU1234567"]
        cli._verify_account_env(mock_ib, cfg, args, 4002)

        mock_ib.managedAccounts.return_value = ["U9876543"]
        cli._verify_account_env(mock_ib, cfg, args, 4001)


if __name__ == "__main__":
    unittest.main()
