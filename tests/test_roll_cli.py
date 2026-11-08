"""Offline tests for the read-only roll CLI adapter (recommendation ONLY).

No real broker, no network, no git, no production/shared writes, no agents.
All broker interaction is a MagicMock; the local intent ledger lives in a
tempdir; files (evidence / universe / output) are tempdir-only.  Candidate
contracts are qualified through mocked reqContractDetails and quotes come from
timestamped tickers whose OWN ``time`` is used (never re-stamped with now).
"""
import argparse
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from thetamill.broker_state import Instrument, Position, Snapshot
from thetamill import roll, roll_cli
from thetamill.roll_cli import DELIVERABLES_SCHEMA, DeliverableEvidenceError
from thetamill.roll import SCHEMA as ROLL_SCHEMA

NOW = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
OLD_EXPIRY = "20261218"      # 41 DTE from NOW
NEW_EXPIRY = "20270115"      # 69 DTE from NOW
QUOTE_TIME = NOW - timedelta(seconds=2)


def old_inst(mult=D(100), **kw):
    p = dict(con_id=20, symbol="AAPL", currency="USD", sec_type="OPT",
             exchange="SMART", underlying_id=10, multiplier=mult,
             right="P", strike=D(40), expiry=OLD_EXPIRY, trading_class="AAPL")
    p.update(kw)
    return Instrument(**p)


def candidate(**kw):
    p = dict(con_id=30, symbol="AAPL", currency="USD", sec_type="OPT",
             exchange="SMART", underlying_id=10, multiplier=D(100),
             right="P", strike=D(40), expiry=NEW_EXPIRY, trading_class="AAPL")
    p.update(kw)
    return Instrument(**p)


def make_snapshot(*, instrument=None, short=D(-5), orders=(), **kw):
    p = dict(account="DU1234567", environment="paper", captured_at=NOW,
             base_currency="USD", cash={"USD": D(16000)},
             net_liquidation=D(20000),
             positions=(Position(instrument or old_inst(), short, D(2)),),
             orders=tuple(orders), fx={"USD": D(1)})
    p.update(kw)
    return Snapshot(**p)


def funded_snapshot(**kw):
    """Cash covers the held short put AND one new short put (fail-open risk gate)."""
    return make_snapshot(cash={"USD": D(60000)}, net_liquidation=D(60000), **kw)


def contract(con_id, inst):
    """A broker-like qualified Contract carrying every field _instrument reads."""
    c = SimpleNamespace(
        conId=con_id, secType=inst.sec_type, symbol=inst.symbol,
        currency=inst.currency, exchange=inst.exchange,
        multiplier=str(inst.multiplier), strike=float(inst.strike),
        right=inst.right, lastTradeDateOrContractMonth=inst.expiry,
        tradingClass=inst.trading_class, comboLegs=[], comboLegsDescrip="",
    )
    return c


def detail(con_id, inst, *, under_con_id=None, under_sec="STK"):
    c = contract(con_id, inst)
    return SimpleNamespace(contract=c,
                           underConId=under_con_id if under_con_id is not None
                           else inst.underlying_id,
                           underSecType=under_sec)


def ticker(con_id, bid=D(3), ask=D("3.5"), *, time=QUOTE_TIME, market_data_type=1):
    return SimpleNamespace(contract=SimpleNamespace(conId=con_id),
                           time=time, bid=bid, ask=ask,
                           marketDataType=market_data_type)


def evidence_record(inst, *, stock_only=True, source="IB contract details + human review",
                    verified_at=None, expires_at=None, **overrides):
    verified_at = verified_at or (NOW - timedelta(hours=1)).isoformat()
    expires_at = expires_at or (NOW + timedelta(hours=23)).isoformat()
    record = {"instrument": roll.instrument_data(inst),
              "stock_only": stock_only, "source": source,
              "verified_at": verified_at, "expires_at": expires_at}
    record["instrument"].update(overrides)
    return record


def write_evidence(path, records, schema=DELIVERABLES_SCHEMA):
    doc = {"schema": schema, "records": records}
    Path(path).write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


def make_args(**overrides):
    p = dict(con_id=20, qty=1, candidate_con_id=[30], dte_min=21, dte_max=90,
             cost_floor=None, max_entry=None, universe=None,
             delivery_evidence=None, format="json", output=None,
             account=None, paper=True, port=4002, delayed=False)
    p.update(overrides)
    return argparse.Namespace(**p)


CFG = {"default_currency": "USD", "host": "127.0.0.1", "live_port": 4001,
       "paper_port": 4002, "client_id": 1, "live_enabled": False,
       "risk": {"enabled": False}}


class FakeContract:
    """Stand-in for cli.Contract (conId-only lookup, no chain guessing)."""
    def __init__(self, *args, **kwargs):
        self.conId = kwargs.get("conId", 0)
        self.secType = kwargs.get("secType", "")
        self.symbol = kwargs.get("symbol", "")
        self.currency = kwargs.get("currency", "")
        self.exchange = kwargs.get("exchange", "")


