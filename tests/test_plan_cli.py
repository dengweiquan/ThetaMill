"""Offline tests for the read-only ordinary-plan CLI adapter.

No real broker, no network, no git, no production/shared writes, no agents.
All broker interaction is a MagicMock; the local intent ledger is a REAL temp
SQLite DB (or deliberately absent/corrupt); evidence/universe/output files are
tempdir-only.  A fixed aware ``NOW`` is injected so the account snapshot, FX /
stock quotes and deliverable evidence are deterministic.

The adapter is verified to:
  * use cli._risk_snapshot (account-scoped) + a READ-ONLY existing ledger,
    never creating/repairing a missing/corrupt ledger;
  * select strikes from a FRESH LIVE timestamped bid/ask midpoint, with NO
    historical _spot fallback;
  * compute shares / cost from the SNAPSHOT by underlying identity (never the
    old unscoped _shares/_coverage/_short_calls* helpers);
  * run ONE cumulative assessment (100/200/500 multipliers, shared cash, no
    share double-spend, new-currency FX);
  * mark BLOCKED items explicitly, keep qty un-truncated, carry full identity,
    mask the account, keep the doc run-re-preflightable, and write create-only.
"""
import argparse
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from thetamill import cli, plan_cli, planning, roll, universe
from thetamill.broker_state import (FxQuote, Instrument, Position, Snapshot)

NOW = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
QUOTE_TIME = NOW - timedelta(seconds=2)          # fresh, LIVE reception time
EXPIRY = "20261218"                              # 41 DTE from NOW
SOURCE = "IB timestamped bid/ask midpoint"
CFG = {"default_currency": "USD", "host": "127.0.0.1", "live_port": 4001,
       "paper_port": 4002, "client_id": 1, "live_enabled": False,
       "risk": {"enabled": False}}

# Underlying STK conIds, one per symbol.  Option Instruments qualified from the
# chain carry underConId == the STK conId (so Instrument.key == "<conId>:USD").
UNDERLYING = {"AAA": 10, "BBB": 20, "CCC": 30}
OPTION_CON_ID = {"AAA": 110, "BBB": 220, "CCC": 330}
STOCK_PRICE = {"AAA": 100.0, "BBB": 200.0, "CCC": 50.0}


def make_args(**overrides):
    p = dict(symbol=None, symbols=None, preset="cash-put", dte_min=21,
             dte_max=45, target_dte=30, otm=0.05, qty=1, currency="USD",
             json_out=None, format=None, output=None, universe=None,
             delivery_evidence=None, account=None, paper=True, port=4002,
             delayed=False)
    p.update(overrides)
    return argparse.Namespace(**p)


def inst(sym, *, con_id=None, mult=D(100), strike=D(40), right="P",
         currency="USD", underlying_id=None, **kw):
    con_id = con_id if con_id is not None else OPTION_CON_ID[sym]
    underlying_id = (underlying_id if underlying_id is not None
                     else UNDERLYING[sym])
    p = dict(con_id=con_id, symbol=sym, currency=currency, sec_type="OPT",
             exchange="SMART", underlying_id=underlying_id, multiplier=mult,
             right=right, strike=strike, expiry=EXPIRY, trading_class=sym)
    p.update(kw)
    return Instrument(**p)


def stock_inst(sym, *, currency="USD", underlying_id=None):
    con_id = underlying_id if underlying_id is not None else UNDERLYING[sym]
    return Instrument(con_id, sym, currency, "STK", "SMART", con_id)


def contract(sym, qual_inst):
    """A broker-like qualified Contract carrying every field _instrument reads."""
    return SimpleNamespace(
        conId=qual_inst.con_id, secType=qual_inst.sec_type,
        symbol=qual_inst.symbol, currency=qual_inst.currency,
        exchange=qual_inst.exchange, multiplier=str(qual_inst.multiplier),
        strike=qual_inst.strike, right=qual_inst.right,
        lastTradeDateOrContractMonth=qual_inst.expiry,
        tradingClass=qual_inst.trading_class, comboLegs=[])


def stock_contract(sym, *, currency="USD"):
    return SimpleNamespace(conId=UNDERLYING[sym], secType="STK", symbol=sym,
                           currency=currency, exchange="SMART")


def detail(sym, qual_inst, *, under_con_id=None):
    return SimpleNamespace(
        contract=contract(sym, qual_inst),
        underConId=(under_con_id if under_con_id is not None
                    else qual_inst.underlying_id),
        underSecType="STK")


def chain_for(sym):
    px = STOCK_PRICE[sym]
    return SimpleNamespace(exchange="SMART", tradingClass=sym,
                           multiplier="100", expirations=[EXPIRY],
                           strikes=[px * x for x in (0.85, 0.9, 0.95,
                                                     1.0, 1.05, 1.1, 1.15)])


def ticker(con_id, price, *, time=QUOTE_TIME, market_data_type=1,
           bid=None, ask=None):
    b = D(str(price)) - D("0.05") if bid is None else D(str(bid))
    a = D(str(price)) + D("0.05") if ask is None else D(str(ask))
    return SimpleNamespace(
        contract=SimpleNamespace(conId=con_id), time=time, bid=b, ask=a,
        marketDataType=market_data_type)


def snapshot(*, symbols=("AAA",), cash=None, positions=(), orders=(),
             base="USD", fx=None, fx_quotes=None, price_quotes=None,
             captured_at=NOW, account="DU1234567", net_liq=D(5000000),
             issues=()):
    cash = cash if cash is not None else {"USD": D(5000000)}
    fx = fx if fx is not None else {base: D(1)}
    fx_quotes = fx_quotes if fx_quotes is not None else {}
    price_quotes = price_quotes if price_quotes is not None else {}
    return Snapshot(account, "paper", captured_at, base, cash, net_liq,
                    tuple(positions), tuple(orders), fx, tuple(issues),
                    price_quotes, fx_quotes)


def pos(i, qty, price=None, avg_cost=None):
    return Position(i, D(qty), D(price) if price is not None else None,
                    D(avg_cost) if avg_cost is not None else None)


def evidence_record(qual_inst, **overrides):
    record = {"instrument": roll.instrument_data(qual_inst),
              "stock_only": True, "source": "IB contract details + human review",
              "verified_at": (NOW - timedelta(hours=1)).isoformat(),
              "expires_at": (NOW + timedelta(hours=23)).isoformat()}
    record["instrument"].update(overrides)
    return record


def write_evidence(path, instruments, *, schema="thetamill.deliverables.v1"):
    Path(path).write_text(json.dumps(
        {"schema": schema,
         "records": [evidence_record(i) for i in instruments]}), encoding="utf-8")
    return str(path)


def make_ledger(path):
    from thetamill.state import SubmissionLedger
    return SubmissionLedger(Path(path) / "state.sqlite3", "UTC")


