"""Offline tests for the audit trail (P0: the audit stream was broken).

Two defects are pinned here:

1. ``_write_audit`` had zero callers, so a REAL submission left no trace in
   ``orders.jsonl`` at all, while the legacy unit-test path wrote rows whose
   fields were ``"<MagicMock name='mock.placeOrder().order.orderId' ...>"``.
2. ``thetamill audit`` printed those mock rows as if they were broker orders and
   showed nothing for genuinely submitted orders (#133 / #140 live only in the
   SQLite ledger).

The tests are OFFLINE: no broker, no network.  Every submission runs against the
existing mock broker harness.
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from thetamill import audit, cli, state, submission_cli as sc

ACCOUNT = "DU7654321"
ENV = "paper"
TZ = "America/New_York"
NOW = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
CONFIG = {"risk": {"enabled": True, "day_timezone": TZ,
                   "max_daily_submissions": 5, "config_version": "p3-1"}}


class MockRepr:
    """Stands in for a leaked MagicMock: its str() is a mock repr."""

    def __repr__(self):
        return "<MagicMock name='mock.placeOrder().order.orderId' id='4488660704'>"

    __str__ = __repr__


# --------------------------------------------------------------------------- #
# Writing: only real values ever reach the file
# --------------------------------------------------------------------------- #

class AuditWriterTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "orders.jsonl"

    def _write(self, fields, **kw):
        warnings = []
        record = audit.append_record(self.path, fields,
                                     warn=warnings.append, **kw)
        return record, warnings

    def _lines(self):
        if not self.path.exists():
            return []
        return [json.loads(line) for line in
                self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_writes_a_clean_record(self):
        record, warnings = self._write(
            {"orderId": 140, "status": "Submitted", "env": "纸账户(Paper)",
             "account": ACCOUNT, "strategy": "cash-put"}, now=NOW)
        self.assertEqual(warnings, [])
        self.assertIsNotNone(record)
        self.assertEqual(record["orderId"], 140)
        self.assertEqual(record["schema"], audit.AUDIT_SCHEMA)
        self.assertEqual(len(self._lines()), 1)

    def test_mock_object_field_is_dropped_not_repr_ified(self):
        record, warnings = self._write(
            {"orderId": 140, "status": "Submitted",
             "qty": MockRepr(), "type": "LMT"}, now=NOW)
        self.assertIsNotNone(record)
        self.assertNotIn("qty", record)
        self.assertEqual(record["dropped_fields"], ["qty"])
        self.assertNotIn("MagicMock", self.path.read_text(encoding="utf-8"))
        self.assertTrue(warnings == [])

    def test_mock_repr_string_field_is_refused(self):
        """A leaked ``default=str`` value is an artefact, not evidence."""
        record, _ = self._write(
            {"orderId": 140, "status": "Submitted",
             "label": "<MagicMock name='mock.symbol' id='1'> 正股"}, now=NOW)
        self.assertEqual(record["dropped_fields"], ["label"])
        self.assertNotIn("MagicMock", self.path.read_text(encoding="utf-8"))

    def test_record_with_no_real_identification_is_not_written(self):
        record, warnings = self._write(
            {"orderId": MockRepr(), "status": MockRepr()}, now=NOW)
        self.assertIsNone(record)
        self.assertFalse(self.path.exists())
        self.assertTrue(any("no real submission fields" in w for w in warnings))

    def test_non_finite_numbers_are_dropped(self):
        record, _ = self._write(
            {"orderId": 140, "status": "Submitted", "px": float("nan"),
             "net": Decimal("Infinity")}, now=NOW)
        self.assertNotIn("px", record)
        self.assertNotIn("net", record)

    def test_unknown_receipt_without_order_id_is_still_recorded(self):
        """An unknown receipt is exactly what the trail must preserve."""
        record, _ = self._write(
            {"orderId": None, "status": "unknown", "intent": "int-1"}, now=NOW)
        self.assertIsNotNone(record)
        self.assertIsNone(record["orderId"])

    def test_appending_never_raises_on_an_unwritable_path(self):
        target = Path(self._tmp.name) / "nope" / "orders.jsonl"
        target.parent.mkdir()
        target.mkdir()  # a directory where the file should be
        warnings = []
        self.assertIsNone(audit.append_record(
            target, {"orderId": 1, "status": "Submitted"}, warn=warnings.append))
        self.assertTrue(warnings)

    def test_decimal_is_serialised_exactly(self):
        record, _ = self._write(
            {"orderId": 140, "status": "Submitted", "net": Decimal("4.20")},
            now=NOW)
        self.assertEqual(record["net"], "4.2")
        self.assertEqual(self._lines()[0]["net"], "4.2")


# --------------------------------------------------------------------------- #
# Reading: mock rows are never presented as broker orders
# --------------------------------------------------------------------------- #

class AuditReaderTests(unittest.TestCase):

    def test_mock_rows_are_not_real(self):
        self.assertFalse(audit.is_real_record({
            "orderId": "<MagicMock name='mock.placeOrder().order.orderId' id='1'>",
            "status": "Submitted"}))
        self.assertFalse(audit.is_real_record({
            "orderId": 140,
            "status": "<MagicMock name='mock.placeOrder().orderStatus.status'>"}))

    def test_real_rows_are_real(self):
        self.assertTrue(audit.is_real_record({"orderId": 140,
                                              "status": "PreSubmitted"}))
        self.assertTrue(audit.is_real_record({"orderId": "140",
                                              "status": "Filled"}))
        self.assertTrue(audit.is_real_record({"orderId": None,
                                              "status": "unknown"}))
        self.assertTrue(audit.is_real_record({"orderId": "",
                                              "status": "Submitted"}))
        self.assertTrue(audit.is_real_record({"orderId": "",
                                              "status": "unknown"}))

    def test_empty_order_id_and_safeguards(self):
        # Empty string is accepted as missing legacy ID
        self.assertTrue(audit.is_real_record({"orderId": "", "status": "Submitted"}))
        self.assertTrue(audit.is_real_record({"orderId": None, "status": "unknown"}))
        # Safeguards retained: bool, non-numeric, synthetic, non-dict
        self.assertFalse(audit.is_real_record({"orderId": True, "status": "Submitted"}))
        self.assertFalse(audit.is_real_record({"orderId": False, "status": "Submitted"}))
        self.assertFalse(audit.is_real_record({"orderId": "invalid_id", "status": "Submitted"}))
        self.assertFalse(audit.is_real_record({"orderId": 12.34, "status": "Submitted"}))
        self.assertFalse(audit.is_real_record({"orderId": "<MagicMock>", "status": "Submitted"}))
        self.assertFalse(audit.is_real_record("not-a-dict"))


# --------------------------------------------------------------------------- #
# The CLI audit view stops showing leaked rows
# --------------------------------------------------------------------------- #

class CliAuditTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "orders.jsonl"

    def _run(self, rows, *, account=ACCOUNT, limit=20, today=False):
        self.path.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                             encoding="utf-8")
        args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
            ["audit", "--account", account, "--limit", str(limit)]
            + (["--today"] if today else []))
        buf = io.StringIO()
        with patch.object(cli, "ORDER_LOG", self.path), \
             patch.object(cli, "_data_dir", return_value=Path(self._tmp.name)), \
             redirect_stdout(buf):
            rc = args.func(dict(cli.DEFAULT_CFG), args)
        return rc, buf.getvalue()

    def test_mock_rows_are_hidden_and_reported(self):
        rc, out = self._run([
            {"account": ACCOUNT, "orderId": 140, "status": "Submitted",
             "preset": "cash-put", "ts": "2026-11-07T10:00:00"},
            {"account": ACCOUNT,
             "orderId": "<MagicMock name='mock.placeOrder().order.orderId'>",
             "status": "<MagicMock name='mock.status'>", "ts": "2026-11-08T00:05:11"},
        ])
        self.assertEqual(rc, 0)
        self.assertIn("cash-put", out)
        self.assertNotIn("MagicMock", out)
        self.assertIn("1", out)
        self.assertIn("非真实", out)

    def test_real_orders_are_listed_in_order(self):
        rc, out = self._run([
            {"account": ACCOUNT, "orderId": 133, "status": "Filled",
             "preset": "order:stk:buy", "ts": "2026-11-07T10:00:00"},
            {"account": ACCOUNT, "orderId": 140, "status": "Submitted",
             "preset": "cash-put", "ts": "2026-11-07T11:00:00"},
        ])
        self.assertEqual(rc, 0)
        self.assertIn("133", out)
        self.assertIn("140", out)

    def test_audit_reads_the_ledger_for_orders_never_written_to_jsonl(self):
        """#133/#140 exist only in SQLite: audit must show them."""
        with tempfile.TemporaryDirectory() as directory:
            ledger_path = Path(directory) / "state.sqlite3"
            ledger = state.SubmissionLedger(ledger_path, TZ)
            day_key = state.day_key_for(TZ, now=NOW)
            ledger.set_daily_limit(account=ACCOUNT, environment=ENV,
                                   day_key=day_key, limit_count=5)
            ledger.set_resource(account=ACCOUNT, environment=ENV,
                                currency="USD", resource_key="cash_base",
                                amount=Decimal("1000000"))
            intent = ledger.create_intent(
                account=ACCOUNT, environment=ENV,
                payload={"kind": "prepared_legs", "legs": [{}]},
                config={}, ttl_seconds=600, now=NOW)
            ledger.claim_intent(account=ACCOUNT, environment=ENV,
                                intent_id=intent["intent_id"],
                                full_digest=intent["full_digest"],
                                code=intent["approval_code"], now=NOW)
            rid = ledger.reserve(
                account=ACCOUNT, environment=ENV, intent_id=intent["intent_id"],
                day_key=day_key,
                items=[state.RiskReservationSpec("USD", "cash_base", "1")],
                day_submission_slots=1, now=NOW)
            refs = ledger.record_legs(
                intent_id=intent["intent_id"], reservation_id=rid, now=NOW,
                legs=[{"contract_id": "937548729", "action": "SELL",
                       "quantity": "1", "order_ref": "ref-133"}])
            ledger.begin_leg_attempt(refs[0], now=NOW)
            ledger.record_leg_result(refs[0], state="submitted", now=NOW)
            ledger.confirm_leg("ref-133", broker_status="Filled",
                               order_id=140, perm_id=1362116732)
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT])
            buf = io.StringIO()
            with patch.object(cli, "ORDER_LOG", Path(directory) / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=Path(directory)), \
                 redirect_stdout(buf):
                rc = args.func(dict(cli.DEFAULT_CFG), args)
            out = buf.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("140", out)
            self.assertIn("账本", out)


    def test_ledger_row_status_prefers_recorded_broker_status(self):
        """A confirmed leg records HOW the broker resolved it, not just that."""
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            ledger = state.SubmissionLedger(data_dir / "state.sqlite3", TZ)
            day_key = state.day_key_for(TZ, now=NOW)
            ledger.set_daily_limit(account=ACCOUNT, environment=ENV,
                                   day_key=day_key, limit_count=5)
            ledger.set_resource(account=ACCOUNT, environment=ENV,
                                currency="USD", resource_key="cash_base",
                                amount=Decimal("1000000"))
            intent = ledger.create_intent(
                account=ACCOUNT, environment=ENV,
                payload={"kind": "prepared_legs", "legs": [{}]},
                config={}, ttl_seconds=600, now=NOW)
            ledger.claim_intent(account=ACCOUNT, environment=ENV,
                                intent_id=intent["intent_id"],
                                full_digest=intent["full_digest"],
                                code=intent["approval_code"], now=NOW)
            rid = ledger.reserve(
                account=ACCOUNT, environment=ENV, intent_id=intent["intent_id"],
                day_key=day_key,
                items=[state.RiskReservationSpec("USD", "cash_base", "1")],
                day_submission_slots=1, now=NOW)
            refs = ledger.record_legs(
                intent_id=intent["intent_id"], reservation_id=rid, now=NOW,
                legs=[{"contract_id": "937548729", "action": "SELL",
                       "quantity": "1", "order_ref": "ref-140"}])
            ledger.begin_leg_attempt(refs[0], now=NOW)
            ledger.record_leg_result(refs[0], state="submitted", now=NOW)
            ledger.confirm_leg("ref-140", broker_status="PreSubmitted",
                               order_id=140)
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT])
            buf = io.StringIO()
            with patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf):
                self.assertEqual(args.func(dict(cli.DEFAULT_CFG), args), 0)
            out = buf.getvalue()
            self.assertIn("140", out)
            self.assertIn("PreSubmitted", out)

    def _parse_table_rows(self, output):
        rows = []
        for line in output.splitlines():
            line_s = line.strip()
            if not line_s or line_s.startswith("审计") or line_s.startswith("时间") or line_s.startswith("共") or line_s.startswith("其中") or line_s.startswith("⚠️") or line_s.startswith("已隐藏"):
                continue
            parts = line_s.split()
            if len(parts) >= 10:
                rows.append(parts)
        return rows

    def _setup_ledger_order(self, data_dir, *, order_ref, contract_id="1001",
                            order_id=None, perm_id=None, broker_status=None,
                            state_val="confirmed", account=ACCOUNT, env=ENV):
        ledger = state.SubmissionLedger(data_dir / "state.sqlite3", TZ)
        day_key = state.day_key_for(TZ, now=NOW)
        ledger.set_daily_limit(account=account, environment=env,
                               day_key=day_key, limit_count=10)
        ledger.set_resource(account=account, environment=env,
                            currency="USD", resource_key="cash_base",
                            amount=Decimal("1000000"))
        intent = ledger.create_intent(
            account=account, environment=env,
            payload={"kind": "prepared_legs", "legs": [{}]},
            config={}, ttl_seconds=600, now=NOW)
        ledger.claim_intent(account=account, environment=env,
                            intent_id=intent["intent_id"],
                            full_digest=intent["full_digest"],
                            code=intent["approval_code"], now=NOW)
        rid = ledger.reserve(
            account=account, environment=env, intent_id=intent["intent_id"],
            day_key=day_key,
            items=[state.RiskReservationSpec("USD", "cash_base", "1")],
            day_submission_slots=1, now=NOW)
        refs = ledger.record_legs(
            intent_id=intent["intent_id"], reservation_id=rid, now=NOW,
            legs=[{"contract_id": contract_id, "action": "SELL",
                   "quantity": "1", "order_ref": order_ref}])
        ledger.begin_leg_attempt(refs[0], now=NOW)
        if state_val == "submitting":
            return refs[0], intent["intent_id"]
        sub_state = "submitted" if state_val in ("submitted", "confirmed") else state_val
        ledger.record_leg_result(refs[0], state=sub_state, now=NOW)
        if state_val == "confirmed":
            ledger.confirm_leg(refs[0], broker_status=broker_status or "Filled",
                               order_id=order_id, perm_id=perm_id, now=NOW)
        elif order_id or perm_id:
            ledger.bind_broker_ids(refs[0], order_id=order_id, perm_id=perm_id, now=NOW)
        return refs[0], intent["intent_id"]

    def test_audit_table_separated_columns_and_confirmed_not_filled(self):
        from thetamill.report import mask_account
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _, int1 = self._setup_ledger_order(data_dir, order_ref="ref-conf", order_id=140,
                                               perm_id=900, broker_status="Filled", state_val="confirmed")
            _, int2 = self._setup_ledger_order(data_dir, order_ref="ref-sub", order_id=141,
                                               state_val="submitted")
            jsonl_rows = [
                {"account": ACCOUNT, "env": "paper", "orderRef": "ref-conf",
                 "orderId": 140, "permId": 900, "preset": "cash-put", "ts": "2026-11-07T10:00:00"},
                {"account": ACCOUNT, "env": "paper", "orderId": 999,
                 "preset": "log-only", "status": "unknown", "ts": "2026-11-07T11:00:00"},
            ]
            (data_dir / "orders.jsonl").write_text(
                "\n".join(json.dumps(r) for r in jsonl_rows) + "\n", encoding="utf-8")
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT])
            buf = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf):
                self.assertEqual(args.func(dict(cli.DEFAULT_CFG), args), 0)
            out = buf.getvalue()
            for header in ("本地腿状态", "最后券商观察", "券商观察时间", "未决风险", "来源"):
                self.assertIn(header, out)
            self.assertNotIn(ACCOUNT, out)
            self.assertIn(mask_account(ACCOUNT), out)
            self.assertNotIn("ref-conf", out)
            self.assertNotIn("ref-sub", out)
            self.assertNotIn(int1, out)
            self.assertNotIn(int2, out)
            rows = self._parse_table_rows(out)
            by_oid = {r[2]: r for r in rows}
            self.assertIn("140", by_oid)
            r140 = by_oid["140"]
            self.assertEqual(r140[5], "confirmed")
            self.assertEqual(r140[6], "Filled")
            self.assertNotEqual(r140[7], "-")
            self.assertEqual(r140[8], "False")
            self.assertEqual(r140[9], "日志+账本")

            self.assertIn("141", by_oid)
            r141 = by_oid["141"]
            self.assertEqual(r141[5], "submitted")
            self.assertEqual(r141[6], "-")
            self.assertEqual(r141[8], "True")
            self.assertEqual(r141[9], "账本")

            self.assertIn("999", by_oid)
            r999 = by_oid["999"]
            self.assertEqual(r999[8], "unknown")
            self.assertEqual(r999[9], "日志")

    def test_multi_leg_jsonl_explicitly_forbidden_from_matching(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _, int1 = self._setup_ledger_order(data_dir, order_ref="ref-multi", order_id=201,
                                               state_val="submitted")
            multi_leg_row = {
                "account": ACCOUNT, "env": "paper", "orderRef": "ref-multi",
                "orderId": 201, "preset": "combo", "ts": "2026-11-07T10:00:00",
                "legs": [
                    {"order_ref": "ref-multi", "state": "submitted"},
                    {"order_ref": "ref-leg2", "state": "submitted"},
                ],
            }
            (data_dir / "orders.jsonl").write_text(json.dumps(multi_leg_row) + "\n",
                                                   encoding="utf-8")
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT])
            buf = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf):
                self.assertEqual(args.func(dict(cli.DEFAULT_CFG), args), 0)
            out = buf.getvalue()
            self.assertNotIn(ACCOUNT, out)
            self.assertNotIn("ref-multi", out)
            self.assertNotIn("ref-leg2", out)
            self.assertNotIn(int1, out)
            rows = self._parse_table_rows(out)
            self.assertEqual(len(rows), 2)
            sources = {r[9] for r in rows}
            self.assertEqual(sources, {"日志", "账本"})
            for r in rows:
                if r[9] == "日志":
                    self.assertEqual(r[8], "unknown")
                elif r[9] == "账本":
                    self.assertEqual(r[8], "True")

    def test_ledger_ambiguity_leaves_rows_separate_without_winner(self):
        mock_ledger_rows = [
            {"account": ACCOUNT, "env": "paper", "orderRef": "ref-ambig",
             "orderId": 301, "local_state": "submitted", "unresolved_risk": True,
             "source": "ledger", "ts": "2026-11-07T10:00:00", "preset": "ledger"},
            {"account": ACCOUNT, "env": "纸账户(Paper)", "orderRef": "ref-ambig",
             "orderId": 302, "local_state": "confirmed", "broker_status": "Filled",
             "unresolved_risk": False, "source": "ledger", "ts": "2026-11-07T10:05:00",
             "preset": "ledger"},
        ]
        jsonl_rows = [
            {"account": ACCOUNT, "env": "paper", "orderRef": "ref-ambig",
             "orderId": 301, "preset": "cash-put", "ts": "2026-11-07T10:00:00"},
        ]
        with patch.object(cli, "_ledger_audit_rows", return_value=(mock_ledger_rows, None)):
            rc, out = self._run(jsonl_rows)
        self.assertEqual(rc, 0)
        self.assertNotIn(ACCOUNT, out)
        self.assertNotIn("ref-ambig", out)
        rows = self._parse_table_rows(out)
        self.assertEqual(len(rows), 3)
        log_rows = [r for r in rows if r[9] == "日志"]
        ledger_rows = [r for r in rows if r[9] == "账本"]
        self.assertEqual(len(log_rows), 1)
        self.assertEqual(len(ledger_rows), 2)
        self.assertEqual(log_rows[0][8], "unknown")

    def test_scope_collision_and_env_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _, int1 = self._setup_ledger_order(data_dir, order_ref="ref-norm", order_id=401,
                                               broker_status="Filled", state_val="confirmed")
            jsonl_rows = [
                {"account": ACCOUNT, "env": "not-paper", "orderRef": "ref-norm",
                 "orderId": 401, "preset": "cash-put", "ts": "2026-11-07T09:00:00"},
                {"account": ACCOUNT, "env": "paperlive", "orderRef": "ref-norm",
                 "orderId": 401, "preset": "cash-put", "ts": "2026-11-07T09:05:00"},
                {"account": "OTHER_ACC", "env": "paper", "orderRef": "ref-norm",
                 "orderId": 401, "preset": "cash-put", "ts": "2026-11-07T09:10:00"},
                {"account": ACCOUNT, "env": "纸账户(Paper)", "orderRef": "ref-norm",
                 "orderId": 401, "preset": "cash-put", "ts": "2026-11-07T09:15:00"},
            ]
            (data_dir / "orders.jsonl").write_text(
                "\n".join(json.dumps(r) for r in jsonl_rows) + "\n", encoding="utf-8")
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT])
            buf = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf):
                self.assertEqual(args.func(dict(cli.DEFAULT_CFG), args), 0)
            out = buf.getvalue()
            self.assertNotIn(ACCOUNT, out)
            self.assertNotIn("OTHER_ACC", out)
            self.assertNotIn("ref-norm", out)
            self.assertNotIn(int1, out)
            rows = self._parse_table_rows(out)
            enriched = [r for r in rows if r[9] == "日志+账本"]
            log_only = [r for r in rows if r[9] == "日志"]
            self.assertEqual(len(enriched), 1)
            self.assertEqual(enriched[0][5], "confirmed")
            self.assertEqual(len(log_only), 2)
            for r in log_only:
                self.assertEqual(r[8], "unknown")

    def test_contradictory_ids_and_no_orderid_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _, int1 = self._setup_ledger_order(
                data_dir, order_ref="ref-contra", order_id=501, perm_id=601,
                broker_status="Filled", state_val="confirmed")
            jsonl_rows = [
                {"account": ACCOUNT, "env": "paper", "orderRef": "ref-contra",
                 "orderId": 999, "permId": 601, "preset": "cash-put", "ts": "2026-11-07T10:00:00"},
                {"account": ACCOUNT, "env": "paper", "orderRef": "ref-contra",
                 "orderId": 501, "permId": 999, "preset": "cash-put", "ts": "2026-11-07T10:05:00"},
                {"account": ACCOUNT, "env": "paper", "orderRef": "ref-contra",
                 "orderId": 501, "intent": "int-wrong", "preset": "cash-put", "ts": "2026-11-07T10:10:00"},
                {"account": ACCOUNT, "env": "paper",
                 "orderId": 501, "preset": "cash-put", "ts": "2026-11-07T10:15:00"},
            ]
            (data_dir / "orders.jsonl").write_text(
                "\n".join(json.dumps(r) for r in jsonl_rows) + "\n", encoding="utf-8")
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT])
            buf = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf):
                self.assertEqual(args.func(dict(cli.DEFAULT_CFG), args), 0)
            out = buf.getvalue()
            self.assertNotIn(ACCOUNT, out)
            self.assertNotIn("ref-contra", out)
            self.assertNotIn("int-wrong", out)
            self.assertNotIn(int1, out)
            rows = self._parse_table_rows(out)
            enriched = [r for r in rows if r[9] == "日志+账本"]
            self.assertEqual(len(enriched), 0)
            ledger_rows = [r for r in rows if r[9] == "账本"]
            self.assertEqual(len(ledger_rows), 1)

    def test_duplicate_jsonl_copies_enriched(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _, int1 = self._setup_ledger_order(data_dir, order_ref="ref-dup-copy", order_id=601,
                                               broker_status="Filled", state_val="confirmed")
            jsonl_rows = [
                {"account": ACCOUNT, "env": "paper", "orderRef": "ref-dup-copy",
                 "orderId": 601, "preset": "cash-put", "ts": "2026-11-07T10:00:00"},
                {"account": ACCOUNT, "env": "paper", "orderRef": "ref-dup-copy",
                 "orderId": 601, "preset": "cash-put", "ts": "2026-11-07T10:00:00"},
            ]
            (data_dir / "orders.jsonl").write_text(
                "\n".join(json.dumps(r) for r in jsonl_rows) + "\n", encoding="utf-8")
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT])
            buf = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf):
                self.assertEqual(args.func(dict(cli.DEFAULT_CFG), args), 0)
            out = buf.getvalue()
            self.assertNotIn(ACCOUNT, out)
            self.assertNotIn("ref-dup-copy", out)
            self.assertNotIn(int1, out)
            rows = self._parse_table_rows(out)
            self.assertEqual(len(rows), 2)
            for r in rows:
                self.assertEqual(r[5], "confirmed")
                self.assertEqual(r[6], "Filled")
                self.assertEqual(r[8], "False")
                self.assertEqual(r[9], "日志+账本")

    def test_regression_latest_offset_date_and_invalid_date(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _, intent_id = self._setup_ledger_order(
                data_dir, order_ref="ref-time", order_id=701, state_val="submitted")
            ledger = state.SubmissionLedger(data_dir / "state.sqlite3", TZ)
            t1 = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
            t2 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone(timedelta(hours=-5)))
            ledger.reconcile(account=ACCOUNT, environment=ENV, order_ref="ref-time",
                             order_id=701, status="reconciled", broker_status="PreSubmitted",
                             now=t1)
            ledger.reconcile(account=ACCOUNT, environment=ENV, order_ref="ref-time",
                             order_id=701, status="reconciled", broker_status="Filled",
                             now=t2)
            with sqlite3.connect(data_dir / "state.sqlite3") as conn:
                conn.execute(
                    "UPDATE reconciliation SET observed_at = ? WHERE broker_status = ?",
                    ("2026-11-07T09:00:00-05:00", "Filled"),
                )
                conn.execute(
                    "INSERT INTO reconciliation(observation_id,intent_id,order_ref,perm_id,"
                    "order_id,status,broker_status,note,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("obs-3", intent_id, "ref-time", None, "701", "reconciled",
                     "Cancelled", "", "invalid-date-string")
                )
                conn.execute(
                    "INSERT INTO reconciliation(observation_id,intent_id,order_ref,perm_id,"
                    "order_id,status,broker_status,note,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("obs-4", intent_id, "ref-time", None, "701", "reconciled",
                     "Inactive", "", "")
                )
                conn.execute(
                    "INSERT INTO reconciliation(observation_id,intent_id,order_ref,perm_id,"
                    "order_id,status,broker_status,note,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("obs-5", intent_id, "ref-time", None, "701", "reconciled",
                     "PreSubmitted", "", "2026-11-07T15:00:00")
                )
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT])
            buf = io.StringIO()
            with patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf):
                self.assertEqual(args.func(dict(cli.DEFAULT_CFG), args), 0)
            out = buf.getvalue()
            rows = self._parse_table_rows(out)
            by_oid = {r[2]: r for r in rows}
            self.assertIn("701", by_oid)
            r701 = by_oid["701"]
            self.assertEqual(r701[6], "Filled")
            self.assertEqual(r701[7], "2026-11-07T09:00:00-05:00"[:19])

    def test_exact_identifiers_with_padding_not_falsely_enriched(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _, int1 = self._setup_ledger_order(
                data_dir, order_ref="ref-pad", order_id=801, perm_id=901,
                broker_status="Filled", state_val="confirmed")
            jsonl_rows = [
                {"account": ACCOUNT, "env": "paper", "orderRef": "ref-pad ",
                 "orderId": 801, "permId": 901, "preset": "cash-put", "ts": "2026-11-07T10:00:00"},
                {"account": ACCOUNT + " ", "env": "paper", "orderRef": "ref-pad",
                 "orderId": 801, "permId": 901, "preset": "cash-put", "ts": "2026-11-07T10:05:00"},
                {"account": ACCOUNT, "env": "paper", "orderRef": "ref-pad",
                 "orderId": 801, "permId": 901, "intent": int1 + " ", "preset": "cash-put", "ts": "2026-11-07T10:10:00"},
            ]
            (data_dir / "orders.jsonl").write_text(
                "\n".join(json.dumps(r) for r in jsonl_rows) + "\n", encoding="utf-8")
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT])
            buf = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf):
                self.assertEqual(args.func(dict(cli.DEFAULT_CFG), args), 0)
            out = buf.getvalue()
            rows = self._parse_table_rows(out)
            enriched = [r for r in rows if r[9] == "日志+账本"]
            self.assertEqual(len(enriched), 0)

    def test_conflicting_reconciliation_ids_rejected(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _, intent_id = self._setup_ledger_order(
                data_dir, order_ref="ref-recon-ids", order_id=701, perm_id=801,
                state_val="submitted")
            with sqlite3.connect(data_dir / "state.sqlite3") as conn:
                conn.execute(
                    "INSERT INTO reconciliation(observation_id,intent_id,order_ref,perm_id,"
                    "order_id,status,broker_status,note,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("obs-bad-oid", intent_id, "ref-recon-ids", "801", "999", "reconciled",
                     "Filled", "", "2026-11-07T12:00:00+00:00")
                )
                conn.execute(
                    "INSERT INTO reconciliation(observation_id,intent_id,order_ref,perm_id,"
                    "order_id,status,broker_status,note,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("obs-bad-pid", intent_id, "ref-recon-ids", "999", "701", "reconciled",
                     "Cancelled", "", "2026-11-07T13:00:00+00:00")
                )
                conn.execute(
                    "INSERT INTO reconciliation(observation_id,intent_id,order_ref,perm_id,"
                    "order_id,status,broker_status,note,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("obs-bad-int", "other-intent", "ref-recon-ids", "801", "701", "reconciled",
                     "Inactive", "", "2026-11-07T14:00:00+00:00")
                )
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT])
            buf = io.StringIO()
            with patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf):
                self.assertEqual(args.func(dict(cli.DEFAULT_CFG), args), 0)
            out = buf.getvalue()
            rows = self._parse_table_rows(out)
            by_oid = {r[2]: r for r in rows}
            self.assertIn("701", by_oid)
            r701 = by_oid["701"]
            self.assertEqual(r701[6], "-")
            self.assertEqual(r701[7], "-")

    def test_all_invalid_observations_yield_no_claimed_broker_observation(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _, intent_id = self._setup_ledger_order(
                data_dir, order_ref="ref-all-inv", order_id=702, state_val="submitted")
            with sqlite3.connect(data_dir / "state.sqlite3") as conn:
                conn.execute(
                    "INSERT INTO reconciliation(observation_id,intent_id,order_ref,perm_id,"
                    "order_id,status,broker_status,note,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("obs-inv-1", intent_id, "ref-all-inv", None, "702", "reconciled",
                     "Filled", "", "garbage-date")
                )
                conn.execute(
                    "INSERT INTO reconciliation(observation_id,intent_id,order_ref,perm_id,"
                    "order_id,status,broker_status,note,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("obs-inv-2", intent_id, "ref-all-inv", None, "702", "reconciled",
                     "PreSubmitted", "", "")
                )
                conn.execute(
                    "INSERT INTO reconciliation(observation_id,intent_id,order_ref,perm_id,"
                    "order_id,status,broker_status,note,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("obs-inv-3", intent_id, "ref-all-inv", None, "702", "reconciled",
                     "Cancelled", "", "2026-11-07 12:00:00")
                )
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT])
            buf = io.StringIO()
            with patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf):
                self.assertEqual(args.func(dict(cli.DEFAULT_CFG), args), 0)
            out = buf.getvalue()
            rows = self._parse_table_rows(out)
            by_oid = {r[2]: r for r in rows}
            self.assertIn("702", by_oid)
            r702 = by_oid["702"]
            self.assertEqual(r702[5], "submitted")
            self.assertEqual(r702[6], "-")
            self.assertEqual(r702[7], "-")

    def test_regression_multiple_ledger_only_accounts_reject_and_explicit_or_single_select(self):
        from thetamill.report import mask_account
        acc1 = "DU1000001"
        acc2 = "DU2000002"
        # Case A: Multiple ledger-only accounts reject BEFORE any output
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._setup_ledger_order(data_dir, order_ref="ref-acc1", order_id=101, account=acc1)
            self._setup_ledger_order(data_dir, order_ref="ref-acc2", order_id=202, account=acc2)
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(["audit"])
            buf = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf):
                with self.assertRaises(SystemExit) as ctx:
                    args.func(dict(cli.DEFAULT_CFG), args)
            self.assertIn("审计含多个账户", str(ctx.exception))
            self.assertEqual(buf.getvalue(), "")

            # Case B: Explicit account exact isolates
            args_sel = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(["audit", "--account", acc1])
            buf_sel = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf_sel):
                self.assertEqual(args_sel.func(dict(cli.DEFAULT_CFG), args_sel), 0)
            out_sel = buf_sel.getvalue()
            rows_sel = self._parse_table_rows(out_sel)
            oids_sel = [r[2] for r in rows_sel]
            self.assertEqual(oids_sel, ["101"])
            self.assertNotIn("202", oids_sel)
            self.assertNotIn(acc1, out_sel)
            self.assertIn(mask_account(acc1), out_sel)

        # Case C: Single ledger account with sent legs; scope with no sent legs does not block
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._setup_ledger_order(data_dir, order_ref="ref-single", order_id=301, account=acc1)
            # Create another scope with no sent legs
            ledger = state.SubmissionLedger(data_dir / "state.sqlite3", TZ)
            day_key = state.day_key_for(TZ, now=NOW)
            ledger.set_daily_limit(account="DU3000003", environment=ENV, day_key=day_key, limit_count=5)

            args_auto = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(["audit"])
            buf_auto = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf_auto):
                self.assertEqual(args_auto.func(dict(cli.DEFAULT_CFG), args_auto), 0)
            out_auto = buf_auto.getvalue()
            self.assertIn("301", out_auto)
            self.assertIn(mask_account(acc1), out_auto)

    def test_regression_local_outcomes_never_broker_status(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            jsonl_rows = [
                {"account": ACCOUNT, "env": "paper", "orderId": 401, "orderRef": "ref-conf",
                 "preset": "cash-put", "outcome": "CONFIRMED", "status": "CONFIRMED",
                 "legs": [{"order_ref": "ref-conf", "state": "submitted"}],
                 "ts": "2026-11-07T10:00:00+00:00"},
                {"account": ACCOUNT, "env": "paper", "orderId": 402, "orderRef": "ref-unk",
                 "preset": "cash-put", "outcome": "UNKNOWN", "status": "UNKNOWN",
                 "ts": "2026-11-07T10:05:00+00:00"},
                {"account": ACCOUNT, "env": "paper", "orderId": 403, "orderRef": "ref-blk",
                 "preset": "cash-put", "outcome": "BLOCKED", "status": "BLOCKED",
                 "ts": "2026-11-07T10:10:00+00:00"},
            ]
            (data_dir / "orders.jsonl").write_text(
                "\n".join(json.dumps(r) for r in jsonl_rows) + "\n", encoding="utf-8")
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT])
            buf = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf):
                self.assertEqual(args.func(dict(cli.DEFAULT_CFG), args), 0)
            out = buf.getvalue()
            rows = self._parse_table_rows(out)
            by_oid = {r[2]: r for r in rows}

            self.assertIn("401", by_oid)
            r401 = by_oid["401"]
            self.assertEqual(r401[5], "submitted")
            self.assertEqual(r401[6], "-")
            self.assertEqual(r401[7], "-")

            self.assertIn("402", by_oid)
            r402 = by_oid["402"]
            self.assertEqual(r402[5], "UNKNOWN")
            self.assertEqual(r402[6], "-")
            self.assertEqual(r402[7], "-")

            self.assertIn("403", by_oid)
            r403 = by_oid["403"]
            self.assertEqual(r403[5], "BLOCKED")
            self.assertEqual(r403[6], "-")
            self.assertEqual(r403[7], "-")

    def test_regression_empty_id_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._setup_ledger_order(
                data_dir, order_ref="ref-bf", order_id=555, perm_id=666,
                broker_status="Filled", state_val="confirmed")
            jsonl_rows = [
                {"account": ACCOUNT, "env": "paper", "orderRef": "ref-bf",
                 "orderId": "", "permId": "", "preset": "cash-put", "ts": "2026-11-07T10:00:00"},
            ]
            (data_dir / "orders.jsonl").write_text(
                "\n".join(json.dumps(r) for r in jsonl_rows) + "\n", encoding="utf-8")
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT])
            buf = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf):
                self.assertEqual(args.func(dict(cli.DEFAULT_CFG), args), 0)
            out = buf.getvalue()
            rows = self._parse_table_rows(out)
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertEqual(r[2], "555")
            self.assertEqual(r[5], "confirmed")
            self.assertEqual(r[6], "Filled")
            self.assertEqual(r[9], "日志+账本")

    def test_regression_offset_ordering_limit_and_today_rollover(self):
        # Part A: Offset ordering with --limit
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            jsonl_rows = [
                {"account": ACCOUNT, "env": "paper", "orderId": 601, "ts": "2026-11-07T12:00:00+00:00"},
                {"account": ACCOUNT, "env": "paper", "orderId": 602, "ts": "2026-11-07T09:00:00-05:00"},
                {"account": ACCOUNT, "env": "paper", "orderId": 603, "ts": "2026-11-07T10:00:00-05:00"},
                {"account": ACCOUNT, "env": "paper", "orderId": 604, "ts": "invalid-timestamp"},
            ]
            (data_dir / "orders.jsonl").write_text(
                "\n".join(json.dumps(r) for r in jsonl_rows) + "\n", encoding="utf-8")
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT, "--limit", "2"])
            buf = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf):
                self.assertEqual(args.func(dict(cli.DEFAULT_CFG), args), 0)
            out = buf.getvalue()
            rows = self._parse_table_rows(out)
            oids = [r[2] for r in rows]
            self.assertEqual(oids, ["602", "603"])

        # Part B: Today UTC/local rollover deterministic clock
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            jsonl_rows = [
                {"account": ACCOUNT, "env": "paper", "orderId": 701, "ts": "2026-11-08T01:30:00+00:00"},
                {"account": ACCOUNT, "env": "paper", "orderId": 702, "ts": "2026-11-07T03:00:00+00:00"},
                {"account": ACCOUNT, "env": "paper", "orderId": 703, "ts": "2026-11-07T15:00:00"},
                {"account": ACCOUNT, "env": "paper", "orderId": 704, "ts": "invalid-timestamp"},
            ]
            (data_dir / "orders.jsonl").write_text(
                "\n".join(json.dumps(r) for r in jsonl_rows) + "\n", encoding="utf-8")
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT, "--today"])
            args.now = datetime(2026, 11, 7, 21, 0, tzinfo=timezone(timedelta(hours=-5)))
            buf = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf):
                self.assertEqual(args.func(dict(cli.DEFAULT_CFG), args), 0)
            out = buf.getvalue()
            rows = self._parse_table_rows(out)
            oids = {r[2] for r in rows}
            self.assertIn("701", oids)
            self.assertIn("703", oids)
            self.assertNotIn("702", oids)
            self.assertNotIn("704", oids)

    def test_regression_sqlite_errors_sanitized_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            jsonl_rows = [
                {"account": ACCOUNT, "env": "paper", "orderId": 801,
                 "preset": "cash-put", "ts": "2026-11-07T10:00:00"},
            ]
            (data_dir / "orders.jsonl").write_text(
                "\n".join(json.dumps(r) for r in jsonl_rows) + "\n", encoding="utf-8")
            # Write a corrupted sqlite file
            (data_dir / "state.sqlite3").write_bytes(b"corrupted-sqlite-database-not-valid-header")
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT])
            buf = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf):
                rc = args.func(dict(cli.DEFAULT_CFG), args)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("⚠️ 提交账本无法只读打开：", out)
            # Sanitization: No absolute file paths or account or sql
            warning_line = [line for line in out.splitlines() if line.startswith("⚠️")][0]
            self.assertNotIn(str(data_dir), warning_line)
            self.assertNotIn("state.sqlite3", warning_line)
            self.assertNotIn(ACCOUNT, warning_line)
            self.assertNotIn("SELECT", warning_line)
            # Available JSONL record is preserved
            rows = self._parse_table_rows(out)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][2], "801")
            self.assertEqual(rows[0][9], "日志")

    def test_regression_padded_account_never_displays_unpadded_data(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            self._setup_ledger_order(data_dir, order_ref="ref-unpadded-led", order_id=101, account=ACCOUNT)
            jsonl_rows = [
                {"account": ACCOUNT, "env": "paper", "orderId": 102, "preset": "cash-put",
                 "status": "Submitted", "ts": "2026-11-07T10:00:00"},
            ]
            (data_dir / "orders.jsonl").write_text(
                "\n".join(json.dumps(r) for r in jsonl_rows) + "\n", encoding="utf-8")

            # 1. Padded account with trailing space must never display unpadded account data
            args_padded = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", f"{ACCOUNT} "])
            buf_padded = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf_padded):
                self.assertEqual(args_padded.func(dict(cli.DEFAULT_CFG), args_padded), 0)
            out_padded = buf_padded.getvalue()
            rows_padded = self._parse_table_rows(out_padded)
            oids_padded = [r[2] for r in rows_padded]
            self.assertEqual(oids_padded, [])
            self.assertNotIn("101", oids_padded)
            self.assertNotIn("102", oids_padded)
            self.assertIn("暂无审计记录", out_padded)

            # 2. Padded account with leading space must also never display unpadded account data
            args_lead = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", f" {ACCOUNT}"])
            buf_lead = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf_lead):
                self.assertEqual(args_lead.func(dict(cli.DEFAULT_CFG), args_lead), 0)
            out_lead = buf_lead.getvalue()
            rows_lead = self._parse_table_rows(out_lead)
            self.assertEqual(rows_lead, [])
            self.assertIn("暂无审计记录", out_lead)

            # 3. Blank account must fail closed rather than silently auto-selecting the account
            for bad_acc in ("", "   "):
                args_blank = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                    ["audit", "--account", bad_acc])
                with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                     patch.object(cli, "_data_dir", return_value=data_dir):
                    with self.assertRaises(SystemExit):
                        args_blank.func(dict(cli.DEFAULT_CFG), args_blank)

            # 4. Exact unpadded account displays both JSONL and ledger data
            args_exact = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT])
            buf_exact = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf_exact):
                self.assertEqual(args_exact.func(dict(cli.DEFAULT_CFG), args_exact), 0)
            out_exact = buf_exact.getvalue()
            rows_exact = self._parse_table_rows(out_exact)
            oids_exact = {r[2] for r in rows_exact}
            self.assertIn("101", oids_exact)
            self.assertIn("102", oids_exact)

    def test_regression_parse_audit_ts_extreme_boundaries_and_no_fabricated_utc(self):
        # Extreme ISO offsets that overflow datetime boundaries return (None, None)
        utc_dt, local_d = cli._parse_audit_ts("9999-12-31T23:59:59-12:00")
        self.assertIsNone(utc_dt)
        self.assertIsNone(local_d)

        utc_dt, local_d = cli._parse_audit_ts("0001-01-01T00:00:00+12:00")
        self.assertIsNone(utc_dt)
        self.assertIsNone(local_d)

        # Naive extreme dates that fail conversion return (None, None) and never fabricate UTC
        utc_dt, local_d = cli._parse_audit_ts(
            datetime(9999, 12, 31, 23, 59, 59),
            local_tz=timezone(timedelta(hours=-5)))
        self.assertIsNone(utc_dt)
        self.assertIsNone(local_d)

        utc_dt, local_d = cli._parse_audit_ts(
            datetime(1, 1, 1, 0, 0, 0),
            local_tz=timezone(timedelta(hours=5)))
        self.assertIsNone(utc_dt)
        self.assertIsNone(local_d)

        # Audit view does not crash when encountering extreme offset or naive overflow
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            jsonl_rows = [
                {"account": ACCOUNT, "env": "paper", "orderId": 901,
                 "status": "Submitted", "ts": "9999-12-31T23:59:59-12:00"},
                {"account": ACCOUNT, "env": "paper", "orderId": 902,
                 "status": "Submitted", "ts": "2026-11-07T10:00:00+00:00"},
            ]
            (data_dir / "orders.jsonl").write_text(
                "\n".join(json.dumps(r) for r in jsonl_rows) + "\n", encoding="utf-8")
            args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(
                ["audit", "--account", ACCOUNT])
            buf = io.StringIO()
            with patch.object(cli, "ORDER_LOG", data_dir / "orders.jsonl"), \
                 patch.object(cli, "_data_dir", return_value=data_dir), \
                 redirect_stdout(buf):
                rc = args.func(dict(cli.DEFAULT_CFG), args)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            rows = self._parse_table_rows(out)
            oids = [r[2] for r in rows]
            self.assertEqual(oids, ["901", "902"])