def run_plan(tmp, args, *, snap=None, details=None, tickers=None, cfg=None,
             ledger_exists=True, now=NOW):
    """Invoke cmd_roll_plan with every I/O boundary mocked."""
    from thetamill import cli
    snap = snap or make_snapshot()
    ib = MagicMock()
    details = details if details is not None else {
        20: [detail(20, old_inst())], 30: [detail(30, candidate())]}
    ib.reqContractDetails.side_effect = lambda c: details.get(c.conId, [])
    tickers = tickers if tickers is not None else [
        ticker(20), ticker(30)]
    ib.reqTickers.return_value = tickers
    ib._diag = []
    if ledger_exists:
        # A real (empty) temp SQLite ledger: no outstanding reservations, but
        # history is known to be empty so a new open is not blocked.
        from thetamill.state import SubmissionLedger
        SubmissionLedger(Path(tmp, "state.sqlite3"), "UTC")
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
         patch.object(cli, "_is_live_env", return_value=False), \
         patch.object(cli, "connect", return_value=ib), \
         patch.object(cli, "finish"), \
         patch.object(cli, "_risk_snapshot", return_value=snap), \
         patch.object(cli, "_data_dir", return_value=Path(tmp)), \
         patch.object(cli, "Contract", FakeContract), \
         patch("sys.stdout", stdout), patch("sys.stderr", stderr):
        rc = roll_cli.cmd_roll_plan(cfg or CFG, args, now=now)
    return rc, stdout.getvalue(), stderr.getvalue(), ib


# --------------------------------------------------------------------------- #
# Public evidence loader
# --------------------------------------------------------------------------- #
class EvidenceLoaderTests(unittest.TestCase):
    def test_valid_evidence_confirms_exact_option_con_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence(Path(tmp) / "e.json",
                                  [evidence_record(old_inst()),
                                   evidence_record(candidate())])
            confirmed = roll_cli.load_delivery_evidence(
                path, [old_inst(), candidate()], NOW)
            self.assertEqual(set(confirmed), {20, 30})
            self.assertEqual(confirmed[20]["source"],
                             "IB contract details + human review")

    def test_missing_file_yields_empty_mapping(self):
        result = roll_cli.load_delivery_evidence(
            "/no/such/file.json", [old_inst()], NOW)
        self.assertEqual(dict(result), {})

    def test_per_option_conid_not_underlying(self):
        # Evidence for a DIFFERENT option sharing underlying 10 must not confirm 30.
        other = candidate(con_id=99)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence(Path(tmp) / "e.json", [evidence_record(other)])
            confirmed = roll_cli.load_delivery_evidence(path, [candidate()], NOW)
            self.assertEqual(dict(confirmed), {})

    def test_partial_coverage_confirms_only_matching_conids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence(Path(tmp) / "e.json",
                                  [evidence_record(old_inst())])
            confirmed = roll_cli.load_delivery_evidence(
                path, [old_inst(), candidate()], NOW)
            self.assertEqual(set(confirmed), {20})  # candidate 30 unconfirmed, no error

    def test_wrong_schema_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence(Path(tmp) / "e.json", [], schema="plan/v1")
            with self.assertRaises(DeliverableEvidenceError):
                roll_cli.load_delivery_evidence(path, [old_inst()], NOW)

    def test_missing_records_key_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.json"
            path.write_text(json.dumps({"schema": DELIVERABLES_SCHEMA}))
            with self.assertRaises(DeliverableEvidenceError):
                roll_cli.load_delivery_evidence(path, [old_inst()], NOW)

    def test_stock_only_false_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence(Path(tmp) / "e.json",
                                  [evidence_record(old_inst(), stock_only=False)])
            with self.assertRaises(DeliverableEvidenceError):
                roll_cli.load_delivery_evidence(path, [old_inst()], NOW)

    def test_empty_source_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence(Path(tmp) / "e.json",
                                  [evidence_record(old_inst(), source="   ")])
            with self.assertRaises(DeliverableEvidenceError):
                roll_cli.load_delivery_evidence(path, [old_inst()], NOW)

    def test_naive_timestamp_rejected(self):
        for field in ("verified_at", "expires_at"):
            rec = evidence_record(old_inst())
            rec[field] = "2026-11-07T12:00:00"  # no offset
            with tempfile.TemporaryDirectory() as tmp:
                path = write_evidence(Path(tmp) / "e.json", [rec])
                with self.assertRaises(DeliverableEvidenceError):
                    roll_cli.load_delivery_evidence(path, [old_inst()], NOW)

    def test_expired_and_future_rejected(self):
        expired = evidence_record(old_inst(),
                                  verified_at=(NOW - timedelta(days=3)).isoformat(),
                                  expires_at=(NOW - timedelta(days=2)).isoformat())
        future = evidence_record(candidate(),
                                 verified_at=(NOW + timedelta(hours=1)).isoformat(),
                                 expires_at=(NOW + timedelta(days=1)).isoformat())
        with tempfile.TemporaryDirectory() as tmp:
            for rec in (expired, future):
                path = write_evidence(Path(tmp) / "e.json", [rec])
                with self.assertRaises(DeliverableEvidenceError):
                    roll_cli.load_delivery_evidence(
                        path, [old_inst(), candidate()], NOW)

    def test_expires_before_verified_rejected(self):
        rec = evidence_record(old_inst(),
                              verified_at=(NOW + timedelta(hours=1)).isoformat())
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence(Path(tmp) / "e.json", [rec])
            with self.assertRaises(DeliverableEvidenceError):
                roll_cli.load_delivery_evidence(path, [old_inst()], NOW)

    def test_every_identity_field_compared(self):
        for field, bad in (("multiplier", "200"), ("strike", "45"),
                           ("underlying_id", 11), ("symbol", "MSFT"),
                           ("currency", "EUR"), ("sec_type", "STK"),
                           ("exchange", "CBOE"), ("right", "C"),
                           ("expiry", "20270101"), ("trading_class", "ADJ")):
            with tempfile.TemporaryDirectory() as tmp:
                path = write_evidence(Path(tmp) / "e.json",
                                      [evidence_record(old_inst(), **{field: bad})])
                with self.assertRaises(DeliverableEvidenceError):
                    roll_cli.load_delivery_evidence(path, [old_inst()], NOW)

    def test_equivalent_decimal_spelling_matches(self):
        rec = evidence_record(old_inst(), multiplier="100.0", strike="40.00")
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence(Path(tmp) / "e.json", [rec])
            self.assertEqual(set(roll_cli.load_delivery_evidence(
                path, [old_inst()], NOW)), {20})

    def test_missing_identity_field_rejected(self):
        rec = evidence_record(old_inst())
        del rec["instrument"]["trading_class"]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence(Path(tmp) / "e.json", [rec])
            with self.assertRaises(DeliverableEvidenceError):
                roll_cli.load_delivery_evidence(path, [old_inst()], NOW)

    def test_stk_needs_no_evidence_and_is_not_returned(self):
        stk = Instrument(10, "AAPL", "USD", "STK", "SMART", 10)
        self.assertEqual(dict(roll_cli.load_delivery_evidence(
            "/no/file", [stk], NOW)), {})

    def test_naive_now_rejected(self):
        with self.assertRaises(DeliverableEvidenceError):
            roll_cli.load_delivery_evidence("/no/file", [old_inst()],
                                            datetime(2026, 11, 7, 20))

    def test_malformed_json_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.json"
            path.write_text("{not json")
            with self.assertRaises(DeliverableEvidenceError):
                roll_cli.load_delivery_evidence(path, [old_inst()], NOW)

    def test_executable_fields_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.json"
            path.write_text(json.dumps({"schema": DELIVERABLES_SCHEMA,
                                        "records": [], "orders_to_place": [1]}))
            with self.assertRaises(DeliverableEvidenceError):
                roll_cli.load_delivery_evidence(path, [old_inst()], NOW)