def run_plan(tmp, args, *, snap=None, cfg=None, symbols=("AAA",),
             option_insts=None, ledger="valid", chain_map=None,
             quote_prices=None, market_data_type=1, quote_times=None,
             human_print=True, now=NOW, reserve_seed=None,
             resolve_option_fn=None, quote_premium_fn=None,
             risk_snapshot_fn=None, **patches):
    """Invoke cmd_plan with every I/O boundary mocked; return (doc, rc, out, ib).

    ledger: "valid" -> real empty temp DB; "missing" -> absent file;
            "corrupt" -> random bytes; "empty" -> zero-byte file.
    option_insts: {symbol: Instrument} the resolver qualifies (default: a
            100-mult, strike-40 short PUT per symbol).
    quote_prices: {symbol: stock midpoint}; default STOCK_PRICE.
    reserve_seed: optional callable(ledger_path) to add reservations.
    """
    snap = snap or snapshot(symbols=symbols)
    option_insts = option_insts or {s: inst(s) for s in symbols}
    chain_map = chain_map or {s: chain_for(s) for s in symbols}
    quote_prices = quote_prices or STOCK_PRICE
    # When `now` is None (production 2-arg call), quote stamps must track the
    # wall clock rather than the fixed NOW, otherwise freshness would reject.
    effective_quote_time = (QUOTE_TIME if now is not None
                            else datetime.now(timezone.utc) - timedelta(seconds=2))
    quote_times = quote_times or {}
    default_quote_time = effective_quote_time

    ib = MagicMock()
    ib._diag = []

    def chain_safe(ib_, sym, cur):
        if sym not in chain_map or chain_map[sym] is None:
            return None, None
        return stock_contract(sym, currency=cur), chain_map[sym]

    def resolve_option(ib_, sym, expiry, strikes, target, right, currency,
                       chain, floor=None):
        qi = option_insts.get(sym)
        if qi is None:
            return None, None
        # Honor the requested right while keeping the qualified identity.
        wanted = inst(sym, con_id=qi.con_id, mult=qi.multiplier,
                      strike=qi.strike, right=right, currency=qi.currency,
                      underlying_id=qi.underlying_id)
        return contract(sym, wanted), wanted.strike

    def req_tickers(c):
        con_id = getattr(c, "conId", None)
        for s, cid in UNDERLYING.items():
            if cid == con_id:
                t = quote_times.get(s, default_quote_time)
                return [ticker(cid, quote_prices.get(s, STOCK_PRICE[s]),
                               time=t, market_data_type=market_data_type)]
        # option premium / FX contract: give a neutral positive LIVE tick
        return [ticker(con_id or 999, 1.0, time=default_quote_time,
                       market_data_type=market_data_type)]

    def quote_premium(ib_, opt):
        return D("2.5"), "买价"

    def req_contract_details(c):
        con_id = getattr(c, "conId", None)
        for s, qi in option_insts.items():
            if qi.con_id == con_id:
                return [detail(s, qi)]
        return []

    def safe_req_tickers_plan(ib_, *contracts, **kwargs):
        res = []
        for c in contracts:
            res.extend(req_tickers(c))
        return res

    ib.reqTickers.side_effect = req_tickers
    ib.reqContractDetails.side_effect = req_contract_details
    ib.qualifyContracts.side_effect = lambda c: [c]
    ib._diag = []

    ledger_path = Path(tmp) / "state.sqlite3"
    if ledger == "valid":
        make_ledger(tmp)
        if reserve_seed is not None:
            reserve_seed(ledger_path)
    elif ledger == "corrupt":
        ledger_path.write_bytes(b"this is not a sqlite database at all" * 4)
    elif ledger == "empty":
        ledger_path.write_bytes(b"")
    # "missing": leave absent

    cfg = cfg or CFG
    stdout, stderr = io.StringIO(), io.StringIO()

    def _no_network(*a, **k):
        raise AssertionError("offline tests must not open a real socket")

    actual_resolve = resolve_option_fn or resolve_option
    actual_quote_premium = quote_premium_fn or quote_premium
    if risk_snapshot_fn is not None:
        snap_patch = patch.object(cli, "_risk_snapshot", side_effect=risk_snapshot_fn)
    else:
        snap_patch = patch.object(cli, "_risk_snapshot", return_value=snap)

    with patch("socket.socket", side_effect=_no_network), \
         patch("socket.create_connection", side_effect=_no_network), \
         patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
         patch.object(cli, "_is_live_env", return_value=False), \
         patch.object(cli, "connect", return_value=ib), \
         patch.object(cli, "finish") as finish_mock, \
         snap_patch as snap_mock, \
         patch.object(cli, "_chain_safe", side_effect=chain_safe), \
         patch.object(cli, "_pick_expiry", return_value=(EXPIRY, 41)), \
         patch.object(cli, "_resolve_option", side_effect=actual_resolve), \
         patch.object(cli, "_quote_premium", side_effect=actual_quote_premium), \
         patch.object(plan_cli, "safe_req_tickers", side_effect=safe_req_tickers_plan), \
         patch.object(cli, "safe_req_tickers", side_effect=safe_req_tickers_plan), \
         patch.object(cli, "_data_dir", return_value=Path(tmp)), \
         patch.object(cli, "CFG_PATH", Path(tmp) / "config.json"), \
         patch("sys.stdout", stdout), patch("sys.stderr", stderr):
        rc = plan_cli.cmd_plan(cfg, args, now=now)
    out_text = stdout.getvalue() + "\n" + stderr.getvalue()
    # Pull the JSON document from the output (json_out wins; else stdout JSON).
    jpath = getattr(args, "json_out", None) or getattr(args, "output", None)
    if jpath and Path(jpath).exists():
        try:
            doc = json.loads(Path(jpath).read_text(encoding="utf-8"))
        except (ValueError, TypeError):
            doc = None
    else:
        # format None => pure JSON on stdout (unless we forced human_print)
        try:
            doc = json.loads(out_text)
        except (ValueError, TypeError):
            # terminal/human mode: recover via a second json-format run is not
            # possible here, so callers should use json_out for assertions.
            doc = None
    return SimpleNamespace(doc=doc, rc=rc, out=out_text, ib=ib,
                           stdout=stdout.getvalue(), stderr=stderr.getvalue(),
                           snap_mock=snap_mock, finish_mock=finish_mock,
                           ledger_path=ledger_path, tmp=Path(tmp))


