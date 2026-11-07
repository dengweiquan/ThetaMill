"""Pure report rendering and create-only export; no broker calls."""
from datetime import datetime, timezone
from decimal import Decimal as D
import json
from pathlib import Path
import tempfile
import unittest

from thetamill.broker_state import Instrument, Position, Snapshot, WorkingOrder
from thetamill.report import build_report, render_report, write_report

NOW = datetime(2026, 11, 7, 20, tzinfo=timezone.utc)


def snapshot(price=D(2), issues=()):
    option = Instrument(20, "AAPL", "USD", "OPT", "SMART", 10, D(200), "P", D(40), "20261218", "AAPL")
    return Snapshot("DU1234567", "paper", NOW, "USD", {"USD": D(16000)}, D(20000),
                    (Position(option, D(-2), price),),
                    (WorkingOrder(option, "SELL", D(1), 1, 2, "tm-secret", D(2), "LMT"),),
                    {"USD": D(1)}, issues)


class ReportTests(unittest.TestCase):
    def test_json_decimal_and_masked_account(self):
        report = build_report(snapshot(), risk_summary={"put_cash": D(16000)}, ledger_summary={}, now=NOW)
        text = render_report(report, "json")
        data = json.loads(text)
        self.assertEqual(data["schema"], "thetamill.report.v1")
        self.assertEqual(data["positions"][0]["instrument"]["multiplier"], "200")
        self.assertEqual(data["risk"]["put_cash"], "16000")
        self.assertEqual(data["account"], "***567")
        self.assertNotIn("DU1234567", text)
        self.assertNotIn("tm-secret", text)
        self.assertTrue(data["read_only"])

    def test_missing_broker_and_risk_are_unknown(self):
        report = build_report(None, now=NOW)
        self.assertFalse(report["complete"])
        self.assertIsNone(report["cash"])
        self.assertIsNone(report["risk"])
        self.assertIsNone(report["ledger"])
        self.assertIn("未知", render_report(report))
        self.assertIn("不得据此重发", render_report(report))

    def test_missing_quote_not_zero(self):
        report = build_report(snapshot(None, ("Missing market price: 20",)), now=NOW)
        self.assertIsNone(report["positions"][0]["market_price"])
        self.assertFalse(report["complete"])
        self.assertIn("N/A", render_report(report, "markdown"))

    def test_missing_price_timestamp_is_not_claimed_complete(self):
        from dataclasses import replace
        snap = snapshot()
        stock = Instrument(10, "AAPL", "USD", "STK", "SMART", 10)
        snap = replace(snap, positions=(Position(stock, D(100), D(100)),))
        report = build_report(snap, risk_summary={"cash_confirmed": True},
                              ledger_summary={"outstanding": 0}, now=NOW)
        self.assertFalse(report["complete"])
        self.assertTrue(any("timestamp" in x for x in report["issues"]))
        self.assertEqual(report["price_sources"], {})

    def test_stale_future_or_mismatched_valuation_withholds_budget(self):
        from dataclasses import replace
        from datetime import timedelta
        from thetamill.broker_state import PriceQuote, FxQuote
        stock = Instrument(10, "AAPL", "USD", "STK", "SMART", 10)
        base = replace(snapshot(), positions=(Position(stock, D(100), D(100)),),
                       price_quotes={10: PriceQuote(10, D(100), NOW, "IB timestamped bid/ask midpoint")})
        cases = [
            replace(base, captured_at=NOW - timedelta(seconds=31)),
            replace(base, captured_at=NOW + timedelta(seconds=6)),
            replace(base, price_quotes={10: PriceQuote(10, D(100), NOW - timedelta(seconds=31), "IB timestamped bid/ask midpoint")}),
            replace(base, price_quotes={10: PriceQuote(10, D(100), NOW + timedelta(seconds=6), "IB timestamped bid/ask midpoint")}),
            replace(base, price_quotes={10: PriceQuote(10, D(101), NOW, "IB timestamped bid/ask midpoint")}),
            replace(base, fx={"USD": D(1), "HKD": D("0.128")},
                    fx_quotes={"HKD": FxQuote("HKD", D("0.128"), NOW - timedelta(seconds=31), "IB timestamped bid/ask midpoint")}),
            replace(base, fx={"USD": D(1), "HKD": D("0.128")},
                    fx_quotes={"HKD": FxQuote("HKD", D("0.129"), NOW, "IB timestamped bid/ask midpoint")}),
        ]
        for snap in cases:
            with self.subTest(snapshot=snap):
                report = build_report(snap, risk_summary={"free_cash_base": D(16000)},
                                      ledger_summary={"outstanding": 0}, now=NOW)
                self.assertFalse(report["complete"])
                self.assertIsNone(report["risk"])
                self.assertTrue(report["issues"])
        valid = build_report(base, risk_summary={"cash_confirmed": True},
                             ledger_summary={"outstanding": 0}, now=NOW)
        self.assertTrue(valid["complete"])

    def test_unsupported_valuation_sources_withhold_budget(self):
        from dataclasses import replace
        from thetamill.broker_state import PriceQuote, FxQuote
        stock = Instrument(10, "AAPL", "USD", "STK", "SMART", 10)
        cases = (
            replace(snapshot(), positions=(Position(stock, D(100), D(100)),),
                    price_quotes={10: PriceQuote(10, D(100), NOW, "IB delayed/frozen historical close")}),
            replace(snapshot(), cash={"USD": D(16000), "HKD": D(780)},
                    fx={"USD": D(1), "HKD": D("0.128")},
                    fx_quotes={"HKD": FxQuote("HKD", D("0.128"), NOW, "manually relabelled yesterday close")}),
        )
        for snap in cases:
            with self.subTest(snapshot=snap):
                report = build_report(snap, risk_summary={"cash_confirmed": True},
                                      ledger_summary={"outstanding": 0}, now=NOW)
                self.assertFalse(report["complete"])
                self.assertIsNone(report["risk"])
                self.assertTrue(any("source" in x for x in report["issues"]))

    def test_freshness_limit_is_explicit_and_validated(self):
        from dataclasses import replace
        from datetime import timedelta
        snap = replace(snapshot(), captured_at=NOW - timedelta(seconds=40))
        report = build_report(snap, risk_summary={"cash_confirmed": True},
                              ledger_summary={"outstanding": 0}, now=NOW,
                              max_data_age_seconds=60)
        self.assertTrue(report["complete"])
        for value in (True, 0, -1, float("nan")):
            with self.assertRaises(ValueError):
                build_report(snap, now=NOW, max_data_age_seconds=value)

    def test_snapshot_age_override_does_not_relax_market_data_age(self):
        from dataclasses import replace
        from datetime import timedelta
        from thetamill.broker_state import Instrument, Position, PriceQuote
        stock = Instrument(10, "AAPL", "USD", "STK", "SMART", 10)
        snap = replace(snapshot(), positions=(Position(stock, D(100), D(100)),),
                       price_quotes={10: PriceQuote(10, D(100), NOW - timedelta(seconds=40), "IB timestamped bid/ask midpoint")})
        report = build_report(snap, risk_summary={"free_cash_base": "16000"},
                              ledger_summary={"outstanding": 0}, now=NOW,
                              max_data_age_seconds=120)
        self.assertFalse(report["complete"])
        self.assertIsNone(report["risk"])

    def test_no_snapshot_cannot_carry_an_unanchored_available_budget(self):
        report = build_report(None, risk_summary={"free_cash_base": "16000"}, now=NOW)
        self.assertIsNone(report["risk"])
        self.assertFalse(report["complete"])

    def test_expiry_is_computed_from_supplied_clock(self):
        report = build_report(snapshot(), now=NOW)
        self.assertEqual(report["expiry_distribution"][0]["days_to_expiry"], 41)
        self.assertEqual(report["expiry_distribution"][0]["contracts"], "2")

    def test_explicit_reveal_is_opt_in(self):
        self.assertEqual(build_report(snapshot(), now=NOW, reveal_account=True)["account"], "DU1234567")

    def test_reveal_account_does_not_reveal_approval_secrets(self):
        report = build_report(snapshot(), risk_summary={"approval_code": "approval-secret"},
                              ledger_summary={"intent_id": "intent-secret", "order_ref": "ref-secret"},
                              now=NOW, reveal_account=True)
        text = render_report(report, "json")
        self.assertIn("DU1234567", text)
        for secret in ("approval-secret", "intent-secret", "ref-secret"):
            self.assertNotIn(secret, text)

    def test_no_nan_in_export_and_no_pnl_inference(self):
        report = build_report(snapshot(), risk_summary={"missing": float("nan"), "unknown": D("NaN")}, now=NOW)
        text = render_report(report, "json")
        self.assertNotIn("NaN", text)
        self.assertEqual(json.loads(text)["risk"], {"missing": None, "unknown": None})
        self.assertIsNone(report["strategy_realized_pnl"])

    def test_export_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.md"
            write_report(path, "original")
            with self.assertRaises(FileExistsError):
                write_report(path, "replacement")
            self.assertEqual(path.read_text(), "original")

    def test_invalid_format_and_naive_time_fail(self):
        with self.assertRaises(ValueError):
            render_report(build_report(None, now=NOW), "html")
        with self.assertRaises(ValueError):
            build_report(None, now=datetime(2026, 11, 7))

    def test_missing_collections_are_not_reported_as_zero(self):
        report = build_report(None, now=NOW)
        self.assertIsNone(report["orders"])
        self.assertIsNone(report["positions"])
        self.assertNotIn("在途订单：0", render_report(report))

    def test_missing_values_and_empty_assessments_mark_incomplete(self):
        from dataclasses import replace
        for snap, risk in ((replace(snapshot(), net_liquidation=None), {}),
                           (snapshot(), {"coverage": float("nan")})):
            report = build_report(snap, risk_summary=risk, ledger_summary={}, now=NOW)
            self.assertFalse(report["complete"])
            self.assertTrue(report["issues"])

    def test_nested_private_fields_are_redacted(self):
        report = build_report(snapshot(), risk_summary={"nested": {"account": "DU1234567"}},
                              ledger_summary={"account": "DU1234567", "order_ref": "tm-secret",
                                              "by_scope": {"DU1234567|paper": "pending"}}, now=NOW)
        for fmt in ("json", "markdown"):
            text = render_report(report, fmt)
            self.assertNotIn("DU1234567", text)
            self.assertNotIn("tm-secret", text)

    def test_expiry_requires_exact_date(self):
        from dataclasses import replace
        snap = snapshot()
        bad = replace(snap.positions[0].instrument, expiry="2026121")
        snap = replace(snap, positions=(replace(snap.positions[0], instrument=bad),))
        report = build_report(snap, risk_summary={"coverage": "known"}, ledger_summary={"unresolved": 0}, now=NOW)
        self.assertFalse(report["complete"])
        self.assertEqual(report["expiry_distribution"], [])

    def test_missing_position_price_is_incomplete_without_collector_warning(self):
        report = build_report(snapshot(None), risk_summary={"cash_confirmed": True},
                              ledger_summary={"outstanding": 0}, now=NOW)
        self.assertFalse(report["complete"])
        self.assertIsNone(report["positions"][0]["market_price"])
        self.assertTrue(any("price unavailable" in issue for issue in report["issues"]))

    def test_markdown_includes_expiry_distribution(self):
        report = build_report(snapshot(), now=NOW)
        text = render_report(report, "markdown")
        self.assertIn("## 到期分布", text)
        self.assertIn("| 20261218 | 41 | 2 |", text)
        self.assertIn("未知", render_report(build_report(None, now=NOW), "markdown"))

    def test_order_table_includes_remaining(self):
        text = render_report(build_report(snapshot(), now=NOW), "markdown")
        self.assertIn("SELL", text)
        self.assertIn("剩余量", text)

    def test_cli_report_has_no_trading_side_effects(self):
        import argparse
        import io
        from unittest.mock import MagicMock, patch
        from thetamill import cli
        ib = MagicMock()
        args = argparse.Namespace(format="json", output=None, reveal_account=False, account=None,
                                  paper=True, port=4002, delayed=False)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
             patch.object(cli, "connect", return_value=ib), \
             patch.object(cli, "finish"), \
             patch.object(cli, "_data_dir", return_value=Path(directory)), \
             patch.object(cli, "_risk_snapshot", return_value=snapshot()), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            rc = cli.cmd_report(cli.DEFAULT_CFG, args)
            data = json.loads(output.getvalue())
            self.assertEqual(rc, 3)
            self.assertFalse(data["complete"])
            self.assertNotIn("DU1234567", output.getvalue())
            self.assertFalse((Path(directory) / "state.sqlite3").exists())
        ib.placeOrder.assert_not_called()
        ib.cancelOrder.assert_not_called()

    def test_cli_report_broker_failure_outputs_unknown_json(self):
        import argparse
        import io
        from unittest.mock import patch
        from thetamill import cli
        args = argparse.Namespace(format="json", output=None, reveal_account=False, account=None,
                                  paper=True, port=4002, delayed=False)
        with patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
             patch.object(cli, "connect", side_effect=SystemExit("offline")), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(cli.cmd_report(cli.DEFAULT_CFG, args), 3)
            self.assertIsNone(json.loads(output.getvalue())["orders"])

    def test_cli_report_suppresses_raw_broker_diagnostics_on_both_streams(self):
        import argparse
        import io
        import sys
        from unittest.mock import MagicMock, patch
        from thetamill import cli
        ib = MagicMock()
        ib._diag = [(999, "account DU1234567 permission error")]
        args = argparse.Namespace(format="json", output=None, reveal_account=False,
                                  account=None, paper=True, port=4002)

        def connect(*a, **kw):
            print("account DU1234567 connect diagnostic")
            print("account DU1234567 broker diagnostic", file=sys.stderr)
            return ib

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
             patch.object(cli, "connect", side_effect=connect), \
             patch.object(cli.IB, "disconnect"), \
             patch.object(cli, "_data_dir", return_value=Path(directory)), \
             patch.object(cli, "_risk_snapshot", return_value=snapshot()), \
             patch("sys.stdout", new_callable=io.StringIO) as out, \
             patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(cli.cmd_report(cli.DEFAULT_CFG, args), 3)
            self.assertNotIn("DU1234567", out.getvalue() + err.getvalue())
            self.assertIsInstance(json.loads(out.getvalue()), dict)
        ib.placeOrder.assert_not_called()
        ib.cancelOrder.assert_not_called()

    def test_report_does_not_initialize_existing_empty_ledger(self):
        import argparse
        import io
        from unittest.mock import MagicMock, patch
        from thetamill import cli
        args = argparse.Namespace(format="json", output=None, reveal_account=False,
                                  account=None, paper=True, port=4002)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            path.touch()
            with patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                 patch.object(cli, "connect", return_value=MagicMock()), patch.object(cli, "finish"), \
                 patch.object(cli, "_data_dir", return_value=Path(directory)), \
                 patch.object(cli, "_risk_snapshot", return_value=snapshot()), \
                 patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(cli.cmd_report(cli.DEFAULT_CFG, args), 3)
                self.assertIsNone(json.loads(output.getvalue())["risk"])
            self.assertEqual(path.stat().st_size, 0)
            self.assertEqual([p.name for p in Path(directory).iterdir()], ["state.sqlite3"])

    def test_cli_report_accounts_for_local_reservations(self):
        import argparse
        import io
        from types import SimpleNamespace as NS
        from unittest.mock import MagicMock, patch
        from thetamill import cli
        ib, ledger = MagicMock(), MagicMock()
        ledger.outstanding_reservations.return_value = [NS(state="unknown")]
        held = {"cash_base": D(4000), "shares": {}, "close": {}, "exposure": {}}
        args = argparse.Namespace(format="json", output=None, reveal_account=False, account=None,
                                  paper=True, port=4002)
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "state.sqlite3").touch()
            with patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                 patch.object(cli, "connect", return_value=ib), patch.object(cli, "finish"), \
                 patch.object(cli, "_data_dir", return_value=Path(directory)), \
                 patch.object(cli, "_risk_snapshot", return_value=snapshot()), \
                 patch("thetamill.state.SubmissionLedger", return_value=ledger), \
                 patch("thetamill.execution.aggregate_reservations", create=True, return_value=held), \
                 patch("thetamill.risk.summarize", return_value={"cash_confirmed": True}) as summarize, \
                 patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(cli.cmd_report(cli.DEFAULT_CFG, args), 3)
                data = json.loads(output.getvalue())
                self.assertFalse(data["complete"])
                self.assertEqual(data["ledger"]["states"], ["unknown"])
                self.assertEqual(summarize.call_args.kwargs["reservations"], held)
        ib.placeOrder.assert_not_called()
        ib.cancelOrder.assert_not_called()

    def test_cli_report_distinguishes_missing_frozen_and_verified_quota(self):
        import argparse
        import io
        from dataclasses import replace
        from unittest.mock import MagicMock, patch
        from thetamill import cli
        from thetamill.state import SubmissionLedger, MigrationFreeze
        cfg = dict(cli.DEFAULT_CFG, risk={"enabled": True, "day_timezone": "UTC",
                                         "max_daily_submissions": 5})
        args = argparse.Namespace(format="json", output=None, reveal_account=False,
                                  account=None, paper=True, port=4002)
        for mode in ("missing", "frozen", "verified"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.sqlite3"
                ledger = SubmissionLedger(path, "UTC")
                if mode != "missing":
                    try:
                        ledger.prepare_day_budget(account="DU1234567", environment="paper",
                                                  day_key="2026-11-07", limit_count=5,
                                                  verified_prior_submissions=2 if mode == "verified" else None)
                    except MigrationFreeze:
                        self.assertEqual(mode, "frozen")
                snap = replace(snapshot(), captured_at=datetime.now(timezone.utc))
                with patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                     patch.object(cli, "connect", return_value=MagicMock()), \
                     patch.object(cli, "finish"), \
                     patch.object(cli, "_data_dir", return_value=Path(directory)), \
                     patch.object(cli, "_risk_snapshot", return_value=snap), \
                     patch("thetamill.state.day_key_for", return_value="2026-11-07"), \
                     patch("sys.stdout", new_callable=io.StringIO) as out:
                    rc = cli.cmd_report(cfg, args)
                report = json.loads(out.getvalue())
                quota = report["ledger"]["quota"]
                self.assertTrue(report["ledger"]["daily_limits_enabled"])
                self.assertEqual(report["ledger"]["quota_day"], "2026-11-07")
                if mode == "missing":
                    self.assertFalse(quota["exists"])
                    self.assertIsNone(quota["remaining"])
                    self.assertEqual(rc, 3)
                elif mode == "frozen":
                    self.assertTrue(quota["migration_frozen"])
                    self.assertEqual(quota["remaining"], 0)
                    self.assertEqual(rc, 3)
                else:
                    self.assertEqual(quota["submitted_count"], 2)
                    self.assertEqual(quota["remaining"], 3)
                    self.assertEqual(rc, 0)
                    self.assertTrue(report["complete"])

    def test_cli_failure_does_not_leak_unavailable_account(self):
        import argparse
        import io
        from unittest.mock import patch
        from thetamill import cli
        args = argparse.Namespace(format="json", output=None, reveal_account=False,
                                  account=None, paper=True, port=4002)
        with patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
             patch.object(cli, "connect", side_effect=SystemExit("检测到实盘账号 U1234567")), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(cli.cmd_report(cli.DEFAULT_CFG, args), 3)
            self.assertNotIn("U1234567", output.getvalue())
            self.assertIsNone(json.loads(output.getvalue())["orders"])

    def test_unresolved_ledger_is_not_complete(self):
        report = build_report(snapshot(), risk_summary={"cash_confirmed": True},
                              ledger_summary={"outstanding": 1, "states": ["unknown"]}, now=NOW)
        self.assertFalse(report["complete"])
        self.assertTrue(any("submission" in x.lower() for x in report["issues"]))

    def test_terminal_and_markdown_include_non_advisory_scope(self):
        report = build_report(snapshot(), now=NOW)
        for fmt in ("terminal", "markdown"):
            text = render_report(report, fmt)
            self.assertIn("非正式收盘报告", text)
            self.assertIn("200", text)
            self.assertIn("不从不完整本地日志", text)


if __name__ == "__main__":
    unittest.main()