# --------------------------------------------------------------------------- #
# Pure quote helpers
# --------------------------------------------------------------------------- #
class QuoteHelperTests(unittest.TestCase):
    def test_ticker_time_is_used_not_now(self):
        notes = []
        quotes = roll_cli._build_quotes({20: ticker(20)}, [old_inst()], NOW, notes)
        self.assertEqual(quotes[20].as_of, QUOTE_TIME)
        self.assertNotEqual(quotes[20].as_of, NOW)

    def test_missing_time_yields_none_and_warning(self):
        notes = []
        quotes = roll_cli._build_quotes(
            {20: ticker(20, time=None)}, [old_inst()], NOW, notes)
        self.assertIsNone(quotes[20])
        self.assertTrue(any("not re-stamped" in n for n in notes))

    def test_naive_time_yields_none(self):
        notes = []
        quotes = roll_cli._build_quotes(
            {20: ticker(20, time=datetime(2026, 11, 7, 19))},
            [old_inst()], NOW, notes)
        self.assertIsNone(quotes[20])

    def test_future_time_yields_none_not_crash(self):
        notes = []
        quotes = roll_cli._build_quotes(
            {20: ticker(20, time=NOW + timedelta(seconds=30))},
            [old_inst()], NOW, notes)
        self.assertIsNone(quotes[20])
        self.assertTrue(any("future" in n for n in notes))

    def test_crossed_quote_unavailable_not_crash(self):
        notes = []
        quotes = roll_cli._build_quotes(
            {20: ticker(20, bid=D(5), ask=D(4))}, [old_inst()], NOW, notes)
        self.assertIsNone(quotes[20])
        self.assertTrue(any("crossed" in n for n in notes))

    def test_nan_bid_is_missing(self):
        notes = []
        quotes = roll_cli._build_quotes(
            {20: ticker(20, bid=float("nan"), ask=D(4))},
            [old_inst()], NOW, notes)
        self.assertIsNotNone(quotes[20])
        self.assertIsNone(quotes[20].bid)

    def test_bool_bid_is_missing(self):
        notes = []
        quotes = roll_cli._build_quotes(
            {20: ticker(20, bid=True, ask=D(4))}, [old_inst()], NOW, notes)
        self.assertIsNone(quotes[20].bid)

    def test_negative_price_is_missing(self):
        notes = []
        quotes = roll_cli._build_quotes(
            {20: ticker(20, bid=D(-1), ask=D(4))}, [old_inst()], NOW, notes)
        self.assertIsNone(quotes[20].bid)

    def test_missing_ticker_yields_none(self):
        notes = []
        quotes = roll_cli._build_quotes({}, [old_inst()], NOW, notes)
        self.assertIsNone(quotes[20])

    def test_delayed_market_data_is_not_usable_current_quote(self):
        for data_type in (2, 3, 4, None):
            notes = []
            quotes = roll_cli._build_quotes(
                {20: ticker(20, market_data_type=data_type)},
                [old_inst()], NOW, notes)
            self.assertIsNone(quotes[20])
            self.assertTrue(any("DELAYED/FROZEN" in n for n in notes))

    def test_live_market_data_is_usable(self):
        notes = []
        quotes = roll_cli._build_quotes(
            {20: ticker(20, market_data_type=1)}, [old_inst()], NOW, notes)
        self.assertIsNotNone(quotes[20])
        self.assertEqual(quotes[20].source, "reqTickers")


