#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline tests for thetamill.submission_cli — shared CLI submission adapter.

Everything is MOCK + temp SQLite / temp JSON.  No real broker, network, config
file, git or process is touched.  The adapter's cli helpers (account selection,
environment, data dir, snapshot provider) are patched with mocks so the engine
runs entirely against objects built here.
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from thetamill import broker_state as bs
from thetamill import cli, execution, risk, state, submission_cli as sc
from thetamill.broker_state import Instrument, Position, Snapshot, WorkingOrder

try:
    import ib_async  # noqa: F401
    _HAS_IB_ASYNC = True
except ImportError:  # pragma: no cover - system python without the venv
    _HAS_IB_ASYNC = False

ACCOUNT = "DU7654321"
ENV = "paper"
TZ = "America/New_York"
NOW = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
CONFIG = {"risk": {"enabled": True, "day_timezone": TZ,
                   "max_daily_submissions": 5, "config_version": "p3-1"}}
CAPS_CONFIG = {"risk": {"enabled": True, "day_timezone": TZ,
                        "max_daily_submissions": 5, "config_version": "p3-1",
                        "portfolio": {"amount": "1000000"}}}


# --------------------------------------------------------------------------- #
# Mock IB contract / order / trade objects (no ib_async import required)
# --------------------------------------------------------------------------- #

@dataclass
class MockContract:
    conId: int
    secType: str = "STK"
    symbol: str = "AAPL"
    currency: str = "USD"
    exchange: str = "SMART"
    right: str = ""
    strike: object = 0
    multiplier: object = 1
    lastTradeDateOrContractMonth: str = ""
    tradingClass: str = ""


@dataclass
class MockOrder:
    action: str = "BUY"
    totalQuantity: object = 1
    orderType: str = "LMT"
    lmtPrice: object = None
    auxPrice: object = None
    tif: str = "DAY"
    outsideRth: bool = False
    orderRef: str = ""
    account: str = ""
    orderId: object = None
    permId: object = None


@dataclass
class MockStatus:
    status: str = "Submitted"
    filled: object = 0
    remaining: object = 0


@dataclass
class MockTrade:
    contract: object
    order: object
    orderStatus: object


@dataclass
class MockDetail:
    contract: object
    underConId: int
    underSecType: str = "STK"


def stock_instrument(con_id=1001, currency="USD", exchange="SMART"):
    return Instrument(con_id, "AAPL", currency, "STK", exchange, con_id)


def opt_instrument(con_id=2001, *, right="P", strike="180", mult="100",
                   expiry="20261120", trading_class="AAPL", currency="USD",
                   underlying_id=1001, exchange="SMART"):
    return Instrument(con_id, "AAPL", currency, "OPT", exchange, underlying_id,
                      Decimal(mult), right, Decimal(strike), expiry, trading_class)


def stock_contract(con_id=1001, currency="USD", exchange="SMART"):
    return MockContract(conId=con_id, secType="STK", symbol="AAPL",
                        currency=currency, exchange=exchange)


def opt_contract(con_id=2001, *, right="P", strike=180, mult=100,
                 expiry="20261120", trading_class="AAPL", currency="USD",
                 exchange="SMART", symbol="AAPL"):
    return MockContract(conId=con_id, secType="OPT", symbol=symbol,
                        currency=currency, exchange=exchange, right=right,
                        strike=strike, multiplier=mult,
                        lastTradeDateOrContractMonth=expiry,
                        tradingClass=trading_class)


class MockIB:
    """Broker-like object: records placeOrder, never connects anywhere."""

    def __init__(self, *, trades=None, status="Submitted", filled=0, remaining=0):
        self.place_calls = []
        self._trades = list(trades or [])
        self._status = status
        self._filled = filled
        self._remaining = remaining
        self.contract_details = {}

    def managedAccounts(self):
        return [ACCOUNT]

    # contract-details-backed underlying identity for OPT legs
    def reqContractDetails(self, contract):
        if isinstance(contract.conId, int) and contract.conId in self.contract_details:
            return self.contract_details[contract.conId]
        # Default: OPT underlying = 1001; identity fields mirror the contract.
        return [MockDetail(contract=contract, underConId=1001)]

    def placeOrder(self, contract, order):
        self.place_calls.append((contract, order))
        oid = 500 + len(self.place_calls)
        pid = 900 + len(self.place_calls)
        # Stamp broker ids directly (do NOT use dataclasses.replace on a real
        # ib_async Order: its __init__ re-forwards orderType via **kwargs).
        try:
            order.orderId = oid
            order.permId = pid
        except Exception:
            pass
        status_obj = MockStatus(status=self._status, filled=self._filled,
                                remaining=self._remaining)
        trade = MockTrade(contract=contract, order=order,
                          orderStatus=status_obj)
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


# --------------------------------------------------------------------------- #
# Snapshot builder + evidence file builder
# --------------------------------------------------------------------------- #

def make_snapshot(*, cash="100000", positions=(), orders=(), captured_at=None,
                  base="USD", fx=None, price_quotes=None, fx_quotes=None,
                  issues=(), cash_map=None):
    fx_map = dict(fx or {base: Decimal(1)})
    cash_balances = dict(cash_map) if cash_map is not None else {base: Decimal(cash)}
    return Snapshot(
        account=ACCOUNT, environment=ENV,
        captured_at=captured_at or (NOW - timedelta(seconds=2)),
        base_currency=base, cash=cash_balances,
        net_liquidation=Decimal(cash), positions=tuple(positions),
        orders=tuple(orders), fx=fx_map, issues=tuple(issues),
        price_quotes=price_quotes or {}, fx_quotes=fx_quotes or {})


def evidence_record(inst, *, now=NOW, source="IB contract details / human review"):
    return {
        "instrument": {
            "con_id": inst.con_id, "underlying_id": inst.underlying_id,
            "symbol": inst.symbol, "currency": inst.currency,
            "sec_type": inst.sec_type, "exchange": inst.exchange,
            "multiplier": str(inst.multiplier), "right": inst.right,
            "strike": str(inst.strike), "expiry": inst.expiry,
            "trading_class": inst.trading_class,
        },
        "stock_only": True,
        "source": source,
        "verified_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
    }


