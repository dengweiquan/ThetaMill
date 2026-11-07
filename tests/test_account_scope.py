"""Explicit account scope for read-only queries and cancellation selection."""
import argparse
import io
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import MagicMock, patch

from thetamill import cli


class AccountScopeTests(unittest.TestCase):
    def args(self, **kwargs):
        return argparse.Namespace(paper=True, port=4002, account=kwargs.get("account"),
                                  order_id=kwargs.get("order_id", 1))

    def test_multiple_accounts_rejected_without_selection(self):
        ib = MagicMock()
        ib.managedAccounts.return_value = ["DU1", "DU2"]
        with self.assertRaises(SystemExit):
            cli._selected_account(ib, cli.DEFAULT_CFG, self.args())
        self.assertEqual(cli._selected_account(ib, cli.DEFAULT_CFG, self.args(account="DU2")), "DU2")
        self.assertEqual(cli._selected_account(ib, {"account": "DU1"}, self.args()), "DU1")

    def test_status_scope_failure_still_disconnects_each_connection(self):
        ib = MagicMock()
        ib.managedAccounts.return_value = ["DU1", "DU2"]
        with patch.object(cli, "CFG_PATH", Path(__file__)), \
             patch.object(cli, "IB", return_value=ib), \
             patch.object(cli, "finish") as finish, \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli.cmd_status(cli.DEFAULT_CFG, self.args()), 0)
        self.assertEqual(finish.call_count, 4)
        ib.accountSummary.assert_not_called()
        ib.placeOrder.assert_not_called()

    def test_account_selection_failures_close_read_and_cancel_connections(self):
        for command in (cli.cmd_account, cli.cmd_positions, cli.cmd_orders, cli.cmd_cancel):
            ib = MagicMock()
            ib.managedAccounts.return_value = ["DU1", "DU2"]
            with self.subTest(command=command.__name__), \
                 patch.object(cli, "CFG_PATH", Path(__file__)), \
                 patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                 patch.object(cli, "connect", return_value=ib), \
                 patch.object(cli, "finish") as finish, \
                 self.assertRaises(SystemExit):
                try:
                    command(cli.DEFAULT_CFG, self.args())
                finally:
                    finish.assert_called_once_with(ib)
            ib.placeOrder.assert_not_called()
            ib.cancelOrder.assert_not_called()

    def test_connect_initialization_failures_disconnect_before_returning(self):
        for failure in ("environment", "market_data", "handler"):
            ib = MagicMock()
            ib.managedAccounts.return_value = ["U1234567" if failure == "environment" else "DU1"]
            if failure == "market_data":
                ib.reqMarketDataType.side_effect = RuntimeError("initialization failed")
            if failure == "handler":
                ib.errorEvent.__iadd__.side_effect = RuntimeError("handler failed")
            expected = SystemExit if failure == "environment" else RuntimeError
            with self.subTest(failure=failure), patch.object(cli, "IB", return_value=ib), \
                 self.assertRaises(expected):
                cli.connect(cli.DEFAULT_CFG, cli.DEFAULT_CFG["paper_port"], args=self.args())
            ib.disconnect.assert_called_once_with()
            ib.placeOrder.assert_not_called()
            ib.cancelOrder.assert_not_called()

    def test_account_summary_filters_other_account_rows(self):
        ib = MagicMock()
        ib.managedAccounts.return_value = ["DU1", "DU2"]
        ib.accountSummary.return_value = [
            NS(account="DU1", tag="AccountType", value="OTHER_ACCOUNT_VALUE", currency="USD"),
            NS(account="DU2", tag="AccountType", value="SELECTED_ACCOUNT_VALUE", currency="USD")]
        with patch.object(cli, "CFG_PATH", Path(__file__)), \
             patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
             patch.object(cli, "connect", return_value=ib), patch.object(cli, "finish"), \
             patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(cli.cmd_account(cli.DEFAULT_CFG, self.args(account="DU2")), 0)
        ib.accountSummary.assert_called_once_with("DU2")
        self.assertIn("SELECTED_ACCOUNT_VALUE", out.getvalue())
        self.assertNotIn("OTHER_ACCOUNT_VALUE", out.getvalue())
        ib.placeOrder.assert_not_called()

    def test_positions_refreshes_and_reads_only_selected_account(self):
        ib = MagicMock()
        ib.managedAccounts.return_value = ["DU1", "DU2"]
        ib.portfolio.return_value = []
        with patch.object(cli, "CFG_PATH", Path(__file__)), \
             patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
             patch.object(cli, "connect", return_value=ib), patch.object(cli, "finish"), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli.cmd_positions(cli.DEFAULT_CFG, self.args(account="DU2")), 0)
        ib.reqAccountUpdates.assert_called_once_with("DU2")
        ib.portfolio.assert_called_once_with("DU2")
        ib.placeOrder.assert_not_called()
        ib.cancelOrder.assert_not_called()

    def test_orders_only_show_selected_account(self):
        ib = MagicMock()
        ib.managedAccounts.return_value = ["DU1", "DU2"]
        ib.reqAllOpenOrders.return_value = [
            NS(order=NS(account=a, orderId=i, action="SELL", totalQuantity=1, lmtPrice=2, orderType="LMT"),
               orderStatus=NS(status="Submitted"), contract=NS(secType="STK", symbol=s))
            for a, i, s in (("DU1", 1, "FIRST"), ("DU2", 2, "SECOND"))]
        with patch.object(cli, "CFG_PATH", Path(__file__)), \
             patch.object(cli, "connect", return_value=ib), patch.object(cli, "finish"), \
             patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            cli.cmd_orders(cli.DEFAULT_CFG, self.args(account="DU2"))
            self.assertIn("SECOND", output.getvalue())
            self.assertNotIn("FIRST", output.getvalue())
        ib.placeOrder.assert_not_called()

    def test_cancel_cannot_target_other_account_or_ambiguous_id(self):
        for accounts in (("DU1",), ("DU2", "DU2")):
            ib = MagicMock()
            ib.managedAccounts.return_value = ["DU1", "DU2"]
            ib.reqAllOpenOrders.return_value = [NS(order=NS(account=a, orderId=1)) for a in accounts]
            with patch.object(cli, "CFG_PATH", Path(__file__)), \
                 patch.object(cli, "connect", return_value=ib), patch.object(cli, "finish"), \
                 patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                 self.assertRaises(SystemExit):
                cli.cmd_cancel(cli.DEFAULT_CFG, self.args(account="DU2"))
            ib.cancelOrder.assert_not_called()

    def test_cancel_rejects_another_client_even_when_account_matches(self):
        ib = MagicMock()
        ib.managedAccounts.return_value = ["DU1", "DU2"]
        ib.client.clientId = 1
        ib.reqAllOpenOrders.return_value = [
            NS(order=NS(account="DU1", orderId=7, clientId=1)),
            NS(order=NS(account="DU2", orderId=7, clientId=2),
               orderStatus=NS(status="Submitted"))]
        with patch.object(cli, "CFG_PATH", Path(__file__)), \
             patch.object(cli, "connect", return_value=ib), patch.object(cli, "finish"), \
             patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
             self.assertRaises(SystemExit):
            cli.cmd_cancel(cli.DEFAULT_CFG, self.args(account="DU2", order_id=7))
        ib.cancelOrder.assert_not_called()

    def test_cancel_same_client_selected_account_is_allowed(self):
        ib = MagicMock()
        ib.managedAccounts.return_value = ["DU1"]
        ib.client.clientId = 1
        order = NS(account="DU1", orderId=7, clientId=1)
        ib.reqAllOpenOrders.return_value = [NS(order=order, orderStatus=NS(status="Cancelled"))]
        with patch.object(cli, "CFG_PATH", Path(__file__)), \
             patch.object(cli, "connect", return_value=ib), patch.object(cli, "finish"), \
             patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(cli.cmd_cancel(cli.DEFAULT_CFG, self.args(order_id=7)), 0)
        ib.cancelOrder.assert_called_once_with(order)
        ib.placeOrder.assert_not_called()

    def test_reconcile_absence_only_records_observation(self):
        import tempfile
        ib = MagicMock()
        ib.managedAccounts.return_value = ["DU1"]
        ib.reqAllOpenOrders.return_value = []
        ledger = MagicMock()
        ledger.list_legs.return_value = [NS(order_ref="tm-leg", perm_id=None)]
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "state.sqlite3").touch()
            with patch.object(cli, "_data_dir", return_value=Path(directory)), \
                 patch.object(cli, "connect", return_value=ib), patch.object(cli, "finish"), \
                 patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                 patch("thetamill.state.SubmissionLedger", return_value=ledger), \
                 patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(cli.cmd_reconcile(cli.DEFAULT_CFG, self.args()), 0)
        self.assertEqual(ledger.reconcile.call_args.kwargs["status"], "absent_open_order")
        ledger.resolve_unknown.assert_not_called()
        ib.placeOrder.assert_not_called()
        ib.cancelOrder.assert_not_called()

    def test_reconcile_rejects_unknown_scope_or_conflicting_ref(self):
        import tempfile
        for trade in (NS(order=NS(account="", orderRef="tm-leg", permId=5, orderId=7)),
                      NS(order=NS(account="DU1", orderRef="different", permId=5, orderId=7))):
            ib, ledger = MagicMock(), MagicMock()
            ib.managedAccounts.return_value = ["DU1"]
            ib.reqAllOpenOrders.return_value = [trade]
            ledger.list_legs.return_value = [NS(order_ref="tm-leg", perm_id="5")]
            with tempfile.TemporaryDirectory() as directory:
                (Path(directory) / "state.sqlite3").touch()
                with patch.object(cli, "_data_dir", return_value=Path(directory)), \
                     patch.object(cli, "connect", return_value=ib), patch.object(cli, "finish"), \
                     patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                     patch("thetamill.state.SubmissionLedger", return_value=ledger), \
                     self.assertRaises(SystemExit):
                    cli.cmd_reconcile(cli.DEFAULT_CFG, self.args())
                ledger.reconcile.assert_not_called()
            ib.placeOrder.assert_not_called()
            ib.cancelOrder.assert_not_called()

    def test_run_rejects_recommendation_schema_before_connection(self):
        import json
        import tempfile
        for doc in ({"schema": "thetamill.roll.v1", "items": [{"i": 1}]},
                    {"recommendation_only": True, "items": [{"i": 1}]}, []):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "recommendation.json"
                path.write_text(json.dumps(doc), encoding="utf-8")
                args = self.args()
                args.file = str(path)
                with patch.object(cli, "connect") as connect, \
                     patch.object(cli, "cmd_combo") as combo, self.assertRaises(SystemExit):
                    cli.cmd_run(cli.DEFAULT_CFG, args)
                connect.assert_not_called()
                combo.assert_not_called()

    def test_run_rejects_blocked_plan_item_before_connection(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps({"items": [{"i": 1, "allowed": False,
                                                    "conId": 20, "mult": "200",
                                                    "tradingClass": "AAPL", "exchange": "SMART",
                                                    "qty": 1, "strike": 40, "premium": 2,
                                                    "symbol": "AAPL", "currency": "USD",
                                                    "preset": "cash-put", "expiry": "20261218",
                                                    "reasons": ["insufficient cumulative cash"]}]}),
                            encoding="utf-8")
            args = self.args()
            args.file, args.i = str(path), 1
            args.net, args.stock_px, args.code = None, None, ""
            args.live_ok, args.delayed = False, False
            with patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                 patch.object(cli, "connect") as connect, \
                 patch.object(cli, "cmd_combo") as combo, self.assertRaisesRegex(SystemExit, "预算"):
                cli.cmd_run(cli.DEFAULT_CFG, args)
            connect.assert_not_called()
            combo.assert_not_called()

    def test_run_compares_expiry_instants_and_rejects_malformed_expiry(self):
        import json
        import tempfile
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
        future = (now + timedelta(hours=1)).astimezone(timezone(timedelta(hours=-12)))
        past = (now - timedelta(hours=1)).astimezone(timezone(timedelta(hours=14)))
        item = {"i": 1, "allowed": True, "conId": 20, "mult": "200",
                "tradingClass": "AAPL", "exchange": "SMART", "qty": 1,
                "strike": "40", "premium": "2", "symbol": "AAPL",
                "currency": "USD", "preset": "cash-put", "expiry": "20261218"}
        for expiry, permitted in ((future.isoformat(), True), (past.isoformat(), False),
                                  ("not-a-date", False), (True, False), ([], False)):
            with self.subTest(expiry=expiry), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "plan.json"
                path.write_text(json.dumps({"valid_until": expiry, "items": [item]}),
                                encoding="utf-8")
                args = self.args()
                args.file, args.i = str(path), 1
                args.net, args.stock_px, args.code = None, None, ""
                args.live_ok, args.delayed = False, False
                with patch.object(cli, "datetime", wraps=datetime) as clock, \
                     patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                     patch.object(cli, "cmd_combo", return_value=2) as combo, \
                     patch("sys.stdout", new_callable=io.StringIO):
                    clock.now.side_effect = lambda tz=None: (now.astimezone(tz) if tz
                                                             else now.replace(tzinfo=None))
                    if permitted:
                        self.assertEqual(cli.cmd_run(cli.DEFAULT_CFG, args), 2)
                        combo.assert_called_once()
                    else:
                        with self.assertRaises(SystemExit):
                            cli.cmd_run(cli.DEFAULT_CFG, args)
                        combo.assert_not_called()

    def test_run_lists_decimal_string_strikes_without_connection(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            item = {"i": 1, "preset": "cash-put", "symbol": "AAPL",
                    "expiry": "20261218", "strike": "40.125", "qty": "2"}
            path.write_text(json.dumps({"items": [item, dict(item, i=2)]}), encoding="utf-8")
            args = self.args()
            args.file, args.i = str(path), None
            with patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                 patch.object(cli, "connect") as connect, \
                 patch("sys.stdout", new_callable=io.StringIO) as out, \
                 self.assertRaises(SystemExit):
                cli.cmd_run(cli.DEFAULT_CFG, args)
            self.assertIn("40.125P", out.getvalue())
            connect.assert_not_called()

    def test_run_rejects_numeric_rounding_before_combo(self):
        import json
        import tempfile
        original = {"i": 1, "allowed": True, "conId": 20, "mult": "200",
                    "tradingClass": "AAPL", "exchange": "SMART", "qty": 1,
                    "strike": "40", "premium": "2", "symbol": "AAPL",
                    "currency": "USD", "preset": "cash-put", "expiry": "20261218"}
        for field, value in (("qty", "9007199254740993"), ("qty", 9007199254740993),
                             ("strike", "40.0000000000000001"),
                             ("premium", "2.0000000000000001"),
                             ("mult", "200.0000000000000001")):
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "plan.json"
                path.write_text(json.dumps({"items": [dict(original, **{field: value})]}),
                                encoding="utf-8")
                args = self.args()
                args.file, args.i = str(path), 1
                args.net, args.stock_px, args.code = None, None, ""
                args.live_ok, args.delayed = False, False
                with patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                     patch.object(cli, "cmd_combo") as combo, self.assertRaises(SystemExit):
                    cli.cmd_run(cli.DEFAULT_CFG, args)
                combo.assert_not_called()

    def test_run_rejects_invalid_or_duplicate_item_indices_before_combo(self):
        import json
        import tempfile
        original = {"i": 1, "allowed": True, "conId": 20, "mult": "200",
                    "tradingClass": "AAPL", "exchange": "SMART", "qty": 1,
                    "strike": 40, "premium": 2, "symbol": "WRONG_ROW",
                    "currency": "USD", "preset": "cash-put", "expiry": "20261218"}
        for invalid in (1.9, True, "1", 1, 0, None):
            with self.subTest(index=invalid), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "plan.json"
                path.write_text(json.dumps({"items": [dict(original, i=invalid),
                    dict(original, i=1, allowed=False, symbol="SELECTED_BLOCKED")]}), encoding="utf-8")
                args = self.args()
                args.file, args.i = str(path), 1
                args.net, args.stock_px, args.code = None, None, ""
                args.live_ok, args.delayed = False, False
                with patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                     patch.object(cli, "cmd_combo") as combo, self.assertRaises(SystemExit):
                    cli.cmd_run(cli.DEFAULT_CFG, args)
                combo.assert_not_called()

    def test_audit_filters_accounts_and_labels_legacy_unknown(self):
        import json
        import tempfile
        rows = [{"account": "DU1", "label": "FIRST"}, {"account": "DU2", "label": "SECOND"},
                {"label": "LEGACY"}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orders.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            args = self.args(account="DU2")
            args.today, args.limit = False, 20
            with patch.object(cli, "ORDER_LOG", path), \
                 patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(cli.cmd_audit({}, args), 0)
                self.assertIn("SECOND", output.getvalue())
                self.assertNotIn("FIRST", output.getvalue())
                self.assertNotIn("LEGACY", output.getvalue())
                self.assertIn("未知账户", output.getvalue())
            args.account = None
            with patch.object(cli, "ORDER_LOG", path), self.assertRaises(SystemExit):
                cli.cmd_audit({}, args)

    def test_global_account_flag_before_or_after_subcommand(self):
        for argv in (("thetamill", "--account", "DU1", "report"),
                     ("thetamill", "report", "--account", "DU1")):
            with patch.object(cli, "load_cfg", return_value=cli.DEFAULT_CFG), \
                 patch.object(cli, "cmd_report", return_value=0) as command, \
                 patch("sys.argv", list(argv)), self.assertRaises(SystemExit):
                cli.main()
            self.assertEqual(command.call_args.args[1].account, "DU1")


if __name__ == "__main__":
    unittest.main()