# --------------------------------------------------------------------------- #
# Full cmd_roll_plan integration (all I/O mocked)
# --------------------------------------------------------------------------- #
class RollPlanCLITests(unittest.TestCase):
    def test_readonly_and_no_trading_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, err, ib = run_plan(tmp, make_args())
            self.assertEqual(rc, 3)
            data = json.loads(out)
            self.assertEqual(data["schema"], ROLL_SCHEMA)
            self.assertFalse(data["executable"])
            self.assertTrue(data["recommendation_only"])
            ib.placeOrder.assert_not_called()
            ib.cancelOrder.assert_not_called()
            self.assertNotIn("DU1234567", out)  # masked
            self.assertIn("***", out)

    def test_no_evidence_rejects_all_candidates_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=None))
            data = json.loads(out)
            self.assertEqual(data["recommended_count"], 0)
            self.assertEqual(data["rejected_count"], 1)
            self.assertTrue(data["candidates"][0]["reasons"])
            self.assertIn("evidence", " ".join(data["issues"]).lower())

    def test_evidence_confirming_only_old_rejects_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence(Path(tmp) / "e.json",
                                  [evidence_record(old_inst())])
            rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=path))
            data = json.loads(out)
            self.assertEqual(data["recommended_count"], 0)
            reasons = " ".join(data["candidates"][0]["reasons"])
            self.assertIn("deliverable not confirmed", reasons)

    def test_full_valid_evidence_produces_recommendation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence(Path(tmp) / "e.json",
                                  [evidence_record(old_inst()),
                                   evidence_record(candidate())])
            rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=path),
                                     snap=funded_snapshot())
            data = json.loads(out)
            self.assertEqual(rc, 0)
            self.assertEqual(data["recommended_count"], 1)
            self.assertEqual(data["confirmed_deliverables"], [20, 30])
            self.assertEqual(data["candidates"][0]["status"], "recommended")
            self.assertIn("reqTickers", json.dumps(data["close_quote"]))

    def test_json_stdout_is_clean_of_connect_finish_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, err, _ = run_plan(tmp, make_args())
            json.loads(out)  # must be pure JSON, no diagnostic prefix
            self.assertNotIn("连接", out)
            self.assertNotIn("连上", out)

    def test_broker_failure_does_not_leak_raw_account(self):
        from thetamill import cli
        args = make_args()
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
             patch.object(cli, "_is_live_env", return_value=False), \
             patch.object(cli, "connect",
                          side_effect=SystemExit("实盘账号 DU9999999 不可用")), \
             patch.object(cli, "finish"), \
             patch.object(cli, "Contract", FakeContract), \
             patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            rc = roll_cli.cmd_roll_plan(CFG, args, now=NOW)
        self.assertEqual(rc, 3)
        self.assertNotIn("DU9999999", stdout.getvalue())
        self.assertNotIn("DU9999999", stderr.getvalue())
        data = json.loads(stdout.getvalue())
        self.assertEqual(data["recommended_count"], 0)
        self.assertTrue(data["issues"])

    def test_absent_ledger_fails_new_open_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence(Path(tmp) / "e.json",
                                  [evidence_record(old_inst()),
                                   evidence_record(candidate())])
            # ledger_exists=False: state.sqlite3 does NOT exist
            rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=path),
                                     ledger_exists=False)
            data = json.loads(out)
            self.assertIn("no local submission ledger",
                          " ".join(data["candidates"][0]["reasons"]))
            self.assertEqual(data["recommended_count"], 0)

    def test_malformed_evidence_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{bad")
            rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=str(path)))
            data = json.loads(out)
            self.assertEqual(data["recommended_count"], 0)
            self.assertTrue(any("evidence rejected" in x for x in data["issues"]))

    def test_unqualified_candidate_is_not_a_recommendation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence(Path(tmp) / "e.json",
                                  [evidence_record(old_inst()),
                                   evidence_record(candidate())])
            details = {20: [detail(20, old_inst())], 30: []}  # candidate not found
            rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=path),
                                     details=details)
            data = json.loads(out)
            self.assertEqual(data["recommended_count"], 0)
            self.assertIn("no contract details for conId 30", " ".join(data["issues"]))

    def test_inconsistent_underlying_detail_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence(Path(tmp) / "e.json",
                                  [evidence_record(old_inst()),
                                   evidence_record(candidate())])
            bad = detail(30, candidate(), under_sec="BAG")
            rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=path),
                                     details={20: [detail(20, old_inst())], 30: [bad]})
            data = json.loads(out)
            self.assertEqual(data["recommended_count"], 0)
            self.assertTrue(any("qualified identity" in x for x in data["issues"]))

    def test_create_only_output_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "plan.json"
            target.write_text("ORIGINAL")
            rc, out, err, _ = run_plan(tmp, make_args(output=str(target)))
            self.assertEqual(rc, 3)
            self.assertEqual(target.read_text(), "ORIGINAL")
            self.assertIn("create-only", err)

    def test_output_file_is_created_with_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "plan.json"
            rc, out, err, _ = run_plan(tmp, make_args(output=str(target)))
            self.assertTrue(target.exists())
            self.assertEqual(json.loads(target.read_text())["schema"], ROLL_SCHEMA)
            self.assertEqual(out, "")  # output goes to file, not stdout

    def test_terminal_and_markdown_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            for fmt in ("terminal", "markdown"):
                rc, out, _, _ = run_plan(tmp, make_args(format=fmt))
                self.assertIn("# ThetaMill", out)
                self.assertNotIn("DU1234567", out)

    def test_universe_whitelist_blocks_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _, _ = run_plan(
                tmp, make_args(universe=str(Path(tmp) / "missing.json")))
            data = json.loads(out)
            self.assertEqual(data["recommended_count"], 0)
            self.assertTrue(any("universe" in x for x in data["issues"]))

    def test_universe_whitelist_allows_eligible_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            upath = Path(tmp) / "universe.json"
            upath.write_text(json.dumps({
                "schema": "thetamill-universe/1", "approved_by": "human",
                "signed_at": "2026-11-01", "valid_until": "2099-12-31",
                "items": [{"symbol": "AAPL", "tier": "ELIGIBLE", "max_entry": 50}]}))
            epath = write_evidence(Path(tmp) / "e.json",
                                   [evidence_record(old_inst()),
                                    evidence_record(candidate())])
            rc, out, _, _ = run_plan(
                tmp, make_args(universe=str(upath), delivery_evidence=epath),
                snap=funded_snapshot())
            data = json.loads(out)
            self.assertEqual(data["recommended_count"], 1)
            self.assertIn("AAPL", data["constraints"]["allowed_symbols"])

    def test_invalid_roll_constraints_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _, _ = run_plan(tmp, make_args(dte_min=99, dte_max=1))
            self.assertEqual(rc, 3)
            data = json.loads(out)
            self.assertTrue(any("incomplete" in x for x in data["issues"]))

    def test_note_diagnostics_are_appended_to_issues(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _, _ = run_plan(tmp, make_args())
            data = json.loads(out)
            self.assertIn("No stock-only delivery evidence supplied",
                          " ".join(data["issues"]))

    def test_crossed_quote_is_warning_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence(Path(tmp) / "e.json",
                                  [evidence_record(old_inst()),
                                   evidence_record(candidate())])
            tickers = [ticker(20, bid=D(5), ask=D(4)), ticker(30)]
            rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=path),
                                     tickers=tickers)
            data = json.loads(out)
            self.assertTrue(any("crossed" in x for x in data["issues"]))
            self.assertEqual(data["recommended_count"], 0)

    def test_config_snapshot_age_passed_to_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = dict(CFG, risk={"enabled": True, "day_timezone": "UTC",
                                  "max_daily_submissions": 5,
                                  "max_snapshot_age_seconds": 120})
            path = write_evidence(Path(tmp) / "e.json",
                                  [evidence_record(old_inst()),
                                   evidence_record(candidate())])
            rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=path), cfg=cfg)
            data = json.loads(out)
            self.assertEqual(data["constraints"]["max_snapshot_age_seconds"], 120.0)

    def test_no_raw_account_leak_in_any_failure_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            for args in (make_args(), make_args(format="terminal"),
                         make_args(format="markdown")):
                rc, out, err, _ = run_plan(tmp, args)
                self.assertNotIn("DU1234567", out)
                self.assertNotIn("DU1234567", err)

    def test_explicit_candidate_ids_only_no_chain_guessing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_evidence(Path(tmp) / "e.json",
                                  [evidence_record(old_inst()),
                                   evidence_record(candidate())])
            rc, out, _, ib = run_plan(tmp, make_args(delivery_evidence=path))
            # only the two explicitly supplied ids were looked up
            looked_up = [c.conId for c, _ in
                         [(call.args[0], None) for call in
                          ib.reqContractDetails.call_args_list]]
            self.assertEqual(looked_up, [20, 30])
            ib.qualifyContracts.assert_not_called()
            ib.reqSecDefOptParams.assert_not_called()

    def test_multiple_candidates_all_qualified_and_evidence_matched(self):
        with tempfile.TemporaryDirectory() as tmp:
            c2 = candidate(con_id=31, expiry="20270219", strike=D(45))
            path = write_evidence(Path(tmp) / "e.json",
                                  [evidence_record(old_inst()),
                                   evidence_record(candidate()),
                                   evidence_record(c2)])
            details = {20: [detail(20, old_inst())],
                       30: [detail(30, candidate())],
                       31: [detail(31, c2)]}
            tickers = [ticker(20), ticker(30), ticker(31)]
            rc, out, _, _ = run_plan(
                tmp, make_args(delivery_evidence=path, candidate_con_id=[30, 31]),
                details=details, tickers=tickers)
            data = json.loads(out)
            self.assertEqual(data["confirmed_deliverables"], [20, 30, 31])
            self.assertEqual(len(data["candidates"]), 2)