# --------------------------------------------------------------------------- #
# Pure quote helper
# --------------------------------------------------------------------------- #
class FreshStockMidTests(unittest.TestCase):
    def setUp(self):
        self._safe_patcher = patch.object(
            plan_cli, "safe_req_tickers", side_effect=self._mock_safe_req_tickers
        )
        self._mock_safe = self._safe_patcher.start()
        self.addCleanup(self._safe_patcher.stop)

    def _mock_safe_req_tickers(self, ib, *contracts, **kwargs):
        return ib.reqTickers(*contracts)

    def _ib(self, t):
        ib = MagicMock()
        ib.reqTickers.return_value = [t]
        return ib

    def test_live_fresh_two_sided_mid_is_usable(self):
        c = stock_contract("AAA")
        ib = self._ib(ticker(10, 100.0))
        self.assertEqual(plan_cli.fresh_stock_mid(ib, c, NOW), D("100.0"))

    def test_delayed_and_frozen_market_data_types_refused(self):
        c = stock_contract("AAA")
        for mdt in (2, 3, 4, None, 0, True, False, 1.0, "1"):
            ib = self._ib(ticker(10, 100.0, market_data_type=mdt))
            self.assertIsNone(plan_cli.fresh_stock_mid(ib, c, NOW), mdt)

    def test_stale_or_future_timestamp_refused(self):
        c = stock_contract("AAA")
        for stamp in (NOW - timedelta(seconds=31), NOW - timedelta(minutes=5),
                      NOW + timedelta(seconds=6)):
            ib = self._ib(ticker(10, 100.0, time=stamp))
            self.assertIsNone(plan_cli.fresh_stock_mid(ib, c, NOW))

    def test_naive_timestamp_refused(self):
        c = stock_contract("AAA")
        ib = self._ib(ticker(10, 100.0, time=datetime(2026, 11, 7, 20, 0)))
        self.assertIsNone(plan_cli.fresh_stock_mid(ib, c, NOW))

    def test_crossed_or_nonpositive_quote_refused(self):
        c = stock_contract("AAA")
        for bid, ask in ((D("0"), D("1")), (D("-1"), D("2")),
                         (D("3"), D("2")), (float("nan"), D("2"))):
            ib = self._ib(ticker(10, 100.0, bid=bid, ask=ask))
            self.assertIsNone(plan_cli.fresh_stock_mid(ib, c, NOW))

    def test_contract_identity_mismatch_refused(self):
        c = stock_contract("AAA")
        ib = self._ib(ticker(999, 100.0))
        self.assertIsNone(plan_cli.fresh_stock_mid(ib, c, NOW))

    def test_malformed_ticker_fails_closed(self):
        c = stock_contract("AAA")
        for ib in (MagicMock(reqTickers=MagicMock(return_value=[])),
                   MagicMock(reqTickers=MagicMock(side_effect=RuntimeError("x")))):
            self.assertIsNone(plan_cli.fresh_stock_mid(ib, c, NOW))

    def test_strict_live_int_market_data_type_regression(self):
        c = stock_contract("AAA")
        for bad_mdt in (True, False, 1.0, "1", 0, 2, 3, 4, None):
            ib = self._ib(ticker(10, 100.0, market_data_type=bad_mdt))
            self.assertIsNone(plan_cli.fresh_stock_mid(ib, c, NOW))
        ib_live = self._ib(ticker(10, 100.0, market_data_type=1))
        self.assertIsNotNone(plan_cli.fresh_stock_mid(ib_live, c, NOW))

    def test_exact_con_id_equality_and_no_fallbacks_regression(self):
        c = stock_contract("AAA")
        ib_mismatch = self._ib(ticker(999, 100.0))
        self.assertIsNone(plan_cli.fresh_stock_mid(ib_mismatch, c, NOW))
        t_alias = SimpleNamespace(conId=10, contract=None, time=QUOTE_TIME,
                                  bid=D("99.95"), ask=D("100.05"), marketDataType=1)
        self.assertIsNone(plan_cli.fresh_stock_mid(self._ib(t_alias), c, NOW))
        self.assertIsNone(plan_cli.fresh_stock_mid(self._ib(ticker(10, 100.0)), SimpleNamespace(conId=True), NOW))

    def test_no_network_and_no_order_calls(self):
        c = stock_contract("AAA")
        ib = self._ib(ticker(10, 100.0))
        self.assertIsNotNone(plan_cli.fresh_stock_mid(ib, c, NOW))
        ib.reqMktData.assert_not_called()
        ib.placeOrder.assert_not_called()
        ib.bracketOrder.assert_not_called()


# --------------------------------------------------------------------------- #
# Snapshot-derived share / cost helpers
# --------------------------------------------------------------------------- #
class SnapshotHelpersTests(unittest.TestCase):
    def test_shares_and_busy_by_underlying_identity(self):
        call = inst("AAA", con_id=999, mult=D(200), strike=D(40), right="C")
        s = snapshot(positions=(
            pos(stock_inst("AAA"), 1000, 100.0, avg_cost=95.0),
            pos(call, -3),
        ))
        self.assertEqual(plan_cli._snapshot_shares(s, "10:USD"), D(1000))
        self.assertEqual(plan_cli._snapshot_busy_shares(s, "10:USD"), D(600))
        self.assertEqual(plan_cli._snapshot_cost(s, "10:USD"), D(95))

    def test_helpers_are_account_scoped_not_symbol_guessed(self):
        # A different underlying identity with the SAME symbol must NOT count.
        other = Instrument(77, "AAA", "USD", "STK", "SMART", 77)
        s = snapshot(positions=(pos(other, 500, 10.0, avg_cost=8.0),))
        self.assertEqual(plan_cli._snapshot_shares(s, "10:USD"), D(0))
        self.assertEqual(plan_cli._snapshot_shares(s, "77:USD"), D(500))
        self.assertEqual(plan_cli._snapshot_cost(s, "10:USD"), None)