# --------------------------------------------------------------------------- #
# Legacy import: a mock row must never burn a real review code
# --------------------------------------------------------------------------- #

class LegacyImportTests(unittest.TestCase):

    def test_mock_rows_are_not_imported_as_consumed_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "orders.jsonl").write_text("\n".join([
                json.dumps({"code": "REALCODE", "orderId": 133,
                            "status": "Filled", "account": ACCOUNT}),
                json.dumps({"code": "12345678",
                            "orderId": "<MagicMock name='mock.placeOrder().order.orderId'>",
                            "status": "<MagicMock name='mock.status'>"}),
            ]) + "\n", encoding="utf-8")
            with patch.object(cli, "_data_dir", return_value=data_dir):
                ledger = sc._ledger(CONFIG, ACCOUNT, ENV)
            codes = ledger.legacy_codes(account=ACCOUNT, environment=ENV)
            self.assertIn("REALCODE", codes)
            self.assertNotIn("12345678", codes)


# --------------------------------------------------------------------------- #
# The real submission path now writes the trail
# --------------------------------------------------------------------------- #

def stock_contract(con_id=1001):
    return SimpleNamespace(conId=con_id, secType="STK", symbol="AAPL",
                           currency="USD", exchange="SMART")


def stock_order():
    return SimpleNamespace(action="BUY", totalQuantity=1, orderType="LMT",
                           lmtPrice=180, auxPrice=None, tif="DAY",
                           outsideRth=False, orderRef="", account="",
                           orderId=None, permId=None)


