"""Offline integration tests for the unified submission and CLI entrypoints.

Verifies:
  * Parser flags (--intent, --delivery-evidence) on order, strat/combo, run, plan.
  * Pre-network validation of the (--intent, --code) confirmation pair.
  * Argument forwarding in cmd_run.
  * Routing of cmd_order and cmd_combo through submission_cli.execute_prepared.
  * Removal of direct placeOrder and legacy _code fallback from live execution paths.
  * Proper connection closure in finally blocks even on SystemExit.
  * Account masking in printed outputs.
  * Covered-call requirement of positive bounded --stock-px and requires_filled=(1,).
"""
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

from thetamill import cli, state, submission_cli
try:
    from ib_async import Option, Stock
except ImportError:
    from thetamill.cli import Option, Stock


class ParserAndForwardingTests(unittest.TestCase):
    def test_parser_has_intent_and_delivery_evidence_flags(self):
        """The REAL parser (not a stand-in) wires the confirmation pair everywhere."""
        parser = cli.build_parser(dict(cli.DEFAULT_CFG))
        order = parser.parse_args(
            ["order", "AAPL", "--action", "SELL", "--qty", "1",
             "--intent", "int-123", "--delivery-evidence", "/path/ev.json"])
        self.assertEqual(order.intent, "int-123")
        self.assertEqual(order.delivery_evidence, "/path/ev.json")
        for cmd in (["strat", "AAPL", "--preset", "cash-put", "--expiry", "20261218", "--k1", "40"],
                    ["run", "--file", "plan.json"]):
            for sub in (["--intent", "int-123"], ["--delivery-evidence", "/path/ev.json"]):
                parsed = parser.parse_args(cmd + sub)
                expected = "int-123" if sub[0] == "--intent" else "/path/ev.json"
                attribute = sub[0].lstrip("-").replace("-", "_")
                self.assertEqual(getattr(parsed, attribute), expected)

    def test_cli_subcommands_support_intent_and_delivery_evidence(self):
        """Every order-placing subcommand routes to a handler through the real parser."""
        parser = cli.build_parser(dict(cli.DEFAULT_CFG))
        self.assertEqual(parser.parse_args(
            ["order", "AAPL", "--action", "BUY", "--qty", "1"]).func, cli.cmd_order)
        self.assertEqual(parser.parse_args(
            ["strat", "AAPL", "--preset", "cash-put", "--expiry", "20261218",
             "--k1", "40"]).func, cli.cmd_combo)
        self.assertEqual(parser.parse_args(
            ["run", "--file", "plan.json"]).func, cli.cmd_run)
        self.assertEqual(parser.parse_args(["roll-plan", "--con-id", "1", "--qty", "1"]).func,
                         cli.cmd_roll_plan)
        # `combo` is an alias of `strat`, not a second implementation.
        self.assertEqual(parser.parse_args(
            ["combo", "AAPL", "--preset", "cash-put", "--expiry", "20261218",
             "--k1", "40"]).func, cli.cmd_combo)

    def test_pre_network_guard_rejects_mismatched_intent_and_code(self):
        """Pre-network guard must exit before connect is ever called."""
        test_cases = [
            (cli.cmd_order, argparse.Namespace(intent="", code="APPROVE1", symbol="AAPL")),
            (cli.cmd_order, argparse.Namespace(intent="int-123", code="", symbol="AAPL")),
            (cli.cmd_combo, argparse.Namespace(intent="", code="APPROVE1", symbol="AAPL", preset="cash-put", qty=1, k1=40.0, expiry="20261218", currency="USD", net=1.0, stock_px=None)),
            (cli.cmd_combo, argparse.Namespace(intent="int-123", code="", symbol="AAPL", preset="cash-put", qty=1, k1=40.0, expiry="20261218", currency="USD", net=1.0, stock_px=None)),
        ]
        for cmd, args in test_cases:
            with self.subTest(cmd=cmd.__name__, intent=args.intent, code=args.code), \
                 patch.object(cli, "connect") as connect_mock, \
                 patch.object(cli, "CFG_PATH", Path(__file__)), \
                 patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                 self.assertRaises(SystemExit) as cm:
                cmd(cli.DEFAULT_CFG, args)
            connect_mock.assert_not_called()
            self.assertTrue("explicit --intent is required" in str(cm.exception) or
                            "explicit --code is required" in str(cm.exception))

    def test_cmd_run_forwards_intent_and_delivery_evidence(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            plan_path = Path(tmp_dir) / "plan.json"
            plan_doc = {
                "schema": "thetamill.plan.v1",
                "valid_until": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                "items": [
                    {
                        "i": 1,
                        "symbol": "AAPL", "preset": "sell-call", "expiry": "20261218",
                        "strike": 150.0, "qty": 1, "currency": "USD", "premium": 2.5,
                        "conId": 101, "mult": 100, "tradingClass": "AAPL", "exchange": "SMART",
                        "allowed": True,
                    }
                ]
            }
            plan_path.write_text(json.dumps(plan_doc), encoding="utf-8")
            run_args = argparse.Namespace(
                file=str(plan_path), i=1, net=None, stock_px=None, code="APPROVAL123",
                intent="int-abc456", delivery_evidence="/tmp/ev.json", live_ok=False,
                paper=True, port=None, delayed=False,
            )
            with patch.object(cli, "cmd_combo", return_value=0) as combo_mock, \
                 patch.object(cli, "resolve_port", return_value=(4002, "paper")):
                rc = cli.cmd_run(cli.DEFAULT_CFG, run_args)
                self.assertEqual(rc, 0)
                combo_mock.assert_called_once()
                forwarded = combo_mock.call_args.args[1]
                self.assertEqual(forwarded.intent, "int-abc456")
                self.assertEqual(forwarded.delivery_evidence, "/tmp/ev.json")
                self.assertEqual(forwarded.code, "APPROVAL123")


class SubmissionIntegrationExecutionTests(unittest.TestCase):
    @contextmanager
    def mock_environment(self, delivery_confirmed=True):
        ib = MagicMock()
        ib.managedAccounts.return_value = ["DU12345"]
        stock = Stock("AAPL", "SMART", "USD")
        stock.conId = 1
        opt = Option("AAPL", "20261218", 150.0, "C", "SMART", currency="USD",
                     multiplier="100", tradingClass="AAPL")
        opt.conId = 101

        ib.portfolio.return_value = [
            SimpleNamespace(account="DU12345", contract=stock, position=100, averageCost=140.0, marketPrice=150.0),
        ]
        ib.openTrades.return_value = []
        ib.trades.return_value = []
        ib.reqAllOpenOrders.return_value = []
        ib.accountValues.return_value = [
            SimpleNamespace(account="DU12345", tag="TotalCashValue", currency="USD", value="500000"),
            SimpleNamespace(account="DU12345", tag="NetLiquidation", currency="USD", value="500000"),
            SimpleNamespace(account="DU12345", tag="Currency", currency="BASE", value="USD"),
        ]
        ib.qualifyContracts.side_effect = lambda c: [c]
        ib.reqContractDetails.side_effect = lambda c: [SimpleNamespace(contract=c, underConId=1, underSecType="STK")]
        ib.reqTickers.side_effect = lambda c: [SimpleNamespace(
            marketDataType=1, contract=c, time=datetime.now(timezone.utc),
            bid=150.0, ask=150.0, last=150.0, close=150.0,
        )]

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir)
            cfg_path = data_dir / "config.json"
            cfg_path.write_text(json.dumps(cli.DEFAULT_CFG), encoding="utf-8")
            ledger_path = data_dir / "state.sqlite3"
            state.SubmissionLedger(ledger_path, "UTC")
            ev_path = data_dir / "evidence.json"
            now = datetime.now(timezone.utc)
            records = []
            if delivery_confirmed:
                records.append({
                    "instrument": {
                        "con_id": 101, "underlying_id": 1, "symbol": "AAPL", "currency": "USD",
                        "sec_type": "OPT", "exchange": "SMART", "multiplier": "100",
                        "right": "C", "strike": "150", "expiry": "20261218", "trading_class": "AAPL"
                    },
                    "stock_only": True, "source": "human review",
                    "verified_at": now.isoformat(),
                    "expires_at": (now + timedelta(days=2)).isoformat(),
                })
            ev_path.write_text(json.dumps({
                "schema": "thetamill.deliverables.v1",
                "records": records,
            }), encoding="utf-8")

            with patch.object(cli, "CFG_PATH", cfg_path), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                 patch.object(cli, "connect", return_value=ib), \
                 patch.object(cli, "finish") as finish_mock, \
                 patch.object(cli, "_opt", return_value=(opt, "SMART")), \
                 patch.object(cli, "_stock", return_value=stock), \
                 patch("sys.stdout", new_callable=io.StringIO) as stdout_mock:
                yield SimpleNamespace(
                    ib=ib, stock=stock, opt=opt, data_dir=data_dir,
                    evidence_path=ev_path, finish=finish_mock, stdout=stdout_mock
                )

    def test_cmd_order_preflight_and_confirm_lifecycle(self):
        with self.mock_environment() as env:
            order_args = argparse.Namespace(
                symbol="AAPL", opt=True, action="SELL", qty=1, type="LMT",
                limit=2.5, stop=None, expiry="20261218", strike=150.0, right="C",
                currency="USD", exchange="SMART", tif="DAY", outside_rth=False,
                whatif=False, code="", intent="", live_ok=False, paper=True, port=4002,
                delivery_evidence=str(env.evidence_path),
            )
            # 1. Preflight
            rc_pre = cli.cmd_order(cli.DEFAULT_CFG, order_args)
            self.assertEqual(rc_pre, 2)
            env.ib.placeOrder.assert_not_called()
            env.finish.assert_called_once_with(env.ib)
            self.assertIn("[PREFLIGHT]", env.stdout.getvalue())
            self.assertNotIn("DU12345", env.stdout.getvalue())  # Masked account
            self.assertIn("***345", env.stdout.getvalue())

            # 2. Retrieve intent from ledger
            import sqlite3
            ledger_path = env.data_dir / "state.sqlite3"
            with sqlite3.connect(ledger_path) as conn:
                row = conn.execute("SELECT intent_id, approval_code FROM intents ORDER BY rowid DESC LIMIT 1").fetchone()
            self.assertIsNotNone(row)
            intent_id, approval_code = row[0], row[1]

            # 3. Confirm
            env.finish.reset_mock()
            trade = SimpleNamespace(
                order=SimpleNamespace(orderId=10, orderRef="test-ref"),
                orderStatus=SimpleNamespace(status="Submitted", filled=0, remaining=1),
                log=[]
            )
            env.ib.placeOrder.return_value = trade
            confirm_args = argparse.Namespace(**vars(order_args))
            confirm_args.intent = intent_id
            confirm_args.code = approval_code

            rc_conf = cli.cmd_order(cli.DEFAULT_CFG, confirm_args)
            self.assertEqual(rc_conf, 0)
            env.ib.placeOrder.assert_called_once()
            env.finish.assert_called_once_with(env.ib)
            self.assertIn("[CONFIRMED]", env.stdout.getvalue())

    def test_cmd_combo_covered_call_requires_bounded_stock_px(self):
        with self.mock_environment() as env:
            env.ib.portfolio.return_value = []
            # Missing stock_px must exit
            args = argparse.Namespace(
                symbol="AAPL", preset="covered-call", qty=1, k1=150.0,
                expiry="20261218", currency="USD", exchange="SMART", net=1.0,
                stock_px=None, code="", intent="", live_ok=False, paper=True, port=4002,
                delivery_evidence=str(env.evidence_path),
            )
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_combo(cli.DEFAULT_CFG, args)
            self.assertIn("covered-call 买正股需要明确的限价 --stock-px", str(cm.exception))
            env.ib.placeOrder.assert_not_called()

            # Non-positive stock_px must exit
            for bad_px in (0, -10.0):
                args.stock_px = bad_px
                with self.subTest(stock_px=bad_px), self.assertRaises(SystemExit) as cm:
                    cli.cmd_combo(cli.DEFAULT_CFG, args)
                self.assertIn("正股价格必须是有限正数", str(cm.exception))
                env.ib.placeOrder.assert_not_called()

    def test_connection_closes_in_finally_on_error_or_exit(self):
        with self.mock_environment() as env:
            # Mismatched conId causes execute_prepared to raise SubmissionCliError -> SystemExit
            args = argparse.Namespace(
                symbol="AAPL", preset="sell-call", qty=1, k1=150.0,
                expiry="20261218", currency="USD", exchange="SMART", net=1.0,
                stock_px=None, code="", intent="", live_ok=False, paper=True, port=4002,
                delivery_evidence=str(env.evidence_path),
            )
            with patch.object(submission_cli, "execute_prepared", side_effect=submission_cli.SubmissionCliError("synthetic error")), \
                 self.assertRaises(SystemExit):
                cli.cmd_combo(cli.DEFAULT_CFG, args)
            env.finish.assert_called_once_with(env.ib)

    def test_no_legacy_code_authorization_fallback(self):
        """Passing --code alone without --intent must never trigger execution or fallback to _code."""
        with self.mock_environment() as env:
            args = argparse.Namespace(
                symbol="AAPL", preset="sell-call", qty=1, k1=150.0,
                expiry="20261218", currency="USD", exchange="SMART", net=1.0,
                stock_px=None, code="LEGACY_CODE", intent="", live_ok=False, paper=True, port=4002,
                delivery_evidence=str(env.evidence_path),
            )
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_combo(cli.DEFAULT_CFG, args)
            self.assertIn("an explicit --intent is required", str(cm.exception))
            env.ib.placeOrder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
