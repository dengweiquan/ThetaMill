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
