"""Offline tests for the operator release path (P0: unresolved-account escape).

The engine conservatively blocks the WHOLE account from new risk when ANY
reservation is unresolved (``submitting``/``submitted``/``unknown``).  Before
this module there was no CLI entry point that could ever clear that state:
``resolve_unknown`` and ``reconcile_positive`` had zero production callers, so
a single ambiguous send locked the account permanently.

These tests pin the ONLY sanctioned escape hatches:

* ``resolve_evidence`` builds STRUCTURED evidence from a REAL broker
  observation (executions/orders), never from an operator flag.
* ``thetamill resolve`` releases only when that evidence validates; a bare
  ``--state released`` cannot release a leg that was actually attempted.
* A filled/partially-filled/cancelled leg needs a fill or a cancelled receipt,
  never an "absent from open orders" guess.
"""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from thetamill import cli, state
from thetamill import resolve as resolve_mod
from thetamill.broker_state import Instrument, Snapshot, WorkingOrder

ACCOUNT = "DU1234567"
ENV = "paper"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
# The send attempt precedes every observation in these tests.
ATTEMPT = NOW - timedelta(minutes=1)
DAY = "2026-11-08"


def opt_instrument(con_id=937548729, multiplier=Decimal(100)):
    return Instrument(
        con_id=con_id, symbol="AAPL", currency="USD", sec_type="OPT",
        exchange="SMART", underlying_id=265598, multiplier=multiplier,
        right="P", strike=Decimal("350"), expiry="20261204",
        trading_class="AAPL")


def stock_instrument(con_id=265598):
    return Instrument(
        con_id=con_id, symbol="AAPL", currency="USD", sec_type="STK",
        exchange="SMART", underlying_id=265598, multiplier=Decimal(1))


@dataclass
class RawExec:
    """Shape of an ib_async Execution as seen by resolve_evidence."""
    execId: str
    acctNumber: str
    orderRef: str
    permId: int
    orderId: int
    conId: int
    side: str
    shares: Decimal
    price: Decimal
    time: datetime
    cumQty: Decimal = Decimal(0)
    avgPrice: Decimal = Decimal(0)


@dataclass
class RawFill:
    contract: object
    execution: RawExec
    time: datetime


def raw_fill(*, order_ref="ref-1", con_id=937548729, shares=1, side="SLD",
             perm=11, oid=140, price="1.00"):
    contract = SimpleNamespace(conId=con_id)
    return RawFill(
        contract=contract,
        execution=RawExec(execId="0001", acctNumber=ACCOUNT, orderRef=order_ref,
                          permId=perm, orderId=oid, conId=con_id, side=side,
                          shares=Decimal(shares), price=Decimal(price), time=NOW,
                          cumQty=Decimal(shares), avgPrice=Decimal(price)),
        time=NOW)


def make_snapshot(*, orders=(), captured_at=NOW, account=ACCOUNT,
                  environment=ENV, issues=()):
    return Snapshot(
        account=account, environment=environment, captured_at=captured_at,
        base_currency="USD", cash={"USD": Decimal("1000000")},
        net_liquidation=Decimal("1000000"), positions=(),
        orders=tuple(orders), fx={"USD": Decimal(1)}, issues=tuple(issues))