class ReviewBoundaryTests(unittest.TestCase):
    def test_empty_ledger_not_initialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp, "state.sqlite3")
            db.touch()
            evidence = write_evidence(Path(tmp, "e.json"),
                                      [evidence_record(old_inst()), evidence_record(candidate())])
            rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=evidence),
                                     snap=funded_snapshot(), ledger_exists=False)
            self.assertEqual(db.read_bytes(), b"")
            self.assertEqual(rc, 3)
            self.assertEqual(json.loads(out)["recommended_count"], 0)

    def test_readonly_ledger_public_aggregate_and_no_mutation(self):
        from thetamill.state import SubmissionLedger
        from thetamill import execution
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp, "state.sqlite3")
            SubmissionLedger(db, "UTC")
            before = db.read_bytes()
            evidence = write_evidence(Path(tmp, "e.json"),
                                      [evidence_record(old_inst()), evidence_record(candidate())])
            with patch.object(execution, "aggregate_reservations",
                              wraps=execution.aggregate_reservations) as aggregate:
                rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=evidence),
                                         snap=funded_snapshot(), ledger_exists=False)
            self.assertEqual(rc, 0, out)
            self.assertEqual(aggregate.call_count, 1)
            self.assertTrue(aggregate.call_args.args[0].read_only)
            self.assertEqual(db.read_bytes(), before)

    def test_qualified_old_identity_matches_snapshot_position_and_order(self):
        from thetamill.broker_state import WorkingOrder
        changes = ({"mult": D(500)}, {"underlying_id": 99}, {"symbol": "MSFT"},
                   {"currency": "HKD"}, {"right": "C"}, {"strike": D(41)},
                   {"expiry": "20261219"}, {"trading_class": "AAPL1"})
        with tempfile.TemporaryDirectory() as tmp:
            evidence = write_evidence(Path(tmp, "e.json"),
                                      [evidence_record(old_inst()), evidence_record(candidate())])
            for change in changes:
                changed = old_inst(**change)
                for snap in (make_snapshot(instrument=changed, cash={"USD": D(1000000)}),
                             make_snapshot(cash={"USD": D(1000000)}, orders=(
                                 WorkingOrder(changed, "BUY", D(0), 1, 0, "r", D(1), "LMT"),))):
                    with self.subTest(change=change, snapshot=snap):
                        rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=evidence), snap=snap)
                        self.assertEqual(rc, 3)
                        self.assertEqual(json.loads(out)["recommended_count"], 0)

    def test_stale_or_missing_stock_quote_evidence_blocks_caps(self):
        from thetamill.broker_state import PriceQuote
        stock = Instrument(10, "AAPL", "USD", "STK", "SMART", 10)
        old, new = old_inst(right="C"), candidate(right="C")
        cfg = dict(CFG, risk={"enabled": True, "day_timezone": "UTC",
                              "max_daily_submissions": 5, "portfolio": {"amount": 1000}})
        source = "IB timestamped bid/ask midpoint"
        evidence_cases = ({}, {10: PriceQuote(10, D(1), NOW-timedelta(seconds=31), source)},
                          {10: PriceQuote(10, D(2), NOW, source)},
                          {10: PriceQuote(10, D(1), NOW+timedelta(seconds=6), source)},
                          {10: PriceQuote(10, D(1), NOW, "historical close")})
        with tempfile.TemporaryDirectory() as tmp:
            evidence = write_evidence(Path(tmp, "e.json"),
                                      [evidence_record(old), evidence_record(new)])
            for quotes in evidence_cases:
                snap = make_snapshot(positions=(Position(old, D(-5), D(2)),
                                                Position(stock, D(600), D(1))), price_quotes=quotes)
                with self.subTest(quotes=quotes):
                    rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=evidence), snap=snap,
                        cfg=cfg, details={20: [detail(20, old)], 30: [detail(30, new)]})
                    self.assertEqual(rc, 3)
                    self.assertEqual(json.loads(out)["recommended_count"], 0)

    def test_stale_or_inconsistent_fx_evidence_blocks_cash(self):
        from thetamill.broker_state import FxQuote
        old, new = old_inst(currency="HKD"), candidate(currency="HKD")
        with tempfile.TemporaryDirectory() as tmp:
            evidence = write_evidence(Path(tmp, "e.json"),
                                      [evidence_record(old), evidence_record(new)])
            source = "IB timestamped bid/ask midpoint"
            for quotes in ({}, {"HKD": FxQuote("HKD", D("0.001"), NOW-timedelta(seconds=31), source)},
                           {"HKD": FxQuote("HKD", D("0.125"), NOW, source)},
                           {"HKD": FxQuote("HKD", D("0.001"), NOW+timedelta(seconds=6), source)},
                           {"HKD": FxQuote("HKD", D("0.001"), NOW, "historical close")}):
                snap = make_snapshot(instrument=old, cash={"USD": D(100)},
                                     fx={"USD": D(1), "HKD": D("0.001")}, fx_quotes=quotes)
                with self.subTest(quotes=quotes):
                    rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=evidence), snap=snap,
                        details={20: [detail(20, old)], 30: [detail(30, new)]})
                    self.assertEqual(rc, 3)
                    self.assertEqual(json.loads(out)["recommended_count"], 0)

    def test_fresh_stock_and_fx_evidence_can_pass(self):
        from thetamill.broker_state import FxQuote, PriceQuote
        source = "IB timestamped bid/ask midpoint"
        old, new = old_inst(currency="HKD", right="C"), candidate(currency="HKD", right="C")
        stock = Instrument(10, "AAPL", "HKD", "STK", "SMART", 10)
        with tempfile.TemporaryDirectory() as tmp:
            evidence = write_evidence(Path(tmp, "e.json"),
                                      [evidence_record(old), evidence_record(new)])
            for age in (timedelta(seconds=30), -timedelta(seconds=5)):
                stamp = NOW-age
                snap = make_snapshot(positions=(Position(old, D(-5), D(2)),
                                                Position(stock, D(600), D(1))),
                                     fx={"USD": D(1), "HKD": D("0.125")},
                                     price_quotes={10: PriceQuote(10, D(1), stamp, source)},
                                     fx_quotes={"HKD": FxQuote("HKD", D("0.125"), stamp, source)})
                with self.subTest(age=age):
                    rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=evidence), snap=snap,
                        details={20: [detail(20, old)], 30: [detail(30, new)]})
                    self.assertEqual(rc, 0, out)
                    self.assertEqual(json.loads(out)["recommended_count"], 1)

    def test_caps_off_same_base_put_does_not_need_unused_stock_price(self):
        stock = Instrument(10, "AAPL", "USD", "STK", "SMART", 10)
        with tempfile.TemporaryDirectory() as tmp:
            evidence = write_evidence(Path(tmp, "e.json"),
                                      [evidence_record(old_inst()), evidence_record(candidate())])
            snap = funded_snapshot(positions=(Position(old_inst(), D(-5), D(2)),
                                               Position(stock, D(10), None)))
            rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=evidence), snap=snap)
            self.assertEqual(rc, 0, out)

    def test_foreign_cash_and_other_obligation_need_fx_evidence(self):
        other = old_inst(con_id=99, currency="HKD")
        with tempfile.TemporaryDirectory() as tmp:
            evidence = write_evidence(Path(tmp, "e.json"),
                                      [evidence_record(old_inst()), evidence_record(candidate())])
            for snap in (make_snapshot(cash={"USD": D(60000), "HKD": D(1)},
                                        fx={"USD": D(1), "HKD": D("0.125")}),
                         funded_snapshot(positions=(Position(old_inst(), D(-5), D(2)),
                                                     Position(other, D(-1), D(2))),
                                         fx={"USD": D(1), "HKD": D("0.125")})):
                with self.subTest(snapshot=snap):
                    rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=evidence), snap=snap)
                    self.assertEqual(rc, 3, out)

    def test_corrupt_and_missing_ledgers_remain_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = write_evidence(Path(tmp, "e.json"),
                                      [evidence_record(old_inst()), evidence_record(candidate())])
            db = Path(tmp, "state.sqlite3")
            for data in (None, b"not sqlite"):
                if data is not None:
                    db.write_bytes(data)
                rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=evidence),
                                         snap=funded_snapshot(), ledger_exists=False)
                self.assertEqual(rc, 3)
                self.assertEqual(json.loads(out)["recommended_count"], 0)
                if data is None:
                    self.assertFalse(db.exists())
                else:
                    self.assertEqual(db.read_bytes(), data)

    def test_universe_ceiling_never_expands_explicit_max_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            evidence = write_evidence(Path(tmp, "e.json"),
                                      [evidence_record(old_inst()), evidence_record(candidate())])
            universe = Path(tmp, "universe.json")
            universe.write_text(json.dumps({"schema": "thetamill-universe/1",
                "approved_by": "human", "signed_at": "2026-11-01", "valid_until": "2099-12-31",
                "items": [{"symbol": "AAPL", "tier": "ELIGIBLE", "max_entry": 50}]}))
            rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=evidence,
                universe=str(universe), max_entry=35), snap=funded_snapshot())
            self.assertEqual(rc, 3)
            doc = json.loads(out)
            self.assertEqual(D(doc["constraints"]["max_entry"]), D(35))
            self.assertIn("max_entry", " ".join(doc["candidates"][0]["reasons"]))

    def test_none_evidence_empty_and_nested_result_frozen(self):
        self.assertEqual(dict(roll_cli.load_delivery_evidence(None, [old_inst()], NOW)), {})
        with tempfile.TemporaryDirectory() as tmp:
            record = evidence_record(old_inst())
            record["extra"] = {"rows": [{"value": 1}]}
            path = write_evidence(Path(tmp, "e.json"), [record])
            evidence = roll_cli.load_delivery_evidence(path, [old_inst()], NOW)
            with self.assertRaises(TypeError):
                evidence[20]["stock_only"] = False
            with self.assertRaises(TypeError):
                evidence[20]["instrument"]["multiplier"] = "500"
            with self.assertRaises(TypeError):
                evidence[20]["extra"]["rows"][0]["value"] = 2

    def test_real_finish_diagnostics_suppressed(self):
        from thetamill import cli
        with tempfile.TemporaryDirectory() as tmp:
            ib = MagicMock()
            ib._diag = [(999, "account DU1234567 error")]
            ib.reqContractDetails.side_effect = lambda c: {
                20: [detail(20, old_inst())], 30: [detail(30, candidate())]}.get(c.conId, [])
            ib.reqTickers.return_value = [ticker(20), ticker(30)]
            out, err = io.StringIO(), io.StringIO()
            def connect(*args, **kwargs):
                print("Connected DU1234567")
                sys.stderr.write("diagnostic DU1234567\n")
                return ib
            with patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                 patch.object(cli, "_is_live_env", return_value=False), \
                 patch.object(cli, "connect", side_effect=connect), \
                 patch.object(cli, "_risk_snapshot", return_value=make_snapshot()), \
                 patch.object(cli, "_data_dir", return_value=Path(tmp)), \
                 patch.object(cli, "Contract", FakeContract), \
                 patch("sys.stdout", out), patch("sys.stderr", err):
                roll_cli.cmd_roll_plan(CFG, make_args(), now=NOW)
            json.loads(out.getvalue())
            self.assertNotIn("DU1234567", out.getvalue()+err.getvalue())


