#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline tests for thetamill.execution — shared submission engine.

All state lives in temporary directories.  Broker-like objects, snapshot
providers, resource plans, order builders and fill proofs are MOCKS: no real
account, network, ``ib_async`` import or CLI import is performed.
"""
import multiprocessing as mp
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from thetamill import risk, state
from thetamill.broker_state import Instrument, Position, Snapshot, WorkingOrder
from thetamill.execution import (
    BrokerReceipt, ConfirmResult, ExecutionEngine, ExecutionError,
    FillProof, PositiveHandoff, Preflight, PreparedLeg, ResourcePlan,
    UnsafeComboError, aggregate_reservations,
)
from thetamill.risk import validate_risk_config
from thetamill.state import RiskReservationSpec, SubmissionLedger

TZ = "America/New_York"
ACCOUNT = "DU1234567"
ENV = "paper"
CONFIG = {"risk": {"enabled": True, "day_timezone": TZ,
                   "max_daily_submissions": 5, "config_version": "p3-1"}}
NOW = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)  # 10:00 EST


def _clock():
    return NOW


def _stock(con_id=1001, symbol="AAPL", currency="USD"):
    return Instrument(con_id=con_id, symbol=symbol, currency=currency,
                      sec_type="STK", exchange="SMART", underlying_id=con_id)


def _snapshot(*, cash="100000", issues=(), captured_at=None, positions=(),
              orders=(), account=ACCOUNT, environment=ENV):
    return Snapshot(
        account=account, environment=environment,
        captured_at=captured_at or NOW - timedelta(seconds=2),
        base_currency="USD", cash={"USD": Decimal(cash)},
        net_liquidation=Decimal(cash),
        positions=tuple(positions), orders=tuple(orders), fx={"USD": Decimal(1)},
        issues=tuple(issues))


def _fill_proof(*, quantity=Decimal("200"), con_id=1001, status="Filled"):
    """Structured FillProof builder (the engine verifies EVERY field)."""
    def proof(broker, ref, ids):
        return FillProof(order_ref=ref, account=ACCOUNT, environment=ENV,
                         con_id=con_id, filled_quantity=quantity, status=status)
    return proof


def _handoff(ref, *, oid=None, pid=None, con_id=1001, action="BUY",
             status="Submitted", captured_at=None, remaining=Decimal("2"),
             account=ACCOUNT, environment=ENV, orders=None):
    """Build a STRUCTURED PositiveHandoff whose observed snapshot has a
    matching WORKING order (conId/action/remaining consistent with the leg)."""
    inst = _stock(con_id)
    wo = WorkingOrder(instrument=inst, action=action, remaining=remaining,
                      order_id=oid if oid is not None else 42,
                      perm_id=pid if pid is not None else 987,
                      order_ref=ref, limit_price=Decimal("180"),
                      order_type="LMT")
    snap = _snapshot(captured_at=captured_at or NOW + timedelta(seconds=1),
                     orders=(wo,) if orders is None else orders,
                     account=account, environment=environment)
    return PositiveHandoff(
        broker_status=status, order_ref=ref, account=account,
        environment=environment, observed_snapshot=snap, con_id=con_id)


def _leg(*, action="BUY", order_type="LMT", con_id=1001,
         limit_price=Decimal("180"), quantity=Decimal("2"), **kw):
    fields = dict(
        instrument=_stock(con_id), action=action, quantity=quantity,
        order_type=order_type, limit_price=limit_price, tif="DAY",
        exchange="SMART", outside_rth=False, strategy="covered-call",
        account=ACCOUNT, deliverable_verified=True)
    fields.update(kw)
    return PreparedLeg(**fields)


def _canonical_plan(snapshot, legs, aggregate, **kw):
    """Realistic FULL-package resource_plan mirroring the engine's independent
    derivation for EVERY leg (increasing AND decreasing).

    Money (cash / exposure) is BASE-denominated; shares / close are
    dimensionless counts.  Each item also has a fresh-snapshot available amount
    so the ledger's ``reserve`` can succeed.  A deferred combo supplies the
    post-fill shares override.
    """
    base = snapshot.base_currency.upper()
    cash = sum(snapshot.cash.values(), Decimal(0))
    items: dict[tuple, Decimal] = {}

    def add(currency, key, amount):
        if amount <= 0:
            return
        k = (currency, key)
        items[k] = items.get(k, Decimal(0)) + amount

    for leg in legs:
        inst = leg.instrument
        qty = leg.quantity
        action = leg.action.upper()
        trade_cur = inst.currency.upper()
        uid = inst.underlying_id
        if inst.sec_type == "STK" and action == "BUY":
            notional = qty * leg.limit_price
            add(base, "cash_base", notional)
            add(base, f"exposure:{uid}:{trade_cur}", notional)
        elif inst.sec_type == "STK" and action == "SELL":
            add(trade_cur, f"shares:{uid}:{trade_cur}", qty)
        elif inst.sec_type == "OPT" and action == "SELL":
            if inst.right.upper() == "P":
                collateral = qty * inst.strike * inst.multiplier
                add(base, "cash_base", collateral)
                add(base, f"exposure:{uid}:{trade_cur}", collateral)
            else:  # CALL
                add(trade_cur, f"shares:{uid}:{trade_cur}", qty * inst.multiplier)
        elif inst.sec_type == "OPT" and action == "BUY":
            add(base, f"close:{inst.con_id}", qty)
            add(base, "cash_base", qty * inst.multiplier * leg.limit_price)

    # Authoritative AVAILABLE amounts: generous cash/exposure headroom, plus
    # per-key amounts so every reserved item has an availability entry.
    resources = {(base, "cash_base"): cash}
    for (cur, key) in items:
        if key.startswith("shares:") or key.startswith("close:"):
            resources.setdefault((cur, key), Decimal("1000000"))
        else:  # cash_base / exposure
            resources.setdefault((cur, key), cash)

    overrides = {}
    if any(getattr(leg, "coverage_deferred", False) for leg in legs):
        for leg in legs:
            if leg.instrument.sec_type == "STK" and leg.action.upper() == "BUY":
                cur = leg.instrument.currency.upper()
                key = (cur, f"shares:{leg.instrument.underlying_id}:{cur}")
                overrides[key] = resources.get(key, Decimal(0)) + leg.quantity

    return ResourcePlan(
        resources=resources,
        items=[RiskReservationSpec(cur, key, str(amount))
               for (cur, key), amount in sorted(items.items())],
        overrides=overrides or kw.pop("overrides", {}),
        **kw)


# Backwards-compatible alias: the default plan is now the full canonical one.
_stock_cash_plan = _canonical_plan


class _Broker:
    """Mock broker: records calls, never connects anywhere."""

    def __init__(self, receipts=None):
        self.calls = []
        self._receipts = list(receipts or [])

    def place(self, broker, leg, ref):
        self.calls.append((leg.instrument.con_id, ref))
        if self._receipts:
            return self._receipts.pop(0)
        return BrokerReceipt(order_id=f"ord-{len(self.calls)}",
                             perm_id=f"perm-{len(self.calls)}",
                             status="Submitted")


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = self.root / "ledger.sqlite3"
        self.ledger = SubmissionLedger(self.db, TZ)
        self.broker = _Broker()
        self.snapshot_calls = []
        self.limits = validate_risk_config(CONFIG)

    def _snapshot_provider(self):
        self.snapshot_calls.append(datetime.now(timezone.utc))
        return _snapshot()

    def _engine(self, **overrides):
        kwargs = dict(
            ledger=self.ledger, snapshot_provider=self._snapshot_provider,
            broker=self.broker, place_order=self.broker.place,
            resource_plan=_stock_cash_plan,
            config=CONFIG, limits=self.limits, lock_dir=self.root / "locks",
            verified_prior_submissions=0, clock=_clock)
        kwargs.update(overrides)
        return ExecutionEngine(**kwargs)

    def _preflight_confirm(self, engine, legs=None, **overrides):
        legs = legs or [_leg()]
        pre = engine.preflight(legs, account=ACCOUNT, environment=ENV)
        kwargs = dict(intent_id=pre.intent_id, code=pre.approval_code,
                      legs=legs, account=ACCOUNT, environment=ENV)
        kwargs.update(overrides)
        return pre, engine.confirm(**kwargs)


# -- preflight / canonical binding ----------------------------------------

class TestPreflight(EngineTestCase):
    def test_preflight_persists_fresh_uuid_full_payload_and_code(self):
        import json
        engine = self._engine()
        one = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        two = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        self.assertIsInstance(one, Preflight)
        self.assertEqual(len(one.full_digest), 64)
        self.assertEqual(len(one.approval_code), 8)
        self.assertNotEqual(one.intent_id, two.intent_id)
        self.assertNotEqual(one.full_digest, two.full_digest)
        row = self.ledger.get_intent(one.intent_id)
        leg = json.loads(row["payload"])["legs"][0]
        for field in ("instrument", "action", "quantity", "order_type",
                      "limit_price", "stop_price", "tif", "exchange",
                      "outside_rth", "strategy", "account",
                      "deliverable_verified", "extra"):
            self.assertIn(field, leg)
        self.assertEqual(leg["action"], "BUY")
        self.assertEqual(leg["outside_rth"], False)
        self.assertEqual(leg["instrument"]["con_id"], 1001)
        self.assertEqual(row["config_digest"], state.config_digest(CONFIG))
        self.assertEqual(row["state"], "pending")
        self.assertEqual(self.broker.calls, [])  # no network in preflight
        self.assertEqual(self.snapshot_calls, [])

    def test_config_field_change_invalidates_old_intent_digest(self):
        engine = self._engine()
        pre = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        changed = dict(CONFIG, risk=dict(CONFIG["risk"], max_daily_submissions=6))
        other = self._engine(config=changed,
                             limits=validate_risk_config(changed))
        with self.assertRaises(ExecutionError) as ctx:
            other.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                          legs=[_leg()], account=ACCOUNT, environment=ENV)
        self.assertIn("digest mismatch", str(ctx.exception))
        self.assertEqual(self.broker.calls, [])

    def test_unsupported_deliverable_fails_closed(self):
        engine = self._engine()
        with self.assertRaises(UnsafeComboError) as ctx:
            engine.preflight([_leg(deliverable_verified=False)],
                             account=ACCOUNT, environment=ENV)
        self.assertIn("never infers", str(ctx.exception))
        self.assertEqual(self.broker.calls, [])

    def test_canonical_extra_fields_change_digest(self):
        engine = self._engine()
        a = engine.preflight([_leg(canonical={"note": "v1"})],
                             account=ACCOUNT, environment=ENV)
        b = engine.preflight([_leg(canonical={"note": "v2"})],
                             account=ACCOUNT, environment=ENV)
        self.assertNotEqual(a.full_digest, b.full_digest)


# -- confirm gates ---------------------------------------------------------

class TestConfirmGates(EngineTestCase):
    def test_explicit_intent_id_and_code_required(self):
        engine = self._engine()
        pre = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        with self.assertRaises(ExecutionError):
            engine.confirm(intent_id="", code=pre.approval_code, legs=[_leg()],
                           account=ACCOUNT, environment=ENV)
        with self.assertRaises(ExecutionError):
            engine.confirm(intent_id=pre.intent_id, code="", legs=[_leg()],
                           account=ACCOUNT, environment=ENV)
        with self.assertRaises(ExecutionError):
            engine.confirm(intent_id=pre.intent_id, code="DEADBEEF",
                           legs=[_leg()], account=ACCOUNT, environment=ENV)
        with self.assertRaises(ExecutionError):  # unknown id: no legacy fallback
            engine.confirm(intent_id="int-unknown", code=pre.approval_code,
                           legs=[_leg()], account=ACCOUNT, environment=ENV)
        self.assertEqual(self.broker.calls, [])

    def test_expired_intent_blocked(self):
        engine = self._engine(ttl_seconds=1)
        pre = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        later_engine = self._engine(clock=lambda: NOW + timedelta(seconds=2))
        with self.assertRaises(ExecutionError) as ctx:
            later_engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                                 legs=[_leg()], account=ACCOUNT, environment=ENV)
        self.assertIn("expired", str(ctx.exception))
        self.assertEqual(self.broker.calls, [])

    def test_snapshot_issues_block_and_no_send(self):
        engine = self._engine()
        pre = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        bad = self._engine(snapshot_provider=lambda: _snapshot(issues=("FX stale",)))
        with self.assertRaises(ExecutionError) as ctx:
            bad.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                        legs=[_leg()], account=ACCOUNT, environment=ENV)
        self.assertIn("data-quality", str(ctx.exception))
        self.assertEqual(self.broker.calls, [])

    def test_risk_block_prevents_reservation_and_send(self):
        engine = self._engine()
        huge = _leg(quantity=Decimal("9999999"))
        pre = engine.preflight([huge], account=ACCOUNT, environment=ENV)
        with self.assertRaises(ExecutionError) as ctx:
            engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                           legs=[huge], account=ACCOUNT, environment=ENV)
        self.assertIn("risk BLOCK", str(ctx.exception))
        self.assertEqual(self.broker.calls, [])
        self.assertFalse(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV, day_key="2026-11-07")["exists"])

    def test_empty_package_for_risk_increasing_leg_fails_closed(self):
        engine = self._engine(resource_plan=lambda s, l, a: ResourcePlan(
            resources={("USD", "cash_base"): Decimal("100000")}, items=[]))
        pre = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        with self.assertRaises(UnsafeComboError) as ctx:
            engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                           legs=[_leg()], account=ACCOUNT, environment=ENV)
        self.assertIn("under-sized", str(ctx.exception))
        self.assertEqual(self.broker.calls, [])

    def test_two_puts_jointly_overspend_blocked_before_any_send(self):
        # Two short PUTs, each requiring 4000 cash with only 4000 free.  Both
        # individually pass against the same snapshot free cash, but the FULL
        # PACKAGE (sum of increments = 8000) must be blocked before ANY send.
        put_inst = Instrument(
            con_id=3001, symbol="AAPL", currency="USD", sec_type="OPT",
            exchange="SMART", underlying_id=1001, multiplier=Decimal(100),
            right="P", strike=Decimal(20), expiry="20261218")
        put = PreparedLeg(
            instrument=put_inst, action="SELL", quantity=Decimal(2),
            order_type="LMT", limit_price=Decimal("1"), tif="DAY",
            exchange="SMART", account=ACCOUNT, deliverable_verified=True)

        def plan(snapshot, legs, aggregate):
            # Two short PUTs, cash 4000/exposure 4000 each; the WHOLE package
            # (8000/8000) is reserved so risk.assess sees the joint overspend.
            return ResourcePlan(
                resources={("USD", "cash_base"): Decimal("4000"),
                           ("USD", "exposure:1001:USD"): Decimal("4000")},
                items=[RiskReservationSpec("USD", "cash_base", "4000"),
                       RiskReservationSpec("USD", "cash_base", "4000"),
                       RiskReservationSpec("USD", "exposure:1001:USD", "4000"),
                       RiskReservationSpec("USD", "exposure:1001:USD", "4000")])
        engine = self._engine(
            snapshot_provider=lambda: _snapshot(cash="4000"),
            resource_plan=plan)
        pre = engine.preflight([put, put], account=ACCOUNT, environment=ENV)
        with self.assertRaises(ExecutionError):
            engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                           legs=[put, put], account=ACCOUNT, environment=ENV)
        self.assertEqual(self.broker.calls, [])

    def test_migration_freeze_blocks_new_risk_with_explanation(self):
        engine = self._engine(verified_prior_submissions=None)
        pre = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        result = engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                                legs=[_leg()], account=ACCOUNT, environment=ENV)
        self.assertIsInstance(result, ConfirmResult)
        self.assertTrue(result.frozen)
        self.assertEqual(result.decision, "BLOCK")
        self.assertIn("cannot prove zero", result.explanation)
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(result.reservation_id, "")

    def test_disabled_p3_internal_budget_never_clobbers_existing_day(self):
        legacy = {"risk": {"enabled": False}}
        limits = validate_risk_config(legacy)
        self.ledger.set_daily_limit(account=ACCOUNT, environment=ENV,
                                    day_key="2026-11-07", limit_count=1)
        engine = self._engine(config=legacy, limits=limits,
                              verified_prior_submissions=None)
        pre, result = self._preflight_confirm(engine)
        self.assertFalse(result.frozen)
        self.assertEqual(result.decision, "ALLOW")
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key="2026-11-07")["limit_count"], 1)

    def test_whole_account_block_on_unresolved_submission(self):
        engine = self._engine()
        pre, first = self._preflight_confirm(engine)
        # First leg is merely "submitted" (callback), so it stays unresolved.
        self.assertTrue(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))
        pre2 = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        with self.assertRaises(ExecutionError) as ctx:
            engine.confirm(intent_id=pre2.intent_id, code=pre2.approval_code,
                           legs=[_leg()], account=ACCOUNT, environment=ENV)
        self.assertIn("unresolved submissions", str(ctx.exception))
        self.assertEqual(len(self.broker.calls), 1)  # no new send


# -- successful single-leg submit ------------------------------------------

class TestSingleLegSubmit(EngineTestCase):
    def test_durable_per_leg_attempt_before_place_order(self):
        observed = {}

        def place(broker, leg, ref):
            observed["budget"] = self.ledger.get_day_budget(
                account=ACCOUNT, environment=ENV, day_key="2026-11-07")
            observed["legs"] = [l.state for l in self.ledger.list_legs(
                account=ACCOUNT, environment=ENV)]
            return BrokerReceipt(order_id=42, perm_id="987", status="Submitted")

        engine = self._engine(place_order=place)
        pre, result = self._preflight_confirm(engine)
        self.assertEqual(result.decision, "ALLOW")
        self.assertEqual(result.legs[0].state, "submitted")  # NOT confirmed
        self.assertEqual(result.legs[0].order_id, "42")
        self.assertEqual(observed["budget"]["submitted_count"], 1)
        self.assertEqual(observed["budget"]["reserved_count"], 0)
        self.assertEqual(observed["legs"], ["submitting"])
        # Callback does NOT release risk; still outstanding/unresolved.
        self.assertEqual(self.ledger.get_reservation(
            result.reservation_id).state, "submitted")
        self.assertTrue(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))
        self.assertGreater(aggregate_reservations(
            self.ledger, ACCOUNT, ENV)["cash_base"], 0)

    def test_positive_snapshot_handoff_is_separate_and_releases_risk(self):
        engine = self._engine()
        pre, result = self._preflight_confirm(engine)
        ref = result.legs[0].order_ref
        # Use the SAME ids bound during place_order (immutable anchors).  The
        # structured evidence must mechanically map them to a real WORKING order.
        evidence = _handoff(ref, oid=result.legs[0].order_id,
                            pid=result.legs[0].perm_id, status="Submitted")
        derived = engine.reconcile_positive(
            account=ACCOUNT, environment=ENV, order_ref=ref,
            evidence=evidence, perm_id=result.legs[0].perm_id,
            order_id=result.legs[0].order_id)
        self.assertEqual(derived, "confirmed")
        self.assertEqual(self.ledger.get_reservation(
            result.reservation_id).state, "confirmed")
        self.assertFalse(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))
        self.assertEqual(aggregate_reservations(
            self.ledger, ACCOUNT, ENV)["cash_base"], 0)

    def test_reconcile_positive_rejects_bad_string_status_and_filled(self):
        engine = self._engine()
        pre, result = self._preflight_confirm(engine)
        ref = result.legs[0].order_ref
        # A bare broker_status string is no longer accepted at all.
        with self.assertRaises(TypeError):
            engine.reconcile_positive(
                account=ACCOUNT, environment=ENV, order_ref=ref,
                broker_status="Filled")
        # A Filled/terminal handoff is conservatively UNSUPPORTED (no working
        # order can prove attribution).
        with self.assertRaises(ExecutionError):
            engine.reconcile_positive(
                account=ACCOUNT, environment=ENV, order_ref=ref,
                evidence=_handoff(ref, status="Filled"))
        self.assertEqual(self.ledger.get_reservation(
            result.reservation_id).state, "submitted")

    def test_unknown_receipt_never_retried_and_keeps_outstanding(self):
        unknown_broker = _Broker([BrokerReceipt(order_id=42, perm_id="987",
                                                status=None)])
        engine = self._engine(broker=unknown_broker,
                              place_order=unknown_broker.place)
        pre, result = self._preflight_confirm(engine)
        self.assertEqual(result.legs[0].state, "unknown")
        self.assertEqual(len(unknown_broker.calls), 1)
        self.assertEqual(self.ledger.get_reservation(
            result.reservation_id).state, "unknown")
        self.assertTrue(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))
        pre2 = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        with self.assertRaises(ExecutionError):
            engine.confirm(intent_id=pre2.intent_id, code=pre2.approval_code,
                           legs=[_leg()], account=ACCOUNT, environment=ENV)
        self.assertEqual(len(unknown_broker.calls), 1)  # never retried

    def test_replay_same_intent_cannot_submit_twice(self):
        engine = self._engine()
        pre, first = self._preflight_confirm(engine)
        # Clear the conservative unresolved block via a positive STRUCTURED
        # handoff (working-order evidence), then the consumed intent still
        # refuses a replay.
        engine.reconcile_positive(
            account=ACCOUNT, environment=ENV,
            order_ref=first.legs[0].order_ref,
            evidence=_handoff(first.legs[0].order_ref,
                              oid=first.legs[0].order_id,
                              pid=first.legs[0].perm_id, status="Submitted"),
            perm_id=first.legs[0].perm_id, order_id=first.legs[0].order_id)
        with self.assertRaises(state.ClaimError):
            engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                           legs=[_leg()], account=ACCOUNT, environment=ENV)
        self.assertEqual(len(self.broker.calls), 1)

    def test_pre_send_failure_aborts_and_refunds(self):
        engine = self._engine(place_order=lambda b, l, r: (_ for _ in ()).throw(
            RuntimeError("connection dropped before send")))
        pre = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        with self.assertRaises(ExecutionError) as ctx:
            engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                           legs=[_leg()], account=ACCOUNT, environment=ENV)
        self.assertIn("left conservatively unresolved", str(ctx.exception))
        self.assertTrue(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key="2026-11-07")["submitted_count"], 1)

    def test_day_quota_exhaustion_blocks_new_intent(self):
        small = dict(CONFIG, risk=dict(CONFIG["risk"], max_daily_submissions=1))
        engine = self._engine(config=small, limits=validate_risk_config(small))
        pre, first = self._preflight_confirm(engine)
        engine.reconcile_positive(
            account=ACCOUNT, environment=ENV,
            order_ref=first.legs[0].order_ref,
            evidence=_handoff(first.legs[0].order_ref,
                              oid=first.legs[0].order_id,
                              pid=first.legs[0].perm_id, status="Submitted"),
            perm_id=first.legs[0].perm_id, order_id=first.legs[0].order_id)
        pre2 = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        with self.assertRaises(state.BudgetError):
            engine.confirm(intent_id=pre2.intent_id, code=pre2.approval_code,
                           legs=[_leg()], account=ACCOUNT, environment=ENV)
        self.assertEqual(len(self.broker.calls), 1)


# -- deferred-coverage combo ----------------------------------------------

def _call_leg(quantity=Decimal("1")):
    inst = Instrument(
        con_id=2002, symbol="AAPL", currency="USD", sec_type="OPT",
        exchange="SMART", underlying_id=1001, multiplier=Decimal(100),
        right="C", strike=Decimal(200), expiry="20261218",
        trading_class="AAPL")
    return PreparedLeg(
        instrument=inst, action="SELL", quantity=quantity,
        order_type="LMT", limit_price=Decimal("3"), tif="DAY",
        exchange="SMART", strategy="covered-call", account=ACCOUNT,
        deliverable_verified=True, coverage_deferred=True,
        requires_filled_leg=0)


class TestDeferredCombo(EngineTestCase):
    def _deferred_plan(self, snapshot, legs, aggregate):
        cash = sum(snapshot.cash.values(), Decimal(0))
        return ResourcePlan(
            resources={("USD", "cash_base"): cash,
                       ("USD", "exposure:1001:USD"): cash,
                       ("USD", "shares:1001:USD"): Decimal(0)},
            items=[RiskReservationSpec("USD", "cash_base", "36000"),
                   RiskReservationSpec("USD", "exposure:1001:USD", "36000"),
                   RiskReservationSpec("USD", "shares:1001:USD", "100")],
            overrides={("USD", "shares:1001:USD"): Decimal(200)})

    def _engine_with_deferred(self, **kw):
        defaults = dict(resource_plan=self._deferred_plan)
        defaults.update(kw)
        return self._engine(**defaults)

    def test_unsafe_combo_without_fill_proof_rejected(self):
        stock = _leg(con_id=1001, action="BUY", quantity=Decimal(200),
                     strategy="covered-call")
        call_leg = _call_leg()
        engine = self._engine_with_deferred()  # no fill_proof
        with self.assertRaises(UnsafeComboError):
            engine.confirm(intent_id="x", code="y", legs=[stock, call_leg],
                           account=ACCOUNT, environment=ENV)

    def test_unsafe_combo_without_overrides_rejected(self):
        stock = _leg(con_id=1001, action="BUY", quantity=Decimal(200))
        call_leg = _call_leg()
        engine = self._engine(
            resource_plan=lambda s, l, a: ResourcePlan(
                resources={("USD", "cash_base"): Decimal("100000")},
                items=[RiskReservationSpec("USD", "cash_base", "36000")]),
            fill_proof=_fill_proof())
        pre = engine.preflight([stock, call_leg], account=ACCOUNT, environment=ENV)
        with self.assertRaises(UnsafeComboError) as ctx:
            engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                           legs=[stock, call_leg], account=ACCOUNT,
                           environment=ENV)
        self.assertIn("override availability", str(ctx.exception))
        self.assertEqual(self.broker.calls, [])

    def test_deferred_combo_needs_positive_filled_proof_and_fresh_assessment(self):
        stock = _leg(con_id=1001, action="BUY", quantity=Decimal(200),
                     strategy="covered-call")
        call_leg = _call_leg()
        snapshots = []

        def provider():
            if not snapshots:
                snapshots.append(_snapshot())
                return snapshots[0]
            fresh = _snapshot(positions=(
                Position(instrument=_stock(), quantity=Decimal(200),
                         price=Decimal(180)),))
            snapshots.append(fresh)
            return fresh

        broker = _Broker([BrokerReceipt(order_id=1, perm_id="p1",
                                        status="Filled"),
                          BrokerReceipt(order_id=2, perm_id="p2",
                                        status="Submitted")])
        proof_calls = []

        def fill_proof(b, ref, ids):
            proof_calls.append((ref, dict(ids)))
            return FillProof(order_ref=ref, account=ACCOUNT, environment=ENV,
                             con_id=stock.instrument.con_id,
                             filled_quantity=stock.quantity, status="Filled")

        engine = self._engine_with_deferred(
            broker=broker, place_order=broker.place,
            snapshot_provider=provider, fill_proof=fill_proof)
        pre = engine.preflight([stock, call_leg], account=ACCOUNT, environment=ENV)
        result = engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                                legs=[stock, call_leg], account=ACCOUNT,
                                environment=ENV)
        self.assertEqual(len(broker.calls), 2)
        self.assertEqual(result.legs[0].state, "submitted")
        self.assertEqual(result.legs[1].state, "submitted")
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key="2026-11-07")["submitted_count"], 2)
        self.assertEqual(len(proof_calls), 1)
        self.assertEqual(proof_calls[0][0], result.legs[0].order_ref)

    def test_deferred_combo_skips_leg_when_fill_proof_is_generic_positive(self):
        stock = _leg(con_id=1001, action="BUY", quantity=Decimal(200))
        call_leg = _call_leg()
        broker = _Broker([BrokerReceipt(order_id=1, perm_id="p1",
                                        status="Submitted")])
        engine = self._engine_with_deferred(
            broker=broker, place_order=broker.place,
            fill_proof=_fill_proof(status="Submitted"))
        pre = engine.preflight([stock, call_leg], account=ACCOUNT, environment=ENV)
        result = engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                                legs=[stock, call_leg], account=ACCOUNT,
                                environment=ENV)
        self.assertEqual(len(broker.calls), 1)  # leg 1 NOT sent
        self.assertEqual(result.legs[1].state, "skipped")
        self.assertIn("structured Filled proof", result.legs[1].reason)
        budget = self.ledger.get_day_budget(account=ACCOUNT, environment=ENV,
                                            day_key="2026-11-07")
        self.assertEqual(budget["submitted_count"], 1)
        self.assertEqual(budget["reserved_count"], 0)

    def test_deferred_combo_skips_leg_when_fill_proof_missing(self):
        stock = _leg(con_id=1001, action="BUY", quantity=Decimal(200))
        call_leg = _call_leg()
        broker = _Broker([BrokerReceipt(order_id=1, perm_id="p1",
                                        status="Submitted")])
        engine = self._engine_with_deferred(
            broker=broker, place_order=broker.place,
            fill_proof=lambda b, r, ids: None)
        pre = engine.preflight([stock, call_leg], account=ACCOUNT, environment=ENV)
        result = engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                                legs=[stock, call_leg], account=ACCOUNT,
                                environment=ENV)
        self.assertEqual(len(broker.calls), 1)
        self.assertEqual(result.legs[1].state, "skipped")
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key="2026-11-07")["submitted_count"], 1)


# -- public aggregate / dynamic resource plan -----------------------------

class TestResourcePlan(EngineTestCase):
    def test_resource_plan_called_under_lock_with_fresh_snapshot_and_aggregate(self):
        seen = {}

        def plan(snapshot, legs, aggregate):
            seen["fresh"] = snapshot
            seen["aggregate"] = aggregate
            return _stock_cash_plan(snapshot, legs, aggregate)

        engine = self._engine(resource_plan=plan)
        pre, result = self._preflight_confirm(engine)
        self.assertIsInstance(seen["fresh"], Snapshot)
        self.assertIn("cash_base", seen["aggregate"])
        self.assertEqual(result.decision, "ALLOW")

    def test_stale_static_resources_not_accepted(self):
        with self.assertRaises(TypeError):
            ExecutionEngine(
                ledger=self.ledger, snapshot_provider=self._snapshot_provider,
                broker=self.broker, place_order=self.broker.place,
                resource_plan=_stock_cash_plan, config=CONFIG, limits=self.limits,
                lock_dir=self.root / "locks",
                resources={("USD", "cash_base"): Decimal("100000")})

    def test_aggregate_reservations_validates_and_reports_outstanding(self):
        engine = self._engine()
        pre, result = self._preflight_confirm(engine)
        agg = aggregate_reservations(self.ledger, ACCOUNT, ENV)
        self.assertGreater(agg["cash_base"], 0)
        self.assertEqual(set(agg), {"cash_base", "shares", "close", "exposure"})

    def test_aggregate_reservations_fails_closed_on_unknown_key(self):
        # Inject an outstanding reservation with a malformed resource key; a
        # read-only report must NOT silently ignore it (would free risk budget).
        self.ledger.prepare_day_budget(account=ACCOUNT, environment=ENV,
                                       day_key="2026-11-07", limit_count=5,
                                       verified_prior_submissions=0)
        self.ledger.set_resource(account=ACCOUNT, environment=ENV,
                                 currency="USD", resource_key="mystery_key",
                                 amount="10")
        a = self.ledger.create_intent(account=ACCOUNT, environment=ENV,
                                      payload={"x": 1}, config=CONFIG,
                                      ttl_seconds=600, now=NOW)
        self.ledger.claim_intent(account=ACCOUNT, environment=ENV,
                                 intent_id=a["intent_id"],
                                 full_digest=a["full_digest"],
                                 code=a["approval_code"], now=NOW)
        self.ledger.reserve(account=ACCOUNT, environment=ENV,
                            intent_id=a["intent_id"], day_key="2026-11-07",
                            items=[RiskReservationSpec("USD", "mystery_key", "5")],
                            day_submission_slots=1, now=NOW)
        with self.assertRaises(ExecutionError) as ctx:
            aggregate_reservations(self.ledger, ACCOUNT, ENV)
        self.assertIn("unrecognised", str(ctx.exception))

    def test_resource_plan_may_return_plain_mapping(self):
        # CLI adapter computes the budget as a dict; the engine normalises it.
        def plan(snapshot, legs, aggregate):
            return {"resources": {("USD", "cash_base"): Decimal("100000"),
                                  ("USD", "exposure:1001:USD"): Decimal("100000")},
                    "items": [RiskReservationSpec("USD", "cash_base", "360"),
                              RiskReservationSpec("USD", "exposure:1001:USD", "360")],
                    "overrides": {}}
        engine = self._engine(resource_plan=plan)
        pre, result = self._preflight_confirm(engine)
        self.assertEqual(result.decision, "ALLOW")
        self.assertEqual(len(self.broker.calls), 1)

    def test_resource_plan_accepts_legacy_two_arg_signature(self):
        # An adapter written for (snapshot, reservations=...) still works.
        def plan(snapshot, reservations=None):
            return ResourcePlan(
                resources={("USD", "cash_base"): Decimal("100000"),
                           ("USD", "exposure:1001:USD"): Decimal("100000")},
                items=[RiskReservationSpec("USD", "cash_base", "360"),
                       RiskReservationSpec("USD", "exposure:1001:USD", "360")])
        engine = self._engine(resource_plan=plan)
        pre, result = self._preflight_confirm(engine)
        self.assertEqual(result.decision, "ALLOW")


# -- independent-review regressions (7 offline-reproducible issues) -------

class TestReviewRegressions(EngineTestCase):

    # (1) confirm must FULLY compare passed legs with the APPROVED payload.
    def test_quantity_drift_between_preflight_and_confirm_blocked(self):
        engine = self._engine()
        approved = _leg(quantity=Decimal("2"))
        pre = engine.preflight([approved], account=ACCOUNT, environment=ENV)
        # Operator was shown qty=2 but a tampered CLI passes qty=100.
        with self.assertRaises(ExecutionError) as ctx:
            engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                           legs=[_leg(quantity=Decimal("100"))],
                           account=ACCOUNT, environment=ENV)
        self.assertIn("do NOT match the approved", str(ctx.exception))
        self.assertEqual(self.broker.calls, [])

    def test_limit_price_and_extra_field_drift_blocked(self):
        engine = self._engine()
        pre = engine.preflight([_leg(limit_price=Decimal("180"))],
                               account=ACCOUNT, environment=ENV)
        for changed in (_leg(limit_price=Decimal("999")),
                        _leg(canonical={"note": "tampered"})):
            with self.assertRaises(ExecutionError):
                engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                               legs=[changed], account=ACCOUNT, environment=ENV)
        self.assertEqual(self.broker.calls, [])

    def test_account_env_drift_blocked(self):
        engine = self._engine()
        pre = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        with self.assertRaises(ExecutionError):
            engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                           legs=[_leg()], account=ACCOUNT, environment="live")
        self.assertEqual(self.broker.calls, [])

    # (2) _validate_plan must INDEPENDENTLY derive per-leg requirements.
    def test_two_stock_buys_with_tiny_reservation_blocked_before_any_send(self):
        # Two STK BUY @ 4000 each, cash 5000, but callback reserves only "1".
        # Each leg individually fits cash 5000; the INDEPENDENT derivation sees
        # 8000 required vs 1 reserved and refuses BEFORE any send.
        def plan(snapshot, legs, aggregate):
            return ResourcePlan(
                resources={("USD", "cash_base"): Decimal("5000")},
                items=[RiskReservationSpec("USD", "cash_base", "1")])
        engine = self._engine(
            snapshot_provider=lambda: _snapshot(cash="5000"),
            resource_plan=plan)
        buy = _leg(quantity=Decimal("20"), limit_price=Decimal("200"))
        pre = engine.preflight([buy, buy], account=ACCOUNT, environment=ENV)
        with self.assertRaises(UnsafeComboError) as ctx:
            engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                           legs=[buy, buy], account=ACCOUNT, environment=ENV)
        self.assertIn("under-sized", str(ctx.exception))
        self.assertEqual(self.broker.calls, [])

    def test_missing_increment_for_second_leg_blocked(self):
        # A package where only ONE leg's cash is reserved must fail closed.
        def plan(snapshot, legs, aggregate):
            return ResourcePlan(
                resources={("USD", "cash_base"): Decimal("100000")},
                items=[RiskReservationSpec("USD", "cash_base", "360")])
        engine = self._engine(resource_plan=plan)
        pre = engine.preflight([_leg(), _leg()], account=ACCOUNT,
                               environment=ENV)
        with self.assertRaises(UnsafeComboError):
            engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                           legs=[_leg(), _leg()], account=ACCOUNT,
                           environment=ENV)
        self.assertEqual(self.broker.calls, [])

    # (3) assessment must NOT double-count the leg's own increment.
    def test_cash_matches_single_buy_notional_is_not_double_counted(self):
        # exactly 360 cash, buy notional 360: with correct self-exclusion the
        # leg sees only the pre-existing obligations (zero) and PASSES.
        def plan(snapshot, legs, aggregate):
            return ResourcePlan(
                resources={("USD", "cash_base"): Decimal("360"),
                           ("USD", "exposure:1001:USD"): Decimal("360")},
                items=[RiskReservationSpec("USD", "cash_base", "360"),
                       RiskReservationSpec("USD", "exposure:1001:USD", "360")])
        engine = self._engine(
            snapshot_provider=lambda: _snapshot(cash="360"),
            resource_plan=plan)
        pre, result = self._preflight_confirm(engine)
        self.assertEqual(result.decision, "ALLOW")
        self.assertEqual(len(self.broker.calls), 1)

    # (4) ALL refreshed snapshots are STRICTLY account/env scoped.
    def test_other_account_initial_snapshot_blocked(self):
        engine = self._engine(snapshot_provider=lambda: _snapshot(
            account="DU9999999"))
        pre = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        with self.assertRaises(ExecutionError) as ctx:
            engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                           legs=[_leg()], account=ACCOUNT, environment=ENV)
        self.assertIn("mismatch", str(ctx.exception))
        self.assertEqual(self.broker.calls, [])

    def test_live_deferred_snapshot_cannot_authorise_paper_call(self):
        stock = _leg(con_id=1001, action="BUY", quantity=Decimal("200"),
                     strategy="covered-call")
        call_leg = _call_leg()
        call_count = [0]

        def provider():
            call_count[0] += 1
            if call_count[0] == 1:
                return _snapshot()  # initial: correct scope
            # deferred fresh snapshot: OTHER/live scope must be refused.
            return _snapshot(account=ACCOUNT, environment="live",
                             positions=(Position(instrument=_stock(),
                                                 quantity=Decimal(200),
                                                 price=Decimal(180)),))

        engine = self._engine(
            resource_plan=lambda s, l, a: ResourcePlan(
                resources={("USD", "cash_base"): Decimal("100000"),
                           ("USD", "exposure:1001:USD"): Decimal("100000"),
                           ("USD", "shares:1001:USD"): Decimal(0)},
                items=[RiskReservationSpec("USD", "cash_base", "36000"),
                       RiskReservationSpec("USD", "exposure:1001:USD", "36000"),
                       RiskReservationSpec("USD", "shares:1001:USD", "100")],
                overrides={("USD", "shares:1001:USD"): Decimal(200)}),
            snapshot_provider=provider, fill_proof=_fill_proof())
        pre = engine.preflight([stock, call_leg], account=ACCOUNT,
                               environment=ENV)
        result = engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                                legs=[stock, call_leg], account=ACCOUNT,
                                environment=ENV)
        self.assertEqual(len(self.broker.calls), 1)  # call NOT sent
        self.assertEqual(result.legs[1].state, "skipped")

    # (5) reconcile_positive: scope + mechanical working-order mapping.
    def test_other_live_ref_cannot_release_paper_reservation(self):
        engine = self._engine()
        pre, result = self._preflight_confirm(engine)
        ref = result.legs[0].order_ref
        # A live-scope handoff with structured evidence must be refused.
        evidence = _handoff(ref, environment="live", status="Submitted")
        with self.assertRaises(ExecutionError):
            engine.reconcile_positive(account=ACCOUNT, environment=ENV,
                                      order_ref=ref, evidence=evidence)
        self.assertEqual(self.ledger.get_reservation(
            result.reservation_id).state, "submitted")

    def test_handoff_releases_only_mapped_leg_sibling_keeps_risk(self):
        # Two-leg package, both positively submitted.  Positively map ONLY leg 0;
        # leg 1's reserved risk must remain (no package-wide blanket release).
        def plan(snapshot, legs, aggregate):
            return ResourcePlan(
                resources={("USD", "cash_base"): Decimal("100000"),
                           ("USD", "exposure:1001:USD"): Decimal("100000")},
                items=[RiskReservationSpec("USD", "cash_base", "720"),
                       RiskReservationSpec("USD", "exposure:1001:USD", "720")])
        engine = self._engine(resource_plan=plan)
        pre = engine.preflight([_leg(), _leg()], account=ACCOUNT,
                               environment=ENV)
        result = engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                                legs=[_leg(), _leg()], account=ACCOUNT,
                                environment=ENV)
        self.assertEqual(len(self.broker.calls), 2)
        ref0 = result.legs[0].order_ref
        derived = engine.reconcile_positive(
            account=ACCOUNT, environment=ENV, order_ref=ref0,
            evidence=_handoff(ref0, oid=result.legs[0].order_id,
                              pid=result.legs[0].perm_id, status="Submitted"),
            perm_id=result.legs[0].perm_id, order_id=result.legs[0].order_id)
        # Reservation is still unresolved because leg 1 is unmapped.
        self.assertEqual(derived, "submitted")
        self.assertTrue(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))
        self.assertGreater(aggregate_reservations(
            self.ledger, ACCOUNT, ENV)["cash_base"], 0)

    def test_handoff_without_matching_working_order_stays_blocked(self):
        engine = self._engine()
        pre, result = self._preflight_confirm(engine)
        ref = result.legs[0].order_ref
        # Fresh, scoped, no-issues snapshot but NO working order for the ref.
        evidence = _handoff(ref, status="Submitted", orders=())
        with self.assertRaises(ExecutionError) as ctx:
            engine.reconcile_positive(account=ACCOUNT, environment=ENV,
                                      order_ref=ref, evidence=evidence)
        self.assertIn("no working order matching", str(ctx.exception))
        self.assertEqual(self.ledger.get_reservation(
            result.reservation_id).state, "submitted")

    def test_handoff_stale_snapshot_or_wrong_conid_stays_blocked(self):
        engine = self._engine()
        pre, result = self._preflight_confirm(engine)
        ref = result.legs[0].order_ref
        # Snapshot captured BEFORE the attempt.
        stale = _handoff(ref, status="Submitted",
                         captured_at=NOW - timedelta(hours=1))
        with self.assertRaises(ExecutionError):
            engine.reconcile_positive(account=ACCOUNT, environment=ENV,
                                      order_ref=ref, evidence=stale)
        # A working order with a DIFFERENT conId than the approved leg.
        wrong = _handoff(ref, status="Submitted", con_id=9999)
        with self.assertRaises(ExecutionError):
            engine.reconcile_positive(account=ACCOUNT, environment=ENV,
                                      order_ref=ref, evidence=wrong)
        self.assertEqual(self.ledger.get_reservation(
            result.reservation_id).state, "submitted")

    # (6) STRUCTURED FillProof: engine verifies exact full qty, not a string.
    def test_bare_filled_string_proof_rejected(self):
        stock = _leg(con_id=1001, action="BUY", quantity=Decimal("200"),
                     strategy="covered-call")
        call_leg = _call_leg()
        engine = self._engine(
            resource_plan=lambda s, l, a: ResourcePlan(
                resources={("USD", "cash_base"): Decimal("100000"),
                           ("USD", "exposure:1001:USD"): Decimal("100000"),
                           ("USD", "shares:1001:USD"): Decimal(0)},
                items=[RiskReservationSpec("USD", "cash_base", "36000"),
                       RiskReservationSpec("USD", "exposure:1001:USD", "36000"),
                       RiskReservationSpec("USD", "shares:1001:USD", "100")],
                overrides={("USD", "shares:1001:USD"): Decimal(200)}),
            snapshot_provider=lambda: _snapshot(positions=(Position(
                instrument=_stock(), quantity=Decimal(200),
                price=Decimal(180)),)),
            fill_proof=lambda b, r, ids: "Filled")
        pre = engine.preflight([stock, call_leg], account=ACCOUNT,
                               environment=ENV)
        result = engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                                legs=[stock, call_leg], account=ACCOUNT,
                                environment=ENV)
        self.assertEqual(len(self.broker.calls), 1)
        self.assertEqual(result.legs[1].state, "skipped")

    def test_partial_zero_overfill_nan_wrong_scope_proof_rejected(self):
        bad_proofs = [
            _fill_proof(quantity=Decimal("100")),       # partial
            _fill_proof(quantity=Decimal("0")),         # zero
            _fill_proof(quantity=Decimal("201")),       # overfill
            _fill_proof(quantity=Decimal("NaN")),       # non-finite
            _fill_proof(con_id=9999),                   # wrong conId
            _fill_proof(status="Submitted"),            # not Filled
        ]
        stock = _leg(con_id=1001, action="BUY", quantity=Decimal("200"),
                     strategy="covered-call")
        call_leg = _call_leg()
        for bad in bad_proofs:
            # Each iteration needs a FRESH ledger: a skipped call leg still
            # leaves the stock leg 'submitted' (unresolved), which would block
            # the next confirm on the same account.
            fresh = tempfile.TemporaryDirectory()
            self.addCleanup(fresh.cleanup)
            ledger = SubmissionLedger(Path(fresh.name) / "ledger.sqlite3", TZ)
            local_broker = _Broker()
            engine = self._engine(
                ledger=ledger, broker=local_broker,
                place_order=local_broker.place,
                resource_plan=lambda s, l, a: ResourcePlan(
                    resources={("USD", "cash_base"): Decimal("100000"),
                               ("USD", "exposure:1001:USD"): Decimal("100000"),
                               ("USD", "shares:1001:USD"): Decimal(0)},
                    items=[RiskReservationSpec("USD", "cash_base", "36000"),
                           RiskReservationSpec("USD", "exposure:1001:USD", "36000"),
                           RiskReservationSpec("USD", "shares:1001:USD", "100")],
                    overrides={("USD", "shares:1001:USD"): Decimal(200)}),
                snapshot_provider=lambda: _snapshot(positions=(Position(
                    instrument=_stock(), quantity=Decimal(200),
                    price=Decimal(180)),)),
                fill_proof=bad)
            pre = engine.preflight([stock, call_leg], account=ACCOUNT,
                                   environment=ENV)
            result = engine.confirm(
                intent_id=pre.intent_id, code=pre.approval_code,
                legs=[stock, call_leg], account=ACCOUNT, environment=ENV)
            self.assertEqual(len(local_broker.calls), 1,
                             f"proof {bad} should not send the call")
            self.assertEqual(result.legs[1].state, "skipped")

    # (7) clock re-read INSIDE the lock + before EACH send; TTL/day re-verify.
    def test_snapshot_provider_delayed_past_ttl_blocks_send(self):
        times = [NOW]

        def clock():
            return times[0]

        def provider():
            # Simulate a 20-minute refresh delay AFTER the initial clock read.
            times[0] = times[0] + timedelta(minutes=20)
            return _snapshot(captured_at=times[0] - timedelta(seconds=2))

        engine = self._engine(clock=clock, snapshot_provider=provider,
                              ttl_seconds=600)
        pre = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        with self.assertRaises(ExecutionError) as ctx:
            engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                           legs=[_leg()], account=ACCOUNT, environment=ENV)
        self.assertIn("expired", str(ctx.exception))
        self.assertEqual(self.broker.calls, [])
        # No attempt was counted on the OLD day.
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key="2026-11-07")["exists"], False)

    def test_cross_config_day_never_counts_attempt_on_old_day(self):
        times = [NOW]

        def clock():
            return times[0]

        def provider():
            # roll into the NEXT configured natural day during the refresh.
            times[0] = NOW + timedelta(days=1)
            return _snapshot(captured_at=times[0] - timedelta(seconds=2))

        # A generous TTL so the DAY change (not expiry) is what stops the send.
        engine = self._engine(clock=clock, snapshot_provider=provider,
                              ttl_seconds=3 * 24 * 3600)
        pre = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        with self.assertRaises(ExecutionError) as ctx:
            engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                           legs=[_leg()], account=ACCOUNT, environment=ENV)
        self.assertIn("natural day changed", str(ctx.exception))
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key="2026-11-07")["exists"], False)


# -- strict positive-handoff mapping + full-package obligation derivation ----

class TestHandoffStrictness(EngineTestCase):
    """Independent offline reproduction of the handoff / close-capacity bugs.

    A fresh Snapshot with a matching ref/ids is NOT enough: the engine must
    also prove the observed snapshot is FRESH (not a day-old capture), that the
    ref maps to EXACTLY ONE working order, and that the working order's FULL
    economic identity (conId/action/order_type/bounded limit_price/remaining)
    matches the APPROVED leg.  A partial/filled handoff is unsupported.
    """

    def _submitted(self, **engine_kw):
        engine = self._engine(**engine_kw)
        pre, result = self._preflight_confirm(engine)
        return engine, result, result.legs[0]

    def test_day_old_observed_snapshot_cannot_release_risk(self):
        # (1) Engine clock jumps to NOW+1day; the observed snapshot is still
        # captured at NOW+1s (only "newer than attempt", NOT fresh).  A stale
        # day-old observation must NOT confirm/release the reservation.
        timebox = [NOW]
        engine, result, sent = self._submitted(clock=lambda: timebox[0])
        timebox[0] = NOW + timedelta(days=1)  # clock jumps AFTER the attempt
        ref = sent.order_ref
        with self.assertRaises(ExecutionError) as ctx:
            engine.reconcile_positive(
                account=ACCOUNT, environment=ENV, order_ref=ref,
                evidence=_handoff(ref, oid=sent.order_id, pid=sent.perm_id,
                                  status="Submitted", captured_at=NOW + timedelta(seconds=1)),
                perm_id=sent.perm_id, order_id=sent.order_id)
        self.assertIn("fresh", str(ctx.exception).lower())
        self.assertEqual(self.ledger.get_reservation(
            result.reservation_id).state, "submitted")

    def test_partial_remaining_cannot_release_whole_leg(self):
        # (2) approved qty=2, working order remaining=0.01 (a partial fill).
        # A positive handoff must NOT release the whole reserved obligation.
        engine, result, sent = self._submitted()
        ref = sent.order_ref
        with self.assertRaises(ExecutionError) as ctx:
            engine.reconcile_positive(
                account=ACCOUNT, environment=ENV, order_ref=ref,
                evidence=_handoff(ref, oid=sent.order_id, pid=sent.perm_id,
                                  status="Submitted", remaining=Decimal("0.01")),
                perm_id=sent.perm_id, order_id=sent.order_id)
        self.assertIn("remaining", str(ctx.exception).lower())
        self.assertEqual(self.ledger.get_reservation(
            result.reservation_id).state, "submitted")

    def test_mismatched_working_limit_price_cannot_release_risk(self):
        # (3) the approved leg is LMT @180 but the working order is LMT @1.
        # The observed economic identity must match the approved leg exactly.
        engine, result, sent = self._submitted()
        ref = sent.order_ref
        wo = WorkingOrder(instrument=_stock(1001), action="BUY",
                          remaining=Decimal("2"), order_id=sent.order_id,
                          perm_id=sent.perm_id, order_ref=ref,
                          limit_price=Decimal("1"), order_type="LMT")
        bad = PositiveHandoff(
            broker_status="Submitted", order_ref=ref, account=ACCOUNT,
            environment=ENV, observed_snapshot=_snapshot(
                captured_at=NOW + timedelta(seconds=1), orders=(wo,)),
            con_id=1001)
        with self.assertRaises(ExecutionError) as ctx:
            engine.reconcile_positive(
                account=ACCOUNT, environment=ENV, order_ref=ref,
                evidence=bad, perm_id=sent.perm_id, order_id=sent.order_id)
        self.assertIn("limit", str(ctx.exception).lower())
        self.assertEqual(self.ledger.get_reservation(
            result.reservation_id).state, "submitted")

    def test_duplicate_working_order_rows_cannot_release_risk(self):
        # (4) two identical working-order rows with the SAME stable ref: an
        # ambiguous mapping must NOT pick the first and release the leg.
        engine, result, sent = self._submitted()
        ref = sent.order_ref
        wo = WorkingOrder(instrument=_stock(1001), action="BUY",
                          remaining=Decimal("2"), order_id=sent.order_id,
                          perm_id=sent.perm_id, order_ref=ref,
                          limit_price=Decimal("180"), order_type="LMT")
        dup = PositiveHandoff(
            broker_status="Submitted", order_ref=ref, account=ACCOUNT,
            environment=ENV, observed_snapshot=_snapshot(
                captured_at=NOW + timedelta(seconds=1), orders=(wo, wo)),
            con_id=1001)
        with self.assertRaises(ExecutionError) as ctx:
            engine.reconcile_positive(
                account=ACCOUNT, environment=ENV, order_ref=ref,
                evidence=dup, perm_id=sent.perm_id, order_id=sent.order_id)
        self.assertIn("unique", str(ctx.exception).lower())
        self.assertEqual(self.ledger.get_reservation(
            result.reservation_id).state, "submitted")

    def test_p1_two_opt_buy_closes_cannot_exceed_single_short(self):
        # (5) A single short option (qty -1) must NOT be buyable twice: both
        # OPT BUY legs are risk-DECREASING (increases_risk()==False), so the old
        # independent derivation skipped them entirely -> an empty package and
        # an empty close-capacity reservation let BOTH sends through, creating
        # one unintended long.  The full package MUST derive close capacity for
        # every OPT BUY and refuse the over-close before ANY send.
        opt = Instrument(2001, "AAPL", "USD", "OPT", "SMART", 1001,
                         Decimal(100), "P", Decimal(40), "20261218", "AAPL")
        snapshot = _snapshot(positions=(
            Position(instrument=opt, quantity=Decimal(-1), price=Decimal(2)),))
        buyback = PreparedLeg(
            instrument=opt, action="BUY", quantity=Decimal(1),
            order_type="LMT", limit_price=Decimal(2), tif="DAY",
            exchange="SMART", account=ACCOUNT, deliverable_verified=True)
        engine = self._engine(
            snapshot_provider=lambda: snapshot,
            resource_plan=lambda s, l, a: ResourcePlan())
        pre = engine.preflight([buyback, buyback], account=ACCOUNT,
                               environment=ENV)
        with self.assertRaises(ExecutionError):
            engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                           legs=[buyback, buyback], account=ACCOUNT,
                           environment=ENV)
        self.assertEqual(self.broker.calls, [])

    def test_full_economic_identity_allows_a_valid_handoff(self):
        # GREEN control: a fresh, unique, FULL-identity working order with
        # remaining == approved qty still releases the single leg.
        engine, result, sent = self._submitted()
        ref = sent.order_ref
        derived = engine.reconcile_positive(
            account=ACCOUNT, environment=ENV, order_ref=ref,
            evidence=_handoff(ref, oid=sent.order_id, pid=sent.perm_id,
                              status="Submitted"),
            perm_id=sent.perm_id, order_id=sent.order_id)
        self.assertEqual(derived, "confirmed")
        self.assertEqual(self.ledger.get_reservation(
            result.reservation_id).state, "confirmed")

    def test_unbounded_mkt_working_order_cannot_release(self):
        # A MKT / order_type mismatch cannot reconcile to a bounded LMT
        # reserved obligation, even with matching ids/qty/price-ref.
        engine, result, sent = self._submitted()
        ref = sent.order_ref
        wo = WorkingOrder(instrument=_stock(1001), action="BUY",
                          remaining=Decimal("2"), order_id=sent.order_id,
                          perm_id=sent.perm_id, order_ref=ref,
                          limit_price=Decimal("180"), order_type="MKT")
        bad = PositiveHandoff(
            broker_status="Submitted", order_ref=ref, account=ACCOUNT,
            environment=ENV, observed_snapshot=_snapshot(
                captured_at=NOW + timedelta(seconds=1), orders=(wo,)),
            con_id=1001)
        with self.assertRaises(ExecutionError) as ctx:
            engine.reconcile_positive(
                account=ACCOUNT, environment=ENV, order_ref=ref,
                evidence=bad, perm_id=sent.perm_id, order_id=sent.order_id)
        self.assertIn("LMT", str(ctx.exception))
        self.assertEqual(self.ledger.get_reservation(
            result.reservation_id).state, "submitted")

    def test_far_future_observed_snapshot_cannot_release(self):
        # A snapshot captured a day in the FUTURE (clock skew / forgery) must
        # be refused, not treated as "newer than the attempt".
        engine, result, sent = self._submitted()
        ref = sent.order_ref
        with self.assertRaises(ExecutionError) as ctx:
            engine.reconcile_positive(
                account=ACCOUNT, environment=ENV, order_ref=ref,
                evidence=_handoff(ref, oid=sent.order_id, pid=sent.perm_id,
                                  status="Submitted",
                                  captured_at=NOW + timedelta(days=1)),
                perm_id=sent.perm_id, order_id=sent.order_id)
        self.assertIn("future", str(ctx.exception).lower())
        self.assertEqual(self.ledger.get_reservation(
            result.reservation_id).state, "submitted")

    def test_double_close_blocked_even_with_correctly_sized_plan(self):
        # A correctly-sized package (close reserved = 2, premium reserved) for
        # two closes of ONE short must STILL be blocked by risk.assess: each
        # leg sees the OTHER leg's close reservation (self-excluded), so short
        # close capacity is exhausted.  No send may happen.
        opt = Instrument(2001, "AAPL", "USD", "OPT", "SMART", 1001,
                         Decimal(100), "P", Decimal(40), "20261218", "AAPL")
        snapshot = _snapshot(positions=(
            Position(instrument=opt, quantity=Decimal(-1), price=Decimal(2)),))

        def plan(snapshot, legs, aggregate):
            return ResourcePlan(
                resources={("USD", "close:2001"): Decimal("2"),
                           ("USD", "cash_base"): Decimal("100000")},
                items=[RiskReservationSpec("USD", "close:2001", "2"),
                       RiskReservationSpec("USD", "cash_base", "400")])
        buyback = PreparedLeg(
            instrument=opt, action="BUY", quantity=Decimal(1),
            order_type="LMT", limit_price=Decimal(2), tif="DAY",
            exchange="SMART", account=ACCOUNT, deliverable_verified=True)
        engine = self._engine(snapshot_provider=lambda: snapshot,
                              resource_plan=plan)
        pre = engine.preflight([buyback, buyback], account=ACCOUNT,
                               environment=ENV)
        with self.assertRaises(ExecutionError):
            engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                           legs=[buyback, buyback], account=ACCOUNT,
                           environment=ENV)
        self.assertEqual(self.broker.calls, [])

    def test_opt_buy_requires_both_close_and_premium_cash(self):
        # An OPT BUY package reserving ONLY close capacity (no premium cash)
        # must fail closed: the callback cannot pass with empty/partial items.
        opt = Instrument(2001, "AAPL", "USD", "OPT", "SMART", 1001,
                         Decimal(100), "P", Decimal(40), "20261218", "AAPL")
        snapshot = _snapshot(positions=(
            Position(instrument=opt, quantity=Decimal(-1), price=Decimal(2)),))

        def plan(snapshot, legs, aggregate):
            return ResourcePlan(
                resources={("USD", "close:2001"): Decimal("1"),
                           ("USD", "cash_base"): Decimal("100000")},
                items=[RiskReservationSpec("USD", "close:2001", "1")])
        buyback = PreparedLeg(
            instrument=opt, action="BUY", quantity=Decimal(1),
            order_type="LMT", limit_price=Decimal(2), tif="DAY",
            exchange="SMART", account=ACCOUNT, deliverable_verified=True)
        engine = self._engine(snapshot_provider=lambda: snapshot,
                              resource_plan=plan)
        pre = engine.preflight([buyback], account=ACCOUNT, environment=ENV)
        with self.assertRaises(UnsafeComboError) as ctx:
            engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                           legs=[buyback], account=ACCOUNT, environment=ENV)
        self.assertIn("cash_base", str(ctx.exception))
        self.assertEqual(self.broker.calls, [])

    def test_stock_sell_requires_shares_reservation(self):
        # A STK SELL is risk-DECREASING (increases_risk()==False) but still
        # COMMITS shares; an empty package must NOT let it through.
        def plan(snapshot, legs, aggregate):
            return ResourcePlan()
        sell = _leg(action="SELL", quantity=Decimal("2"))
        engine = self._engine(resource_plan=plan)
        pre = engine.preflight([sell], account=ACCOUNT, environment=ENV)
        with self.assertRaises(UnsafeComboError) as ctx:
            engine.confirm(intent_id=pre.intent_id, code=pre.approval_code,
                           legs=[sell], account=ACCOUNT, environment=ENV)
        self.assertIn("shares", str(ctx.exception))
        self.assertEqual(self.broker.calls, [])


# -- real interprocess claim race via the engine --------------------------

def _engine_confirm_worker(db_path, lock_dir, pre, out, start):
    from datetime import datetime, timezone
    from decimal import Decimal
    from thetamill import state
    from thetamill.broker_state import Instrument, Snapshot
    from thetamill.execution import (
        BrokerReceipt, ExecutionEngine, ExecutionError, PreparedLeg,
        ResourcePlan,
    )
    from thetamill.risk import validate_risk_config
    from thetamill.state import RiskReservationSpec, SubmissionLedger

    now = datetime(2026, 11, 7, 15, 0, tzinfo=timezone.utc)
    inst = Instrument(1001, "AAPL", "USD", "STK", "SMART", 1001)
    leg = PreparedLeg(
        instrument=inst, action="BUY", quantity=Decimal(2), order_type="LMT",
        limit_price=Decimal(180), tif="DAY", exchange="SMART",
        strategy="covered-call", account="DU1234567",
        deliverable_verified=True)
    sends = []
    cfg = {"risk": {"enabled": True, "day_timezone": "America/New_York",
                    "max_daily_submissions": 5, "config_version": "p3-1"}}

    def provider():
        return Snapshot(account="DU1234567", environment="paper",
                        captured_at=now - __import__("datetime").timedelta(seconds=2),
                        base_currency="USD", cash={"USD": Decimal("100000")},
                        net_liquidation=Decimal("100000"), positions=(),
                        orders=(), fx={"USD": Decimal(1)})

    def plan(snapshot, legs, aggregate):
        # Full canonical package: STK BUY @180 qty2 -> cash 360 + exposure 360.
        return ResourcePlan(
            resources={("USD", "cash_base"): Decimal("100000"),
                       ("USD", "exposure:1001:USD"): Decimal("100000")},
            items=[RiskReservationSpec("USD", "cash_base", "360"),
                   RiskReservationSpec("USD", "exposure:1001:USD", "360")])

    def place(broker, l, ref):
        sends.append(ref)
        return BrokerReceipt(order_id=1, perm_id="p1", status="Submitted")

    engine = ExecutionEngine(
        ledger=SubmissionLedger(db_path, "America/New_York"),
        snapshot_provider=provider, broker=object(), place_order=place,
        resource_plan=plan, config=cfg, limits=validate_risk_config(cfg),
        lock_dir=lock_dir, verified_prior_submissions=0,
        clock=lambda: now)
    start.wait()
    try:
        result = engine.confirm(intent_id=pre["intent_id"], code=pre["code"],
                                legs=[leg], account="DU1234567",
                                environment="paper")
        out.put(("ok", len(sends), result.decision))
    except (ExecutionError, state.ClaimError) as exc:
        out.put(("rejected", type(exc).__name__, len(sends)))
    except Exception as exc:  # pragma: no cover
        out.put(("error", f"{type(exc).__name__}: {exc}"))


class TestInterprocessConfirmRace(EngineTestCase):
    def test_same_intent_confirmed_exactly_once_across_processes(self):
        engine = self._engine()
        pre = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        ctx = mp.get_context("spawn")
        start = ctx.Event()
        out = ctx.Queue()
        payload = {"intent_id": pre.intent_id, "code": pre.approval_code}
        procs = [ctx.Process(target=_engine_confirm_worker,
                             args=(str(self.db), str(self.root / "locks"),
                                   payload, out, start)) for _ in range(6)]
        for p in procs:
            p.start()
        start.set()
        results = [out.get(timeout=30) for _ in procs]
        for p in procs:
            p.join(timeout=30)
            if p.is_alive():  # pragma: no cover
                p.terminate()
        errors = [r for r in results if r[0] == "error"]
        self.assertEqual(errors, [], f"unexpected worker errors: {errors}")
        ok = [r for r in results if r[0] == "ok"]
        self.assertEqual(len(ok), 1, results)
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key="2026-11-07")["submitted_count"], 1)


# -- independent resource requirements & risk-reduction quota exception ----

def _opt_inst(con_id=2001, right="P", strike=Decimal("180"), mult=Decimal("100"),
              currency="USD"):
    return Instrument(
        con_id=con_id, symbol="AAPL", currency=currency, sec_type="OPT",
        exchange="SMART", underlying_id=1001, multiplier=mult,
        right=right, strike=strike, expiry="20261218", trading_class="AAPL")


def _opt_leg(con_id=2001, action="BUY", right="P", strike=Decimal("180"),
             mult=Decimal("100"), price=Decimal("2"), qty=Decimal("1"),
             currency="USD", **kw):
    inst = _opt_inst(con_id=con_id, right=right, strike=strike, mult=mult, currency=currency)
    fields = dict(
        instrument=inst, action=action, quantity=qty,
        order_type="LMT", limit_price=price, tif="DAY",
        exchange="SMART", account=ACCOUNT, deliverable_verified=True)
    fields.update(kw)
    return PreparedLeg(**fields)


class TestIndependentRequirementsAndQuotaException(EngineTestCase):
    def test_independent_requirements_for_all_legs(self):
        from thetamill.execution import _independent_requirements
        snap = _snapshot(cash="100000")

        # 1. STK BUY: cash_base + exposure (both in BASE)
        stk_buy = _leg(action="BUY", limit_price=Decimal("180"), quantity=Decimal("2"))
        req_stk_buy = _independent_requirements([stk_buy], snap)
        self.assertEqual(req_stk_buy[("USD", "cash_base")], Decimal("360"))
        self.assertEqual(req_stk_buy[("USD", "exposure:1001:USD")], Decimal("360"))

        # 2. STK SELL: shares only
        stk_sell = _leg(action="SELL", quantity=Decimal("2"))
        req_stk_sell = _independent_requirements([stk_sell], snap)
        self.assertEqual(req_stk_sell[("USD", "shares:1001:USD")], Decimal("2"))
        self.assertNotIn(("USD", "cash_base"), req_stk_sell)
        self.assertNotIn(("USD", "exposure:1001:USD"), req_stk_sell)

        # 3. OPT BUY: close capacity + premium cash
        opt_buy = _opt_leg(con_id=2001, action="BUY", price=Decimal("2"), qty=Decimal("1"), mult=Decimal("100"))
        req_opt_buy = _independent_requirements([opt_buy], snap)
        self.assertEqual(req_opt_buy[("USD", "close:2001")], Decimal("1"))
        self.assertEqual(req_opt_buy[("USD", "cash_base")], Decimal("200"))

        # 4. OPT SELL PUT: 100/200/500 multipliers cash + exposure, never default 100
        for m, want in (("100", Decimal("18000")),
                        ("200", Decimal("36000")),
                        ("500", Decimal("90000"))):
            put_leg = _opt_leg(con_id=2002, action="SELL", right="P", strike=Decimal("180"),
                               mult=Decimal(m), qty=Decimal("1"))
            req_put = _independent_requirements([put_leg], snap)
            self.assertEqual(req_put[("USD", "cash_base")], want)
            self.assertEqual(req_put[("USD", "exposure:1001:USD")], want)

        # 5. OPT SELL CALL: shares cover only, no double counting stock exposure
        call_leg = _opt_leg(con_id=2003, action="SELL", right="C", mult=Decimal("100"), qty=Decimal("1"))
        req_call = _independent_requirements([call_leg], snap)
        self.assertEqual(req_call[("USD", "shares:1001:USD")], Decimal("100"))
        self.assertNotIn(("USD", "cash_base"), req_call)
        self.assertNotIn(("USD", "exposure:1001:USD"), req_call)

        # 6. Multiple legs cannot reuse capacity
        two_buys = [opt_buy, opt_buy]
        req_two = _independent_requirements(two_buys, snap)
        self.assertEqual(req_two[("USD", "close:2001")], Decimal("2"))
        self.assertEqual(req_two[("USD", "cash_base")], Decimal("400"))

    def test_handoff_quote_independent_cap_and_future_tolerance(self):
        engine = self._engine()
        pre, result = self._preflight_confirm(engine)
        leg = result.legs[0]

        # Snapshot 35s old: exceeds 30s default cap even with 300s limits
        wo = WorkingOrder(instrument=_stock(1001), action="BUY", remaining=Decimal("2"),
                          order_id=leg.order_id, perm_id=leg.perm_id, order_ref=leg.order_ref,
                          limit_price=Decimal("180"), order_type="LMT")
        stale_snap = _snapshot(captured_at=NOW - timedelta(seconds=35), orders=(wo,))
        handoff_stale = PositiveHandoff(
            broker_status="Submitted", order_ref=leg.order_ref, account=ACCOUNT,
            environment=ENV, observed_snapshot=stale_snap, con_id=1001)
        with self.assertRaises(ExecutionError) as ctx:
            engine.reconcile_positive(account=ACCOUNT, environment=ENV,
                                      order_ref=leg.order_ref, evidence=handoff_stale)
        self.assertIn("not fresh", str(ctx.exception))

        # Snapshot 6s in the future: exceeds 5s future tolerance
        future_snap = _snapshot(captured_at=NOW + timedelta(seconds=6), orders=(wo,))
        handoff_future = PositiveHandoff(
            broker_status="Submitted", order_ref=leg.order_ref, account=ACCOUNT,
            environment=ENV, observed_snapshot=future_snap, con_id=1001)
        with self.assertRaises(ExecutionError) as ctx:
            engine.reconcile_positive(account=ACCOUNT, environment=ENV,
                                      order_ref=leg.order_ref, evidence=handoff_future)
        self.assertIn("in the future", str(ctx.exception))

        # Snapshot 3s in the future: within 5s future tolerance -> succeeds
        valid_snap = _snapshot(captured_at=NOW + timedelta(seconds=3), orders=(wo,))
        handoff_valid = PositiveHandoff(
            broker_status="Submitted", order_ref=leg.order_ref, account=ACCOUNT,
            environment=ENV, observed_snapshot=valid_snap, con_id=1001)
        outcome = engine.reconcile_positive(account=ACCOUNT, environment=ENV,
                                            order_ref=leg.order_ref, evidence=handoff_valid)
        self.assertEqual(outcome, "confirmed")

    def test_handoff_rejects_economic_mismatch_and_partial_fill(self):
        engine = self._engine()
        pre, result = self._preflight_confirm(engine)
        leg = result.legs[0]

        # Limit price mismatch
        wo_bad_price = WorkingOrder(
            instrument=_stock(1001), action="BUY", remaining=Decimal("2"),
            order_id=leg.order_id, perm_id=leg.perm_id, order_ref=leg.order_ref,
            limit_price=Decimal("181"), order_type="LMT")
        snap = _snapshot(captured_at=NOW + timedelta(seconds=1), orders=(wo_bad_price,))
        with self.assertRaises(ExecutionError) as ctx:
            engine.reconcile_positive(
                account=ACCOUNT, environment=ENV, order_ref=leg.order_ref,
                evidence=PositiveHandoff(broker_status="Submitted", order_ref=leg.order_ref,
                                        account=ACCOUNT, environment=ENV,
                                        observed_snapshot=snap, con_id=1001))
        self.assertIn("limit price does not match", str(ctx.exception))

        # Partial remaining (1 instead of 2)
        wo_partial = WorkingOrder(
            instrument=_stock(1001), action="BUY", remaining=Decimal("1"),
            order_id=leg.order_id, perm_id=leg.perm_id, order_ref=leg.order_ref,
            limit_price=Decimal("180"), order_type="LMT")
        snap_partial = _snapshot(captured_at=NOW + timedelta(seconds=1), orders=(wo_partial,))
        with self.assertRaises(ExecutionError) as ctx:
            engine.reconcile_positive(
                account=ACCOUNT, environment=ENV, order_ref=leg.order_ref,
                evidence=PositiveHandoff(broker_status="Submitted", order_ref=leg.order_ref,
                                        account=ACCOUNT, environment=ENV,
                                        observed_snapshot=snap_partial, con_id=1001))
        self.assertIn("EXACTLY equal", str(ctx.exception))

    def test_risk_reduction_daily_quota_exception_in_execution_engine(self):
        # 1. Setup engine with max_daily_submissions=1 and exhaust it
        small = dict(CONFIG, risk=dict(CONFIG["risk"], max_daily_submissions=1))
        engine = self._engine(config=small, limits=validate_risk_config(small))
        pre1, res1 = self._preflight_confirm(engine)
        engine.reconcile_positive(
            account=ACCOUNT, environment=ENV,
            order_ref=res1.legs[0].order_ref,
            evidence=_handoff(res1.legs[0].order_ref,
                              oid=res1.legs[0].order_id,
                              pid=res1.legs[0].perm_id, status="Submitted"),
            perm_id=res1.legs[0].perm_id, order_id=res1.legs[0].order_id)

        # Quota is exhausted (1 of 1)
        budget = self.ledger.get_day_budget(account=ACCOUNT, environment=ENV, day_key="2026-11-07")
        self.assertEqual(budget["submitted_count"], 1)
        self.assertEqual(budget["remaining"], 0)

        # New risk order is blocked by daily budget
        pre_new = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        with self.assertRaises(state.BudgetError):
            engine.confirm(intent_id=pre_new.intent_id, code=pre_new.approval_code,
                           legs=[_leg()], account=ACCOUNT, environment=ENV)

        # 2. Independently verified net reduction order: OPT BUY closing short put in snapshot
        short_pos = Position(instrument=_opt_inst(con_id=2001, right="P"),
                             quantity=Decimal("-1"), price=Decimal("2"))
        snap_with_short = _snapshot(cash="100000", positions=(short_pos,))
        engine_rr = self._engine(
            config=small, limits=validate_risk_config(small),
            snapshot_provider=lambda: snap_with_short)

        opt_close = _opt_leg(con_id=2001, action="BUY", right="P", price=Decimal("2"), qty=Decimal("1"))
        pre_rr = engine_rr.preflight([opt_close], account=ACCOUNT, environment=ENV)

        # Confirm should succeed under the approved risk-reduction daily-quota exception!
        res_rr = engine_rr.confirm(intent_id=pre_rr.intent_id, code=pre_rr.approval_code,
                                   legs=[opt_close], account=ACCOUNT, environment=ENV)
        self.assertEqual(res_rr.decision, "ALLOW")
        self.assertEqual(len(res_rr.legs), 1)

        # Real attempt count is retained and incremented!
        budget_after = self.ledger.get_day_budget(account=ACCOUNT, environment=ENV, day_key="2026-11-07")
        self.assertEqual(budget_after["submitted_count"], 2)

        # Clear unresolved state for res_rr leg
        self.ledger.confirm_leg(res_rr.legs[0].order_ref, broker_status="Filled")

        # 3. Non-reduction attempt: OPT BUY with qty=2 when short position is only -1
        # It would create a long option, so it is NOT an independently verified net reduction.
        opt_excess = _opt_leg(con_id=2001, action="BUY", right="P", price=Decimal("2"), qty=Decimal("2"))
        pre_excess = engine_rr.preflight([opt_excess], account=ACCOUNT, environment=ENV)
        with self.assertRaises((state.BudgetError, ExecutionError)):
            engine_rr.confirm(intent_id=pre_excess.intent_id, code=pre_excess.approval_code,
                              legs=[opt_excess], account=ACCOUNT, environment=ENV)

    def test_risk_reduction_quota_exception_bypasses_migration_freeze_in_execution_engine(self):
        short_pos = Position(instrument=_opt_inst(con_id=2001, right="P"),
                             quantity=Decimal("-1"), price=Decimal("2"))
        snap_with_short = _snapshot(cash="100000", positions=(short_pos,))
        engine_frozen = self._engine(
            verified_prior_submissions=None,  # triggers migration freeze
            snapshot_provider=lambda: snap_with_short)

        # New risk order is blocked by migration freeze
        pre_new = engine_frozen.preflight([_leg()], account=ACCOUNT, environment=ENV)
        res_new = engine_frozen.confirm(intent_id=pre_new.intent_id, code=pre_new.approval_code,
                                        legs=[_leg()], account=ACCOUNT, environment=ENV)
        self.assertTrue(res_new.frozen)
        self.assertEqual(res_new.decision, "BLOCK")

        # Independently verified net reduction bypasses migration freeze
        opt_close = _opt_leg(con_id=2001, action="BUY", right="P", price=Decimal("2"), qty=Decimal("1"))
        pre_rr = engine_frozen.preflight([opt_close], account=ACCOUNT, environment=ENV)
        res_rr = engine_frozen.confirm(intent_id=pre_rr.intent_id, code=pre_rr.approval_code,
                                       legs=[opt_close], account=ACCOUNT, environment=ENV)
        self.assertFalse(res_rr.frozen)
        self.assertEqual(res_rr.decision, "ALLOW")

    def test_reconcile_positive_full_canonical_instrument_fields_parametrized(self):
        big = dict(CONFIG, risk=dict(CONFIG["risk"], max_daily_submissions=50))
        engine = self._engine(config=big, limits=validate_risk_config(big))
        pre, result = self._preflight_confirm(engine)
        leg = result.legs[0]
        ref = leg.order_ref
        oid = leg.order_id
        pid = leg.perm_id

        # Valid unchanged handoff succeeds
        wo_valid = WorkingOrder(
            instrument=_stock(1001), action="BUY", remaining=Decimal("2"),
            order_id=oid, perm_id=pid, order_ref=ref,
            limit_price=Decimal("180"), order_type="LMT")
        snap_valid = _snapshot(captured_at=NOW + timedelta(seconds=1), orders=(wo_valid,))
        res = engine.reconcile_positive(
            account=ACCOUNT, environment=ENV, order_ref=ref,
            evidence=PositiveHandoff(broker_status="Submitted", order_ref=ref,
                                     account=ACCOUNT, environment=ENV,
                                     observed_snapshot=snap_valid, con_id=1001))
        self.assertEqual(res, "confirmed")

        # Every canonical instrument identity field parametrized with actual mismatches
        field_variations = [
            ("con_id", Instrument(con_id=200, symbol="AAPL", currency="USD", sec_type="STK", exchange="SMART", underlying_id=1001)),
            ("underlying_id", Instrument(con_id=1001, symbol="AAPL", currency="USD", sec_type="STK", exchange="SMART", underlying_id=500)),
            ("symbol", Instrument(con_id=1001, symbol="TSLA", currency="USD", sec_type="STK", exchange="SMART", underlying_id=1001)),
            ("sec_type", Instrument(con_id=1001, symbol="AAPL", currency="USD", sec_type="OPT", exchange="SMART", underlying_id=1001)),
            ("currency", Instrument(con_id=1001, symbol="AAPL", currency="EUR", sec_type="STK", exchange="SMART", underlying_id=1001)),
            ("exchange", Instrument(con_id=1001, symbol="AAPL", currency="USD", sec_type="STK", exchange="ISLAND", underlying_id=1001)),
            ("multiplier", Instrument(con_id=1001, symbol="AAPL", currency="USD", sec_type="STK", exchange="SMART", underlying_id=1001, multiplier=Decimal("100"))),
            ("right", Instrument(con_id=1001, symbol="AAPL", currency="USD", sec_type="STK", exchange="SMART", underlying_id=1001, right="C")),
            ("strike", Instrument(con_id=1001, symbol="AAPL", currency="USD", sec_type="STK", exchange="SMART", underlying_id=1001, strike=Decimal("150"))),
            ("expiry", Instrument(con_id=1001, symbol="AAPL", currency="USD", sec_type="STK", exchange="SMART", underlying_id=1001, expiry="20261120")),
            ("trading_class", Instrument(con_id=1001, symbol="AAPL", currency="USD", sec_type="STK", exchange="SMART", underlying_id=1001, trading_class="CLASS_B")),
        ]

        for field_name, bad_inst in field_variations:
            with self.subTest(field=field_name):
                pre_i, res_i = self._preflight_confirm(engine)
                leg_i = res_i.legs[0]
                wo_bad = WorkingOrder(
                    instrument=bad_inst, action="BUY", remaining=Decimal("2"),
                    order_id=leg_i.order_id, perm_id=leg_i.perm_id, order_ref=leg_i.order_ref,
                    limit_price=Decimal("180"), order_type="LMT")
                snap_bad = _snapshot(captured_at=NOW + timedelta(seconds=1), orders=(wo_bad,))
                with self.assertRaises(ExecutionError) as ctx:
                    engine.reconcile_positive(
                        account=ACCOUNT, environment=ENV, order_ref=leg_i.order_ref,
                        evidence=PositiveHandoff(broker_status="Submitted", order_ref=leg_i.order_ref,
                                                 account=ACCOUNT, environment=ENV,
                                                 observed_snapshot=snap_bad, con_id=getattr(bad_inst, "con_id", 1001)))
                self.assertIn("does not match the approved", str(ctx.exception))
                # Resolve leg so the account does not stay blocked on unresolved submission
                self.ledger.confirm_leg(leg_i.order_ref, broker_status="Filled")

    def test_reconcile_positive_adversarial_same_conid_full_mismatch(self):
        engine = self._engine()
        pre, result = self._preflight_confirm(engine)
        leg = result.legs[0]
        # Approved was AAPL USD STK con_id=1001.
        # Corrupted working order provides same con_id=1001 but TSLA EUR OPT PUT strike=500 mult=100
        corrupted_inst = Instrument(
            con_id=1001, symbol="TSLA", currency="EUR", sec_type="OPT", exchange="SMART",
            underlying_id=500, multiplier=Decimal("100"), right="P", strike=Decimal("500"),
            expiry="20261120", trading_class="TSLA")
        wo_corrupted = WorkingOrder(
            instrument=corrupted_inst, action="BUY", remaining=Decimal("2"),
            order_id=leg.order_id, perm_id=leg.perm_id, order_ref=leg.order_ref,
            limit_price=Decimal("180"), order_type="LMT")
        snap = _snapshot(captured_at=NOW + timedelta(seconds=1), orders=(wo_corrupted,))
        with self.assertRaises(ExecutionError) as ctx:
            engine.reconcile_positive(
                account=ACCOUNT, environment=ENV, order_ref=leg.order_ref,
                evidence=PositiveHandoff(broker_status="Submitted", order_ref=leg.order_ref,
                                         account=ACCOUNT, environment=ENV,
                                         observed_snapshot=snap, con_id=1001))
        self.assertIn("does not match the approved canonical instrument", str(ctx.exception))

    def test_fresh_ledger_reduction_engine_path_and_cross_day_migration(self):
        short_pos = Position(instrument=_opt_inst(con_id=2001, right="P"),
                             quantity=Decimal("-1"), price=Decimal("2"))
        snap_day1 = _snapshot(captured_at=NOW, cash="100000", positions=(short_pos,))
        engine = self._engine(
            verified_prior_submissions=None,  # fresh ledger, no verified prior submissions
            snapshot_provider=lambda: snap_day1)

        # Risk reduction order succeeds under quota exception on fresh ledger
        opt_close = _opt_leg(con_id=2001, action="BUY", right="P", price=Decimal("2"), qty=Decimal("1"))
        pre_rr = engine.preflight([opt_close], account=ACCOUNT, environment=ENV)
        self.assertEqual(pre_rr.approval_code, state.approval_code(pre_rr.full_digest, f"{ACCOUNT}\x1f{ENV}"))
        res_rr = engine.confirm(intent_id=pre_rr.intent_id, code=pre_rr.approval_code,
                                legs=[opt_close], account=ACCOUNT, environment=ENV)
        self.assertEqual(res_rr.decision, "ALLOW")
        self.assertFalse(res_rr.frozen)
        self.assertTrue(res_rr.verification["exception_applied"])

        # Check ledger day budget: migration_frozen is True and submitted_count is retained (1)
        day1_key = "2026-11-07"
        budget1 = self.ledger.get_day_budget(account=ACCOUNT, environment=ENV, day_key=day1_key)
        self.assertTrue(budget1["migration_frozen"])
        self.assertEqual(budget1["submitted_count"], 1)

        # Resolve res_rr leg so account is not blocked by unresolved submission
        self.ledger.confirm_leg(res_rr.legs[0].order_ref, broker_status="Filled")

        # Subsequent new risk order on same day is BLOCKED by migration freeze
        pre_new = engine.preflight([_leg()], account=ACCOUNT, environment=ENV)
        res_new = engine.confirm(intent_id=pre_new.intent_id, code=pre_new.approval_code,
                                 legs=[_leg()], account=ACCOUNT, environment=ENV)
        self.assertEqual(res_new.decision, "BLOCK")
        self.assertTrue(res_new.frozen)

        # Cross-day behavior: advance snapshot and clock to next configured natural day
        tomorrow = NOW + timedelta(days=1)
        snap_day2 = _snapshot(captured_at=tomorrow, cash="100000")
        engine_day2 = self._engine(
            verified_prior_submissions=None,
            clock=lambda: tomorrow,
            snapshot_provider=lambda: snap_day2)

        # On the next natural day, controlled zero unlocks new risk
        pre_day2 = engine_day2.preflight([_leg()], account=ACCOUNT, environment=ENV)
        res_day2 = engine_day2.confirm(intent_id=pre_day2.intent_id, code=pre_day2.approval_code,
                                       legs=[_leg()], account=ACCOUNT, environment=ENV)
        self.assertEqual(res_day2.decision, "ALLOW")
        self.assertFalse(res_day2.frozen)

        # Day 1 budget still retains its actual attempt count
        budget1_after = self.ledger.get_day_budget(account=ACCOUNT, environment=ENV, day_key=day1_key)
        self.assertEqual(budget1_after["submitted_count"], 1)
        self.assertTrue(budget1_after["migration_frozen"])

    def test_verify_net_reduction_unconfirmed_cash_and_shared_metrics(self):
        engine = self._engine()
        # Snapshot with unconfirmed cash (cash is empty mapping) but valid short position
        short_pos = Position(instrument=_opt_inst(con_id=2001, right="P"),
                             quantity=Decimal("-1"), price=Decimal("2"))
        snap_unconfirmed = Snapshot(
            account=ACCOUNT, environment=ENV,
            captured_at=NOW - timedelta(seconds=2),
            base_currency="USD", cash={},
            net_liquidation=Decimal("100000"),
            positions=(short_pos,), orders=(), fx={"USD": Decimal(1)},
            issues=())
        opt_close = _opt_leg(con_id=2001, action="BUY", right="P", price=Decimal("2"), qty=Decimal("1"))
        ok, reason = engine._verify_net_reduction([opt_close], snap_unconfirmed, {})
        self.assertFalse(ok)
        self.assertIn("cash balance not confirmed", reason)

        # Snapshot with confirmed cash but insufficient free cash
        snap_poor = _snapshot(cash="100", positions=(short_pos,))
        ok2, reason2 = engine._verify_net_reduction([opt_close], snap_poor, {})
        self.assertFalse(ok2)
        self.assertIn("exceeds free cash", reason2)


if __name__ == "__main__":
    unittest.main()