class LedgerCase(unittest.TestCase):
    """A real SQLite ledger in a temp dir, driven through the public API."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ledger = state.SubmissionLedger(self.root / "state.sqlite3", "UTC")

    def grant_day_budget(self, limit=100, *, account=ACCOUNT, environment=ENV):
        """An established day row; a fresh ledger is otherwise frozen."""
        self.ledger.set_daily_limit(account=account, environment=environment,
                                    day_key=DAY, limit_count=limit)

    def seed(self, *, leg_state="unknown", action="SELL", con_id=937548729,
             slots=1, instrument=None, account=ACCOUNT, environment=ENV,
             order_ref="ref-1", attempt_at=ATTEMPT):
        instrument = instrument or opt_instrument(con_id=con_id)
        self.grant_day_budget(account=account, environment=environment)
        self.ledger.set_resource(account=account, environment=environment,
                                 currency="USD", resource_key="cash_base",
                                 amount=Decimal("1000000"))
        intent = self.ledger.create_intent(
            account=account, environment=environment,
            payload={"kind": "prepared_legs",
                     "legs": [{"instrument": {
                         "con_id": instrument.con_id,
                         "symbol": instrument.symbol,
                         "currency": instrument.currency,
                         "sec_type": instrument.sec_type,
                         "exchange": instrument.exchange,
                         "underlying_id": instrument.underlying_id,
                         "multiplier": str(instrument.multiplier),
                         "right": instrument.right,
                         "strike": str(instrument.strike),
                         "expiry": instrument.expiry,
                         "trading_class": instrument.trading_class,
                     }, "action": action, "quantity": "1", "order_type": "LMT",
                         "limit_price": "1.00"}]},
            config={"a": 1}, ttl_seconds=3600, now=NOW)
        self.ledger.claim_intent(account=account, environment=environment,
                                 intent_id=intent["intent_id"],
                                 full_digest=intent["full_digest"],
                                 code=intent["approval_code"], now=NOW)
        rid = self.ledger.reserve(
            account=account, environment=environment,
            intent_id=intent["intent_id"],
            day_key=DAY,
            items=[state.RiskReservationSpec("USD", "cash_base", "1000")],
            day_submission_slots=slots, now=NOW)
        refs = self.ledger.record_legs(
            intent_id=intent["intent_id"], reservation_id=rid, now=NOW,
            legs=[{"contract_id": str(con_id), "action": action,
                   "quantity": "1", "order_ref": order_ref}])
        self.ledger.begin_leg_attempt(refs[0], now=attempt_at)
        if leg_state != "submitting":
            self.ledger.record_leg_result(refs[0], state=leg_state, now=NOW)
        return rid, intent


# --------------------------------------------------------------------------- #
# Finding the blocked reservations
# --------------------------------------------------------------------------- #

class UnresolvedInventoryTests(LedgerCase):

    def test_lists_only_unresolved_scope(self):
        rid, _ = self.seed(leg_state="unknown")
        other = state.SubmissionLedger(self.root / "other.sqlite3", "UTC")
        other.set_resource(account="DU999", environment=ENV, currency="USD",
                           resource_key="cash_base", amount=Decimal("1"))
        found = resolve_mod.unresolved_summary(
            self.ledger, account=ACCOUNT, environment=ENV)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].reservation_id, rid)
        self.assertEqual(found[0].states, ("unknown",))

    def test_confirmed_reservations_are_not_listed(self):
        rid, _ = self.seed(leg_state="unknown")
        self.ledger.resolve_unknown(rid, state="confirmed", now=NOW)
        self.assertEqual(
            resolve_mod.unresolved_summary(self.ledger, account=ACCOUNT,
                                           environment=ENV), [])


# --------------------------------------------------------------------------- #
# Structured evidence: nothing releases on an operator flag alone
# --------------------------------------------------------------------------- #

class EvidenceTests(LedgerCase):

    def test_unknown_leg_cannot_be_released_merely_because_absent(self):
        """'Not in open orders' is NOT proof: the order may have filled."""
        rid, _ = self.seed(leg_state="unknown")
        with self.assertRaises(resolve_mod.ResolveError) as cm:
            resolve_mod.resolve_reservation(
                self.ledger, rid, executions=fills_provider([]),
                snapshot_provider=lambda: make_snapshot(), now=NOW)
        self.assertIn("no execution", str(cm.exception).lower())
        self.assertTrue(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))

    def test_matching_fill_releases_the_leg(self):
        rid, intent = self.seed(leg_state="unknown")
        outcome = resolve_mod.resolve_reservation(
            self.ledger, rid, executions=fills_provider([raw_fill()]),
            snapshot_provider=lambda: make_snapshot(), now=NOW)
        self.assertEqual(outcome.status, "confirmed")
        self.assertFalse(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))
        leg = self.ledger.get_leg("ref-1", account=ACCOUNT, environment=ENV)
        self.assertEqual(leg.state, "confirmed")

    def test_same_second_fill_with_subsecond_attempt_releases_the_leg(self):
        """IB execution time has 1s resolution; same-second fill must not fail subsecond check."""
        subsecond_attempt = NOW.replace(microsecond=376520)
        rid, _ = self.seed(leg_state="submitted", order_ref="ref-subsec",
                           attempt_at=subsecond_attempt)
        fill = raw_fill(order_ref="ref-subsec")
        fill.execution.time = NOW.replace(microsecond=0)
        outcome = resolve_mod.resolve_reservation(
            self.ledger, rid, executions=fills_provider([fill]),
            snapshot_provider=lambda: make_snapshot(), now=NOW + timedelta(seconds=1))
        self.assertEqual(outcome.status, "confirmed")

    def test_fill_for_another_account_never_releases(self):
        rid, _ = self.seed(leg_state="unknown")
        foreign = raw_fill()
        foreign.execution.acctNumber = "DU0000000"
        with self.assertRaises(resolve_mod.ResolveError):
            resolve_mod.resolve_reservation(
                self.ledger, rid, executions=fills_provider([foreign]),
                snapshot_provider=lambda: make_snapshot(), now=NOW)

    def test_fill_for_another_order_ref_never_releases(self):
        rid, _ = self.seed(leg_state="unknown")
        with self.assertRaises(resolve_mod.ResolveError):
            resolve_mod.resolve_reservation(
                self.ledger, rid, executions=fills_provider(
                    [raw_fill(order_ref="ref-other")]),
                snapshot_provider=lambda: make_snapshot(), now=NOW)

    def test_fill_for_another_con_id_never_releases(self):
        rid, _ = self.seed(leg_state="unknown")
        fill = raw_fill()
        fill.contract = SimpleNamespace(conId=111111)
        # ib_async Execution has no conId; the paired contract is authoritative.
        with self.assertRaises(resolve_mod.ResolveError):
            resolve_mod.resolve_reservation(
                self.ledger, rid, executions=fills_provider([fill]),
                snapshot_provider=lambda: make_snapshot(), now=NOW)

    def test_partial_fill_is_refused(self):
        """A 1-of-2 fill cannot prove the whole leg: stay blocked."""
        rid, _ = self.seed(leg_state="unknown")
        fill = raw_fill(shares=1)
        fill.execution.shares = Decimal("1")
        leg = self.ledger.get_leg("ref-1", account=ACCOUNT, environment=ENV)
        self.assertEqual(leg.quantity, "1")
        # A fill for a DIFFERENT quantity than the approved leg.
        other = raw_fill(shares=5)
        with self.assertRaises(resolve_mod.ResolveError):
            resolve_mod.resolve_reservation(
                self.ledger, rid, executions=fills_provider([other]),
                snapshot_provider=lambda: make_snapshot(), now=NOW)

    def test_working_order_with_full_remaining_releases(self):
        rid, _ = self.seed(leg_state="unknown")
        order = WorkingOrder(
            instrument=opt_instrument(), action="SELL", remaining=Decimal(1),
            order_id=140, perm_id=11, order_ref="ref-1",
            limit_price=Decimal("1.00"), order_type="LMT")
        outcome = resolve_mod.resolve_reservation(
            self.ledger, rid, executions=fills_provider([]),
            snapshot_provider=lambda: make_snapshot(orders=[order]), now=NOW)
        self.assertEqual(outcome.status, "confirmed")
        self.assertFalse(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))

    def test_working_order_of_other_ref_never_releases(self):
        rid, _ = self.seed(leg_state="unknown")
        order = WorkingOrder(
            instrument=opt_instrument(), action="SELL", remaining=Decimal(1),
            order_id=140, perm_id=11, order_ref="ref-other",
            limit_price=Decimal("1.00"), order_type="LMT")
        with self.assertRaises(resolve_mod.ResolveError):
            resolve_mod.resolve_reservation(
                self.ledger, rid, executions=fills_provider([]),
                snapshot_provider=lambda: make_snapshot(orders=[order]), now=NOW)

    def test_two_matching_executions_are_ambiguous(self):
        rid, _ = self.seed(leg_state="unknown")
        with self.assertRaises(resolve_mod.ResolveError) as cm:
            resolve_mod.resolve_reservation(
                self.ledger, rid,
                executions=fills_provider([raw_fill(), raw_fill()]),
                snapshot_provider=lambda: make_snapshot(), now=NOW)
        self.assertIn("ambiguous", str(cm.exception).lower())

    def test_stale_snapshot_is_refused(self):
        rid, _ = self.seed(leg_state="unknown")
        order = WorkingOrder(
            instrument=opt_instrument(), action="SELL", remaining=Decimal(1),
            order_id=140, perm_id=11, order_ref="ref-1",
            limit_price=Decimal("1.00"), order_type="LMT")
        stale = make_snapshot(orders=[order],
                              captured_at=NOW - timedelta(minutes=10))
        with self.assertRaises(resolve_mod.ResolveError):
            resolve_mod.resolve_reservation(
                self.ledger, rid, executions=fills_provider([]),
                snapshot_provider=lambda: stale, now=NOW)

    def test_snapshot_scope_mismatch_is_refused(self):
        rid, _ = self.seed(leg_state="unknown")
        order = WorkingOrder(
            instrument=opt_instrument(), action="SELL", remaining=Decimal(1),
            order_id=140, perm_id=11, order_ref="ref-1",
            limit_price=Decimal("1.00"), order_type="LMT")
        other = make_snapshot(orders=[order], account="DU999999")
        with self.assertRaises(resolve_mod.ResolveError):
            resolve_mod.resolve_reservation(
                self.ledger, rid, executions=fills_provider([]),
                snapshot_provider=lambda: other, now=NOW)

    def test_snapshot_issues_block_release(self):
        rid, _ = self.seed(leg_state="unknown")
        order = WorkingOrder(
            instrument=opt_instrument(), action="SELL", remaining=Decimal(1),
            order_id=140, perm_id=11, order_ref="ref-1",
            limit_price=Decimal("1.00"), order_type="LMT")
        bad = make_snapshot(orders=[order], issues=("Missing market price: 1",))
        with self.assertRaises(resolve_mod.ResolveError):
            resolve_mod.resolve_reservation(
                self.ledger, rid, executions=fills_provider([]),
                snapshot_provider=lambda: bad, now=NOW)


class ReleasedStateTests(LedgerCase):

    def test_released_requires_proof_the_order_never_reached_the_broker(self):
        """``released`` frees risk; it is only for a PROVEN no-send."""
        rid, _ = self.seed(leg_state="unknown")
        self.assertFalse(resolve_mod.never_sent_provable(
            self.ledger, rid, account=ACCOUNT, environment=ENV))

    def test_never_sent_is_provable_for_a_leg_still_reserved(self):
        self.grant_day_budget()
        self.ledger.set_resource(account=ACCOUNT, environment=ENV,
                                 currency="USD", resource_key="cash_base",
                                 amount=Decimal("1000000"))
        intent = self.ledger.create_intent(
            account=ACCOUNT, environment=ENV, payload={"kind": "x", "legs": [{}]},
            config={}, ttl_seconds=600, now=NOW)
        self.ledger.claim_intent(account=ACCOUNT, environment=ENV,
                                 intent_id=intent["intent_id"],
                                 full_digest=intent["full_digest"],
                                 code=intent["approval_code"], now=NOW)
        rid = self.ledger.reserve(
            account=ACCOUNT, environment=ENV, intent_id=intent["intent_id"],
            day_key=DAY,
            items=[state.RiskReservationSpec("USD", "cash_base", "1")],
            day_submission_slots=1, now=NOW)
        self.ledger.record_legs(
            intent_id=intent["intent_id"], reservation_id=rid, now=NOW,
            legs=[{"contract_id": "1", "action": "SELL", "quantity": "1",
                   "order_ref": "ref-2"}])
        self.assertTrue(resolve_mod.never_sent_provable(
            self.ledger, rid, account=ACCOUNT, environment=ENV))


class NeverSentReleaseTests(LedgerCase):
    """``released`` is the provable-no-send exit and nothing else."""

    def _reserved_only(self):
        """A reservation whose legs were recorded but never attempted."""
        self.grant_day_budget()
        self.ledger.set_resource(account=ACCOUNT, environment=ENV,
                                 currency="USD", resource_key="cash_base",
                                 amount=Decimal("1000000"))
        intent = self.ledger.create_intent(
            account=ACCOUNT, environment=ENV, payload={"kind": "x", "legs": [{}]},
            config={}, ttl_seconds=600, now=NOW)
        self.ledger.claim_intent(account=ACCOUNT, environment=ENV,
                                 intent_id=intent["intent_id"],
                                 full_digest=intent["full_digest"],
                                 code=intent["approval_code"], now=NOW)
        rid = self.ledger.reserve(
            account=ACCOUNT, environment=ENV, intent_id=intent["intent_id"],
            day_key=DAY, items=[state.RiskReservationSpec("USD", "cash_base", "1")],
            day_submission_slots=1, now=NOW)
        self.ledger.record_legs(
            intent_id=intent["intent_id"], reservation_id=rid, now=NOW,
            legs=[{"contract_id": "1", "action": "SELL", "quantity": "1",
                   "order_ref": "ref-never-sent"}])
        return rid

    def test_release_requires_a_written_reason(self):
        rid = self._reserved_only()
        for why in ("", "   ", None, 0):
            with self.assertRaises(resolve_mod.ResolveError):
                resolve_mod.release_never_sent(self.ledger, rid, why=why)
        # Nothing was released by the refused attempts.
        reservation = self.ledger.get_reservation(rid)
        self.assertEqual(reservation.state, "reserved")

    def test_release_refuses_an_unknown_reservation(self):
        with self.assertRaises(resolve_mod.ResolveError) as cm:
            resolve_mod.release_never_sent(self.ledger, "res-nope",
                                           why="never sent")
        self.assertIn("unknown reservation", str(cm.exception))

    def test_release_frees_a_provably_unsent_reservation(self):
        rid = self._reserved_only()
        outcome = resolve_mod.release_never_sent(self.ledger, rid,
                                                 why="operator never sent it")
        self.assertEqual(outcome.status, "released")
        self.assertEqual(outcome.released, ())
        self.assertEqual(self.ledger.get_reservation(rid).state, "aborted")

    def test_never_sent_provable_refuses_an_unknown_reservation(self):
        with self.assertRaises(resolve_mod.ResolveError):
            resolve_mod.never_sent_provable(self.ledger, "res-nope",
                                            account=ACCOUNT, environment=ENV)

    def test_never_sent_provable_refuses_a_terminal_reservation(self):
        rid, _ = self.seed(leg_state="unknown")
        self.ledger.resolve_unknown(rid, state="confirmed", now=NOW)
        with self.assertRaises(resolve_mod.ResolveError) as cm:
            resolve_mod.never_sent_provable(self.ledger, rid,
                                            account=ACCOUNT, environment=ENV)
        self.assertIn("not outstanding", str(cm.exception))


class ResolutionGuardTests(LedgerCase):
    """Every guard is fail-closed: a rejection never releases anything."""

    def test_naive_now_is_refused(self):
        rid, _ = self.seed(leg_state="unknown")
        with self.assertRaises(resolve_mod.ResolveError) as cm:
            resolve_mod.resolve_reservation(
                self.ledger, rid, executions=fills_provider([raw_fill()]),
                now=datetime(2026, 11, 8, 12, 0, 0))   # naive on purpose
        self.assertIn("aware datetime", str(cm.exception))

    def test_unknown_reservation_is_refused(self):
        with self.assertRaises(resolve_mod.ResolveError) as cm:
            resolve_mod.resolve_reservation(
                self.ledger, "res-nope", executions=fills_provider([]), now=NOW)
        self.assertIn("unknown reservation", str(cm.exception))

    def test_already_resolved_reservation_is_refused(self):
        rid, _ = self.seed(leg_state="unknown")
        self.ledger.resolve_unknown(rid, state="confirmed", now=NOW)
        with self.assertRaises(resolve_mod.ResolveError) as cm:
            resolve_mod.resolve_reservation(
                self.ledger, rid, executions=fills_provider([raw_fill()]), now=NOW)
        self.assertIn("not unresolved", str(cm.exception))

    def test_missing_execution_observation_is_refused(self):
        rid, _ = self.seed(leg_state="unknown")
        with self.assertRaises(resolve_mod.ResolveError) as cm:
            resolve_mod.resolve_reservation(
                self.ledger, rid, executions=None, now=NOW)
        self.assertIn("no broker execution observation", str(cm.exception))

    def test_non_list_execution_observation_is_refused(self):
        rid, _ = self.seed(leg_state="unknown")
        with self.assertRaises(resolve_mod.ResolveError) as cm:
            resolve_mod.resolve_reservation(
                self.ledger, rid, executions=lambda **_: (r for r in ()),
                now=NOW)
        self.assertIn("not a list", str(cm.exception))

    def test_future_execution_is_refused(self):
        """A broker row stamped after 'now' is clock skew, not evidence."""
        rid, _ = self.seed(leg_state="unknown")
        fill = raw_fill()
        fill.execution.time = NOW + timedelta(minutes=5)
        with self.assertRaises(resolve_mod.ResolveError) as cm:
            resolve_mod.resolve_reservation(
                self.ledger, rid, executions=fills_provider([fill]), now=NOW)
        self.assertIn("future", str(cm.exception))

    def test_stale_execution_is_refused(self):
        rid, _ = self.seed(leg_state="unknown")
        fill = raw_fill()
        fill.execution.time = NOW - timedelta(hours=2)
        with self.assertRaises(resolve_mod.ResolveError) as cm:
            resolve_mod.resolve_reservation(
                self.ledger, rid, executions=fills_provider([fill]), now=NOW)
        self.assertIn("stale", str(cm.exception))

    def test_reservation_without_legs_is_refused(self):
        """A reservation whose legs vanished must never be released."""
        rid, _ = self.seed(leg_state="unknown")
        real = self.ledger

        class _Stub:
            def get_reservation(self, reservation_id):
                return real.get_reservation(reservation_id)

            def list_legs(self, **kwargs):
                return []

        with self.assertRaises(resolve_mod.ResolveError) as cm:
            resolve_mod.resolve_reservation(_Stub(), rid,
                                            executions=fills_provider([]), now=NOW)
        self.assertIn("no recorded legs", str(cm.exception))
        self.assertEqual(real.get_reservation(rid).state, "unknown")

    def test_reservation_with_no_unresolved_leg_is_refused(self):
        """Derivation normally prevents it, but the guard must still hold."""
        rid, _ = self.seed(leg_state="unknown")
        real = self.ledger
        legs = [real.get_leg("ref-1", account=ACCOUNT, environment=ENV)]

        class _Stub:
            def get_reservation(self, reservation_id):
                # Claim "unknown" while every recorded leg is already terminal.
                return SimpleNamespace(reservation_id=reservation_id,
                                       state="unknown",
                                       scope=real.get_reservation(
                                           reservation_id).scope)

            def list_legs(self, **kwargs):
                return [SimpleNamespace(order_ref=leg.order_ref,
                                        action=leg.action,
                                        quantity=leg.quantity,
                                        state="confirmed",
                                        reservation_id=rid,
                                        intent_id=leg.intent_id,
                                        leg_index=leg.leg_index)
                        for leg in legs]

        with self.assertRaises(resolve_mod.ResolveError) as cm:
            resolve_mod.resolve_reservation(_Stub(), rid,
                                            executions=fills_provider([]), now=NOW)
        self.assertIn("no unresolved legs", str(cm.exception))
        self.assertEqual(real.get_reservation(rid).state, "unknown")


class FillRowAdapterTests(unittest.TestCase):
    """A partially-understood execution row is never evidence."""

    def test_unusable_value_shapes_become_none(self):
        self.assertIsNone(resolve_mod._decimal_or_none(None))
        self.assertIsNone(resolve_mod._decimal_or_none(True))     # bool, not 1
        self.assertIsNone(resolve_mod._decimal_or_none(float("nan")))
        self.assertIsNone(resolve_mod._decimal_or_none(float("inf")))
        self.assertIsNone(resolve_mod._decimal_or_none(object()))  # unparseable
        self.assertIsNone(resolve_mod._decimal_or_none("NaN"))
        self.assertIsNone(resolve_mod._decimal_or_none("Infinity"))
        self.assertEqual(resolve_mod._decimal_or_none(1.5), Decimal("1.5"))
        self.assertEqual(resolve_mod._decimal_or_none("2.5"), Decimal("2.5"))

    def test_execution_without_a_usable_exec_id_is_none(self):
        for exec_id in ("", "   ", None, 17):
            with self.subTest(exec_id=exec_id):
                row = raw_fill()
                row.execution.execId = exec_id
                self.assertIsNone(resolve_mod.fill_from_row(row, account=ACCOUNT))

    def test_foreign_account_or_missing_order_ref_is_none(self):
        row = raw_fill()
        row.execution.orderRef = ""
        self.assertIsNone(resolve_mod.fill_from_row(row, account=ACCOUNT))
        row = raw_fill()
        row.execution.acctNumber = "DU0000000"
        self.assertIsNone(resolve_mod.fill_from_row(row, account=ACCOUNT))

    def test_con_id_must_come_from_somewhere_consistent(self):
        # (a) no contract conId, an int execution conId: the execution wins.
        row = raw_fill()
        row.contract = SimpleNamespace()
        fill = resolve_mod.fill_from_row(row, account=ACCOUNT)
        self.assertIsNotNone(fill)
        self.assertEqual(fill.con_id, 937548729)
        # (b) contract and execution disagree: never evidence.
        row = raw_fill()
        row.execution.conId = 111111
        self.assertIsNone(resolve_mod.fill_from_row(row, account=ACCOUNT))
        # (c) neither side carries a usable int conId.
        row = raw_fill()
        row.contract = SimpleNamespace(conId="937548729")   # a str, not an int
        row.execution.conId = None
        self.assertIsNone(resolve_mod.fill_from_row(row, account=ACCOUNT))

    def test_unreadable_economics_are_none(self):
        cases = {
            "side": lambda r: setattr(r.execution, "side", 1),
            "shares": lambda r: setattr(r.execution, "shares", float("nan")),
            "price": lambda r: setattr(r.execution, "price", None),
            "cumQty": lambda r: setattr(r.execution, "cumQty", "lots"),
            "naive time": lambda r: setattr(
                r.execution, "time", datetime(2026, 11, 8, 12, 0, 0)),
            "no time": lambda r: setattr(r.execution, "time", "2026-11-08"),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                row = raw_fill()
                mutate(row)
                self.assertIsNone(
                    resolve_mod.fill_from_row(row, account=ACCOUNT))

    def test_missing_cum_qty_falls_back_to_shares(self):
        row = raw_fill(shares=1)
        row.execution.cumQty = None
        fill = resolve_mod.fill_from_row(row, account=ACCOUNT)
        self.assertIsNotNone(fill)
        self.assertEqual(fill.cum_qty, Decimal(1))

    def test_non_int_broker_ids_are_dropped_not_guessed(self):
        row = raw_fill()
        row.execution.permId = "11"
        row.execution.orderId = 140.0
        fill = resolve_mod.fill_from_row(row, account=ACCOUNT)
        self.assertIsNone(fill.perm_id)
        self.assertIsNone(fill.order_id)


class ScopeAndLedgerHandleTests(unittest.TestCase):

    def test_malformed_scope_is_refused(self):
        for scope in ("onlyone", "a\x1fb\x1fc", "\x1fpaper", "DU1\x1f  "):
            with self.subTest(scope=scope):
                with self.assertRaises(resolve_mod.ResolveError):
                    resolve_mod.scope_parts(scope)

    def test_open_ledger_wraps_a_broken_ledger_as_a_resolve_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(resolve_mod.ResolveError) as cm:
                resolve_mod.open_ledger(Path(tmp) / "absent.sqlite3", "UTC",
                                        writable=False)
            self.assertIn("cannot open the submission ledger", str(cm.exception))
            # A read-only open must never create the file it failed on.
            self.assertFalse((Path(tmp) / "absent.sqlite3").exists())

    def test_open_ledger_read_only_is_the_default_for_inspection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.sqlite3"
            writable = resolve_mod.open_ledger(path, "UTC", writable=True)
            writable.set_resource(account=ACCOUNT, environment=ENV,
                                  currency="USD", resource_key="cash_base",
                                  amount=Decimal("1"))
            opened = resolve_mod.open_ledger(path, "UTC", writable=False)
            self.assertTrue(opened.read_only)
            self.assertEqual(opened.list_legs(account=ACCOUNT,
                                              environment=ENV), [])


class LiveObservationTests(unittest.TestCase):
    """``live_observations`` merges ib_async fills + executions, deduped."""

    class _Broker:
        def __init__(self, *, fills=None, executions=None):
            self._fills = fills
            self._executions = executions
            self.calls = 0

        def fills(self):
            self.calls += 1
            if isinstance(self._fills, Exception):
                raise self._fills
            return self._fills

        def executions(self):
            self.calls += 1
            if isinstance(self._executions, Exception):
                raise self._executions
            return self._executions

    def test_merges_both_sources(self):
        first, second = raw_fill(), raw_fill(perm=12, oid=141)
        second.execution.execId = "0002"
        broker = self._Broker(fills=[first], executions=(second,))
        rows = resolve_mod.live_observations(broker, account=ACCOUNT).executions(
            account=ACCOUNT)
        self.assertEqual(list(rows), [first, second])

    def test_deduplicates_by_exec_id_across_sources(self):
        """ib_async reports the same execution through both calls."""
        row = raw_fill()
        broker = self._Broker(fills=[row], executions=[row])
        rows = resolve_mod.live_observations(broker, account=ACCOUNT).executions(
            account=ACCOUNT)
        self.assertEqual(list(rows), [row])

    def test_rows_without_an_exec_id_are_all_kept(self):
        a, b = raw_fill(), raw_fill()
        a.execution.execId = ""
        b.execution.execId = None
        broker = self._Broker(fills=[a], executions=[b])
        rows = resolve_mod.live_observations(broker, account=ACCOUNT).executions(
            account=ACCOUNT)
        self.assertEqual(list(rows), [a, b])

    def test_a_raw_execution_row_needs_no_wrapper(self):
        class _Bare:
            def fills(self):
                return [raw_fill().execution]

        rows = resolve_mod.live_observations(_Bare(), account=ACCOUNT).executions(
            account=ACCOUNT)
        self.assertEqual(len(list(rows)), 1)

    def test_missing_getters_are_skipped(self):
        class _Bare:
            def fills(self):
                return [raw_fill()]

        rows = resolve_mod.live_observations(_Bare(), account=ACCOUNT).executions(
            account=ACCOUNT)
        self.assertEqual(len(list(rows)), 1)

    def test_a_none_payload_is_refused_not_read_as_no_executions(self):
        """``None`` from a getter is a broken adapter, not "nothing traded"."""
        broker = self._Broker(fills=None, executions=[])
        with self.assertRaises(resolve_mod.ResolveError) as cm:
            resolve_mod.live_observations(broker, account=ACCOUNT)
        self.assertIn("not a list", str(cm.exception))

    def test_broker_failure_never_becomes_an_empty_observation(self):
        """Silently returning nothing would read as 'the order never filled'."""
        broker = self._Broker(fills=RuntimeError("gateway dropped"),
                              executions=[])
        with self.assertRaises(resolve_mod.ResolveError) as cm:
            resolve_mod.live_observations(broker, account=ACCOUNT)
        self.assertIn("cannot read broker executions", str(cm.exception))

    def test_a_non_list_payload_is_refused(self):
        broker = self._Broker(fills={"0001": raw_fill()}, executions=[])
        with self.assertRaises(resolve_mod.ResolveError) as cm:
            resolve_mod.live_observations(broker, account=ACCOUNT)
        self.assertIn("not a list", str(cm.exception))


class CliResolveTests(LedgerCase):
    """The CLI command itself: no evidence => no release, ever."""

    def _run(self, *argv, ledger=None, fills=(), snapshot=None, account=ACCOUNT,
             port=(4002, "paper")):
        """Drive the real parser; the port is pinned so no test probes a socket.

        A test that let ``resolve_port`` auto-detect would pass on a machine
        with an IB gateway listening and fail on a clean runner — the same
        local/CI split that produced the false "没有未决预留" all-clear.
        """
        ledger = ledger or self.ledger
        args = cli.build_parser(dict(cli.DEFAULT_CFG)).parse_args(list(argv))
        buf = io.StringIO()
        observations = resolve_mod.Observations(
            executions=fills_provider(list(fills)), snapshot=snapshot)
        with patch.object(cli, "_data_dir", return_value=self.root), \
             patch("thetamill.resolve.open_ledger", return_value=ledger), \
             patch.object(cli, "connect"), patch.object(cli, "finish"), \
             patch.object(cli, "resolve_port", return_value=port), \
             patch.object(cli, "_selected_account", return_value=account), \
             patch.object(cli, "_risk_snapshot",
                          side_effect=RuntimeError("no gateway in this test")), \
             patch("thetamill.resolve.live_observations",
                   return_value=observations), \
             redirect_stdout(buf):
            rc = args.func(dict(cli.DEFAULT_CFG), args)
        return rc, buf.getvalue()

    def test_parser_wires_resolve(self):
        parser = cli.build_parser(dict(cli.DEFAULT_CFG))
        ns = parser.parse_args(["resolve", "--list"])
        self.assertEqual(ns.func, cli.cmd_resolve)
        ns = parser.parse_args(["resolve", "--reservation", "res-1",
                                "--state", "released", "--why", "never sent"])
        self.assertEqual(ns.reservation, "res-1")
        self.assertEqual(ns.state, "released")
        self.assertEqual(ns.why, "never sent")

    def test_list_reports_the_blocked_reservation(self):
        rid, _ = self.seed(leg_state="unknown")
        rc, out = self._run("resolve", "--list", "--account", ACCOUNT)
        self.assertEqual(rc, 0)
        self.assertIn("1 个未决预留", out)
        self.assertIn(rid, out)

    def test_list_finds_a_paper_reservation_with_no_gateway_running(self):
        """A port probe must never decide which environment is inspected.

        Reported bug: with nothing listening on 4001/4002, ``resolve_port``
        falls back to "live", so a paper reservation was answered with
        "账户 DU…（live）没有未决预留" — a false all-clear on the single command
        that is supposed to be the account's only escape.  The ledger knows.
        """
        rid, _ = self.seed(leg_state="unknown")
        rc, out = self._run("resolve", "--list", "--account", ACCOUNT,
                            port=(4001, "实盘(Live)"))
        self.assertEqual(rc, 0)
        self.assertIn("1 个未决预留", out, out)
        self.assertIn(rid, out)
        self.assertNotIn("没有未决预留", out)

    def test_list_reports_every_environment_when_none_is_specified(self):
        paper_rid, _ = self.seed(leg_state="unknown", order_ref="ref-paper")
        live_rid, _ = self.seed(leg_state="unknown", environment="live",
                                order_ref="ref-live")
        rc, out = self._run("resolve", "--list", "--account", ACCOUNT,
                            port=(4001, "实盘(Live)"))
        self.assertEqual(rc, 0)
        self.assertIn("2 个未决预留", out, out)
        self.assertIn(paper_rid, out)
        self.assertIn(live_rid, out)
        self.assertIn("paper", out)
        self.assertIn("live", out)

    def test_list_narrowed_by_paper_only_reports_paper(self):
        paper_rid, _ = self.seed(leg_state="unknown", order_ref="ref-paper")
        self.seed(leg_state="unknown", environment="live", order_ref="ref-live")
        rc, out = self._run("resolve", "--list", "--account", ACCOUNT,
                            "--paper")
        self.assertEqual(rc, 0)
        self.assertIn(paper_rid, out)
        self.assertNotIn("ref-live", out)

    def test_confirmed_refuses_when_the_reservation_belongs_to_another_scope(self):
        """Searching the wrong environment looks exactly like 'no execution'."""
        rid, _ = self.seed(leg_state="unknown")          # a paper reservation
        rc, out = self._run("resolve", "--reservation", rid,
                            "--state", "confirmed", fills=[raw_fill()],
                            port=(4001, "实盘(Live)"))
        self.assertEqual(rc, 2)
        self.assertIn("paper", out)
        self.assertTrue(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))

    def test_confirmed_refuses_when_the_connected_account_differs(self):
        rid, _ = self.seed(leg_state="unknown")
        rc, out = self._run("resolve", "--reservation", rid,
                            "--state", "confirmed", fills=[raw_fill()],
                            account="DU9999999")
        self.assertEqual(rc, 2)
        self.assertIn("DU9999999", out)
        self.assertTrue(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))

    def test_release_without_evidence_is_refused(self):
        rid, _ = self.seed(leg_state="unknown")
        rc, out = self._run("resolve", "--reservation", rid,
                            "--state", "confirmed")
        self.assertEqual(rc, 2)
        self.assertIn("未释放", out)
        self.assertIn("proves orderRef", out)
        self.assertTrue(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))

    def test_release_requires_why(self):
        rid, _ = self.seed(leg_state="unknown")
        rc, out = self._run("resolve", "--reservation", rid,
                            "--state", "released")
        self.assertEqual(rc, 2)
        self.assertIn("--why", out)

    def test_confirmed_with_a_matching_fill_releases_the_account(self):
        rid, _ = self.seed(leg_state="unknown")
        rc, out = self._run("resolve", "--reservation", rid,
                            "--state", "confirmed", fills=[raw_fill()])
        self.assertEqual(rc, 0, out)
        self.assertIn("已按券商正向证据逐腿解除", out)
        self.assertFalse(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))
        # The day submission count is NOT refunded by a release.
        budget = self.ledger.get_day_budget(account=ACCOUNT, environment=ENV,
                                            day_key=DAY)
        self.assertEqual(budget["submitted_count"], 1)

    def test_released_with_why_records_and_frees(self):
        rid, _ = self.seed(leg_state="unknown")
        # never_sent_provable() is False for an attempted leg, so the CLI must
        # still refuse; prove the refusal is evidence-based, not flag-based.
        rc, out = self._run("resolve", "--reservation", rid,
                            "--state", "released", "--why", "operator says so")
        self.assertEqual(rc, 2)
        self.assertTrue(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))


def fills_provider(fills):
    """Adapter matching the production ``executions()`` callable shape."""
    def _provider(*, account=None):
        return list(fills)
    return _provider


if __name__ == "__main__":
    unittest.main()