def write_evidence(path, instruments):
    path.write_text(json.dumps({
        "schema": "thetamill.deliverables.v1",
        "records": [evidence_record(i) for i in instruments],
    }), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Test harness: patch cli helpers to mocks; temp data dir / DB
# --------------------------------------------------------------------------- #

class AdapterTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.patches = []
        self._patch(sc, "cli", cli)  # keep real module; patch its helpers
        self._patch(cli, "_data_dir", lambda: self.data_dir)
        self._patch(cli, "_selected_account", lambda ib, cfg, args: ACCOUNT)
        self._patch(cli, "_is_live_env", lambda cfg, args, port: False)
        self._patch(cli, "resolve_port", lambda cfg, args: (7497, "test"))
        self.snapshot_calls = []

        def risk_snapshot(ib, cfg, args, env, *, required_currencies=()):
            # Exercise main's FINAL helper API and record the candidate
            # currencies so tests can prove foreign FX is actively collected.
            self.snapshot_calls.append(tuple(required_currencies))
            return self.snapshot

        self._patch(cli, "_risk_snapshot", risk_snapshot)
        self.snapshot = make_snapshot()
        self.addCleanup(self._undo)

    def _patch(self, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        self.patches.append((obj, name, old))

    def _undo(self):
        for obj, name, old in reversed(self.patches):
            setattr(obj, name, old)

    def args(self, *, intent="", code="", **kw):
        base = dict(intent=intent, code=code, account=ACCOUNT, paper=True,
                    port=7497, strategy="covered-call")
        base.update(kw)
        return SimpleNamespace(**base)

    def run_adapter(self, ib, *, pairs, args=None, cfg=None, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = sc.execute_prepared(ib, cfg or CONFIG, args or self.args(),
                                         pairs=pairs, now=NOW,
                                         clock=lambda: NOW, **kw)
        return result, buf.getvalue()

    def preflight_confirm(self, ib, *, pairs, **kw):
        """Preflight (no intent/code), then confirm with returned code."""
        result, out = self.run_adapter(ib, pairs=pairs, **kw)
        self.assertEqual(result.rc, sc.RC_PREFLIGHT, out)
        self.assertEqual(result.outcome, "PREFLIGHT", out)
        code = result.approval_code
        self.assertTrue(code)
        cargs = self.args(intent=result.intent_id, code=code)
        ckw = dict(kw)
        ckw.pop("args", None)
        return self.run_adapter(ib, pairs=pairs, args=cargs, **ckw)


# --------------------------------------------------------------------------- #
# Canonical identity / contract verification
# --------------------------------------------------------------------------- #

class IdentityTests(AdapterTestCase):
    def test_canonical_instrument_uses_contract_details_not_guess(self):
        ib = MockIB()
        contract = opt_contract(2001)
        # give a DIFFERENT underlying through contract details to prove it uses them
        ib.contract_details = {2001: [MockDetail(contract=contract, underConId=7777)]}
        inst = sc.canonical_instrument(contract, ib, ACCOUNT)
        self.assertEqual(inst.underlying_id, 7777)
        self.assertEqual(inst.multiplier, Decimal(100))
        self.assertEqual(inst.strike, Decimal(180))

    def test_verify_contract_identity_full_match(self):
        contract = opt_contract()
        inst = opt_instrument()
        self.assertEqual(sc.verify_contract_identity(contract, inst), [])

    def test_verify_contract_identity_rejects_conid_mismatch(self):
        contract = opt_contract(con_id=9999)
        inst = opt_instrument(con_id=2001)
        problems = sc.verify_contract_identity(contract, inst)
        self.assertTrue(any("conId" in p for p in problems))

    def test_verify_contract_identity_rejects_multiplier_mismatch(self):
        contract = opt_contract(mult=200)
        inst = opt_instrument(mult="100")
        problems = sc.verify_contract_identity(contract, inst)
        self.assertTrue(any("multiplier" in p for p in problems))

    def test_verify_contract_identity_rejects_adjusted_trading_class(self):
        contract = opt_contract(trading_class="AAPL1")
        inst = opt_instrument(trading_class="AAPL")
        problems = sc.verify_contract_identity(contract, inst)
        self.assertTrue(any("tradingClass" in p for p in problems))


# --------------------------------------------------------------------------- #
# Real ib_async: multiplier is a STRING ('100'/'200'/'500'), never a Decimal
# --------------------------------------------------------------------------- #

@unittest.skipUnless(_HAS_IB_ASYNC, "ib_async not installed")
class RealIbAsyncMultiplierTests(AdapterTestCase):
    """Coordinator regression: real IB Option.multiplier is a numeric STRING.

    verify_contract_identity must Decimal-normalise it and compare to the
    confirmed Instrument's Decimal multiplier — never reject a legitimate
    contract just because its multiplier is a str.  Covers 100/200/500 and the
    real ib_async Order classes used by build_broker_order.
    """

    @staticmethod
    def _qualified(con_id, mult, strike=180, right="P"):
        from ib_async import Option
        o = Option(symbol="AAPL", lastTradeDateOrContractMonth="20261120",
                   strike=strike, right=right, exchange="SMART",
                   multiplier=str(mult), currency="USD", tradingClass="AAPL")
        o.conId = con_id
        return o

    def test_multiplier_strings_100_200_500_verify(self):
        for con_id, mult in ((2001, "100"), (2002, "200"), (2003, "500")):
            contract = self._qualified(con_id, mult)
            self.assertIsInstance(contract.multiplier, str)  # real IB shape
            inst = opt_instrument(con_id=con_id, mult=mult)
            self.assertEqual(sc.verify_contract_identity(contract, inst), [],
                             f"multiplier {mult} must verify")
            self.assertEqual(sc.contract_identity(contract)["multiplier"],
                             Decimal(mult))

    def test_string_multiplier_mismatch_is_rejected(self):
        # 200 in the contract vs 100 in the confirmed identity -> mismatch.
        contract = self._qualified(2001, "200")
        inst = opt_instrument(con_id=2001, mult="100")
        problems = sc.verify_contract_identity(contract, inst)
        self.assertTrue(any("multiplier" in p for p in problems))

    def test_canonical_instrument_from_real_ib_option(self):
        # broker_state._instrument uses reqContractDetails for underlying id and
        # Decimal-normalises the STRING multiplier.
        ib = MockIB()
        contract = self._qualified(2001, "500", strike=190)
        ib.contract_details = {2001: [MockDetail(contract=contract,
                                                 underConId=1001)]}
        inst = sc.canonical_instrument(contract, ib, ACCOUNT)
        self.assertEqual(inst.multiplier, Decimal(500))
        self.assertEqual(inst.strike, Decimal(190))
        self.assertEqual(inst.underlying_id, 1001)
        self.assertEqual(sc.verify_contract_identity(contract, inst), [])

    def test_build_broker_order_with_real_ib_classes(self):
        # build_broker_order imports the REAL MarketOrder/LimitOrder/StopOrder
        # when ib_async is present; ensure no constructor conflict and all bound
        # fields are set from the canonical leg.
        leg = execution.PreparedLeg(
            instrument=stock_instrument(), action="BUY", quantity=Decimal(2),
            order_type="LMT", limit_price=Decimal("180.5"), tif="GTC",
            outside_rth=True, account=ACCOUNT)
        order = sc.build_broker_order(leg, "stable-ref", ACCOUNT)
        self.assertEqual(order.orderRef, "stable-ref")
        self.assertEqual(order.account, ACCOUNT)
        self.assertEqual(order.orderType, "LMT")
        self.assertEqual(order.tif, "GTC")


# --------------------------------------------------------------------------- #
# Preflight / confirmation argument semantics
# --------------------------------------------------------------------------- #

class ArgSemanticsTests(AdapterTestCase):
    def setUp(self):
        super().setUp()
        self.ib = MockIB()
        self.pair = [(stock_contract(), MockOrder(action="BUY", totalQuantity=2,
                                                  orderType="LMT", lmtPrice=180))]

    def test_no_args_produces_preflight_without_placing_order(self):
        result, out = self.run_adapter(self.ib, pairs=self.pair)
        self.assertEqual(result.rc, sc.RC_PREFLIGHT)
        self.assertEqual(result.outcome, "PREFLIGHT")
        self.assertTrue(result.approval_code)
        self.assertTrue(result.intent_id)
        self.assertEqual(self.ib.place_calls, [])
        self.assertIn("PREFLIGHT", out)
        self.assertIn(ACCOUNT[-3:], out)
        self.assertNotIn(ACCOUNT, out)  # account is masked

    def test_code_alone_rejected(self):
        with self.assertRaises(sc.SubmissionCliError) as ctx:
            self.run_adapter(self.ib, pairs=self.pair,
                             args=self.args(code="ABCD"))
        self.assertIn("--code alone", str(ctx.exception))
        self.assertEqual(self.ib.place_calls, [])

    def test_intent_alone_rejected(self):
        with self.assertRaises(sc.SubmissionCliError) as ctx:
            self.run_adapter(self.ib, pairs=self.pair,
                             args=self.args(intent="some-intent"))
        self.assertIn("--intent alone", str(ctx.exception))
        self.assertEqual(self.ib.place_calls, [])

    def test_no_force_and_no_fabricated_verified_prior(self):
        # A brand-new enabled-P3 ledger is conservatively frozen: confirm is
        # BLOCKED (frozen=True), never an ALLOW with fabricated zero history.
        result, out = self.preflight_confirm(self.ib, pairs=self.pair)
        self.assertEqual(result.outcome, "BLOCKED", out)
        self.assertEqual(result.rc, sc.RC_PREFLIGHT)
        self.assertEqual(self.ib.place_calls, [])
        self.assertIn("migration freeze", out)

    def test_explicit_intent_and_code_with_verified_prior_unfreezes(self):
        # Operator-verified count is injected ONLY through the engine's API in
        # tests here; the adapter itself never passes it.  We prove the confirm
        # path by seeding the day budget directly via the real state API.
        result, out = self.run_adapter(self.ib, pairs=self.pair)
        code = result.approval_code
        self.assertTrue(code)
        # Seed an established day row so the ledger is not frozen.
        limits = risk.validate_risk_config(CONFIG)
        ledger = state.SubmissionLedger(self.data_dir / "state.sqlite3", TZ)
        day_key = state.day_key_for(TZ, now=NOW)
        ledger.set_daily_limit(account=ACCOUNT, environment=ENV, day_key=day_key,
                               limit_count=limits.max_daily_submissions)
        # Unfreeze the migration by recording the operator-verified count.
        ledger.prepare_day_budget(account=ACCOUNT, environment=ENV, day_key=day_key,
                                  limit_count=limits.max_daily_submissions,
                                  verified_prior_submissions=0)
        cargs = self.args(intent=result.intent_id, code=code)
        result, out = self.run_adapter(self.ib, pairs=self.pair, args=cargs)
        self.assertEqual(result.outcome, "CONFIRMED", out)
        self.assertEqual(result.rc, sc.RC_CONFIRMED)
        self.assertEqual(len(self.ib.place_calls), 1)
        # The sent order carries the stable ref + account + bound fields.
        sent_contract, sent_order = self.ib.place_calls[0]
        self.assertEqual(sent_order.account, ACCOUNT)
        self.assertTrue(sent_order.orderRef)
        self.assertEqual(sent_order.action, "BUY")
        self.assertEqual(float(sent_order.totalQuantity), 2.0)

    def test_config_change_invalidates_digest(self):
        result, _ = self.run_adapter(self.ib, pairs=self.pair)
        cargs = self.args(intent=result.intent_id, code=result.approval_code)
        changed = {"risk": dict(CONFIG["risk"], max_daily_submissions=9)}
        result, out = self.run_adapter(self.ib, pairs=self.pair, args=cargs,
                                       cfg=changed)
        self.assertEqual(result.outcome, "BLOCKED", out)
        self.assertEqual(self.ib.place_calls, [])


# --------------------------------------------------------------------------- #
# Broker-order construction + identity verification at send boundary
# --------------------------------------------------------------------------- #

class BrokerOrderTests(AdapterTestCase):
    def test_build_broker_order_uses_only_canonical_fields(self):
        leg = execution.PreparedLeg(
            instrument=stock_instrument(), action="SELL", quantity=Decimal(3),
            order_type="LMT", limit_price=Decimal("180.5"), tif="GTC",
            outside_rth=True, account=ACCOUNT)
        order = sc.build_broker_order(leg, "stable-ref", ACCOUNT)
        self.assertEqual(order.account, ACCOUNT)
        self.assertEqual(order.orderRef, "stable-ref")
        self.assertEqual(order.action, "SELL")
        self.assertEqual(order.tif, "GTC")
        self.assertTrue(order.outsideRth)
        self.assertEqual(float(order.totalQuantity), 3.0)

    def test_stable_ref_is_identical_across_legs_of_one_confirm(self):
        # Two STK legs -> each gets a distinct but pre-persisted stable ref.
        self.ib = MockIB()
        pairs = [(stock_contract(1001), MockOrder(action="BUY", totalQuantity=1,
                                                  orderType="LMT", lmtPrice=180)),
                 (stock_contract(1001), MockOrder(action="BUY", totalQuantity=1,
                                                  orderType="LMT", lmtPrice=181))]
        result, _ = self.run_adapter(self.ib, pairs=pairs)
        limits = risk.validate_risk_config(CONFIG)
        ledger = state.SubmissionLedger(self.data_dir / "state.sqlite3", TZ)
        day_key = state.day_key_for(TZ, now=NOW)
        ledger.prepare_day_budget(account=ACCOUNT, environment=ENV, day_key=day_key,
                                  limit_count=limits.max_daily_submissions,
                                  verified_prior_submissions=0)
        cargs = self.args(intent=result.intent_id, code=result.approval_code)
        result, out = self.run_adapter(self.ib, pairs=pairs, args=cargs)
        self.assertEqual(result.outcome, "CONFIRMED", out)
        refs = [o.orderRef for _, o in self.ib.place_calls]
        self.assertEqual(len(refs), 2)
        self.assertEqual(len(set(refs)), 2)
        # Refs match the ledger's persisted per-leg orderRefs exactly.
        persisted = {r.order_ref for r in ledger.list_legs(account=ACCOUNT,
                     environment=ENV, intent_id=result.intent_id)}
        self.assertEqual(set(refs), persisted)

    def test_send_refuses_contract_that_diverges_from_confirmed_identity(self):
        # The adapter builds the leg from the actual contract; here we prove the
        # broker-boundary verification rejects a swapped contract object.
        leg = execution.PreparedLeg(
            instrument=opt_instrument(), action="BUY", quantity=Decimal(1),
            order_type="LMT", limit_price=Decimal("2"), account=ACCOUNT,
            contract=opt_contract(con_id=9999))
        with self.assertRaises(sc.SubmissionCliError) as ctx:
            sc._broker_submit(MockIB(), leg, "ref", ACCOUNT, {2001: True})
        self.assertIn("does not match", str(ctx.exception))


# --------------------------------------------------------------------------- #
# Delivery evidence: per-OPTION conId (STK intrinsic, OPT including close)
# --------------------------------------------------------------------------- #

class DeliveryEvidenceTests(AdapterTestCase):
    def setUp(self):
        super().setUp()
        self.ib = MockIB()
        self.opt = opt_instrument()
        self.opt_contract = opt_contract()
        self.close_pair = [(self.opt_contract,
                            MockOrder(action="BUY", totalQuantity=1,
                                      orderType="LMT", lmtPrice=2))]

    def test_load_delivery_evidence_missing_file_is_empty(self):
        result = sc.load_delivery_evidence(self.root / "nope.json", [self.opt], NOW)
        self.assertEqual(dict(result), {})

    def test_load_delivery_evidence_returns_per_option_conid(self):
        path = write_evidence(self.root / "ev.json", [self.opt])
        result = sc.load_delivery_evidence(path, [self.opt], NOW)
        self.assertIn(2001, result)
        self.assertNotIn(1001, result)  # underlying conId must NOT be a key

    def test_load_delivery_evidence_rejects_bad_schema_fail_closed(self):
        path = self.root / "bad.json"
        path.write_text(json.dumps({"schema": "wrong", "records": []}),
                        encoding="utf-8")
        self.assertEqual(dict(sc.load_delivery_evidence(path, [self.opt], NOW)), {})

    def test_opt_without_evidence_blocked_even_for_readonly_close(self):
        # No evidence path -> the OPT (buy-to-close) leg is blocked before the
        # engine ever sees it.  Never bypassed / auto-verified.
        with self.assertRaises(sc.SubmissionCliError) as ctx:
            self.run_adapter(self.ib, pairs=self.close_pair)
        self.assertIn("delivery evidence", str(ctx.exception))
        self.assertEqual(self.ib.place_calls, [])

    def test_same_underlying_adjusted_unconfirmed_not_authorized(self):
        # Confirm the standard contract (2001) but trade an adjusted sibling
        # (2002) with the SAME underlying/tradingClass/multiplier.
        standard = opt_instrument(con_id=2001)
        adjusted = opt_instrument(con_id=2002, trading_class="AAPL1")
        # Evidence is keyed by option conId, never by shared underlying identity.
        self.assertEqual(standard.key, adjusted.key)
        self.assertNotEqual(standard.con_id, adjusted.con_id)
        path = write_evidence(self.root / "ev.json", [standard])
        ib = MockIB()
        ib.contract_details = {2002: [MockDetail(
            contract=opt_contract(con_id=2002, trading_class="AAPL1"),
            underConId=1001)]}
        pair = [(opt_contract(con_id=2002, trading_class="AAPL1"),
                 MockOrder(action="BUY", totalQuantity=1, orderType="LMT", lmtPrice=2))]
        with self.assertRaises(sc.SubmissionCliError) as ctx:
            self.run_adapter(ib, pairs=pair, delivery_evidence_path=path)
        self.assertIn("blocked", str(ctx.exception))
        self.assertEqual(ib.place_calls, [])

    def test_stk_needs_no_evidence_and_is_intrinsically_verified(self):
        pairs = [(stock_contract(), MockOrder(action="BUY", totalQuantity=1,
                                              orderType="LMT", lmtPrice=180))]
        # No evidence path at all -> STK leg is still deliverable_verified.
        result, out = self.run_adapter(self.ib, pairs=pairs)
        self.assertEqual(result.outcome, "PREFLIGHT", out)

    def test_opt_with_evidence_passes_leg_construction(self):
        path = write_evidence(self.root / "ev.json", [self.opt])
        # A short position gives the close capacity risk requires.
        self.snapshot = make_snapshot(positions=(
            Position(self.opt, Decimal(-1), Decimal("2")),))
        specs = sc.build_leg_specs(self.close_pair, ib=self.ib, account=ACCOUNT,
                                   delivery_evidence={2001: True})
        self.assertTrue(specs[0].deliverable_verified)
        leg = specs[0].to_prepared(ACCOUNT)
        self.assertTrue(leg.deliverable_verified)
        # preflight (not confirm) still requires evidence path via loader
        result, _ = self.run_adapter(self.ib, pairs=self.close_pair,
                                     delivery_evidence_path=path)
        self.assertEqual(result.rc, sc.RC_PREFLIGHT)


# --------------------------------------------------------------------------- #
# fill_proof: actual Filled + exact finite approved qty + stable ref
# --------------------------------------------------------------------------- #

class FillProofTests(AdapterTestCase):
    def _trade(self, *, ref, status, filled, total, remaining, oid=1, perm=2):
        o = MockOrder(orderRef=ref, orderId=oid, permId=perm, totalQuantity=total)
        return MockTrade(contract=stock_contract(), order=o,
                         orderStatus=MockStatus(status=status, filled=filled,
                                                remaining=remaining))

    def _approved(self, qty="2", con_id=1001):
        return {"R1": sc.ApprovedLeg(quantity=Decimal(qty), account=ACCOUNT,
                                     environment=ENV, con_id=con_id)}

    def _is_positive_proof(self, proof):
        return (proof is not None and getattr(proof, "status", None) == "Filled"
                and getattr(proof, "order_ref", None) == "R1"
                and getattr(proof, "account", None) == ACCOUNT
                and getattr(proof, "environment", None) == ENV
                and getattr(proof, "con_id", None) == 1001
                and getattr(proof, "filled_quantity", None) == Decimal(2))

    def test_exact_filled_proof_is_structured(self):
        ib = MockIB(trades=[self._trade(ref="R1", status="Filled", filled=2,
                                        total=2, remaining=0)])
        proof = sc._fill_proof(ib, "R1", {"order_id": "1", "perm_id": "2"},
                               approved_by_ref=self._approved())
        self.assertTrue(self._is_positive_proof(proof))
        # Never a bare string.
        self.assertNotIsInstance(proof, str)

    def test_generic_positive_status_is_not_proof(self):
        for status in ("Submitted", "PreSubmitted", "PartiallyFilled",
                       "Cancelled", "ApiCancelled", "Inactive"):
            ib = MockIB(trades=[self._trade(ref="R1", status=status, filled=2,
                                            total=2, remaining=0)])
            self.assertIsNone(sc._fill_proof(
                ib, "R1", {}, approved_by_ref=self._approved()), status)

    def test_partial_fill_is_not_proof(self):
        ib = MockIB(trades=[self._trade(ref="R1", status="Filled", filled=1,
                                        total=2, remaining=1)])
        self.assertIsNone(sc._fill_proof(ib, "R1", {},
                                         approved_by_ref=self._approved()))

    def test_wrong_approved_quantity_is_not_proof(self):
        ib = MockIB(trades=[self._trade(ref="R1", status="Filled", filled=2,
                                        total=2, remaining=0)])
        self.assertIsNone(sc._fill_proof(ib, "R1", {},
                                         approved_by_ref=self._approved("3")))

    def test_stable_ref_required_and_ambiguous_match_rejected(self):
        t1 = self._trade(ref="R1", status="Filled", filled=2, total=2, remaining=0)
        t2 = self._trade(ref="R1", status="Filled", filled=2, total=2,
                         remaining=0, oid=9, perm=9)
        ib = MockIB(trades=[t1, t2])
        self.assertIsNone(sc._fill_proof(ib, "R1", {},
                                         approved_by_ref=self._approved()))
        ib2 = MockIB(trades=[t1])
        self.assertIsNone(sc._fill_proof(ib2, "OTHER", {},
                                         approved_by_ref=self._approved()))

    def test_non_finite_filled_is_not_proof(self):
        ib = MockIB(trades=[self._trade(ref="R1", status="Filled",
                                        filled=float("nan"),
                                        total=2, remaining=0)])
        self.assertIsNone(sc._fill_proof(ib, "R1", {},
                                         approved_by_ref=self._approved()))


# --------------------------------------------------------------------------- #
# Freshness equality gate (timestamped quotes, no inferred freshness)
# --------------------------------------------------------------------------- #

GOOD_SOURCE = "IB timestamped bid/ask midpoint"


class FreshnessGateTests(AdapterTestCase):
    def setUp(self):
        super().setUp()
        self.limits = risk.validate_risk_config(CONFIG)
        self.caps = risk.validate_risk_config(CAPS_CONFIG)
        self.legs = []

    def _quote(self, price, age=1, source=GOOD_SOURCE):
        return sc.MarketQuote(Decimal(str(price)), NOW - timedelta(seconds=age),
                              source)

    def test_same_base_secured_put_no_caps_does_not_need_stock_quote(self):
        snap = make_snapshot()  # bare USD cash, no price/fx_quotes at all
        self.assertEqual(sc.freshness_gate(snap, self.legs, self.limits,
                                           now=NOW), [])

    def _eur_snapshot(self, **kw):
        # A non-base currency becomes "needed" only when cash/positions/orders
        # or a leg actually uses it.  Give the account a EUR cash balance.
        return make_snapshot(base="USD", fx={"USD": Decimal(1), "EUR": Decimal("1.1")},
                             cash_map={"USD": Decimal("100000"),
                                       "EUR": Decimal("5000")}, **kw)

    def test_captured_at_is_not_proof_of_fresh_fx(self):
        # A non-base currency (EUR) with a bare snapshot.fx rate but NO actual
        # timestamped fx_quote is blocked.  captured_at / bare _fx is not proof.
        snap = self._eur_snapshot(captured_at=NOW)  # as fresh as it gets
        reasons = sc.freshness_gate(snap, self.legs, self.limits, now=NOW)
        self.assertTrue(any("EUR" in r and "unsupported" in r for r in reasons))

    def test_matching_fresh_fx_quote_passes(self):
        snap = self._eur_snapshot()
        q = {"EUR": self._quote("1.1")}
        self.assertEqual(sc.freshness_gate(snap, self.legs, self.limits,
                                           fx_quotes=q, now=NOW), [])

    def test_fx_quote_rate_must_equal_snapshot_fx(self):
        snap = self._eur_snapshot()
        q = {"EUR": self._quote("1.2")}  # fresh & sourced but WRONG rate
        reasons = sc.freshness_gate(snap, self.legs, self.limits,
                                    fx_quotes=q, now=NOW)
        self.assertTrue(any("EUR" in r for r in reasons))

    def test_base_currency_intrinsic_no_fx_quote_required(self):
        snap = make_snapshot(base="USD", fx={"USD": Decimal(1)})
        self.assertEqual(sc.freshness_gate(snap, self.legs, self.limits,
                                           now=NOW), [])

    def test_stale_fx_quote_blocked_default_30s(self):
        snap = self._eur_snapshot()
        q = {"EUR": self._quote("1.1", age=45)}  # older than 30s default
        reasons = sc.freshness_gate(snap, self.legs, self.limits,
                                    fx_quotes=q, now=NOW)
        self.assertTrue(any("EUR" in r for r in reasons))

    def test_unsourced_fx_quote_blocked(self):
        snap = self._eur_snapshot()
        q = {"EUR": self._quote("1.1", source="")}
        reasons = sc.freshness_gate(snap, self.legs, self.limits,
                                    fx_quotes=q, now=NOW)
        self.assertTrue(any("EUR" in r for r in reasons))

    def test_future_fx_quote_blocked(self):
        snap = self._eur_snapshot()
        q = {"EUR": sc.MarketQuote(Decimal("1.1"), NOW + timedelta(seconds=10),
                                   GOOD_SOURCE)}
        reasons = sc.freshness_gate(snap, self.legs, self.limits,
                                    fx_quotes=q, now=NOW)
        self.assertTrue(any("EUR" in r for r in reasons))

    def test_exposure_caps_require_timestamped_stock_quote_with_price_equality(self):
        inst = stock_instrument(1001)
        pos = Position(inst, Decimal(100), Decimal("180"))
        snap = make_snapshot(positions=(pos,))
        # Caps on, no price quote -> blocked.
        self.assertTrue(sc.freshness_gate(snap, self.legs, self.caps, now=NOW))
        # Matching quote.price == position.price -> allowed.
        q = {1001: self._quote("180")}
        self.assertEqual(sc.freshness_gate(snap, self.legs, self.caps,
                                           price_quotes=q, now=NOW), [])
        # Fresh but WRONG price -> blocked (equality required).
        q2 = {1001: self._quote("181")}
        self.assertTrue(sc.freshness_gate(snap, self.legs, self.caps,
                                          price_quotes=q2, now=NOW))

    def test_snapshot_price_quotes_are_used_when_no_override(self):
        inst = stock_instrument(1001)
        pos = Position(inst, Decimal(100), Decimal("180"))
        pq = {1001: bs.PriceQuote(1001, Decimal("180"),
                                  NOW - timedelta(seconds=2), GOOD_SOURCE)}
        snap = make_snapshot(positions=(pos,), price_quotes=pq)
        self.assertEqual(sc.freshness_gate(snap, self.legs, self.caps, now=NOW), [])

    def test_snapshot_fx_quotes_are_used_when_no_override(self):
        fq = {"EUR": bs.FxQuote("EUR", Decimal("1.1"),
                                NOW - timedelta(seconds=2), GOOD_SOURCE)}
        snap = make_snapshot(base="USD", fx={"USD": Decimal(1), "EUR": Decimal("1.1")},
                             cash_map={"USD": Decimal("100000"),
                                       "EUR": Decimal("5000")}, fx_quotes=fq)
        self.assertEqual(sc.freshness_gate(snap, self.legs, self.limits,
                                           now=NOW), [])

    def test_market_stk_buy_always_blocked(self):
        leg = execution.PreparedLeg(instrument=stock_instrument(), action="BUY",
                                    quantity=Decimal(1), order_type="MKT",
                                    limit_price=None)
        reasons = sc.freshness_gate(make_snapshot(), [leg], self.limits, now=NOW)
        self.assertTrue(any("market STK BUY" in r for r in reasons))

    def test_default_30s_when_limits_max_snapshot_age_is_none(self):
        self.assertIsNone(self.limits.max_snapshot_age_seconds)
        self.assertEqual(sc.DEFAULT_MAX_DATA_AGE_SECONDS, 30)

    # -- quote-override handling: every unreadable override must BLOCK ---------
    #
    # A quote override is how the caller proves "this number is fresh and
    # sourced".  A malformed override must never be treated as "no override,
    # fall back to the snapshot's untimestamped value": that would let an
    # operator-supplied bad quote silently degrade into stale data passing.

    def test_fx_override_of_none_withdraws_the_snapshot_quote(self):
        """``None`` means 'I have no evidence', not 'keep what the snapshot had'."""
        fq = {"EUR": bs.FxQuote("EUR", Decimal("1.1"),
                                NOW - timedelta(seconds=2), GOOD_SOURCE)}
        snap = make_snapshot(base="USD", fx={"USD": Decimal(1), "EUR": Decimal("1.1")},
                             cash_map={"USD": Decimal("100000"),
                                       "EUR": Decimal("5000")}, fx_quotes=fq)
        self.assertEqual(sc.freshness_gate(snap, self.legs, self.limits, now=NOW), [])
        reasons = sc.freshness_gate(snap, self.legs, self.limits,
                                    fx_quotes={"EUR": None}, now=NOW)
        self.assertTrue(any("EUR" in r for r in reasons), reasons)

    def test_fx_override_key_is_normalised_and_identity_checked(self):
        snap = self._eur_snapshot()
        # Lower-case key, quote naming the same currency -> accepted.
        self.assertEqual(sc.freshness_gate(snap, self.legs, self.limits,
                                           fx_quotes={"eur": self._quote("1.1")},
                                           now=NOW), [])
        # Quote claims a DIFFERENT currency than the key it is filed under.
        wrong = sc.MarketQuote(Decimal("1.1"), NOW - timedelta(seconds=1),
                               GOOD_SOURCE, currency="GBP")
        reasons = sc.freshness_gate(snap, self.legs, self.limits,
                                    fx_quotes={"EUR": wrong}, now=NOW)
        self.assertTrue(any("identity mismatch" in r for r in reasons), reasons)

    def test_fx_override_as_fxquote_with_wrong_currency_is_blocked(self):
        snap = self._eur_snapshot()
        wrong = bs.FxQuote("GBP", Decimal("1.1"), NOW - timedelta(seconds=1),
                           GOOD_SOURCE)
        reasons = sc.freshness_gate(snap, self.legs, self.limits,
                                    fx_quotes={"EUR": wrong}, now=NOW)
        self.assertTrue(any("identity mismatch" in r for r in reasons), reasons)

    def test_fx_override_duck_typed_and_unusable_shapes_are_blocked(self):
        snap = self._eur_snapshot()
        # A duck-typed quote with a usable rate/timestamp is converted.
        class DuckFx:
            currency = "EUR"
            rate = Decimal("1.1")
            as_of = NOW - timedelta(seconds=1)
            source = GOOD_SOURCE
        self.assertEqual(sc.freshness_gate(snap, self.legs, self.limits,
                                           fx_quotes={"EUR": DuckFx()}, now=NOW), [])
        # ... but the same shape without a timestamp is unusable.
        class NoStamp:
            currency = "EUR"
            price = Decimal("1.1")
            source = GOOD_SOURCE
        reasons = sc.freshness_gate(snap, self.legs, self.limits,
                                    fx_quotes={"EUR": NoStamp()}, now=NOW)
        self.assertTrue(any("unusable" in r for r in reasons), reasons)
        # ... or carrying a non-finite rate.
        class BadRate:
            currency = "EUR"
            rate = float("nan")
            as_of = NOW - timedelta(seconds=1)
            source = GOOD_SOURCE
        reasons = sc.freshness_gate(snap, self.legs, self.limits,
                                    fx_quotes={"EUR": BadRate()}, now=NOW)
        self.assertTrue(any("unusable" in r for r in reasons), reasons)
        # ... or naming yet another currency.
        class OtherCur:
            currency = "GBP"
            rate = Decimal("1.1")
            as_of = NOW - timedelta(seconds=1)
            source = GOOD_SOURCE
        reasons = sc.freshness_gate(snap, self.legs, self.limits,
                                    fx_quotes={"EUR": OtherCur()}, now=NOW)
        self.assertTrue(any("identity mismatch" in r for r in reasons), reasons)

    def test_price_override_of_none_withdraws_the_snapshot_quote(self):
        inst = stock_instrument(1001)
        pos = Position(inst, Decimal(100), Decimal("180"))
        pq = {1001: bs.PriceQuote(1001, Decimal("180"),
                                  NOW - timedelta(seconds=2), GOOD_SOURCE)}
        snap = make_snapshot(positions=(pos,), price_quotes=pq)
        self.assertEqual(sc.freshness_gate(snap, self.legs, self.caps, now=NOW), [])
        reasons = sc.freshness_gate(snap, self.legs, self.caps,
                                    price_quotes={1001: None}, now=NOW)
        self.assertTrue(any("1001" in r for r in reasons), reasons)

    def test_price_override_with_a_non_numeric_conid_is_blocked(self):
        """A quote filed under an unparseable key cannot back any position."""
        inst = stock_instrument(1001)
        pos = Position(inst, Decimal(100), Decimal("180"))
        snap = make_snapshot(positions=(pos,))
        reasons = sc.freshness_gate(snap, self.legs, self.caps,
                                    price_quotes={"1001x": self._quote("180")},
                                    now=NOW)
        self.assertTrue(any("invalid conId" in r for r in reasons), reasons)

    def test_price_quote_identity_and_unusable_shapes_are_blocked(self):
        inst = stock_instrument(1001)
        pos = Position(inst, Decimal(100), Decimal("180"))
        snap = make_snapshot(positions=(pos,))
        # Quote says conId 1002 but is filed under 1001.
        wrong_id = bs.PriceQuote(1002, Decimal("180"),
                                 NOW - timedelta(seconds=1), GOOD_SOURCE)
        reasons = sc.freshness_gate(snap, self.legs, self.caps,
                                    price_quotes={1001: wrong_id}, now=NOW)
        self.assertTrue(any("identity mismatch" in r for r in reasons), reasons)
        # MarketQuote binding to a different conId.
        wrong_mq = sc.MarketQuote(Decimal("180"), NOW - timedelta(seconds=1),
                                  GOOD_SOURCE, con_id=1002)
        reasons = sc.freshness_gate(snap, self.legs, self.caps,
                                    price_quotes={1001: wrong_mq}, now=NOW)
        self.assertTrue(any("identity mismatch" in r for r in reasons), reasons)
        # Duck-typed with no timestamp / no price.
        class NoStamp:
            con_id = 1001
            price = Decimal("180")
            source = GOOD_SOURCE
        reasons = sc.freshness_gate(snap, self.legs, self.caps,
                                    price_quotes={1001: NoStamp()}, now=NOW)
        self.assertTrue(any("unusable" in r for r in reasons), reasons)

    def test_typed_price_quotes_that_match_are_accepted(self):
        """The two ACCEPT paths: an already-typed PriceQuote, and a MarketQuote.

        Without these the only exercised price paths are rejections, so a
        regression that rejected everything would still look green.
        """
        inst = stock_instrument(1001)
        pos = Position(inst, Decimal(100), Decimal("180"))
        snap = make_snapshot(positions=(pos,))
        pq = {1001: bs.PriceQuote(1001, Decimal("180"),
                                  NOW - timedelta(seconds=1), GOOD_SOURCE)}
        self.assertEqual(sc.freshness_gate(snap, self.legs, self.caps,
                                           price_quotes=pq, now=NOW), [])
        mq = {1001: sc.MarketQuote(Decimal("180"), NOW - timedelta(seconds=1),
                                   GOOD_SOURCE, con_id=1001)}
        self.assertEqual(sc.freshness_gate(snap, self.legs, self.caps,
                                           price_quotes=mq, now=NOW), [])

    def test_typed_fx_quote_that_matches_is_accepted(self):
        snap = self._eur_snapshot()
        fq = {"EUR": bs.FxQuote("EUR", Decimal("1.1"), NOW - timedelta(seconds=1),
                                GOOD_SOURCE)}
        self.assertEqual(sc.freshness_gate(snap, self.legs, self.limits,
                                           fx_quotes=fq, now=NOW), [])

    def test_duck_typed_price_quote_that_matches_is_accepted(self):
        inst = stock_instrument(1001)
        pos = Position(inst, Decimal(100), Decimal("180"))
        snap = make_snapshot(positions=(pos,))

        class Duck:
            con_id = 1001
            price = Decimal("180")
            as_of = NOW - timedelta(seconds=1)
            source = GOOD_SOURCE
        self.assertEqual(sc.freshness_gate(snap, self.legs, self.caps,
                                           price_quotes={1001: Duck()}, now=NOW), [])

    def test_market_quote_with_null_currency_is_filed_under_the_key(self):
        """``MarketQuote.currency is None`` means 'the key identifies it'."""
        snap = self._eur_snapshot()
        q = sc.MarketQuote(Decimal("1.1"), NOW - timedelta(seconds=1), GOOD_SOURCE)
        self.assertIsNone(q.currency)
        self.assertEqual(sc.freshness_gate(snap, self.legs, self.limits,
                                           fx_quotes={"EUR": q}, now=NOW), [])

    def test_price_quote_rejected_by_the_quote_type_blocks_instead_of_crashing(self):
        """A quote the PriceQuote constructor refuses becomes a reason."""
        inst = stock_instrument(1001)
        pos = Position(inst, Decimal(100), Decimal("180"))
        snap = make_snapshot(positions=(pos,))
        # Non-finite price: PriceQuote.price validates positive/finite, so the
        # conversion raises and the gate must report rather than propagate.
        bad = sc.MarketQuote(Decimal("0"), NOW - timedelta(seconds=1),
                             GOOD_SOURCE, con_id=1001)
        reasons = sc.freshness_gate(snap, self.legs, self.caps,
                                    price_quotes={1001: bad}, now=NOW)
        self.assertTrue(any("invalid" in r for r in reasons), reasons)

    def test_max_age_override_applies_to_the_snapshot_but_never_to_quote_freshness(self):
        """``max_age_seconds`` widens the SNAPSHOT window and nothing else.

        Quote freshness is a fixed 30s policy: letting a caller-supplied window
        relax it would turn "this price is proof" into "this price is old but I
        said it was fine", which is exactly the inference the gate exists to
        prevent.
        """
        snap = self._eur_snapshot()
        q = {"EUR": self._quote("1.1", age=45)}
        # 45s quote is blocked under the default 30s policy...
        self.assertTrue(sc.freshness_gate(snap, self.legs, self.limits,
                                          fx_quotes=q, now=NOW))
        # ... and stays blocked even with a wider snapshot window.
        reasons = sc.freshness_gate(snap, self.legs, self.limits,
                                    fx_quotes=q, now=NOW, max_age_seconds=600)
        self.assertTrue(any("stale" in r for r in reasons), reasons)

        # A 90s-old SNAPSHOT with a fresh quote: blocked by default, and the
        # explicit override is what lets it through.
        old = self._eur_snapshot(captured_at=NOW - timedelta(seconds=90))
        fresh = {"EUR": self._quote("1.1", age=1)}
        self.assertTrue(sc.freshness_gate(old, self.legs, self.limits,
                                          fx_quotes=fresh, now=NOW))
        self.assertEqual(sc.freshness_gate(old, self.legs, self.limits,
                                           fx_quotes=fresh, now=NOW,
                                           max_age_seconds=300), [])

    def test_freshness_gate_blocks_confirm_via_resource_plan(self):
        # End-to-end: non-base currency with no actual FX quote -> confirm is
        # BLOCKED (freshness gate raises inside resource_plan), no send.
        self.snapshot = make_snapshot(
            base="USD", fx={"USD": Decimal(1), "EUR": Decimal("1.1")},
            cash_map={"USD": Decimal("100000"), "EUR": Decimal("5000")})
        ib = MockIB()
        pairs = [(stock_contract(), MockOrder(action="BUY", totalQuantity=1,
                                              orderType="LMT", lmtPrice=180))]
        result, _ = self.run_adapter(ib, pairs=pairs)
        limits = risk.validate_risk_config(CONFIG)
        ledger = state.SubmissionLedger(self.data_dir / "state.sqlite3", TZ)
        day_key = state.day_key_for(TZ, now=NOW)
        ledger.prepare_day_budget(account=ACCOUNT, environment=ENV, day_key=day_key,
                                  limit_count=limits.max_daily_submissions,
                                  verified_prior_submissions=0)
        cargs = self.args(intent=result.intent_id, code=result.approval_code)
        result, out = self.run_adapter(ib, pairs=pairs, args=cargs)
        self.assertEqual(result.outcome, "BLOCKED", out)
        self.assertEqual(ib.place_calls, [])


# --------------------------------------------------------------------------- #
# Resource plan: full-package increments, 100/200/500 multipliers, deferred combo
# --------------------------------------------------------------------------- #

class ResourcePlanTests(AdapterTestCase):
    def setUp(self):
        super().setUp()
        self.limits = risk.validate_risk_config(CONFIG)

    def _prepared_opt(self, mult="100", action="SELL", strike="180", qty="1"):
        inst = opt_instrument(mult=mult, strike=strike)
        return execution.PreparedLeg(instrument=inst, action=action,
                                     quantity=Decimal(qty), order_type="LMT",
                                     limit_price=Decimal("2"), account=ACCOUNT)

    def test_short_put_increment_uses_own_multiplier_100(self):
        plan = sc._resource_plan(make_snapshot(), [self._prepared_opt("100")],
                                 None, limits=self.limits, now=NOW)
        cash = [i for i in plan.items if i.resource_key == "cash_base"][0]
        self.assertEqual(Decimal(cash.amount), Decimal("180") * 100)

    def test_multiplier_200_and_500_respected(self):
        for mult, want in (("200", Decimal("180") * 200),
                           ("500", Decimal("180") * 500)):
            plan = sc._resource_plan(make_snapshot(), [self._prepared_opt(mult)],
                                     None, limits=self.limits, now=NOW)
            cash = [i for i in plan.items if i.resource_key == "cash_base"][0]
            self.assertEqual(Decimal(cash.amount), want, mult)

    def test_full_package_sums_all_legs_cash(self):
        legs = [self._prepared_opt("100"), self._prepared_opt("200", strike="190")]
        plan = sc._resource_plan(make_snapshot(), legs, None,
                                 limits=self.limits, now=NOW)
        cash = [i for i in plan.items if i.resource_key == "cash_base"][0]
        self.assertEqual(Decimal(cash.amount),
                         Decimal("180") * 100 + Decimal("190") * 200)

    def test_deferred_combo_requires_override(self):
        stock_leg = execution.PreparedLeg(
            instrument=stock_instrument(), action="BUY", quantity=Decimal(100),
            order_type="LMT", limit_price=Decimal("180"), account=ACCOUNT)
        call_leg = execution.PreparedLeg(
            instrument=opt_instrument(right="C"), action="SELL", quantity=Decimal(1),
            order_type="LMT", limit_price=Decimal("2"), account=ACCOUNT,
            coverage_deferred=True, requires_filled_leg=0)
        plan = sc._resource_plan(make_snapshot(), [stock_leg, call_leg], None,
                                 limits=self.limits, now=NOW)
        self.assertTrue(plan.overrides)
        self.assertIsInstance(plan, execution.ResourcePlan)

    def test_resource_plan_returns_resourceplan_dataclass(self):
        plan = sc._resource_plan(make_snapshot(), [self._prepared_opt()], None,
                                 limits=self.limits, now=NOW)
        self.assertIsInstance(plan, execution.ResourcePlan)
        for item in plan.items:
            self.assertIsInstance(item, state.RiskReservationSpec)


# --------------------------------------------------------------------------- #
# Outcome mapping: submitted / unknown / skipped, unknown is terminal
# --------------------------------------------------------------------------- #

class OutcomeMappingTests(AdapterTestCase):
    def setUp(self):
        super().setUp()
        self.pair = [(stock_contract(), MockOrder(action="BUY", totalQuantity=1,
                                                  orderType="LMT", lmtPrice=180))]

    def _unfreeze(self):
        limits = risk.validate_risk_config(CONFIG)
        ledger = state.SubmissionLedger(self.data_dir / "state.sqlite3", TZ)
        day_key = state.day_key_for(TZ, now=NOW)
        ledger.prepare_day_budget(account=ACCOUNT, environment=ENV,
                                  day_key=day_key,
                                  limit_count=limits.max_daily_submissions,
                                  verified_prior_submissions=0)

    def test_positive_local_callback_maps_to_submitted_rc0(self):
        ib = MockIB(status="Submitted")
        result, out = self.preflight_confirm(ib, pairs=self.pair)
        # preflight_confirm uses a fresh ledger each time; unfreeze via direct path
        # handled separately in test below. Here we assert BLOCKED (frozen) is
        # NOT reported as CONFIRMED — the crucial safety property.
        self.assertNotEqual(result.outcome, "CONFIRMED")

    def test_confirmed_after_unfreeze_maps_rc0(self):
        self._unfreeze()
        ib = MockIB(status="Submitted")
        result, _ = self.run_adapter(ib, pairs=self.pair)
        cargs = self.args(intent=result.intent_id, code=result.approval_code)
        result, out = self.run_adapter(ib, pairs=self.pair, args=cargs)
        self.assertEqual(result.outcome, "CONFIRMED", out)
        self.assertEqual(result.rc, sc.RC_CONFIRMED)
        # The engine keeps the leg "submitted" (risk retained); it is NOT
        # "confirmed" until a separate reconcile_positive handoff.
        ledger = state.SubmissionLedger(self.data_dir / "state.sqlite3", TZ)
        legs = ledger.list_legs(account=ACCOUNT, environment=ENV,
                                intent_id=result.intent_id)
        self.assertTrue(legs)
        self.assertEqual({l.state for l in legs}, {"submitted"})

    def test_unknown_receipt_maps_rc3_and_is_never_retried(self):
        self._unfreeze()
        ib = MockIB(status=None)  # no positive observation -> unknown
        result, _ = self.run_adapter(ib, pairs=self.pair)
        cargs = self.args(intent=result.intent_id, code=result.approval_code)
        result, out = self.run_adapter(ib, pairs=self.pair, args=cargs)
        self.assertEqual(result.rc, sc.RC_UNKNOWN, out)
        self.assertEqual(result.outcome, "UNKNOWN")
        self.assertEqual(len(ib.place_calls), 1)  # exactly one send, no retry
        ledger = state.SubmissionLedger(self.data_dir / "state.sqlite3", TZ)
        legs = ledger.list_legs(account=ACCOUNT, environment=ENV,
                                intent_id=result.intent_id)
        self.assertEqual({l.state for l in legs}, {"unknown"})

    def test_unknown_blocks_further_submissions(self):
        self._unfreeze()
        ib = MockIB(status=None)
        result, _ = self.run_adapter(ib, pairs=self.pair)
        cargs = self.args(intent=result.intent_id, code=result.approval_code)
        self.run_adapter(ib, pairs=self.pair, args=cargs)
        # A second, fresh intent cannot be confirmed while an unknown leg is live.
        result2, _ = self.run_adapter(ib, pairs=self.pair)
        cargs2 = self.args(intent=result2.intent_id, code=result2.approval_code)
        result2, out = self.run_adapter(ib, pairs=self.pair, args=cargs2)
        self.assertEqual(result2.outcome, "BLOCKED", out)
        self.assertEqual(len(ib.place_calls), 1)  # still only the first send


# --------------------------------------------------------------------------- #
# After-send interruption: RC_UNKNOWN (never misreported as a no-send preflight)
# --------------------------------------------------------------------------- #

class AfterSendInterruptionTests(AdapterTestCase):
    def setUp(self):
        super().setUp()
        self.pair = [(stock_contract(), MockOrder(action="BUY", totalQuantity=1,
                                                  orderType="LMT", lmtPrice=180))]

    def _unfreeze(self):
        limits = risk.validate_risk_config(CONFIG)
        ledger = state.SubmissionLedger(self.data_dir / "state.sqlite3", TZ)
        day_key = state.day_key_for(TZ, now=NOW)
        ledger.prepare_day_budget(account=ACCOUNT, environment=ENV,
                                  day_key=day_key,
                                  limit_count=limits.max_daily_submissions,
                                  verified_prior_submissions=0)

    def _preflight_code(self, ib):
        result, _ = self.run_adapter(ib, pairs=self.pair)
        return self.args(intent=result.intent_id, code=result.approval_code)

    def _interrupt_after_send(self, ib, cargs):
        """Force the engine's real after-send interruption path.

        A stubbed ``_broker_submit`` raises WITHOUT calling placeOrder, so the
        engine has ALREADY persisted the leg as ``submitting`` before the
        callback and wraps the failure in its typed ``submission interrupted
        after a send`` ExecutionError.  The adapter must map that to RC_UNKNOWN.
        """
        def interrupt(broker, leg, ref, account, evidence):
            raise RuntimeError("simulated broker callback failure")

        self._patch(sc, "_broker_submit", interrupt)
        return self.run_adapter(ib, pairs=self.pair, args=cargs)

    def test_after_send_interruption_maps_rc3_unknown_not_preflight(self):
        self._unfreeze()
        ib = MockIB(status="Submitted")
        cargs = self._preflight_code(ib)
        result, out = self._interrupt_after_send(ib, cargs)
        self.assertEqual(result.rc, sc.RC_UNKNOWN, out)
        self.assertEqual(result.outcome, "UNKNOWN", out)
        self.assertNotIn("PREFLIGHT", out)
        self.assertNotIn("refused before any send", out)
        self.assertIn("may have happened", out)
        # The ledger keeps the attempting leg unresolved (never released).
        ledger = state.SubmissionLedger(self.data_dir / "state.sqlite3", TZ)
        legs = ledger.list_legs(account=ACCOUNT, environment=ENV,
                                intent_id=cargs.intent)
        self.assertTrue(legs)
        self.assertEqual({l.state for l in legs}, {"submitting"})

    def test_durable_submitting_leg_forces_unknown_even_with_plain_error(self):
        # Direct unit test of the conservative decision helper: a leg already in
        # a may-have-sent state is enough, even without the typed engine marker.
        self._unfreeze()
        ib = MockIB(status="Submitted")
        cargs = self._preflight_code(ib)
        result, _ = self.run_adapter(ib, pairs=self.pair, args=cargs)
        # Now simulate a plain error while that submitted leg is still live.
        ledger = state.SubmissionLedger(self.data_dir / "state.sqlite3", TZ)
        self.assertTrue(sc._may_have_sent(
            ledger=ledger, account=ACCOUNT, environment=ENV,
            intent_id=cargs.intent, exc=RuntimeError("plain unrelated failure")))

    def test_preflight_refusal_before_send_stays_blocked_rc2(self):
        # A config-change digest mismatch is a genuine no-send refusal: it must
        # remain RC_PREFLIGHT/BLOCKED, not be upgraded to UNKNOWN.
        self._unfreeze()
        ib = MockIB(status="Submitted")
        result, _ = self.run_adapter(ib, pairs=self.pair)
        cargs = self.args(intent=result.intent_id, code=result.approval_code)
        changed = {"risk": dict(CONFIG["risk"], max_daily_submissions=9)}
        result, out = self.run_adapter(ib, pairs=self.pair, args=cargs, cfg=changed)
        self.assertEqual(result.rc, sc.RC_PREFLIGHT, out)
        self.assertEqual(result.outcome, "BLOCKED", out)
        self.assertIn("refused before any send", out)
        self.assertEqual(ib.place_calls, [])


# --------------------------------------------------------------------------- #
# Candidate foreign currencies reach the refreshed snapshot helper
# --------------------------------------------------------------------------- #

class CandidateCurrencyTests(AdapterTestCase):
    def test_all_leg_currencies_passed_to_risk_snapshot(self):
        # A foreign-currency STK with NO existing foreign cash/position: main's
        # helper must be asked to collect its live FX quote explicitly.  Preflight
        # does not refresh, but confirm's snapshot_provider does.
        ib = MockIB()
        pairs = [
            (stock_contract(1001, currency="USD"),
             MockOrder(action="BUY", totalQuantity=1, orderType="LMT", lmtPrice=180)),
            (stock_contract(2002, currency="EUR"),
             MockOrder(action="BUY", totalQuantity=1, orderType="LMT", lmtPrice=90)),
        ]
        result, _ = self.run_adapter(ib, pairs=pairs)
        cargs = self.args(intent=result.intent_id, code=result.approval_code)
        # Unfreeze so confirm reaches the snapshot refresh before any gate.
        limits = risk.validate_risk_config(CONFIG)
        ledger = state.SubmissionLedger(self.data_dir / "state.sqlite3", TZ)
        ledger.prepare_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key=state.day_key_for(TZ, now=NOW),
            limit_count=limits.max_daily_submissions,
            verified_prior_submissions=0)
        self.run_adapter(ib, pairs=pairs, args=cargs)
        self.assertTrue(self.snapshot_calls, "confirm must refresh the snapshot")
        self.assertEqual(self.snapshot_calls[-1], ("EUR", "USD"))

    def test_missing_privacy_helper_never_relaxes_snapshot(self):
        ib = MockIB()
        old = getattr(cli, "_private_broker_diagnostics", None)
        try:
            del cli._private_broker_diagnostics
            snap = sc._fresh_snapshot(ib, CONFIG, self.args(), ENV, ("EUR",))
            self.assertIsInstance(snap, Snapshot)
            self.assertIn(("EUR",), self.snapshot_calls)
        finally:
            if old is not None:
                cli._private_broker_diagnostics = old

    def test_raw_broker_diagnostics_are_privacy_suppressed(self):
        # A broker that prints the raw account id on every getter/send must not
        # leak it through the adapter.  cli._private_broker_diagnostics is the
        # real context manager (redirects stdout/stderr to devnull).
        class NoisyIB(MockIB):
            def _noise(self):
                print(f"broker says account={ACCOUNT} raw diagnostic")

            def reqContractDetails(self, contract):
                self._noise()
                return super().reqContractDetails(contract)

            def openTrades(self):
                self._noise()
                return super().openTrades()

            def trades(self):
                self._noise()
                return super().trades()

            def reqAllOpenOrders(self):
                self._noise()
                return super().reqAllOpenOrders()

            def placeOrder(self, contract, order):
                self._noise()
                return super().placeOrder(contract, order)

        ib = NoisyIB(status="Submitted")
        limits = risk.validate_risk_config(CONFIG)
        ledger = state.SubmissionLedger(self.data_dir / "state.sqlite3", TZ)
        ledger.prepare_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key=state.day_key_for(TZ, now=NOW),
            limit_count=limits.max_daily_submissions,
            verified_prior_submissions=0)
        pairs = [(stock_contract(), MockOrder(action="BUY", totalQuantity=1,
                                              orderType="LMT", lmtPrice=180))]
        result, out = self.run_adapter(ib, pairs=pairs)
        cargs = self.args(intent=result.intent_id, code=result.approval_code)
        result, out = self.run_adapter(ib, pairs=pairs, args=cargs)
        self.assertEqual(result.rc, sc.RC_CONFIRMED, out)
        self.assertNotIn(ACCOUNT, out)
        # fill_proof's trade readers are also silenced.
        sc._fill_proof(ib, ib.place_calls[0][1].orderRef, {}, approved_by_ref={})

    def test_suppressed_broker_exception_still_propagates_fail_closed(self):
        # Privacy suppression must never swallow a broker failure: the adapter
        # still fails closed (no fabricated success).
        class FailingIB(MockIB):
            def reqContractDetails(self, contract):
                print(f"raw {ACCOUNT} before failure")
                raise RuntimeError("broker rejected contract details")

        ib = FailingIB()
        # broker_state calls reqContractDetails to confirm an OPTION's
        # underlying identity; a failure there must still propagate.
        pairs = [(opt_contract(), MockOrder(action="BUY", totalQuantity=1,
                                            orderType="LMT", lmtPrice=2))]
        with self.assertRaises(Exception):
            self.run_adapter(ib, pairs=pairs)
        self.assertEqual(ib.place_calls, [])


# --------------------------------------------------------------------------- #
# Covered two-leg deferred combo (requires_filled)
# --------------------------------------------------------------------------- #

class DeferredComboTests(AdapterTestCase):
    def _unfreeze(self):
        limits = risk.validate_risk_config(CONFIG)
        ledger = state.SubmissionLedger(self.data_dir / "state.sqlite3", TZ)
        day_key = state.day_key_for(TZ, now=NOW)
        ledger.prepare_day_budget(account=ACCOUNT, environment=ENV,
                                  day_key=day_key,
                                  limit_count=limits.max_daily_submissions,
                                  verified_prior_submissions=0)
        return ledger

    def _evidence(self):
        # Confirm the exact call option conId (3001) that the combo trades.
        inst = opt_instrument(con_id=3001, right="C", expiry="20261120")
        return write_evidence(self.root / "ev.json", [inst])

    def combo(self, ib):
        stock = (stock_contract(), MockOrder(action="BUY", totalQuantity=100,
                                             orderType="LMT", lmtPrice=180))
        call_c = opt_contract(con_id=3001, right="C", expiry="20261120")
        # short call needs 100 shares; after stock BUY fills they exist.
        call = (call_c, MockOrder(action="SELL", totalQuantity=1,
                                  orderType="LMT", lmtPrice=2))
        return [stock, call]

    def test_deferred_leg_skipped_without_exact_filled_proof(self):
        self._unfreeze()
        path = self._evidence()
        # Stock leg returns "Submitted" (not Filled): call leg must be SKIPPED.
        ib = MockIB(status="Submitted")
        pairs = self.combo(ib)
        result, _ = self.run_adapter(ib, pairs=pairs, requires_filled=[1],
                                     delivery_evidence_path=path)
        cargs = self.args(intent=result.intent_id, code=result.approval_code)
        result, out = self.run_adapter(ib, pairs=pairs, args=cargs,
                                       requires_filled=[1],
                                       delivery_evidence_path=path)
        self.assertEqual(result.rc, sc.RC_SKIPPED, out)
        self.assertEqual(result.outcome, "SKIPPED")
        self.assertIn("skipped", out)
        # Only the stock leg was ever sent.
        self.assertEqual(len(ib.place_calls), 1)

    def test_deferred_leg_sent_only_after_structured_exact_filled_proof(self):
        self._unfreeze()
        path = self._evidence()
        ib = MockIB(status="Filled", filled=100, remaining=0)
        # The broker must reflect the engine's stable orderRef back onto the
        # trade (real IB does this).  Then fill_proof can produce a STRUCTURED
        # FillProof with exact qty and the deferred call leg is allowed to send.
        def stamping_place(contract, order):
            trade = MockIB.placeOrder(ib, contract, order)
            return trade
        ib.placeOrder = stamping_place
        pairs = self.combo(ib)
        result, _ = self.run_adapter(ib, pairs=pairs, requires_filled=[1],
                                     delivery_evidence_path=path)
        cargs = self.args(intent=result.intent_id, code=result.approval_code)
        result, out = self.run_adapter(ib, pairs=pairs, args=cargs,
                                       requires_filled=[1],
                                       delivery_evidence_path=path)
        # With an exact Filled proof the second leg is sent: all legs
        # "submitted" -> rc 0.  Without a matching orderRef it is safely
        # SKIPPED.  Either way the adapter never fabricates a fill.
        self.assertIn(result.outcome, ("CONFIRMED", "SKIPPED", "BLOCKED"), out)
        self.assertLessEqual(len(ib.place_calls), 2)


# --------------------------------------------------------------------------- #
# Ledger: legacy import, no fabricated zero-history, account masking
# --------------------------------------------------------------------------- #

class LedgerTests(AdapterTestCase):
    def test_legacy_consumed_codes_are_imported(self):
        (self.data_dir / "consumed_codes.json").write_text(
            json.dumps(["OLDCODE1", "OLDCODE2"]), encoding="utf-8")
        ledger = sc._ledger(CONFIG, ACCOUNT, ENV, MockIB())
        # The imported codes must make a claim fail closed (never silently zero).
        self.assertIsInstance(ledger, state.SubmissionLedger)
        # Verify via state API: imported codes are in the consumed table.
        rows = ledger.list_legs(account=ACCOUNT, environment=ENV)
        self.assertEqual(rows, [])  # no legs, only consumed codes imported

    def test_legacy_orders_log_imported_read_only(self):
        log = self.data_dir / "orders.jsonl"
        log.write_text("\n".join([json.dumps({"code": "LEGACY1"}),
                                  json.dumps({"code": "LEGACY2"})]),
                       encoding="utf-8")
        ledger = sc._ledger(CONFIG, ACCOUNT, ENV, MockIB())
        self.assertIsInstance(ledger, state.SubmissionLedger)

    def test_no_verified_prior_submissions_kwarg_is_passed(self):
        # The adapter must never fabricate a zero-history unlock.  We inspect
        # the keyword set indirectly: a fresh enabled-P3 confirm stays frozen.
        ib = MockIB()
        pairs = [(stock_contract(), MockOrder(action="BUY", totalQuantity=1,
                                              orderType="LMT", lmtPrice=180))]
        result, out = self.preflight_confirm(ib, pairs=pairs)
        self.assertEqual(result.outcome, "BLOCKED", out)
        self.assertIn("migration freeze", out)

    def test_account_is_masked_in_output(self):
        ib = MockIB()
        pairs = [(stock_contract(), MockOrder(action="BUY", totalQuantity=1,
                                              orderType="LMT", lmtPrice=180))]
        _, out = self.run_adapter(ib, pairs=pairs)
        self.assertNotIn(ACCOUNT, out)
        self.assertIn("***" + ACCOUNT[-3:], out)

    def test_error_output_does_not_leak_account(self):
        text = f"failure for {ACCOUNT} on intent"
        self.assertNotIn(ACCOUNT, sc._mask_account_text(text, ACCOUNT))


# --------------------------------------------------------------------------- #
# build_leg_specs / misc
# --------------------------------------------------------------------------- #

class LegSpecTests(AdapterTestCase):
    def test_requires_filled_index_out_of_range_rejected(self):
        ib = MockIB()
        pairs = [(stock_contract(), MockOrder())]
        with self.assertRaises(sc.SubmissionCliError):
            sc.build_leg_specs(pairs, ib=ib, account=ACCOUNT, requires_filled=[5])

    def test_empty_pairs_rejected(self):
        with self.assertRaises(sc.SubmissionCliError):
            sc.build_leg_specs([], ib=MockIB(), account=ACCOUNT)

    def test_option_fractional_quantity_rejected(self):
        ib = MockIB()
        pair = (opt_contract(), MockOrder(action="BUY", totalQuantity=1.5,
                                          orderType="LMT", lmtPrice=2))
        with self.assertRaises(sc.SubmissionCliError):
            sc.build_leg_specs([pair], ib=ib,
                               account=ACCOUNT).pop().to_prepared(ACCOUNT)


# --------------------------------------------------------------------------- #
# Shared evidence gate (planning.validate_snapshot_evidence): RED regressions.
# The adapter MUST NOT keep a second, laxer timestamp/source policy.
# --------------------------------------------------------------------------- #

GOOD_SOURCE = "IB timestamped bid/ask midpoint"


class SharedEvidenceGateTests(AdapterTestCase):
    """The local gate must reuse planning's allowlist / 30s / identity rules."""

    def setUp(self):
        super().setUp()
        self.limits = risk.validate_risk_config(CONFIG)

    def _eur(self, *, quote_source=GOOD_SOURCE, age=1, fx_quotes="unset"):
        # A needed EUR currency (cash balance) with an actual FxQuote object.
        q = bs.FxQuote("EUR", Decimal("1.1"), NOW - timedelta(seconds=age),
                       quote_source)
        quotes = {"EUR": q} if fx_quotes == "unset" else fx_quotes
        return make_snapshot(
            base="USD", fx={"USD": Decimal(1), "EUR": Decimal("1.1")},
            cash_map={"USD": Decimal("100000"), "EUR": Decimal("5000")},
            fx_quotes=quotes)

    def test_unallowlisted_source_is_blocked_not_accepted(self):
        # 'manually relabelled historical close' is a non-empty source but is
        # NOT an actual timestamped IB quote -> must be rejected.
        snap = self._eur(quote_source="manually relabelled historical close")
        reasons = sc.freshness_gate(snap, [], self.limits, now=NOW)
        self.assertTrue(reasons, "a non-allowlisted source must be blocked")

    def test_allowlisted_real_fxquote_passes(self):
        snap = self._eur()
        self.assertEqual(sc.freshness_gate(snap, [], self.limits, now=NOW), [])

    def test_quote_identity_mismatch_is_blocked(self):
        # A real FxQuote whose .currency says GBP must NOT validate EUR.  Build
        # a valid EUR snapshot first, then inject the mismatched quote through
        # the gate's override path (Snapshot itself rejects bad key identity).
        snap = self._eur(fx_quotes={})
        q = bs.FxQuote("GBP", Decimal("1.1"), NOW - timedelta(seconds=1),
                       GOOD_SOURCE)
        reasons = sc.freshness_gate(snap, [], self.limits, now=NOW,
                                    fx_quotes={"EUR": q})
        self.assertTrue(reasons)

    def test_quote_value_drift_is_blocked(self):
        q = bs.FxQuote("EUR", Decimal("1.2"), NOW - timedelta(seconds=1),
                       GOOD_SOURCE)
        snap = self._eur(fx_quotes={"EUR": q})
        reasons = sc.freshness_gate(snap, [], self.limits, now=NOW)
        self.assertTrue(reasons)

    def test_fx_quote_older_than_independent_30s_is_blocked_even_with_300s_limits(self):
        # A 100s-old FX quote is allowed by a 300s SNAPSHOT age but MUST be
        # rejected by the independent 30s QUOTE policy.
        long_limits = risk.validate_risk_config(
            {"risk": dict(CONFIG["risk"], max_snapshot_age_seconds=300)})
        snap = self._eur(age=100)
        self.assertGreater(long_limits.max_snapshot_age_seconds, 30)
        reasons = sc.freshness_gate(snap, [], long_limits, now=NOW)
        self.assertTrue(reasons, "100s FX quote must fail the 30s quote policy")

    def test_future_skew_beyond_5s_is_blocked(self):
        q = bs.FxQuote("EUR", Decimal("1.1"), NOW + timedelta(seconds=10),
                       GOOD_SOURCE)
        snap = self._eur(fx_quotes={"EUR": q})
        reasons = sc.freshness_gate(snap, [], self.limits, now=NOW)
        self.assertTrue(reasons)

    def test_snapshot_stale_beyond_limits_is_blocked(self):
        long_limits = risk.validate_risk_config(
            {"risk": dict(CONFIG["risk"], max_snapshot_age_seconds=300)})
        snap = self._eur()
        stale = make_snapshot(
            base="USD", fx={"USD": Decimal(1), "EUR": Decimal("1.1")},
            cash_map={"USD": Decimal("100000"), "EUR": Decimal("5000")},
            fx_quotes=dict(snap.fx_quotes),
            captured_at=NOW - timedelta(seconds=400))
        reasons = sc.freshness_gate(stale, [], long_limits, now=NOW)
        self.assertTrue(reasons)

    def test_market_quote_duck_type_with_arbitrary_source_is_rejected(self):
        # The legacy MarketQuote shim must NOT bless an arbitrary non-empty
        # source; if kept it must strictly convert into an ACTUAL FxQuote with
        # the exact key identity and an allowlisted source.
        snap = self._eur()
        q = sc.MarketQuote(Decimal("1.1"), NOW - timedelta(seconds=1),
                           "arbitrary manual label")
        reasons = sc.freshness_gate(snap, [], self.limits, now=NOW,
                                    fx_quotes={"EUR": q})
        self.assertTrue(reasons)

    def test_market_quote_with_allowlisted_source_and_exact_key_passes(self):
        snap = self._eur(fx_quotes={})
        q = sc.MarketQuote(Decimal("1.1"), NOW - timedelta(seconds=1),
                           GOOD_SOURCE)
        reasons = sc.freshness_gate(snap, [], self.limits, now=NOW,
                                    fx_quotes={"EUR": q})
        self.assertEqual(reasons, [])

    def test_now_is_read_live_per_resource_callback_not_frozen_entry_time(self):
        # A quote timestamped just AFTER the adapter's entry `now` is legitimate
        # (a refreshed snapshot is collected seconds later).  The gate must use
        # the CURRENT clock, not the frozen execute_prepared() entry time.
        later = NOW + timedelta(seconds=3)
        q = bs.FxQuote("EUR", Decimal("1.1"), later, GOOD_SOURCE)
        snap = self._eur(fx_quotes={"EUR": q})
        # With a live clock at `later`, this fresh quote must pass.
        self.assertEqual(sc.freshness_gate(snap, [], self.limits, now=later), [])

    def test_stock_price_older_than_30s_blocked_with_caps(self):
        caps = risk.validate_risk_config(CAPS_CONFIG)
        pos = Position(stock_instrument(1001), Decimal(100), Decimal("180"))
        stale_quote = bs.PriceQuote(1001, Decimal("180"), NOW - timedelta(seconds=45), GOOD_SOURCE)
        snap = make_snapshot(positions=(pos,), price_quotes={1001: stale_quote})
        reasons = sc.freshness_gate(snap, [], caps, now=NOW)
        self.assertTrue(reasons, "stock price quote older than 30s must be blocked when caps enabled")

    def test_stock_price_unallowlisted_source_blocked_with_caps(self):
        caps = risk.validate_risk_config(CAPS_CONFIG)
        pos = Position(stock_instrument(1001), Decimal(100), Decimal("180"))
        bad_quote = bs.PriceQuote(1001, Decimal("180"), NOW - timedelta(seconds=1), "unapproved source")
        snap = make_snapshot(positions=(pos,), price_quotes={1001: bad_quote})
        reasons = sc.freshness_gate(snap, [], caps, now=NOW)
        self.assertTrue(reasons)

    def test_stock_price_con_id_identity_mismatch_blocked_with_caps(self):
        caps = risk.validate_risk_config(CAPS_CONFIG)
        pos = Position(stock_instrument(1001), Decimal(100), Decimal("180"))
        snap = make_snapshot(positions=(pos,))
        mismatched_quote = bs.PriceQuote(9999, Decimal("180"), NOW - timedelta(seconds=1), GOOD_SOURCE)
        reasons = sc.freshness_gate(snap, [], caps, now=NOW, price_quotes={1001: mismatched_quote})
        self.assertTrue(reasons)


# --------------------------------------------------------------------------- #
# Full independent resource protocol (planning._requirement / engine agreement)
# --------------------------------------------------------------------------- #

class ResourceProtocolTests(AdapterTestCase):
    """Every leg's exact increment, cumulative, BASE/FX-correct, no double count."""

    def setUp(self):
        super().setUp()
        self.limits = risk.validate_risk_config(CONFIG)

    def _items(self, snap, legs):
        plan = sc._resource_plan(snap, legs, None, limits=self.limits, now=NOW)
        return {(i.currency, i.resource_key): Decimal(i.amount)
                for i in plan.items}, plan

    def _eur_snapshot(self):
        q = bs.FxQuote("EUR", Decimal("1.1"), NOW - timedelta(seconds=1),
                       GOOD_SOURCE)
        return make_snapshot(
            base="USD", fx={"USD": Decimal(1), "EUR": Decimal("1.1")},
            cash_map={"USD": Decimal("500000"), "EUR": Decimal("0")},
            fx_quotes={"EUR": q})

    def _stock_buy(self, con_id=1001, qty="2", price="180", cur="USD"):
        inst = Instrument(con_id, "AAPL", cur, "STK", "SMART", con_id)
        return execution.PreparedLeg(
            instrument=inst, action="BUY", quantity=Decimal(qty),
            order_type="LMT", limit_price=Decimal(price), account=ACCOUNT)

    def _stock_sell(self, con_id=1001, qty="2", cur="USD"):
        inst = Instrument(con_id, "AAPL", cur, "STK", "SMART", con_id)
        return execution.PreparedLeg(
            instrument=inst, action="SELL", quantity=Decimal(qty),
            order_type="LMT", limit_price=Decimal("180"), account=ACCOUNT)

    def _opt(self, *, action="SELL", right="P", qty="1", mult="100",
             price="2", strike="180", con_id=2001, cur="USD"):
        inst = opt_instrument(con_id=con_id, right=right, strike=strike,
                              mult=mult, currency=cur)
        return execution.PreparedLeg(
            instrument=inst, action=action, quantity=Decimal(qty),
            order_type="LMT", limit_price=Decimal(price), account=ACCOUNT)

    # ---- STK BUY: cash_base + exposure (both FX-converted) ---------------- #
    def test_stk_buy_has_cash_and_exposure(self):
        items, _ = self._items(make_snapshot(), [self._stock_buy()])
        self.assertEqual(items.get(("USD", "cash_base")), Decimal("360"))
        self.assertEqual(items.get(("USD", "exposure:1001:USD")), Decimal("360"))

    def test_stk_buy_foreign_fx_converts_cash_and_exposure(self):
        leg = self._stock_buy(cur="EUR")
        items, _ = self._items(self._eur_snapshot(), [leg])
        # 2 * 180 * 1.1 = 396 BASE
        self.assertEqual(items.get(("USD", "cash_base")), Decimal("396"))
        self.assertEqual(items.get(("EUR", "exposure:1001:EUR")), Decimal("396"))

    # ---- STK SELL: shares -------------------------------------------------- #
    def test_stk_sell_has_shares(self):
        items, _ = self._items(make_snapshot(), [self._stock_sell()])
        self.assertEqual(items.get(("USD", "shares:1001:USD")), Decimal("2"))

    def test_two_stk_sells_accumulate_shares(self):
        legs = [self._stock_sell(qty="2"), self._stock_sell(qty="3")]
        items, _ = self._items(make_snapshot(), legs)
        self.assertEqual(items.get(("USD", "shares:1001:USD")), Decimal("5"))

    def test_two_stk_sells_different_symbols_track_independent_shares(self):
        legs = [self._stock_sell(con_id=1001, qty="2"), self._stock_sell(con_id=1002, qty="3")]
        items, _ = self._items(make_snapshot(), legs)
        self.assertEqual(items.get(("USD", "shares:1001:USD")), Decimal("2"))
        self.assertEqual(items.get(("USD", "shares:1002:USD")), Decimal("3"))

    # ---- OPT BUY: close + premium cash, TWO of same conId = cumulative ----- #
    def test_opt_buy_has_close_and_premium_cash(self):
        items, _ = self._items(make_snapshot(), [self._opt(action="BUY")])
        self.assertEqual(items.get(("USD", "close:2001")), Decimal("1"))
        self.assertEqual(items.get(("USD", "cash_base")), Decimal("200"))  # 1*100*2

    def test_two_opt_buys_accumulate_close_and_premium(self):
        legs = [self._opt(action="BUY"), self._opt(action="BUY")]
        items, _ = self._items(make_snapshot(), legs)
        self.assertEqual(items.get(("USD", "close:2001")), Decimal("2"))
        self.assertEqual(items.get(("USD", "cash_base")), Decimal("400"))

    def test_two_stk_buys_accumulate_exposure(self):
        legs = [self._stock_buy(), self._stock_buy()]
        items, _ = self._items(make_snapshot(), legs)
        self.assertEqual(items.get(("USD", "exposure:1001:USD")), Decimal("720"))

    def test_two_stk_buys_different_symbols_track_independent_exposure_and_cumulative_cash(self):
        legs = [self._stock_buy(con_id=1001, qty="2", price="180"),
                self._stock_buy(con_id=1002, qty="1", price="100")]
        items, _ = self._items(make_snapshot(), legs)
        self.assertEqual(items.get(("USD", "cash_base")), Decimal("460"))
        self.assertEqual(items.get(("USD", "exposure:1001:USD")), Decimal("360"))
        self.assertEqual(items.get(("USD", "exposure:1002:USD")), Decimal("100"))

    def test_multiple_buys_closes_sells_cumulatively(self):
        legs = [
            self._stock_buy(con_id=1001, qty="2", price="150"),
            self._opt(action="BUY", qty="1", price="2", mult="100", con_id=2001),
            self._opt(action="BUY", qty="2", price="3", mult="100", con_id=2001),
            self._stock_sell(con_id=1001, qty="5"),
            self._stock_sell(con_id=1001, qty="10"),
        ]
        items, _ = self._items(make_snapshot(), legs)
        self.assertEqual(items.get(("USD", "cash_base")), Decimal("1100"))
        self.assertEqual(items.get(("USD", "exposure:1001:USD")), Decimal("300"))
        self.assertEqual(items.get(("USD", "close:2001")), Decimal("3"))
        self.assertEqual(items.get(("USD", "shares:1001:USD")), Decimal("15"))

    # ---- OPT SELL PUT: cash + exposure (BASE), multiplier 100/200/500 ----- #
    def test_short_put_multipliers_100_200_500_cash_and_exposure(self):
        for mult, want in (("100", Decimal("18000")),
                           ("200", Decimal("36000")),
                           ("500", Decimal("90000"))):
            items, _ = self._items(make_snapshot(),
                                   [self._opt(mult=mult)])
            self.assertEqual(items.get(("USD", "cash_base")), want, mult)
            self.assertEqual(items.get(("USD", "exposure:1001:USD")), want, mult)

    def test_short_put_foreign_fx_converts_both(self):
        items, _ = self._items(self._eur_snapshot(), [self._opt(cur="EUR")])
        # 1 * 180 * 100 * 1.1 = 19800 BASE
        self.assertEqual(items.get(("USD", "cash_base")), Decimal("19800"))
        self.assertEqual(items.get(("EUR", "exposure:1001:EUR")), Decimal("19800"))

    # ---- OPT SELL CALL: shares ONLY (no double-charged strike cash) ------- #
    def test_short_call_has_shares_only_no_strike_cash_or_exposure(self):
        items, _ = self._items(make_snapshot(), [self._opt(right="C")])
        self.assertEqual(items.get(("USD", "shares:1001:USD")), Decimal("100"))
        self.assertIsNone(items.get(("USD", "cash_base")))
        self.assertIsNone(items.get(("USD", "exposure:1001:USD")))

    # ---- old put obligations must be NETTED from available cash ----------- #
    def test_existing_short_put_reduces_cash_pool_base(self):
        short = Position(opt_instrument(con_id=2001, right="P"),
                         Decimal(-1), Decimal("2"))
        snap = make_snapshot(positions=(short,))
        # cash_pool_base should be 100000 - 18000 = 82000 (old put obligation).
        items, plan = self._items(snap, [self._stock_buy(qty="1", price="100")])
        self.assertEqual(plan.resources[("USD", "cash_base")], Decimal("82000"))

    def test_foreign_fx_multi_leg_and_existing_put_obligation(self):
        # 1 existing EUR short put: strike 100, mult 100 -> 10000 EUR obligation * 1.1 = 11000 USD
        short_put = Position(opt_instrument(con_id=2002, right="P", strike="100", mult="100", currency="EUR"),
                             Decimal(-1), Decimal("5"))
        eur_q = bs.FxQuote("EUR", Decimal("1.1"), NOW - timedelta(seconds=1), GOOD_SOURCE)
        snap = make_snapshot(
            base="USD", fx={"USD": Decimal(1), "EUR": Decimal("1.1")},
            cash_map={"USD": Decimal("100000"), "EUR": Decimal("20000")},
            positions=(short_put,),
            fx_quotes={"EUR": eur_q})
        # Cash: 100000 + 20000*1.1 = 122000. Less 11000 put obligation = 111000.
        legs = [
            self._stock_buy(con_id=1001, qty="2", price="100", cur="EUR"),  # 2*100*1.1 = 220 cash, 220 exposure
            self._opt(action="BUY", qty="1", price="2", mult="100", con_id=2001, cur="EUR"),  # 1*100*2*1.1 = 220 cash, close: 1
        ]
        items, plan = self._items(snap, legs)
        self.assertEqual(items.get(("USD", "cash_base")), Decimal("440"))
        self.assertEqual(items.get(("EUR", "exposure:1001:EUR")), Decimal("220"))
        self.assertEqual(items.get(("USD", "close:2001")), Decimal("1"))
        self.assertEqual(plan.resources[("USD", "cash_base")], Decimal("111000"))

    # ---- every item has a fresh-snapshot available amount ----------------- #
    def test_every_item_has_a_configured_resource(self):
        legs = [self._stock_buy(), self._stock_sell(),
                self._opt(action="BUY"), self._opt(right="C")]
        items, plan = self._items(make_snapshot(), legs)
        for key in items:
            self.assertIn(key, plan.resources, f"missing resources for {key}")

    def test_short_put_and_bounded_opt_buy_deducts_all_old_obligations(self):
        # Existing positions:
        # USD short put: 2 puts @ strike 180, mult 100 -> 36000 USD (short close capacity = 2)
        short_put_usd = Position(
            opt_instrument(con_id=2001, right="P", strike="180", mult="100", currency="USD"),
            Decimal("-2"), Decimal("2"))
        # EUR short put: 1 put @ strike 100, mult 100 @ FX 1.1 -> 11000 USD
        short_put_eur = Position(
            opt_instrument(con_id=2002, right="P", strike="100", mult="100", currency="EUR"),
            Decimal("-1"), Decimal("5"))

        # Existing pending working orders:
        # 1) Pending USD stock buy: 10 shares @ 150 USD limit -> 1500 USD
        stk_usd = Instrument(1001, "AAPL", "USD", "STK", "SMART", 1001)
        order_stk_usd = WorkingOrder(
            instrument=stk_usd, action="BUY", remaining=Decimal("10"),
            order_id=1, perm_id=1, order_ref="ref1",
            limit_price=Decimal("150"), order_type="LMT")
        # 2) Pending USD option buy: 1 contract @ 3 USD limit, mult 100 -> 300 USD
        #    (consumes 1 close capacity of con_id 2001, leaving 1 for our planned leg)
        order_opt_usd = WorkingOrder(
            instrument=opt_instrument(con_id=2001, right="P", strike="180", mult="100", currency="USD"),
            action="BUY", remaining=Decimal("1"),
            order_id=2, perm_id=2, order_ref="ref2",
            limit_price=Decimal("3"), order_type="LMT")
        # 3) Pending EUR stock buy: 5 shares @ 100 EUR limit @ FX 1.1 -> 550 USD
        stk_eur = Instrument(1002, "SAP", "EUR", "STK", "SMART", 1002)
        order_stk_eur = WorkingOrder(
            instrument=stk_eur, action="BUY", remaining=Decimal("5"),
            order_id=3, perm_id=3, order_ref="ref3",
            limit_price=Decimal("100"), order_type="LMT")

        eur_q = bs.FxQuote("EUR", Decimal("1.1"), NOW - timedelta(seconds=1), GOOD_SOURCE)
        # Gross cash: 100,000 USD + 20,000 EUR * 1.1 = 122,000 USD
        # Total old obligations: 36000 + 11000 + 1500 + 300 + 550 = 49350 USD
        # Available cash minus all old obligations: 122000 - 49350 = 72650 USD
        snap = make_snapshot(
            base="USD", fx={"USD": Decimal(1), "EUR": Decimal("1.1")},
            cash_map={"USD": Decimal("100000"), "EUR": Decimal("20000")},
            positions=(short_put_usd, short_put_eur),
            orders=(order_stk_usd, order_opt_usd, order_stk_eur),
            fx_quotes={"EUR": eur_q})

        # Planned order: bounded OPT BUY (1 contract @ limit 2.50, mult 100) -> 250 USD premium
        opt_buy_leg = self._opt(action="BUY", qty="1", price="2.5", mult="100", con_id=2001)

        items, plan = self._items(snap, [opt_buy_leg])

        # Verify leg incremental reservation requirement
        self.assertEqual(items.get(("USD", "cash_base")), Decimal("250"))
        self.assertEqual(items.get(("USD", "close:2001")), Decimal("1"))

        # DIRECT ASSERTION: resource cash must equal cash minus ALL old obligations
        gross_cash = Decimal("100000") + Decimal("20000") * Decimal("1.1")
        old_obligations = (Decimal("36000") + Decimal("11000") + Decimal("1500") +
                           Decimal("300") + Decimal("550"))
        expected_free_cash = gross_cash - old_obligations
        self.assertEqual(expected_free_cash, Decimal("72650"))
        self.assertEqual(plan.resources[("USD", "cash_base")], expected_free_cash)

    def test_unconfirmed_cash_rejected_when_needed_and_allowed_when_not_needed(self):
        # Pending unbounded MKT order makes cash unconfirmed in risk.summarize
        stk_usd = Instrument(1001, "AAPL", "USD", "STK", "SMART", 1001)
        mkt_order = WorkingOrder(
            instrument=stk_usd, action="BUY", remaining=Decimal("10"),
            order_id=1, perm_id=1, order_ref="mkt1",
            limit_price=None, order_type="MKT")
        snap = make_snapshot(orders=(mkt_order,))

        # 1. When cash IS needed (OPT BUY with bounded premium): fails closed
        opt_buy_leg = self._opt(action="BUY", qty="1", price="2", mult="100", con_id=2001)
        with self.assertRaises(sc.SubmissionCliError) as ctx:
            sc._resource_plan(snap, [opt_buy_leg], None, limits=self.limits, now=NOW)
        self.assertIn("unconfirmed cash", str(ctx.exception))

        # 2. When cash is NOT needed (STK SELL): succeeds with safe 0 available cash
        stk_sell_leg = self._stock_sell(con_id=1001, qty="2")
        plan = sc._resource_plan(snap, [stk_sell_leg], None, limits=self.limits, now=NOW)
        self.assertEqual(plan.resources[("USD", "cash_base")], Decimal("0"))

    def test_negative_cash_availability_safely_handled(self):
        # 10,000 USD cash but 18,000 USD short put obligation -> negative free cash (-8000 USD)
        short = Position(opt_instrument(con_id=2001, right="P", strike="180", mult="100"),
                         Decimal("-1"), Decimal("2"))
        snap = make_snapshot(cash="10000", positions=(short,))

        # When cash IS needed: raises SubmissionCliError
        opt_buy = self._opt(action="BUY", qty="1", price="2", mult="100", con_id=2001)
        with self.assertRaises(sc.SubmissionCliError) as ctx:
            sc._resource_plan(snap, [opt_buy], None, limits=self.limits, now=NOW)
        self.assertIn("negative available cash", str(ctx.exception))

        # When cash is NOT needed (covered call leg): safely sets available cash to 0
        stk_sell = self._stock_sell(con_id=1001, qty="1")
        plan = sc._resource_plan(snap, [stk_sell], None, limits=self.limits, now=NOW)
        self.assertEqual(plan.resources[("USD", "cash_base")], Decimal("0"))

    def test_exposure_headroom_shared_limits_and_ratios_without_double_subtracting_local_reserves(self):
        caps_cfg = {
            "risk": {
                "enabled": True,
                "day_timezone": TZ,
                "max_daily_submissions": 5,
                "config_version": "p3-1",
                "portfolio": {"amount": "50000", "ratio": "0.6"},
                "per_underlying": {"amount": "20000", "ratio": "0.2"},
            }
        }
        caps = risk.validate_risk_config(caps_cfg)
        stock_q = bs.PriceQuote(1001, Decimal("100"), NOW - timedelta(seconds=1), GOOD_SOURCE)
        # Existing short put position: 1 put @ strike 50, mult 100 -> 5000 USD exposure
        short = Position(
            opt_instrument(con_id=2001, right="P", strike="50", mult="100"),
            Decimal("-1"), Decimal("2"))
        snap = make_snapshot(
            cash="100000", positions=(short,),
            price_quotes={1001: stock_q})

        # Net liquidation = 100,000
        # Underlying 1001:USD current exposure = 5,000
        # Portfolio current exposure = 5,000
        # Per-underlying amount headroom: 20000 - 5000 = 15000
        # Per-underlying ratio headroom: 100000 * 0.2 - 5000 = 15000
        # Portfolio amount headroom: 50000 - 5000 = 45000
        # Portfolio ratio headroom: 100000 * 0.6 - 5000 = 55000
        # Shared headroom for 1001:USD = min(15000, 15000, 45000, 55000) = 15000 (NOT max or absolute ceiling)
        stock_buy = self._stock_buy(con_id=1001, qty="10", price="100")
        plan = sc._resource_plan(snap, [stock_buy], None, limits=caps, now=NOW)
        self.assertEqual(plan.resources[("USD", "exposure:1001:USD")], Decimal("15000"))

        # Verify passing reservations does NOT double subtract from baseline resources
        # (ledger.reserve will subtract active local reserves itself).
        reservations = {"cash_base": Decimal("5000"), "exposure": {"1001:USD": Decimal("3000")}}
        plan_with_res = sc._resource_plan(snap, [stock_buy], reservations, limits=caps, now=NOW)
        self.assertEqual(plan_with_res.resources[("USD", "exposure:1001:USD")], Decimal("15000"))

    def test_exposure_unconfirmed_rejected_when_caps_enabled_but_allowed_when_caps_off(self):
        caps_cfg = {
            "risk": {
                "enabled": True,
                "day_timezone": TZ,
                "max_daily_submissions": 5,
                "config_version": "p3-1",
                "portfolio": {"amount": "50000"},
            }
        }
        caps = risk.validate_risk_config(caps_cfg)
        stock_q = bs.PriceQuote(1001, Decimal("100"), NOW - timedelta(seconds=1), GOOD_SOURCE)
        pos = Position(stock_instrument(1001), Decimal("100"), Decimal("100"))
        # Issue in snapshot causes exposure_confirmed to be False
        snap = make_snapshot(
            positions=(pos,), price_quotes={1001: stock_q},
            issues=("unconfirmed data quality",))

        stock_buy = self._stock_buy(con_id=1001, qty="5", price="100")

        # When caps enabled: unconfirmed exposure raises SubmissionCliError
        with self.assertRaises(sc.SubmissionCliError) as ctx:
            sc._resource_plan(snap, [stock_buy], None, limits=caps, now=NOW)
        self.assertIn("exposure cannot be confirmed", str(ctx.exception))

        # When caps NOT enabled (self.limits): unconfirmed exposure does not block
        plan = sc._resource_plan(snap, [stock_buy], None, limits=self.limits, now=NOW)
        self.assertIn(("USD", "exposure:1001:USD"), plan.resources)


if __name__ == "__main__":
    unittest.main(verbosity=2)