def stock_detail(contract, under=1001):
    return SimpleNamespace(contract=contract, underConId=under,
                           underSecType="STK")


class MockStatus:
    def __init__(self, status, filled=0, remaining=0):
        self.status = status
        self.filled = filled
        self.remaining = remaining


class MockTrade:
    def __init__(self, contract, order, status):
        self.contract = contract
        self.order = order
        self.orderStatus = status


class MockIB:
    def __init__(self, status="Submitted"):
        self.place_calls = []
        self._status = status
        self._trades = []

    def managedAccounts(self):
        return [ACCOUNT]

    def reqContractDetails(self, contract):
        return [stock_detail(contract)]

    def placeOrder(self, contract, order):
        self.place_calls.append((contract, order))
        order.orderId = 500 + len(self.place_calls)
        order.permId = 900 + len(self.place_calls)
        trade = MockTrade(contract, order,
                          MockStatus(self._status, filled=0, remaining=1))
        self._trades.append(trade)
        return trade

    def openTrades(self):
        return list(self._trades)

    def trades(self):
        return list(self._trades)

    def reqAllOpenOrders(self):
        return list(self._trades)

    def sleep(self, _):
        pass


def make_snapshot():
    from thetamill.broker_state import Snapshot
    return Snapshot(
        account=ACCOUNT, environment=ENV,
        captured_at=NOW - timedelta(seconds=2), base_currency="USD",
        cash={"USD": Decimal("100000")}, net_liquidation=Decimal("100000"),
        positions=(), orders=(), fx={"USD": Decimal(1)}, issues=(),
        price_quotes={}, fx_quotes={})