class SharedEvidenceIntegrationTests(unittest.TestCase):
    def test_new_open_calls_shared_gate_with_candidate_currency(self):
        from thetamill import planning
        with tempfile.TemporaryDirectory() as tmp:
            old, new = old_inst(), candidate()
            evidence = write_evidence(Path(tmp, "e.json"),
                                      [evidence_record(old), evidence_record(new)])
            snap = funded_snapshot()
            with patch.object(planning, "validate_snapshot_evidence",
                              wraps=planning.validate_snapshot_evidence) as gate:
                rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=evidence), snap=snap)
            self.assertEqual(rc, 0, out)
            gate.assert_called_once()
            self.assertIs(gate.call_args.args[0], snap)
            self.assertEqual(gate.call_args.args[2], NOW)
            self.assertEqual(tuple(gate.call_args.kwargs["needed_currencies"]), (new.currency,))

    def test_shared_gate_failure_or_exception_blocks_without_private_details(self):
        from thetamill import planning, risk
        with tempfile.TemporaryDirectory() as tmp:
            evidence = write_evidence(Path(tmp, "e.json"),
                                      [evidence_record(old_inst()), evidence_record(candidate())])
            for error in (False, True):
                with self.subTest(error=error), patch.object(
                        planning, "validate_snapshot_evidence",
                        **({"side_effect": ValueError("DU1234567 private")}
                           if error else {"return_value": ("DU1234567 private",)})), \
                     patch.object(risk, "assess", wraps=risk.assess) as assess:
                    rc, out, err, _ = run_plan(tmp, make_args(delivery_evidence=evidence),
                                               snap=funded_snapshot())
                    self.assertEqual(rc, 3, out)
                    self.assertEqual(json.loads(out)["recommended_count"], 0)
                    self.assertNotIn("DU1234567", out + err)
                    assess.assert_not_called()

    def test_same_foreign_currency_roll_uses_old_position_fx_without_cash_balance(self):
        from thetamill.broker_state import FxQuote
        old, new = old_inst(currency="HKD"), candidate(currency="HKD")
        snap = make_snapshot(instrument=old, cash={"USD": D(10000)},
                             fx={"USD": D(1), "HKD": D("0.125")},
                             fx_quotes={"HKD": FxQuote("HKD", D("0.125"), NOW,
                                           "IB timestamped bid/ask midpoint")})
        with tempfile.TemporaryDirectory() as tmp:
            evidence = write_evidence(Path(tmp, "e.json"),
                                      [evidence_record(old), evidence_record(new)])
            rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=evidence), snap=snap,
                details={20: [detail(20, old)], 30: [detail(30, new)]})
            self.assertEqual(rc, 0, out)
            self.assertNotIn("HKD", snap.cash)
            self.assertEqual(json.loads(out)["recommended_count"], 1)

    def test_cross_currency_candidate_rejected_before_risk_gate(self):
        from thetamill import planning
        old, new = old_inst(), candidate(currency="HKD")
        with tempfile.TemporaryDirectory() as tmp:
            evidence = write_evidence(Path(tmp, "e.json"),
                                      [evidence_record(old), evidence_record(new)])
            with patch.object(planning, "validate_snapshot_evidence") as gate:
                rc, out, _, _ = run_plan(tmp, make_args(delivery_evidence=evidence),
                    snap=funded_snapshot(), details={20: [detail(20, old)], 30: [detail(30, new)]})
            self.assertEqual(rc, 3)
            row = json.loads(out)["candidates"][0]
            self.assertTrue(any("currency does not match" in reason for reason in row["reasons"]))
            gate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