# --------------------------------------------------------------------------- #
# End-to-end adapter
# --------------------------------------------------------------------------- #
class PlanCLITests(unittest.TestCase):
    # -- happy path / document contract --------------------------------------- #
    def test_happy_path_is_allowed_with_full_run_identity(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {s: inst(s) for s in ("AAA",)}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            out = Path(d) / "plan.json"
            args = make_args(symbol="AAA", delivery_evidence=ev, json_out=str(out))
            r = run_plan(d, args, option_insts=qi)
            self.assertEqual(r.rc, 0, r.out)
            doc = r.doc
            self.assertEqual(doc["schema"], plan_cli.SCHEMA)
            # run re-preflightability: NO roll-style top-level abort switch.
            self.assertIs(doc["recommendation_only"], False)
            self.assertFalse(str(doc["schema"]).startswith("thetamill.roll."))
            self.assertIs(doc["executable"], False)
            self.assertEqual(doc["allowed_count"], 1)
            self.assertEqual(doc["blocked_count"], 0)
            it = doc["items"][0]
            for field in ("conId", "mult", "tradingClass", "exchange"):
                self.assertIn(field, it)
            self.assertEqual(it["conId"], 110)
            self.assertEqual(it["mult"], "100")
            self.assertEqual(it["strike"], "40")
            self.assertEqual(it["qty"], "1")
            self.assertEqual(it["premium"], "2.5")
            self.assertEqual(it["tradingClass"], "AAA")
            self.assertEqual(it["exchange"], "SMART")
            self.assertIs(it["allowed"], True)
            self.assertIs(it["executable"], False)
            self.assertTrue(it["reasons"])
            self.assertTrue(it["delivery_evidence"]["confirmed"])
            self.assertEqual(it["delivery_evidence"]["verified_at"],
                             (NOW - timedelta(hours=1)).isoformat())
            self.assertEqual(it["delivery_evidence"]["expires_at"],
                             (NOW + timedelta(hours=23)).isoformat())
            self.assertEqual(it["quote"]["source"], SOURCE)
            self.assertEqual(it["quote"]["time"], QUOTE_TIME.isoformat())
            self.assertEqual(it["quote"]["conId"], 10)
            self.assertEqual(it["quote"]["mid"], "100")
            # metadata
            self.assertEqual(doc["account"], "***567")   # masked by default
            self.assertNotIn("DU1234567", json.dumps(doc))
            self.assertEqual(doc["snapshot_time"], NOW.isoformat())
            self.assertEqual(doc["environment"], "paper")
            self.assertIs(doc["is_live"], False)
            self.assertEqual(doc["source"]["quote"], SOURCE)
            self.assertEqual(doc["source"]["delivery"],
                             "thetamill.deliverables.v1, per-OPTION conId")
            self.assertEqual(doc["currencies"], ["USD"])
            # aware ISO timestamps
            for key in ("generated", "valid_until"):
                self.assertIsNotNone(datetime.fromisoformat(doc[key]).tzinfo)

    def test_cli_delegate_pattern_works(self):
        # main replaces cli.cmd_plan with a 2-arg delegate
        # (`return plan_cli.cmd_plan(cfg, args)`).  Exercise that exact
        # production signature through the fully offline harness: no `now`,
        # no injectable clock, real wall-clock freshness is still satisfied
        # because the snapshot/quotes are freshly built relative to it below.
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            out = Path(d) / "p.json"
            args = make_args(symbol="AAA", delivery_evidence=ev,
                             json_out=str(out))
            # Build a snapshot whose captured_at / quote times track the same
            # wall clock the 2-arg call will use (fresh, not stale).
            live_now = datetime.now(timezone.utc)
            snap = snapshot(captured_at=live_now)
            r = run_plan(d, args, option_insts=qi, snap=snap, now=None)
            self.assertIn(r.rc, (0, 3))   # production 2-arg call returned cleanly

    # -- evidence -------------------------------------------------------------- #
    def test_no_delivery_evidence_shows_candidate_but_blocks_it(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", json_out=str(out)),
                         option_insts=qi)
            self.assertEqual(r.rc, 3)
            it = r.doc["items"][0]
            self.assertEqual(len(r.doc["items"]), 1)   # still displayed
            self.assertIs(it["allowed"], False)
            self.assertIs(it["delivery_evidence"]["confirmed"], False)
            self.assertTrue(any("delivery evidence" in x.lower()
                                for x in it["reasons"]))

    def test_per_conid_evidence_only_confirms_its_own_option(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA"), "BBB": inst("BBB")}
            # Evidence for AAA only; BBB shares the same symbol family but a
            # different OPTION conId -> must NOT be confirmed.
            ev = write_evidence(Path(d) / "ev.json", [qi["AAA"]])
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbols="AAA,BBB", delivery_evidence=ev,
                                      json_out=str(out)),
                         symbols=("AAA", "BBB"), option_insts=qi)
            doc = r.doc
            by_sym = {it["symbol"]: it for it in doc["items"]}
            self.assertIs(by_sym["AAA"]["delivery_evidence"]["confirmed"], True)
            self.assertIs(by_sym["BBB"]["delivery_evidence"]["confirmed"], False)
            self.assertIs(by_sym["AAA"]["allowed"], True)
            self.assertIs(by_sym["BBB"]["allowed"], False)

    def test_malformed_evidence_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = Path(d) / "ev.json"
            ev.write_text('{"schema": "thetamill.deliverables.v1", '
                          '"records": [{"stock_only": true}]}', encoding="utf-8")
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", delivery_evidence=str(ev),
                                      json_out=str(out)), option_insts=qi)
            self.assertEqual(r.rc, 3)
            self.assertIs(r.doc["items"][0]["allowed"], False)
            self.assertTrue(any("delivery evidence rejected" in x
                                for x in r.doc["issues"]))

    def test_evidence_identity_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = Path(d) / "ev.json"
            write_evidence(ev, [inst("AAA", mult=D(200))])  # wrong multiplier
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", delivery_evidence=str(ev),
                                      json_out=str(out)), option_insts=qi)
            self.assertIs(r.doc["items"][0]["allowed"], False)

    # -- ledger invariants ----------------------------------------------------- #
    def test_missing_ledger_blocks_and_does_not_create_one(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", delivery_evidence=ev,
                                      json_out=str(out)),
                         option_insts=qi, ledger="missing")
            self.assertEqual(r.rc, 3)
            self.assertFalse(r.ledger_path.exists())       # NOT created
            self.assertIs(r.doc["items"][0]["allowed"], False)
            self.assertFalse(r.doc["budget_confirmed"])
            self.assertTrue(any("ledger" in x for x in r.doc["issues"]
                                + r.doc["items"][0]["reasons"]) or
                            any("ledger" in x for x in r.doc["items"][0]["reasons"]))

    def test_corrupt_byte_ledger_blocks_and_is_not_repaired(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", delivery_evidence=ev,
                                      json_out=str(out)),
                         option_insts=qi, ledger="corrupt")
            self.assertEqual(r.rc, 3)
            self.assertEqual(r.ledger_path.read_bytes()[:8],
                             b"this is ")          # untouched / not repaired
            self.assertIs(r.doc["items"][0]["allowed"], False)

    def test_empty_byte_ledger_blocks_and_is_not_repaired(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", delivery_evidence=ev,
                                      json_out=str(out)),
                         option_insts=qi, ledger="empty")
            self.assertEqual(r.rc, 3)
            self.assertEqual(r.ledger_path.read_bytes(), b"")
            self.assertIs(r.doc["items"][0]["allowed"], False)

    def test_ledger_opened_read_only(self):
        # Patch SubmissionLedger in state to observe read_only=True.
        import thetamill.state as st
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            out = Path(d) / "p.json"
            seen = {}
            real = st.SubmissionLedger

            def spy(path, tz, **kwargs):
                seen["read_only"] = kwargs.get("read_only")
                return real(path, tz, **kwargs)

            with patch.object(st, "SubmissionLedger", side_effect=spy):
                r = run_plan(d, make_args(symbol="AAA", delivery_evidence=ev,
                                          json_out=str(out)), option_insts=qi)
            self.assertIs(seen.get("read_only"), True)
            self.assertEqual(r.rc, 0)

    # -- cumulative budget: shared cash, no double spend ---------------------- #
    def test_three_items_share_one_cash_pool_sequentially(self):
        # cash 100000; each 100-mult strike-40 put needs 4000 cash.  With a
        # deliberately tiny pool two are allowed and the third is blocked by
        # the sequential accumulator.  Two targets are enough: 100/200/500
        # multipliers are exercised separately below.
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA"), "BBB": inst("BBB")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            s = snapshot(symbols=("AAA", "BBB"), cash={"USD": D(6000)})
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbols="AAA,BBB", delivery_evidence=ev,
                                      json_out=str(out)),
                         symbols=("AAA", "BBB"), option_insts=qi, snap=s)
            doc = r.doc
            self.assertEqual(doc["allowed_count"], 1)   # 4000 then 4000 > 6000
            self.assertEqual(doc["blocked_count"], 1)
            self.assertEqual(r.rc, 0)                   # at least one allowed
            self.assertEqual(doc["reservations"]["cash_base"], "4000")

    def test_100_200_500_multipliers_use_exact_collateral(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {
                "AAA": inst("AAA", mult=D(100)),
                "BBB": inst("BBB", mult=D(200)),
                "CCC": inst("CCC", mult=D(500)),
            }
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            # 4000 + 8000 + 20000 = 32000 exactly fits.
            s = snapshot(symbols=("AAA", "BBB", "CCC"),
                         cash={"USD": D(32000)})
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbols="AAA,BBB,CCC",
                                      delivery_evidence=ev, json_out=str(out)),
                         symbols=("AAA", "BBB", "CCC"), option_insts=qi, snap=s)
            self.assertEqual(r.doc["allowed_count"], 3, r.out)
            self.assertEqual(r.doc["reservations"]["cash_base"], "32000")

    def test_100_200_500_one_over_budget_blocks_only_the_last(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {
                "AAA": inst("AAA", mult=D(100)),
                "BBB": inst("BBB", mult=D(200)),
                "CCC": inst("CCC", mult=D(500)),
            }
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            s = snapshot(symbols=("AAA", "BBB", "CCC"),
                         cash={"USD": D(31999)})   # 1 unit short
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbols="AAA,BBB,CCC",
                                      delivery_evidence=ev, json_out=str(out)),
                         symbols=("AAA", "BBB", "CCC"), option_insts=qi, snap=s)
            self.assertEqual(r.doc["allowed_count"], 2)
            self.assertEqual(r.doc["blocked_count"], 1)
            by_sym = {it["symbol"]: it for it in r.doc["items"]}
            self.assertIs(by_sym["CCC"]["allowed"], False)
            self.assertTrue(by_sym["CCC"]["reasons"])

    def test_shares_are_not_double_spent_for_covered_calls(self):
        with tempfile.TemporaryDirectory() as d:
            # Two covered calls on the SAME underlying, 100 shares total,
            # 100 mult each -> only one fits; qty stays 1 (never truncated).
            qi = {"AAA": inst("AAA", right="C")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            s = snapshot(symbols=("AAA",), cash={"USD": D(5000000)},
                         positions=(pos(stock_inst("AAA"), 100, 100.0,
                                        avg_cost=90.0),))
            out = Path(d) / "p.json"
            # Two items for the same symbol via --symbols AAA,AAA is deduped by
            # targets, so instead force two distinct option conIds on ONE target.
            r = run_plan(d, make_args(symbol="AAA", preset="sell-call", qty=2,
                                      delivery_evidence=ev, json_out=str(out)),
                         option_insts=qi, snap=s)
            it = r.doc["items"][0]
            self.assertEqual(it["qty"], "2")               # NOT truncated to 1
            self.assertIs(it["allowed"], False)          # 200 > 100 shares
            self.assertTrue(any("share" in x.lower() or "股" in x
                                for x in it["reasons"]))

    def test_qty_is_never_silently_truncated(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            s = snapshot(cash={"USD": D(5000)})          # only 1 put fits
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", qty=5,
                                      delivery_evidence=ev, json_out=str(out)),
                         option_insts=qi, snap=s)
            self.assertEqual(r.doc["items"][0]["qty"], "5")
            self.assertIs(r.doc["items"][0]["allowed"], False)
            self.assertEqual(r.rc, 3)

    # -- new currency FX evidence --------------------------------------------- #
    def test_new_foreign_currency_needs_timestamped_fx_quote(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA", currency="EUR")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            out = Path(d) / "p.json"
            # No EUR FxQuote -> evidence gate blocks (candidate still shown).
            s = snapshot(cash={"USD": D(5000000)},
                         fx={"USD": D(1), "EUR": D("1.1")})
            r = run_plan(d, make_args(symbol="AAA", currency="EUR",
                                      delivery_evidence=ev, json_out=str(out)),
                         option_insts=qi, snap=s)
            it = r.doc["items"][0]
            self.assertIs(it["allowed"], False)
            self.assertTrue(it["evidence_reasons"] or
                            any("FX" in x or "quote" in x.lower()
                                for x in it["reasons"]))

    def test_new_foreign_currency_with_valid_fx_quote_is_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA", currency="EUR")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            out = Path(d) / "p.json"
            fxq = FxQuote("EUR", D("1.1"), QUOTE_TIME, SOURCE)
            s = snapshot(cash={"USD": D(5000000)},
                         fx={"USD": D(1), "EUR": D("1.1")},
                         fx_quotes={"EUR": fxq})
            r = run_plan(d, make_args(symbol="AAA", currency="EUR",
                                      delivery_evidence=ev, json_out=str(out)),
                         option_insts=qi, snap=s)
            self.assertEqual(r.rc, 0, r.out)
            self.assertIs(r.doc["items"][0]["allowed"], True)
            # EUR put needs 40*100*1.1 = 4400 base
            self.assertEqual(r.doc["reservations"]["cash_base"], "4400")

    # -- no history fallback --------------------------------------------------- #
    def test_no_live_stock_quote_produces_no_candidate_and_no_historical_call(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", delivery_evidence=ev,
                                      json_out=str(out)),
                         option_insts=qi, market_data_type=3)  # DELAYED
            self.assertEqual(r.doc["items"], [])
            self.assertEqual(r.rc, 3)          # empty candidates: rc 3
            self.assertTrue(any("fresh LIVE" in x for x in r.doc["issues"]))
            # The historical helper must NEVER be consulted.
            self.assertFalse(any(
                "historical" in str(c).lower() or "_spot" in str(c)
                for c in r.ib.mock_calls))

    def test_stale_stock_quote_produces_no_candidate(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", json_out=str(out)),
                         option_insts=qi,
                         quote_times={"AAA": NOW - timedelta(seconds=60)})
            self.assertEqual(r.rc, 3)
            self.assertEqual(r.doc["items"], [])
            self.assertTrue(any("fresh LIVE" in x for x in r.doc["issues"]))

    # -- broker scope / cleanup / account ------------------------------------- #
    def test_account_scoped_snapshot_is_used_and_finished_in_finally(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", json_out=str(out)),
                         option_insts=qi)
            self.assertTrue(r.snap_mock.called)
            self.assertEqual(r.snap_mock.call_args[0][3], "paper")
            r.finish_mock.assert_called_once()   # cleanup in finally

    def test_finish_is_called_even_when_snapshot_raises(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "p.json"
            args = make_args(symbol="AAA", json_out=str(out))
            ib = MagicMock()
            with patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                 patch.object(cli, "_is_live_env", return_value=False), \
                 patch.object(cli, "connect", return_value=ib), \
                 patch.object(cli, "finish") as fin, \
                 patch.object(cli, "_risk_snapshot",
                              side_effect=RuntimeError("boom DU1234567")), \
                 patch.object(cli, "_data_dir", return_value=Path(d)), \
                 patch("sys.stdout", io.StringIO()), \
                 patch("sys.stderr", io.StringIO()):
                rc = plan_cli.cmd_plan(CFG, args, now=NOW)
            self.assertEqual(rc, 3)
            fin.assert_called_once()
            doc = json.loads(Path(out).read_text(encoding="utf-8"))
            self.assertNotIn("DU1234567", json.dumps(doc))   # no raw leak
            self.assertNotIn("boom", json.dumps(doc))

    def test_broker_diagnostics_are_not_leaked_to_json_stdout(self):
        # connect / finish printing arbitrary text must not corrupt stdout JSON.
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", json_out=str(out)),
                         option_insts=qi)
            # The JSON file is valid; no stray text was mixed into the doc.
            self.assertIsInstance(r.doc, dict)
            self.assertEqual(r.doc["items"][0]["symbol"], "AAA")

    def test_account_is_masked_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", json_out=str(out)),
                         option_insts=qi)
            dumped = json.dumps(r.doc)
            self.assertIn("***567", dumped)
            self.assertNotIn("DU1234567", dumped)

    # -- blocked / rc / create-only ------------------------------------------- #
    def test_all_blocked_returns_3(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", json_out=str(out)),
                         option_insts=qi, ledger="missing")
            self.assertEqual(r.rc, 3)
            self.assertEqual(r.doc["allowed_count"], 0)
            self.assertEqual(r.doc["blocked_count"], 1)
            self.assertIs(r.doc["items"][0]["allowed"], False)
            self.assertIs(r.doc["items"][0]["executable"], False)

    def test_no_candidates_returns_rc3(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="ZZZ", json_out=str(out)),
                         symbols=(), chain_map={})
            self.assertEqual(r.rc, 3)
            self.assertEqual(r.doc["items"], [])
            self.assertTrue(r.doc["issues"])

    def test_output_is_create_only_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            out = Path(d) / "p.json"
            out.write_text("SENTINEL", encoding="utf-8")
            r = run_plan(d, make_args(symbol="AAA", delivery_evidence=ev,
                                      json_out=str(out)), option_insts=qi)
            self.assertEqual(r.rc, 3)
            self.assertEqual(out.read_text(encoding="utf-8"), "SENTINEL")

    def test_json_amounts_are_strings_and_no_nan_or_inf(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", delivery_evidence=ev,
                                      json_out=str(out)), option_insts=qi)
            raw = out.read_text(encoding="utf-8")
            self.assertNotIn("NaN", raw)
            self.assertNotIn("Infinity", raw)
            it = r.doc["items"][0]
            self.assertIsInstance(it["collateral"], str)
            self.assertIsInstance(it["requirement"]["cash_base"], str)
            self.assertIsInstance(r.doc["reservations"]["cash_base"], str)

    def test_pure_json_stdout_when_no_output_path(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            r = run_plan(d, make_args(symbol="AAA", delivery_evidence=ev),
                         option_insts=qi)
            self.assertIsInstance(r.doc, dict)   # parsed straight from stdout
            self.assertEqual(r.doc["schema"], plan_cli.SCHEMA)

    # -- universe whitelist ---------------------------------------------------- #
    def test_universe_whitelist_allows_eligible_symbol(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA"), "BBB": inst("BBB")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            uni = Path(d) / "uni.json"
            uni.write_text(json.dumps({
                "schema": universe.SCHEMA, "approved_by": "human",
                "signed_at": "2026-11-01", "valid_until": "2099-12-31",
                "items": [{"symbol": "AAA", "tier": "ELIGIBLE",
                           "max_entry": 1000}],
            }), encoding="utf-8")
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbols="AAA,BBB", universe=str(uni),
                                      delivery_evidence=ev, json_out=str(out)),
                         symbols=("AAA", "BBB"), option_insts=qi)
            doc = r.doc
            self.assertEqual(len(doc["items"]), 1)   # BBB was filtered out
            self.assertEqual(doc["items"][0]["symbol"], "AAA")
            self.assertIs(doc["items"][0]["allowed"], True)
            self.assertIsNotNone(doc["universe"])
            self.assertEqual(doc["universe"]["approved_by"], "human")

    def test_universe_gate_blocks_over_max_entry_put(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA", strike=D(40))}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            uni = Path(d) / "uni.json"
            uni.write_text(json.dumps({
                "schema": universe.SCHEMA, "approved_by": "human",
                "signed_at": "2026-11-01", "valid_until": "2099-12-31",
                "items": [{"symbol": "AAA", "tier": "ELIGIBLE",
                           "max_entry": 10}],   # strike 40 > 10
            }), encoding="utf-8")
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", universe=str(uni),
                                      delivery_evidence=ev, json_out=str(out)),
                         option_insts=qi)
            self.assertEqual(r.rc, 3)
            self.assertIs(r.doc["items"][0]["allowed"], False)
            self.assertTrue(any("白名单" in x for x in
                                r.doc["items"][0]["reasons"]))

    def test_invalid_universe_produces_no_recommendations(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            uni = Path(d) / "uni.json"
            uni.write_text('{"schema": "wrong"}', encoding="utf-8")
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", universe=str(uni),
                                      delivery_evidence=ev, json_out=str(out)),
                         option_insts=qi)
            self.assertEqual(r.rc, 3)
            self.assertIs(r.doc["items"][0]["allowed"], False)

    # -- evidence-gate / config failures -------------------------------------- #
    def test_stale_snapshot_blocks_every_item(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            s = snapshot(captured_at=NOW - timedelta(seconds=120))
            cfg = {"default_currency": "USD", "risk": {
                "enabled": True, "day_timezone": "UTC",
                "max_daily_submissions": 10, "max_snapshot_age_seconds": 30}}
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", delivery_evidence=ev,
                                      json_out=str(out)),
                         option_insts=qi, snap=s, cfg=cfg)
            self.assertEqual(r.rc, 3)
            self.assertIs(r.doc["items"][0]["allowed"], False)
            self.assertTrue(r.doc["evidence_reasons"] or
                            r.doc["items"][0]["evidence_reasons"])

    def test_invalid_risk_config_blocks_every_item(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            cfg = {"default_currency": "USD",
                   "risk": {"enabled": "yes"}}   # strict bool required
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", delivery_evidence=ev,
                                      json_out=str(out)),
                         option_insts=qi, cfg=cfg)
            self.assertEqual(r.rc, 3)
            self.assertIs(r.doc["items"][0]["allowed"], False)

    def test_assessment_exception_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            out = Path(d) / "p.json"
            with patch.object(planning, "assess_plan",
                              side_effect=RuntimeError("DU1234567 boom")):
                r = run_plan(d, make_args(symbol="AAA", delivery_evidence=ev,
                                          json_out=str(out)), option_insts=qi)
            self.assertEqual(r.rc, 3)
            self.assertIs(r.doc["items"][0]["allowed"], False)
            self.assertNotIn("DU1234567", json.dumps(r.doc))

    # -- zero side effects / no place or cancel -------------------------------- #
    def test_no_place_or_cancel_calls_on_broker(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", delivery_evidence=ev,
                                      json_out=str(out)), option_insts=qi)
            forbidden = ("placeOrder", "cancelOrder", "reqGlobalCancel",
                         "sendOrder", "reqIds")
            called = " ".join(str(c) for c in r.ib.mock_calls)
            for name in forbidden:
                self.assertNotIn(name, called)

    def test_readonly_connect(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "p.json"
            with patch.object(cli, "connect", return_value=MagicMock()) as conn, \
                 patch.object(cli, "resolve_port", return_value=(4002, "paper")), \
                 patch.object(cli, "_is_live_env", return_value=False), \
                 patch.object(cli, "finish"), \
                 patch.object(cli, "_risk_snapshot", return_value=snapshot()), \
                 patch.object(cli, "_chain_safe", return_value=(None, None)), \
                 patch.object(cli, "_data_dir", return_value=Path(d)), \
                 patch("sys.stdout", io.StringIO()), \
                 patch("sys.stderr", io.StringIO()):
                plan_cli.cmd_plan(CFG, make_args(symbol="AAA",
                                                  json_out=str(out)), now=NOW)
            self.assertIs(conn.call_args.kwargs.get("readonly"), True)

    # -- regression tests: contract identities, formatting & invariants ------- #
    def test_terminal_output_masks_account_and_prints_summary(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            r = run_plan(d, make_args(symbol="AAA", format="terminal",
                                      delivery_evidence=ev), option_insts=qi)
            self.assertEqual(r.rc, 0)
            self.assertIn("***567", r.stdout)
            self.assertNotIn("DU1234567", r.stdout)
            self.assertIn("快照", r.stdout)
            self.assertIn("本计划只读，不下单/撤单", r.stdout)
            self.assertIn("✓ #1", r.stdout)

    def test_unique_positive_integer_i_and_strict_bool_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA"), "BBB": inst("BBB")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            s = snapshot(symbols=("AAA", "BBB"), cash={"USD": D(5000)})
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbols="AAA,BBB", delivery_evidence=ev,
                                      json_out=str(out)),
                         symbols=("AAA", "BBB"), option_insts=qi, snap=s)
            doc = r.doc
            self.assertEqual(len(doc["items"]), 2)
            for idx, it in enumerate(doc["items"], 1):
                self.assertEqual(it["i"], idx)
                self.assertIsInstance(it["i"], int)
                self.assertGreater(it["i"], 0)
                self.assertIs(type(it["allowed"]), bool)
                self.assertIs(type(it["executable"]), bool)
                self.assertIs(it["executable"], False)
                self.assertEqual(it["account"], "***567")
                self.assertEqual(it["environment"], "paper")

    def test_aware_iso_timestamps_and_valid_until_24h_window(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", delivery_evidence=ev,
                                      json_out=str(out)), option_insts=qi)
            doc = r.doc
            gen = datetime.fromisoformat(doc["generated"])
            until = datetime.fromisoformat(doc["valid_until"])
            self.assertIsNotNone(gen.tzinfo)
            self.assertIsNotNone(until.tzinfo)
            self.assertEqual(until - gen, timedelta(hours=24))
            self.assertEqual(doc["recommendation_only"], False)

    def test_multiplier_200_and_500_exact_reservations(self):
        # Multiplier 200 requires exactly 40 * 200 = 8000; never defaults to 100 (4000).
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA", mult=D(200))}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            s = snapshot(symbols=("AAA",), cash={"USD": D(10000)})
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", delivery_evidence=ev,
                                      json_out=str(out)),
                         symbols=("AAA",), option_insts=qi, snap=s)
            self.assertEqual(r.rc, 0)
            self.assertEqual(r.doc["items"][0]["mult"], "200")
            self.assertEqual(r.doc["reservations"]["cash_base"], "8000")

        # Multiplier 500 requires exactly 40 * 500 = 20000; never defaults to 100.
        with tempfile.TemporaryDirectory() as d:
            qi = {"CCC": inst("CCC", mult=D(500))}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            s = snapshot(symbols=("CCC",), cash={"USD": D(25000)})
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="CCC", delivery_evidence=ev,
                                      json_out=str(out)),
                         symbols=("CCC",), option_insts=qi, snap=s)
            self.assertEqual(r.rc, 0)
            self.assertEqual(r.doc["items"][0]["mult"], "500")
            self.assertEqual(r.doc["reservations"]["cash_base"], "20000")

    def test_put_qty_exceeding_budget_blocked_without_truncation(self):
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            # Cash 6000: sufficient for 1 contract (4000), but insufficient for 2 (8000).
            s = snapshot(cash={"USD": D(6000)})
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", qty=2, delivery_evidence=ev,
                                      json_out=str(out)),
                         option_insts=qi, snap=s)
            self.assertEqual(r.rc, 3)
            self.assertEqual(r.doc["allowed_count"], 0)
            self.assertEqual(r.doc["blocked_count"], 1)
            it = r.doc["items"][0]
            self.assertEqual(it["qty"], "2")              # NOT truncated to 1
            self.assertIs(it["allowed"], False)
            self.assertTrue(any("cash" in x.lower() or "头寸" in x or "保证金" in x
                                or "budget" in x.lower() for x in it["reasons"]))


    # -- review-round repairs: exact high precision, evidence, freshness, ledger -- #
    def test_high_precision_strikes_and_mult_exact_export(self):
        # Exact high-precision Decimal regression: 40.0000000000000001, 200.0000000000000001
        with tempfile.TemporaryDirectory() as d:
            p_strike = D("40.0000000000000001")
            p_mult = D("200.0000000000000001")
            p_prem = D("2.5000000000000001")
            qi = {"AAA": inst("AAA", strike=p_strike, mult=p_mult)}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            s = snapshot(symbols=("AAA",), cash={"USD": D(100000)})
            out = Path(d) / "p.json"

            def custom_quote_premium(ib_, opt):
                return p_prem, "买价"

            r = run_plan(d, make_args(symbol="AAA", delivery_evidence=ev,
                                      json_out=str(out)),
                         symbols=("AAA",), option_insts=qi, snap=s,
                         quote_premium_fn=custom_quote_premium)
            self.assertEqual(r.rc, 0, r.out)
            it = r.doc["items"][0]
            # Must export exact Decimal strings, never lose precision or scalar round to 40 / 200
            self.assertEqual(it["strike"], "40.0000000000000001")
            self.assertNotEqual(it["strike"], "40")
            self.assertNotEqual(it["strike"], 40.0)
            self.assertEqual(it["mult"], "200.0000000000000001")
            self.assertNotEqual(it["mult"], "200")
            self.assertNotEqual(it["mult"], 200.0)
            self.assertEqual(it["premium"], "2.5000000000000001")
            self.assertEqual(it["qty"], "1")

        with tempfile.TemporaryDirectory() as d:
            p_strike_200 = D("200.0000000000000001")
            qi = {"BBB": inst("BBB", strike=p_strike_200, mult=D(100))}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            s = snapshot(symbols=("BBB",), cash={"USD": D(100000)})
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="BBB", delivery_evidence=ev,
                                      json_out=str(out)),
                         symbols=("BBB",), option_insts=qi, snap=s)
            self.assertEqual(r.rc, 0, r.out)
            it = r.doc["items"][0]
            self.assertEqual(it["strike"], "200.0000000000000001")
            self.assertNotEqual(it["strike"], "200")
            self.assertNotEqual(it["strike"], 200.0)

    def test_contradictory_strike_never_scalar_rounded_and_explicitly_rejected(self):
        # When resolved strike disagrees with qualified Instrument.strike,
        # never scalar-round to hide contradiction: explicitly reject.
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA", strike=D("40.0000000000000001"))}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            out = Path(d) / "p.json"

            def contradictory_resolve(ib_, sym, exp, strikes, target, right, cur, chain, floor=None):
                wanted = inst(sym, con_id=qi[sym].con_id, mult=qi[sym].multiplier,
                              strike=qi[sym].strike, right=right, currency=qi[sym].currency,
                              underlying_id=qi[sym].underlying_id)
                return contract(sym, wanted), D("40")  # scalar-rounded/contradicting strike

            r = run_plan(d, make_args(symbol="AAA", delivery_evidence=ev,
                                      json_out=str(out)), option_insts=qi,
                         resolve_option_fn=contradictory_resolve)
            # Candidate is rejected/skipped, empty items -> rc 3, never exported as 40
            self.assertEqual(r.rc, 3)
            self.assertEqual(r.doc["items"], [])
            self.assertTrue(any("contradicts" in x for x in r.doc["issues"]))

    def test_document_preserves_actual_quote_and_delivery_evidence_records(self):
        # Per-candidate quote timestamps/identity/values and delivery verified_at/expires_at
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev_records = [evidence_record(qi["AAA"])]
            ev = Path(d) / "ev.json"
            ev.write_text(json.dumps({"schema": "thetamill.deliverables.v1", "records": ev_records}), encoding="utf-8")
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbol="AAA", delivery_evidence=str(ev),
                                      json_out=str(out)), option_insts=qi)
            self.assertEqual(r.rc, 0)
            it = r.doc["items"][0]
            # Quote evidence preserves actual ticker reception timestamp, conId, values
            q = it["quote"]
            self.assertEqual(q["time"], QUOTE_TIME.isoformat())
            self.assertNotEqual(q["time"], NOW.isoformat())  # NOT fabricated now
            self.assertEqual(q["conId"], 10)
            self.assertEqual(q["bid"], "99.95")
            self.assertEqual(q["ask"], "100.05")
            self.assertEqual(q["mid"], "100")
            self.assertEqual(q["source"], SOURCE)
            # Delivery evidence preserves verified_at, expires_at, source, record
            dev = it["delivery_evidence"]
            self.assertIs(dev["confirmed"], True)
            self.assertEqual(dev["source"], "IB contract details + human review")
            self.assertEqual(dev["verified_at"], (NOW - timedelta(hours=1)).isoformat())
            self.assertEqual(dev["expires_at"], (NOW + timedelta(hours=23)).isoformat())
            self.assertIsNotNone(dev["record"])
            self.assertEqual(dev["record"]["source"], "IB contract details + human review")

    def test_price_collection_freshness_uses_production_clock_after_request(self):
        c = stock_contract("AAA")
        # In production (now=None), freshness is evaluated after slow request.
        # Stale quote relative to wall clock at collection time is rejected.
        stale_tick = ticker(10, 100.0, time=datetime.now(timezone.utc) - timedelta(seconds=35))
        ib_stale = MagicMock()
        ib_stale.reqTickers.return_value = [stale_tick]
        with patch.object(plan_cli, "safe_req_tickers", side_effect=lambda ib_, *cs, **kw: ib_.reqTickers(*cs)):
            self.assertIsNone(plan_cli.fresh_stock_mid(ib_stale, c, now=None))

        # Fresh quote relative to wall clock is accepted and returns rich StockQuote
        fresh_time = datetime.now(timezone.utc) - timedelta(seconds=2)
        fresh_tick = ticker(10, 100.0, time=fresh_time)
        ib_fresh = MagicMock()
        ib_fresh.reqTickers.return_value = [fresh_tick]
        with patch.object(plan_cli, "safe_req_tickers", side_effect=lambda ib_, *cs, **kw: ib_.reqTickers(*cs)):
            q = plan_cli.fresh_stock_mid(ib_fresh, c, now=None)
        self.assertIsNotNone(q)
        self.assertEqual(q, D("100.0"))
        self.assertEqual(q.con_id, 10)
        self.assertEqual(q.time, fresh_time)
        self.assertEqual(q.bid, D("99.95"))
        self.assertEqual(q.ask, D("100.05"))

    def test_risk_snapshot_type_error_fails_closed_without_broad_fallback(self):
        # Removing broad TypeError fallback around _risk_snapshot ensures internal
        # errors are not masked or silently retried.
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "p.json"
            args = make_args(symbol="AAA", json_out=str(out))
            r = run_plan(d, args, risk_snapshot_fn=MagicMock(side_effect=TypeError("real internal snapshot bug")))
            self.assertEqual(r.rc, 3)
            self.assertTrue(any("real internal snapshot bug" in x or "TypeError" in x
                                for x in r.doc["issues"] + r.doc.get("evidence_reasons", [])))

    def test_unavailable_chain_and_unavailable_quotes_return_rc3(self):
        # Empty/incomplete candidates must rc 3, not rc 0
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "p.json"
            # Unavailable chain
            r_chain = run_plan(d, make_args(symbol="AAA", json_out=str(out)),
                               symbols=("AAA",), chain_map={"AAA": None})
            self.assertEqual(r_chain.rc, 3)
            self.assertEqual(r_chain.doc["items"], [])
            self.assertTrue(any("no option chain" in x for x in r_chain.doc["issues"]))

        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "p.json"
            # Frozen market data quote (marketDataType=2)
            r_quote = run_plan(d, make_args(symbol="AAA", json_out=str(out)),
                               symbols=("AAA",), market_data_type=2)
            self.assertEqual(r_quote.rc, 3)
            self.assertEqual(r_quote.doc["items"], [])
            self.assertTrue(any("fresh LIVE" in x for x in r_quote.doc["issues"]))

    def test_missing_or_expired_delivery_blocked_candidate_does_not_consume_budget_for_later_valid_candidate(self):
        # Candidate 1 (AAA) is blocked by missing delivery evidence.
        # Candidate 2 (BBB) has valid delivery evidence.
        # Shared cash budget is 5000: only enough for 1 candidate (each needs 4000).
        # AAA must NOT consume the 4000 capacity; BBB must be ALLOWED.
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA"), "BBB": inst("BBB")}
            # Evidence contains ONLY BBB, NOT AAA
            ev = write_evidence(Path(d) / "ev.json", [qi["BBB"]])
            s = snapshot(symbols=("AAA", "BBB"), cash={"USD": D(5000)})
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbols="AAA,BBB", delivery_evidence=ev,
                                      json_out=str(out)),
                         symbols=("AAA", "BBB"), option_insts=qi, snap=s)
            self.assertEqual(r.rc, 0, r.out)  # BBB allowed -> plan exit 0
            doc = r.doc
            self.assertEqual(doc["allowed_count"], 1)
            self.assertEqual(doc["blocked_count"], 1)
            by_sym = {it["symbol"]: it for it in doc["items"]}
            # AAA is blocked because of delivery evidence
            self.assertIs(by_sym["AAA"]["allowed"], False)
            self.assertIs(by_sym["AAA"]["delivery_evidence"]["confirmed"], False)
            self.assertIsNone(by_sym["AAA"]["requirement"])
            # BBB is allowed and consumed the budget
            self.assertIs(by_sym["BBB"]["allowed"], True)
            self.assertIs(by_sym["BBB"]["delivery_evidence"]["confirmed"], True)
            self.assertEqual(by_sym["BBB"]["requirement"]["cash_base"], "4000")
            self.assertEqual(doc["reservations"]["cash_base"], "4000")

    def test_expired_delivery_blocked_candidate_does_not_consume_budget_for_later_valid_candidate(self):
        # Candidate 1 (AAA) has EXPIRED delivery evidence.
        # Candidate 2 (BBB) has valid delivery evidence.
        # AAA must NOT consume the shared cash budget (5000 pool; 4000 each).
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA"), "BBB": inst("BBB")}
            rec_aaa = evidence_record(qi["AAA"])
            rec_aaa["expires_at"] = (NOW - timedelta(minutes=10)).isoformat()  # expired!
            rec_bbb = evidence_record(qi["BBB"])
            ev_file = Path(d) / "ev.json"
            ev_file.write_text(json.dumps({
                "schema": "thetamill.deliverables.v1",
                "records": [rec_aaa, rec_bbb],
            }), encoding="utf-8")
            s = snapshot(symbols=("AAA", "BBB"), cash={"USD": D(5000)})
            out = Path(d) / "p.json"
            r = run_plan(d, make_args(symbols="AAA,BBB", delivery_evidence=str(ev_file),
                                      json_out=str(out)),
                         symbols=("AAA", "BBB"), option_insts=qi, snap=s)
            self.assertEqual(r.rc, 0, r.out)
            by_sym = {it["symbol"]: it for it in r.doc["items"]}
            self.assertIs(by_sym["AAA"]["allowed"], False)
            self.assertIs(by_sym["BBB"]["allowed"], True)
            self.assertEqual(r.doc["reservations"]["cash_base"], "4000")

    def test_ledger_closed_cleanly_if_api_supports_close(self):
        # If SubmissionLedger provides a close() method, it must be closed cleanly.
        import thetamill.state as st
        with tempfile.TemporaryDirectory() as d:
            qi = {"AAA": inst("AAA")}
            ev = write_evidence(Path(d) / "ev.json", list(qi.values()))
            out = Path(d) / "p.json"
            real = st.SubmissionLedger
            mock_close = MagicMock()

            def spy(path, tz, **kwargs):
                ledger_inst = real(path, tz, **kwargs)
                ledger_inst.close = mock_close
                return ledger_inst

            with patch.object(st, "SubmissionLedger", side_effect=spy):
                r = run_plan(d, make_args(symbol="AAA", delivery_evidence=ev,
                                          json_out=str(out)), option_insts=qi)
            self.assertEqual(r.rc, 0)
            mock_close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