class SubmissionAuditTests(unittest.TestCase):
    """A mock-broker confirm must leave exactly one clean audit record."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name)
        self.patches = []

        def patch_attr(obj, name, value):
            old = getattr(obj, name)
            setattr(obj, name, value)
            self.patches.append((obj, name, old))

        patch_attr(cli, "_data_dir", lambda: self.data_dir)
        patch_attr(cli, "_selected_account", lambda ib, cfg, args: ACCOUNT)
        patch_attr(cli, "_is_live_env", lambda cfg, args, port: False)
        patch_attr(cli, "resolve_port", lambda cfg, args: (7497, "test"))
        patch_attr(cli, "_risk_snapshot",
                   lambda ib, cfg, args, env, *, required_currencies=(): make_snapshot())
        self.addCleanup(self._undo)

    def _undo(self):
        for obj, name, old in reversed(self.patches):
            setattr(obj, name, old)

    def _unfreeze(self):
        ledger = state.SubmissionLedger(self.data_dir / "state.sqlite3", TZ)
        ledger.prepare_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key=state.day_key_for(TZ, now=NOW), limit_count=5,
            verified_prior_submissions=0)

    def _args(self, *, intent="", code=""):
        return SimpleNamespace(intent=intent, code=code, account=ACCOUNT,
                               paper=True, port=7497, strategy="order:stk:buy")

    def _run(self, ib, args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = sc.execute_prepared(
                ib, CONFIG, args,
                pairs=[(stock_contract(), stock_order())], now=NOW,
                clock=lambda: NOW)
        return result, buf.getvalue()

    def _audit_rows(self):
        path = self.data_dir / "orders.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_preflight_writes_no_audit_record(self):
        """Nothing was sent, so nothing is audited."""
        self._unfreeze()
        result, _ = self._run(MockIB(), self._args())
        self.assertEqual(result.outcome, "PREFLIGHT")
        self.assertEqual(self._audit_rows(), [])

    def test_confirmed_submission_writes_one_clean_record(self):
        self._unfreeze()
        ib = MockIB(status="Submitted")
        first, _ = self._run(ib, self._args())
        result, out = self._run(
            ib, self._args(intent=first.intent_id, code=first.approval_code))
        self.assertEqual(result.outcome, "CONFIRMED", out)
        rows = self._audit_rows()
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["outcome"], "CONFIRMED")
        self.assertEqual(row["orderId"], 501)
        self.assertEqual(row["account"], ACCOUNT)
        self.assertEqual(row["env"], "paper")
        self.assertEqual(row["intent"], first.intent_id)
        self.assertEqual(row["legs"][0]["state"], "submitted")
        self.assertNotIn("MagicMock", json.dumps(row))

    def test_blocked_confirm_writes_no_audit_record(self):
        """A migration freeze never sends, so it must not look like a send."""
        result, out = self._run(MockIB(), self._args(intent="int-x", code="CODE"))
        self.assertNotEqual(result.outcome, "CONFIRMED")
        self.assertEqual(self._audit_rows(), [])

    def test_leaked_mock_field_never_reaches_the_file(self):
        """A mock broker leaking a mock status is dropped, not stringified."""
        self._unfreeze()
        ib = MockIB(status="Submitted")
        ib.openTrades = lambda: [MockTrade(stock_contract(), stock_order(),
                                           MockStatus(MockRepr()))]
        first, _ = self._run(MockIB(), self._args())
        result, _ = self._run(
            ib, self._args(intent=first.intent_id, code=first.approval_code))
        text = (self.data_dir / "orders.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("MagicMock", text)
        self.assertEqual(result.outcome, "CONFIRMED")


if __name__ == "__main__":
    unittest.main()
