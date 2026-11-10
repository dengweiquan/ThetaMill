#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared, offline-testable execution engine for order / combo / run.

This module wires the authoritative SQLite submission ledger
(:mod:`thetamill.state`) and the pure risk layer (:mod:`thetamill.risk`) into a
single, small protocol that every submission entry point can call.  It contains
NO CLI imports, NO config-file reads and NO ``ib_async`` import: the caller
supplies a broker-like object, a snapshot provider, a ``resource_plan`` callback,
an order builder and the canonical prepared legs.  No real account or network
operation is performed by this module itself — it only orchestrates durable
bookkeeping around the caller's ``place_order`` boundary.

Protocol (what the coordinator must uphold)
-------------------------------------------
1. ``preflight`` persists a fresh UUID intent with a canonical payload covering
   EVERY order field (contract/action/quantity/type/prices/TIF/outsideRth/
   exchange/strategy/account/env/config/ttl) and returns the short approval
   code.  Re-preflighting identical terms yields a *new* intent.
2. ``confirm`` requires an EXPLICIT ``intent_id`` AND ``code``.  Inside ONE
   account-scoped ``fcntl`` lock it refresh-checks, re-verifies the canonical
   payload/config digest and expiry, atomically claims the intent, refreshes
   the broker snapshot, and runs pure risk assessment.
3. The full-package budget is computed by ``resource_plan`` from the FRESH
   snapshot UNDER THE LOCK (static constructor values are NOT trusted).  It
   returns the available resource amounts and the per-leg INCREMENTAL risk
   items; the whole package (all legs' day slots + all risk increments) is
   reserved BEFORE any send.  Empty / under-sized packages for risk-increasing
   legs are refused (fail closed).
4. Stable per-leg ``orderRef`` records are persisted BEFORE ``placeOrder``.
   ``begin_leg_attempt`` durably counts ONE actual attempt immediately before
   the network boundary (may-have-sent, never refunded).  Unsent legs are
   ``skipped`` and their unspent slots refunded.
5. A ``place_order`` callback receipt is a LOCAL object, NOT a positive broker
   snapshot handoff.  A leg is only mapped ``submitted`` (retaining risk) on a
   positive callback status; it is NEVER ``confirm_leg``-ed from the callback.
   Release of risk requires a SEPARATE, explicit, STRUCTURED
   :meth:`ExecutionEngine.reconcile_positive` handoff: a :class:`PositiveHandoff`
   carrying a fresh, strictly-scoped Snapshot that MECHANICALLY maps the same
   stable orderRef/permId/orderId to a real WORKING order (a Filled/terminal
   order has no working order and is conservatively UNSUPPORTED).  Release is
   PER-LEG only; ``unknown`` is terminal, never auto-released or auto-retried.
   One unresolved submission conservatively blocks the WHOLE account from new
   risk.
6. ``confirm`` FULLY compares the passed canonical legs/config/account/env to
   the APPROVED stored payload before any claim or send; INDEPENDENTLY derives
   each increasing leg's resource requirements (never trusting the callback);
   assesses each leg against existing + OTHER legs' increments only (self
   excluded, no double-count); STRICTLY scopes EVERY refreshed snapshot; and
   re-reads the clock AFTER the in-lock refresh and before EACH send to
   re-verify TTL/snapshot freshness/day.  A deferred leg needs a STRUCTURED
   :class:`FillProof` whose account/env/conId/stableRef/finite EXACT full qty
   the engine verifies against the approved dependency leg.

Explicit limitations
--------------------
Single-host serialisation only; no exactly-once broker execution, no
distributed lock, no control over manual TWS / third-party clients, no claim
that an empty open-order list proves a non-submission.  A deferred-coverage
combo is NEVER atomic: legs are sent sequentially and the second leg proceeds
only on a STRUCTURED ``FillProof`` (an actual ``Filled`` with an EXACT full-
quantity match, verified against the approved leg) plus a fresh, passing risk
assessment.  A positive-broker-snapshot release is PER-LEG and requires a
mechanically-mapped WORKING order in the observed snapshot (Filled/terminal
orders are conservatively unsupported — no guessing from positions).
Risk-reducing (e.g. option BUY close) legs are
supported only through the same conservative whole-package reservation; an
independent close path that bypasses the unresolved-account block is
deliberately NOT implemented (it would require verified, unambiguous resource
accounting that the current ledger cannot prove).  All attempts — including
risk-reducing ones — are counted against the persistent day budget.
"""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from thetamill import risk, state
from thetamill.broker_state import Instrument, Snapshot, WorkingOrder
from thetamill.risk import Decision, OrderRequest, RiskLimits
from thetamill.state import RiskReservationSpec, SubmissionLedger

# A disabled P3 config must not impose a user-facing submission limit; the
# ledger still needs an internal bookkeeping cap.  This is deliberately huge
# and is never surfaced as a "user limit".  It is applied ONLY when no day row
# exists yet; an already-established / frozen day is NEVER clobbered.
_DISABLED_INTERNAL_BUDGET = 10 ** 6

# A positive-handoff observed Snapshot must be FRESH: at most this many seconds
# old (also capped by a configured max_snapshot_age_seconds when present) and at
# most this many seconds in the FUTURE relative to the in-lock clock.  A snapshot
# that is merely "newer than the attempt" but a day old is NOT evidence that the
# specific order is currently a live working order.
_HANDOFF_MAX_SNAPSHOT_AGE_SECONDS = 30
_HANDOFF_MAX_FUTURE_SECONDS = 5

# Only a genuinely FILLED order can serve as deferred-coverage proof.  A
# PreSubmitted / PartiallyFilled / Submitted / Cancelled status is NOT enough:
# the shares covering a short call must actually exist.  The proof is now a
# STRUCTURED FillProof whose EVERY field the engine verifies independently.


class ExecutionError(Exception):
    """Fail-closed execution error (never degrade to an unsafe submission)."""


class UnsafeComboError(ExecutionError):
    """A multi-leg combo lacks the safeguards required for sequential sends."""


@dataclass(frozen=True)
class FillProof:
    """STRUCTURED proof that an approved dependency leg actually FILLED.

    A bare ``"Filled"`` string (or any positive broker status) is NOT enough:
    the engine independently verifies EVERY field below against the APPROVED
    dependency leg, so the adapter's promise is never trusted on its own.
    ``filled_quantity`` must be a finite Decimal EXACTLY equal to the approved
    leg quantity (0 / partial / overfill / NaN / float all fail).  ``order_ref``
    must be the SAME stable ref used by ``begin_leg_attempt``/``bind_broker_ids``.
    """

    order_ref: str
    account: str
    environment: str
    con_id: int
    filled_quantity: Decimal
    status: str = "Filled"


@dataclass(frozen=True)
class PositiveFill:
    """STRUCTURED proof that ONE execution row belongs to an APPROVED leg.

    A ``Filled`` status string is NOT enough, and neither is a fresh position:
    attribution has to come from a broker EXECUTION row that names the SAME
    stable ``order_ref``.  Every field below is re-verified against the approved
    stored leg by :func:`verify_fill_against_approved`; nothing the adapter
    promises is trusted on its own.  ``shares``/``cum_qty`` must be finite
    Decimals EXACTLY equal to the approved quantity (a partial fill can never
    release the whole reserved obligation).
    """

    exec_id: str
    account: str
    order_ref: str
    con_id: int
    side: str            # normalised to BUY / SELL
    shares: Decimal
    price: Decimal
    cum_qty: Decimal
    as_of: datetime
    perm_id: Optional[int] = None
    order_id: Optional[int] = None


@dataclass(frozen=True)
class PositiveHandoff:
    """STRUCTURED, positively-observed broker-snapshot handoff evidence.

    A fresh Snapshot alone (scope / freshness / no issues) can NOT prove that a
    SPECIFIC order was absorbed by the market: the engine MECHANICALLY requires
    the SAME stable ``order_ref`` (and bound ``perm_id``/``order_id``) to map to
    a row in ``observed_snapshot.orders`` whose conId/action/remaining are
    consistent with the approved leg (so the matched item genuinely feeds
    ``risk._build_state``).  A ``Filled`` order has no working order, so a
    Filled handoff is CONSERVATIVELY UNSUPPORTED: we never guess attribution
    from a fresh position.  The observed snapshot must be captured AFTER the
    attempt and its IDs must not contradict the ledger's bound IDs.  Release is
    PER-LEG only — unmapped legs of the same package keep their reserved risk.
    """

    broker_status: str
    order_ref: str
    account: str
    environment: str
    observed_snapshot: Snapshot
    con_id: Optional[int] = None


@dataclass(frozen=True)
class ResourcePlan:
    """Full-package budget derived from a FRESH snapshot under the account lock.

    ``resources`` maps ``(currency, resource_key) -> available amount`` (the
    authoritative availability from the refreshed snapshot).  ``items`` is the
    per-leg INCREMENTAL ``RiskReservationSpec`` list for the WHOLE package
    (includes deferred legs).  ``overrides`` maps ``(currency, resource_key) ->
    expected post-fill availability`` for deferred-coverage combos; it is
    refused unless at least one leg is ``coverage_deferred``.

    Every leg (RISK-INCREASING OR RISK-DECREASING) MUST have a matching,
    non-zero incremental item: an OPT BUY-to-close still consumes short-close
    capacity + premium cash, a STK SELL still commits shares.  A missing /
    under-sized / empty package fails closed (never trusting the callback).
    """

    resources: Mapping[Any, Any] = field(default_factory=dict)
    items: Sequence[RiskReservationSpec] = ()
    overrides: Mapping[Any, Any] = field(default_factory=dict)


def handoff_decimal(value: Any, label: str) -> Optional[Decimal]:
    """Strict finite non-negative Decimal for a handoff numeric field.
    Bools / floats / NaN / non-Decimal strings are refused (fail closed).
    """
    if value is None or isinstance(value, bool) or isinstance(value, float):
        return None
    if not isinstance(value, (Decimal, int, str)):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not result.is_finite() or result < 0:
        return None
    return result


def check_observed_scope(snapshot: Any, account: str, environment: str,
                         label: str) -> Snapshot:
    """STRICTLY validate an observed snapshot's account/env + data quality.

    EVERY refreshed snapshot (initial, deferred, and any handoff/resolve
    evidence) must pass this check: an OTHER/live snapshot must never authorise
    a paper release, and a low-quality snapshot must never pass.
    """
    if not isinstance(snapshot, Snapshot):
        raise ExecutionError(f"{label} must be a fresh Snapshot")
    if snapshot.account != account or snapshot.environment != environment:
        raise ExecutionError(
            f"{label} account/environment mismatch "
            f"({snapshot.account}/{snapshot.environment} != "
            f"{account}/{environment})")
    if snapshot.issues:
        raise ExecutionError(
            f"{label} has unresolved data-quality issues: "
            + "; ".join(snapshot.issues))
    return snapshot


def approved_leg_canonical(ledger: Any, intent_id: str,
                           leg_index: int) -> Mapping[str, Any]:
    """Read the APPROVED canonical leg terms from the stored intent payload.

    The approved terms are the AUTHORITATIVE comparison target for any positive
    evidence — never the caller's freshly-constructed objects.  A missing /
    malformed / out-of-range stored leg fails closed.
    """
    row = ledger.get_intent(intent_id)
    if row is None:
        raise ExecutionError("handoff: approved intent is missing")
    payload = json.loads(row["payload"])
    legs = payload.get("legs")
    if not isinstance(legs, list) or not (0 <= leg_index < len(legs)):
        raise ExecutionError("handoff: approved leg is missing from the intent")
    canonical = legs[leg_index]
    if not isinstance(canonical, Mapping):
        raise ExecutionError("handoff: approved leg canonical is malformed")
    return canonical


def _compare_canonical_instrument(inst: Mapping[str, Any],
                                  observed: Mapping[str, Any],
                                  label: str) -> None:
    """FULL canonical Instrument comparison (no partial-identity shortcut)."""
    if str(inst.get("con_id")) != str(observed["con_id"]):
        raise ExecutionError(
            f"{label} conId does not match the approved canonical instrument")
    if str(inst.get("underlying_id")) != str(observed["underlying_id"]):
        raise ExecutionError(
            f"{label} underlying_id does not match the approved canonical instrument")
    for name in ("symbol", "sec_type", "currency", "exchange", "right"):
        if str(inst.get(name, "")).upper() != str(observed[name]).upper():
            raise ExecutionError(
                f"{label} {name} does not match the approved canonical instrument")
    approved_mult = handoff_decimal(inst.get("multiplier", "1"),
                                    "approved multiplier")
    observed_mult = handoff_decimal(observed["multiplier"], "observed multiplier")
    if approved_mult != observed_mult:
        raise ExecutionError(
            f"{label} multiplier does not match the approved canonical instrument")
    approved_strike = handoff_decimal(inst.get("strike", "0"), "approved strike")
    observed_strike = handoff_decimal(observed["strike"], "observed strike")
    if approved_strike != observed_strike:
        raise ExecutionError(
            f"{label} strike does not match the approved canonical instrument")
    for name in ("expiry", "trading_class"):
        if str(inst.get(name, "")) != str(observed[name]):
            raise ExecutionError(
                f"{label} {name} does not match the approved canonical instrument")


def verify_observed_working_order(*, order_ref: str, account: str,
                                  environment: str, leg: state.LegView,
                                  approved: Mapping[str, Any],
                                  snapshot: Any, now: datetime,
                                  max_snapshot_age_seconds: Optional[int] = None,
                                  con_id: Optional[int] = None,
                                  perm_id: Any = None,
                                  order_id: Any = None) -> WorkingOrder:
    """MECHANICALLY map ONE stable orderRef to EXACTLY ONE real working order.

    Shared by :meth:`ExecutionEngine.reconcile_positive` and the operator
    ``resolve`` entry point so there is ONE identity implementation.  Every
    check here is a refusal: the caller never gets a "best guess" order.
    """
    snap = check_observed_scope(snapshot, account, environment,
                                "handoff observed snapshot")
    # (1) FRESH relative to the IN-LOCK clock, not merely "newer than the
    #     attempt": a day-old capture cannot prove a live working obligation.
    if snap.captured_at.tzinfo is None or snap.captured_at.utcoffset() is None:
        raise ExecutionError(
            "handoff observed snapshot captured_at must be an aware datetime")
    age = (now - snap.captured_at).total_seconds()
    cap = max_snapshot_age_seconds
    max_age = _HANDOFF_MAX_SNAPSHOT_AGE_SECONDS if cap is None \
        else min(_HANDOFF_MAX_SNAPSHOT_AGE_SECONDS, cap)
    if age < -_HANDOFF_MAX_FUTURE_SECONDS:
        raise ExecutionError(
            "handoff observed snapshot is in the future (clock skew); "
            "refusing to release risk")
    if age > max_age:
        raise ExecutionError(
            f"handoff observed snapshot is not fresh ({age:.0f}s old > "
            f"{max_age}s); a stale snapshot cannot prove the specific "
            "order is a current working obligation")
    # The observed snapshot must ALSO be captured AFTER the attempt.
    attempt_iso = leg.submitted_at
    if attempt_iso:
        attempt_dt = datetime.fromisoformat(attempt_iso.replace("Z", "+00:00"))
        if snap.captured_at <= attempt_dt:
            raise ExecutionError(
                "handoff observed snapshot is not newer than the attempt")
    # (4) MECHANICALLY map the SAME ref to EXACTLY ONE working order.
    #     Duplicate rows with the same ref are an AMBIGUOUS mapping: never
    #     pick the first and release a whole leg.
    matched = [o for o in snap.orders if o.order_ref == order_ref]
    if not matched:
        raise ExecutionError(
            "handoff observed snapshot has no working order matching "
            "the stable orderRef; refusing to release risk")
    if len(matched) != 1:
        raise ExecutionError(
            "handoff observed snapshot has multiple working orders with "
            "the same stable orderRef; refusing an ambiguous/non-unique "
            "mapping")
    order = matched[0]
    oid = None if order_id is None else str(order_id)
    pid = None if perm_id is None else str(perm_id)
    if oid is not None and str(order.order_id) != oid:
        raise ExecutionError("handoff working order orderId mismatch")
    if pid is not None and str(order.perm_id) != pid:
        raise ExecutionError("handoff working order permId mismatch")
    if leg.order_id not in (None, "") and str(order.order_id) != leg.order_id:
        raise ExecutionError("handoff working order contradicts bound orderId")
    if leg.perm_id not in (None, "") and str(order.perm_id) != leg.perm_id:
        raise ExecutionError("handoff working order contradicts bound permId")
    if con_id is not None and con_id != order.instrument.con_id:
        raise ExecutionError("handoff conId does not match the working order")
    if str(order.instrument.con_id) != leg.contract_id:
        raise ExecutionError(
            "handoff working order conId does not match the approved leg")
    if order.action.upper() != leg.action.upper():
        raise ExecutionError(
            "handoff working order action does not match the approved leg")
    if not isinstance(order.instrument, Instrument):
        raise ExecutionError("handoff working order instrument is invalid")
    # (2)(3) FULL economic identity against the APPROVED stored leg, never
    #       against caller-supplied objects.  A different limit price / order
    #       type, or a partial/filled remaining, cannot release the whole
    #       reserved obligation.
    _compare_canonical_instrument(approved.get("instrument") or {},
                                  canonical_instrument(order.instrument),
                                  "handoff working order")
    if str(approved.get("action", "")).upper() != order.action.upper():
        raise ExecutionError(
            "handoff working order action does not match the approved canonical leg")
    approved_order_type = str(approved.get("order_type", "")).upper()
    observed_order_type = str(order.order_type or "").upper()
    # A bounded LMT order is REQUIRED: a MKT / unbounded working order cannot
    # be reconciled to a reserved, bounded obligation.
    if approved_order_type != "LMT" or observed_order_type != "LMT":
        raise ExecutionError(
            "handoff requires a bounded LMT working order; refusing "
            "an unbounded/order-type mismatch")
    approved_limit = handoff_decimal(approved.get("limit_price"),
                                     "approved limit price")
    observed_limit = handoff_decimal(order.limit_price,
                                     "working order limit price")
    if approved_limit is None or observed_limit is None:
        raise ExecutionError(
            "handoff requires a bounded limit price on both the "
            "approved leg and the working order")
    if approved_limit != observed_limit:
        raise ExecutionError(
            "handoff working order limit price does not match the "
            "approved leg (economic identity mismatch)")
    approved_qty = handoff_decimal(approved.get("quantity"), "approved quantity")
    remaining = handoff_decimal(order.remaining, "working remaining")
    if approved_qty is None or remaining is None:
        raise ExecutionError(
            "handoff has a non-finite/negative quantity; refusing")
    if remaining <= 0:
        raise ExecutionError(
            "handoff working order has no remaining quantity; cannot "
            "positively map an outstanding obligation")
    # remaining MUST be the EXACT approved full quantity: a partial fill (or
    # any over/under remaining) is conservatively UNSUPPORTED.
    if remaining != approved_qty:
        raise ExecutionError(
            "handoff working order remaining does not EXACTLY equal "
            "the approved full quantity; partial/filled handoff is "
            "unsupported (no fill/position bridge evidence)")
    return order


_FILL_SIDE = {"BOT": "BUY", "BUY": "BUY", "SLD": "SELL", "SELL": "SELL"}


def verify_fill_against_approved(*, fill: Any, leg: state.LegView,
                                 approved: Mapping[str, Any],
                                 account: str, environment: str) -> None:
    """Prove ONE execution row belongs to the APPROVED leg (fill bridge).

    A filled order has no working order, so :func:`verify_observed_working_order`
    can never release it — and "it is gone from open orders" is not proof of
    anything.  The only sound bridge is the broker's own EXECUTION record naming
    the SAME stable ``orderRef``.  Every field is re-verified here against the
    approved stored leg; a partial fill (shares != approved quantity) refuses,
    because it cannot discharge the whole reserved obligation.
    """
    if not isinstance(fill, PositiveFill):
        raise ExecutionError(
            "fill evidence must be a structured PositiveFill (a bare status "
            "string or a position delta is not proof)")
    if fill.account != account:
        raise ExecutionError(
            "fill evidence account does not match the reservation scope")
    if fill.order_ref != leg.order_ref:
        raise ExecutionError("fill evidence orderRef does not match the approved leg")
    if not isinstance(fill.exec_id, str) or not fill.exec_id.strip():
        raise ExecutionError("fill evidence has no execution id")
    if type(fill.con_id) is not int or fill.con_id != int(leg.contract_id):
        raise ExecutionError("fill conId does not match the approved leg conId")
    observed_inst = approved.get("instrument") or {}
    if str(observed_inst.get("con_id")) != str(fill.con_id):
        raise ExecutionError(
            "fill conId does not match the approved canonical instrument")
    side = _FILL_SIDE.get(str(fill.side).upper()) if isinstance(fill.side, str) else None
    if side is None:
        raise ExecutionError("fill side is not a recognised BUY/SELL")
    if side != str(leg.action).upper() or side != str(approved.get("action", "")).upper():
        raise ExecutionError("fill side does not match the approved leg action")
    qty = fill.shares
    if isinstance(qty, bool) or isinstance(qty, float) or not isinstance(qty, Decimal) \
            or not qty.is_finite() or qty <= 0:
        raise ExecutionError(
            "fill shares must be a positive finite Decimal")
    cum = fill.cum_qty
    if isinstance(cum, bool) or isinstance(cum, float) or not isinstance(cum, Decimal) \
            or not cum.is_finite() or cum <= 0:
        raise ExecutionError("fill cumulative quantity must be a positive finite Decimal")
    approved_qty = handoff_decimal(approved.get("quantity"), "approved quantity")
    if approved_qty is None:
        raise ExecutionError("approved leg has no usable quantity")
    if qty != approved_qty or cum != qty:
        raise ExecutionError(
            "fill quantity does not EXACTLY equal the approved full quantity; "
            "a partial fill cannot release the whole reserved obligation")
    price = fill.price
    if isinstance(price, bool) or isinstance(price, float) or not isinstance(price, Decimal) \
            or not price.is_finite() or price <= 0:
        raise ExecutionError("fill price must be a positive finite Decimal")
    if not isinstance(fill.as_of, datetime) or fill.as_of.tzinfo is None \
            or fill.as_of.utcoffset() is None:
        raise ExecutionError("fill execution time must be an aware datetime")
    if leg.submitted_at:
        attempt_dt = datetime.fromisoformat(leg.submitted_at.replace("Z", "+00:00"))
        # IB execution timestamps have 1-second resolution (truncating subseconds),
        # so an immediate fill in the same second as a subsecond attempt_dt has
        # fill.as_of == attempt_dt.replace(microsecond=0).
        if fill.as_of.microsecond == 0:
            attempt_dt = attempt_dt.replace(microsecond=0)
        if fill.as_of < attempt_dt:
            raise ExecutionError(
                "fill execution time predates this attempt; refusing to attribute it")


def canonical_instrument(inst: Instrument) -> Mapping[str, Any]:
    """Extract canonical mapping for an Instrument instance."""
    if not isinstance(inst, Instrument):
        raise ExecutionError("expected an Instrument instance")
    return {
        "con_id": inst.con_id,
        "symbol": inst.symbol,
        "currency": inst.currency,
        "sec_type": inst.sec_type,
        "exchange": inst.exchange,
        "underlying_id": inst.underlying_id,
        "multiplier": str(inst.multiplier),
        "right": inst.right,
        "strike": str(inst.strike),
        "expiry": inst.expiry,
        "trading_class": inst.trading_class,
    }


@dataclass(frozen=True)
class PreparedLeg:
    """Canonical description of ONE order leg.

    ``contract`` is the opaque broker contract object handed to the injected
    ``order_builder`` / ``place_order`` callable; this module never introspects
    it.  ``instrument`` is the confirmed identity used by the pure risk layer.
    All money/quantity fields are Decimal (never float).  ``canonical`` holds
    any extra fields that must be bound into the intent digest (e.g.
    multiplier, strike, expiry, trading class).

    ``deliverable_verified`` MUST be True before preflight.  The engine can NOT
    infer verification from multiplier / underConId / tradingClass: the CLI /
    adapter must load explicit delivery evidence (e.g.
    ``roll_cli.load_delivery_evidence``) and set this flag ONLY for an STK or
    an OPT whose conId is in that evidence set.  A missing/unknown deliverable
    MUST block, never fail open.

    ``coverage_deferred`` marks a leg whose collateral (e.g. shares covering a
    short call) will only exist AFTER a prior leg fills.  Such a leg requires
    ``requires_filled_leg`` (index of the stock leg) and a ``resource_plan``
    carrying ``overrides``; it is never sent without a positive Filled proof
    (exact full quantity) and a fresh passing risk assessment.
    """

    instrument: Instrument
    action: str
    quantity: Decimal
    order_type: str
    contract: Any = None
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    tif: str = "DAY"
    exchange: str = ""
    outside_rth: bool = False
    strategy: str = ""
    account: str = ""
    canonical: Mapping[str, Any] = field(default_factory=dict)
    deliverable_verified: bool = False
    coverage_deferred: bool = False
    requires_filled_leg: Optional[int] = None

    def to_canonical(self) -> Mapping[str, Any]:
        """Full canonical field set bound into the intent digest."""
        return {
            "instrument": dict(canonical_instrument(self.instrument)),
            "action": self.action.upper(),
            "quantity": str(self.quantity),
            "order_type": self.order_type.upper(),
            "limit_price": None if self.limit_price is None else str(self.limit_price),
            "stop_price": None if self.stop_price is None else str(self.stop_price),
            "tif": self.tif,
            "exchange": self.exchange,
            "outside_rth": bool(self.outside_rth),
            "strategy": self.strategy,
            "account": self.account,
            "deliverable_verified": bool(self.deliverable_verified),
            "coverage_deferred": bool(self.coverage_deferred),
            "requires_filled_leg": self.requires_filled_leg,
            "extra": dict(self.canonical),
        }

    def to_order_request(self) -> OrderRequest:
        return OrderRequest(
            instrument=self.instrument, action=self.action,
            quantity=self.quantity, order_type=self.order_type,
            limit_price=self.limit_price, tif=self.tif)

    def increases_risk(self) -> bool:
        return self.to_order_request().increases_risk


@dataclass(frozen=True)
class BrokerReceipt:
    """Result of ONE ``place_order`` call at the network boundary.

    ``status`` is a POSITIVE *callback* status (e.g. a local
    ``PendingSubmit``/``Submitted`` object); it is NOT proof of a positively
    observed broker snapshot and NEVER triggers ``confirm_leg``.  ``None``
    means unknown (may have been sent).  ``filled_quantity`` is the EXACT
    filled quantity for a genuine ``Filled`` proof (used only by the caller's
    ``fill_proof``; the engine never infers a fill from this receipt).
    """

    order_id: Optional[str] = None
    perm_id: Optional[str] = None
    status: Optional[str] = None
    filled_quantity: Optional[Decimal] = None


@dataclass(frozen=True)
class Preflight:
    intent_id: str
    approval_code: str
    full_digest: str
    config_digest: str
    expires_at: str
    account: str
    environment: str
    day_key: str
    leg_count: int


@dataclass(frozen=True)
class LegOutcome:
    order_ref: str
    leg_index: int
    state: str
    order_id: Optional[str] = None
    perm_id: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class ConfirmResult:
    intent_id: str
    reservation_id: str
    day_key: str
    decision: str
    reasons: tuple[str, ...]
    legs: tuple[LegOutcome, ...]
    frozen: bool = False
    explanation: str = ""
    verification: Mapping[str, Any] = field(default_factory=dict)


def _dec_amount(value: Any, label: str) -> Decimal:
    """Validate a POSITIVE incremental amount (must be > 0)."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ExecutionError(f"{label}: use a Decimal/int/str, not float/bool")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ExecutionError(f"{label}: invalid decimal {value!r}") from exc
    if not result.is_finite() or result <= 0:
        raise ExecutionError(f"{label}: must be finite and positive")
    return result


def _nonneg_amount(value: Any, label: str) -> Decimal:
    """Validate an AVAILABLE amount (may legitimately be zero, e.g. no shares)."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ExecutionError(f"{label}: use a Decimal/int/str, not float/bool")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ExecutionError(f"{label}: invalid decimal {value!r}") from exc
    if not result.is_finite() or result < 0:
        raise ExecutionError(f"{label}: must be finite and non-negative")
    return result


def _validate_legs(legs: Sequence[PreparedLeg], account: str) -> None:
    if not isinstance(legs, Sequence) or isinstance(legs, (str, bytes)) or not legs:
        raise ExecutionError("at least one prepared leg is required")
    for index, leg in enumerate(legs):
        if not isinstance(leg, PreparedLeg):
            raise ExecutionError(f"leg {index} must be a PreparedLeg")
        _dec_amount(leg.quantity, f"leg {index} quantity")
        if leg.action.upper() not in {"BUY", "SELL"}:
            raise ExecutionError(f"leg {index}: action must be BUY/SELL")
        if not leg.deliverable_verified:
            raise UnsafeComboError(
                f"leg {index}: deliverable structure not verified; refusing "
                "unsupported/adjusted contract. The CLI MUST load explicit "
                "delivery evidence and set deliverable_verified=True ONLY for a "
                "qualified STK/OPT — the engine never infers it from "
                "multiplier/underConId/tradingClass")
        if leg.account and leg.account != account:
            raise ExecutionError(f"leg {index}: account mismatch")
        if leg.coverage_deferred:
            if leg.requires_filled_leg is None \
                    or not isinstance(leg.requires_filled_leg, int) \
                    or leg.requires_filled_leg < 0 \
                    or leg.requires_filled_leg >= index:
                raise UnsafeComboError(
                    f"leg {index}: coverage_deferred requires a valid earlier "
                    "requires_filled_leg index")
        elif leg.requires_filled_leg is not None:
            raise UnsafeComboError(
                f"leg {index}: requires_filled_leg set without coverage_deferred")


def aggregate_reservations(ledger: SubmissionLedger, account: str,
                           environment: str) -> Mapping[str, Any]:
    """Public: translate outstanding ledger reservations into risk.py's 4 buckets.

    Keys follow the coordinator's convention: ``cash_base``,
    ``shares:<underlying_id>:<currency>``, ``close:<conId>``,
    ``exposure:<underlying_id>:<currency>``.  An unrecognised key or malformed
    decimal FAILS CLOSED rather than being silently ignored (which would free
    risk budget).  This is the single mapping the CLI report should pass to
    ``risk.summarize``; it makes NO release / de-dupe assumptions — a still-
    outstanding reservation is reported as incomplete, never zeroed.
    """
    out: dict[str, Any] = {"cash_base": Decimal(0), "shares": {},
                           "close": {}, "exposure": {}}
    for view in ledger.outstanding_reservations(account=account,
                                                environment=environment):
        for currency, key, amount in view.items:
            try:
                value = Decimal(amount)
            except (InvalidOperation, ValueError, TypeError) as exc:
                raise ExecutionError(
                    f"outstanding reservation {key!r}: invalid amount {amount!r}") from exc
            if not value.is_finite() or value < 0:
                raise ExecutionError(
                    f"outstanding reservation {key!r}: invalid amount {amount!r}")
            if key == "cash_base":
                out["cash_base"] += value
            elif key.startswith("shares:"):
                parts = key.split(":", 2)
                if len(parts) != 3 or not parts[1].strip() or not parts[2].strip():
                    raise ExecutionError(f"unrecognised shares resource key {key!r}")
                underlying_key = f"{parts[1]}:{parts[2]}"
                out["shares"][underlying_key] = (
                    out["shares"].get(underlying_key, Decimal(0)) + value)
            elif key.startswith("close:"):
                parts = key.split(":", 1)
                try:
                    con_id = int(parts[1])
                except (ValueError, IndexError) as exc:
                    raise ExecutionError(
                        f"unrecognised close resource key {key!r}") from exc
                out["close"][con_id] = out["close"].get(con_id, Decimal(0)) + value
            elif key.startswith("exposure:"):
                parts = key.split(":", 2)
                if len(parts) != 3 or not parts[1].strip() or not parts[2].strip():
                    raise ExecutionError(f"unrecognised exposure resource key {key!r}")
                underlying_key = f"{parts[1]}:{parts[2]}"
                out["exposure"][underlying_key] = (
                    out["exposure"].get(underlying_key, Decimal(0)) + value)
            else:
                raise ExecutionError(f"unrecognised risk resource key {key!r}")
    return out


# Backward-compatible private alias used inside this module.
_reservation_aggregate = aggregate_reservations


def _coerce_resource_plan(value: Any) -> ResourcePlan:
    """Accept a :class:`ResourcePlan` OR a plain mapping with the three keys.

    The CLI adapter computes the budget as a dict (``items`` / ``resources`` /
    ``overrides``); normalise both shapes so the engine never trusts a static
    constructor value and always validates the fresh-snapshot plan.
    """
    if isinstance(value, ResourcePlan):
        return value
    if isinstance(value, Mapping):
        missing = {"items", "resources", "overrides"} - set(value)
        if missing:
            raise ExecutionError(
                f"resource_plan mapping missing keys: {sorted(missing)}")
        return ResourcePlan(resources=value["resources"], items=value["items"],
                            overrides=value["overrides"])
    raise ExecutionError(
        "resource_plan must return a ResourcePlan or a mapping with "
        "items/resources/overrides")


def _independent_requirements(legs: Sequence[PreparedLeg],
                              snapshot: Snapshot) -> dict[Any, Decimal]:
    """INDEPENDENTLY derive EVERY leg's minimal resource increments.

    This is the engine's OWN calculation — it NEVER trusts the ``resource_plan``
    callback and NEVER trusts ``PreparedLeg.increases_risk()`` to decide which
    legs need reserving.  EVERY leg of the package MUST have its obligation
    derived, including RISK-DECREASING legs (an OPT BUY-to-close still consumes
    short-close capacity + premium cash, and a STK SELL still commits shares).
    Skipping those lets an empty package send two buybacks against ONE short and
    create an unintended long.  Keys are ``(currency, resource_key)`` matching
    :class:`~thetamill.state.RiskReservationSpec`.  A leg whose obligation is
    unbounded / unverifiable (e.g. MKT buy, OPT BUY without a bounded LMT price,
    or an unrecognised instrument) yields a sentinel that always fails
    validation (the package cannot prove it covered such a leg).

    ``cash_base`` and ``exposure`` amounts are MONEY in the BASE currency (all
    money pools share one pool and risk.py's exposure map is BASE-denominated),
    so prices / premiums / strike collateral are converted by the snapshot's FX
    rate for the trade currency.  ``cash_base`` uses the ``base`` ledger currency
    namespace; ``exposure`` (like ``shares``) uses ``trade_cur`` as its namespace
    to match risk.summarize's ``exposure_by_underlying`` / the adapter's item
    keys.  ``shares`` / ``close`` are DIMENSIONLESS counts (never FX-converted):
    shares use ``trade_cur``, close uses ``base`` (matching the adapter).
    """
    base = snapshot.base_currency.upper()
    reqs: dict[Any, Decimal] = {}

    def add(key: Any, amount: Decimal) -> None:
        if amount <= 0:
            return
        reqs[key] = reqs.get(key, Decimal(0)) + amount

    def unverifiable(leg: PreparedLeg) -> None:
        # Sentinel key NO resource_plan can satisfy -> _validate_plan refuses.
        add(("__unverifiable__", str(leg.instrument.con_id)), Decimal(1))

    def fx_rate(currency: str) -> Optional[Decimal]:
        cur = currency.upper()
        if cur == base:
            return Decimal(1)
        value = snapshot.fx.get(cur)
        if value is None:
            return None
        try:
            rate = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None
        return rate if rate.is_finite() and rate > 0 else None

    for leg in legs:
        inst = leg.instrument
        qty = leg.quantity
        action = leg.action.upper()
        order_type = leg.order_type.upper()
        trade_cur = inst.currency.upper()
        rate = fx_rate(trade_cur)
        mult = inst.multiplier
        if mult is None:
            mult_dec = None
        else:
            try:
                mult_dec = Decimal(str(mult))
                if not mult_dec.is_finite() or mult_dec <= 0:
                    mult_dec = None
            except (InvalidOperation, ValueError, TypeError):
                mult_dec = None

        if inst.sec_type == "STK" and action == "BUY":
            # A stock BUY spends cash AND creates potential holding exposure,
            # both bounded ONLY by a LMT limit price.  A MKT / missing-price buy
            # is unbounded and cannot be reserved (fail closed).
            if order_type != "LMT" or leg.limit_price is None or rate is None:
                unverifiable(leg)
                continue
            notional_base = qty * leg.limit_price * rate
            add((base, "cash_base"), notional_base)
            add((trade_cur, f"exposure:{inst.underlying_id}:{trade_cur}"),
                notional_base)
        elif inst.sec_type == "STK" and action == "SELL":
            # A stock SELL commits shares (reduces the cover available to short
            # calls / pending sells).  A dimensionless share-count obligation.
            add((trade_cur, f"shares:{inst.underlying_id}:{trade_cur}"), qty)
        elif inst.sec_type == "OPT" and action == "SELL":
            if mult_dec is None:
                unverifiable(leg)
                continue
            if inst.right.upper() == "P":
                # Cash-secured PUT collateral = strike*mult*qty in BASE, plus
                # potential holding exposure of the same BASE notional.
                if rate is None:
                    unverifiable(leg)  # cash obligation cannot be bounded
                else:
                    collateral_base = qty * inst.strike * mult_dec * rate
                    add((base, "cash_base"), collateral_base)
                    add((trade_cur,
                         f"exposure:{inst.underlying_id}:{trade_cur}"),
                        collateral_base)
            elif inst.right.upper() == "C":
                # A covered CALL consumes ONLY `mult*qty` shares as cover.  It
                # does NOT consume strike cash/exposure (that would double-charge
                # a covered call): shares are the binding resource.
                add((trade_cur,
                     f"shares:{inst.underlying_id}:{trade_cur}"), qty * mult_dec)
            else:
                unverifiable(leg)  # unrecognised option right
        elif inst.sec_type == "OPT" and action == "BUY":
            # BUY-to-close: consumes SHORT-CLOSE capacity for this exact conId
            # and spends a bounded premium.  Both are MANDATORY even though
            # increases_risk() is False, otherwise multiple closes of one short
            # slip through with an empty package.  Only a LMT with a real limit
            # price is bounded; MKT / missing price is unverifiable.
            if order_type != "LMT" or leg.limit_price is None or rate is None or mult_dec is None:
                unverifiable(leg)
                continue
            add((base, f"close:{inst.con_id}"), qty)
            # Premium uses the option's OWN multiplier (100/200/500 fidelity).
            add((base, "cash_base"), qty * mult_dec * leg.limit_price * rate)
        else:
            # Unrecognised instrument / action: no verifiable bound.
            unverifiable(leg)
    return reqs


class ExecutionEngine:
    """Small, offline-testable submission protocol (no CLI / ib_async import).

    Parameters
    ----------
    ledger
        An initialised :class:`~thetamill.state.SubmissionLedger`.
    snapshot_provider
        ``Callable[[], Snapshot]`` that REFRESHES broker state on each call.
        Called at confirm and again before each deferred-coverage leg.
    broker
        Opaque broker-like object handed to ``place_order``.
    place_order
        ``Callable[[broker, PreparedLeg, order_ref], BrokerReceipt]`` — the ONLY
        network boundary.  A returned positive ``status`` is a LOCAL callback,
        NOT a broker snapshot; it does not release reserved risk.
    resource_plan
        ``Callable[[Snapshot, Sequence[PreparedLeg], Mapping], ResourcePlan]``.
        Called INSIDE the account lock on the FRESH snapshot; returns the
        authoritative available ``resources``, the WHOLE-PACKAGE incremental
        ``items`` and (for deferred combos) ``overrides``.  Static resource
        values are NOT trusted.  A missing/empty package for a risk-increasing
        leg fails closed.  The third argument is the validated outstanding-
        reservation aggregate (ready to pass to ``risk.summarize``).
    fill_proof
        ``Callable[[broker, order_ref, Mapping[str, str]], Optional[FillProof]``.
        MUST return a structured :class:`FillProof` (NOT a bare ``"Filled"``
        string) ONLY when the stable ``order_ref`` maps to a genuinely Filled
        order with a finite EXACT full-quantity match; ``None`` otherwise.  The
        engine INDEPENDENTLY verifies account/env/conId/stableRef/status/exact
        qty against the APPROVED dependency leg.  Required for deferred-
        coverage legs.  The engine passes the SAME ``order_ref`` used by
        ``begin_leg_attempt``/``bind_broker_ids`` plus any bound IDs as
        corroboration.
    config
        The whole canonical config mapping bound into every intent digest.
    limits
        Validated :class:`~thetamill.risk.RiskLimits` (``enabled=False`` for a
        legacy config still enforces baseline safety).
    lock_dir
        Directory holding the account-scoped ``fcntl`` lock files.
    verified_prior_submissions
        Optional operator-verified local submission count for the current
        configured natural day.  WITHOUT it, an enabled-P3 fresh ledger is
        conservatively FROZEN (cannot infer zero historic submissions); never
        fabricate a zero.  An explicit verified count unlocks; otherwise wait
        for the next natural day.
    ttl_seconds
        Intent lifetime (seconds, UTC).
    clock
        Injectable ``Callable[[], datetime]`` (aware, UTC) for deterministic
        expiry / day-key tests.
    """

    def __init__(self, *, ledger: SubmissionLedger,
                 snapshot_provider: Callable[[], Snapshot],
                 broker: Any,
                 place_order: Callable[[Any, PreparedLeg, str], BrokerReceipt],
                 resource_plan: Callable[[Snapshot, Sequence[PreparedLeg],
                                          Mapping[str, Any]], ResourcePlan],
                 fill_proof: Optional[Callable[[Any, str, Mapping[str, str]],
                                               Optional[str]]] = None,
                 config: Mapping[str, Any],
                 limits: RiskLimits,
                 lock_dir: Any,
                 verified_prior_submissions: Optional[int] = None,
                 ttl_seconds: int = 600,
                 clock: Optional[Callable[[], datetime]] = None) -> None:
        if not isinstance(ledger, SubmissionLedger):
            raise ExecutionError("ledger must be a SubmissionLedger")
        if not callable(snapshot_provider) or not callable(place_order) \
                or not callable(resource_plan):
            raise ExecutionError(
                "snapshot_provider, place_order and resource_plan must be callable")
        if not isinstance(config, Mapping):
            raise ExecutionError("config must be a mapping")
        if not isinstance(limits, RiskLimits):
            raise ExecutionError("limits must be a validated RiskLimits")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) \
                or ttl_seconds <= 0:
            raise ExecutionError("ttl_seconds must be a positive int")
        self.ledger = ledger
        self.snapshot_provider = snapshot_provider
        self.broker = broker
        self.place_order = place_order
        self.resource_plan = resource_plan
        self.fill_proof = fill_proof
        self.config = dict(config)
        self.limits = limits
        self.lock_dir = Path(lock_dir)
        self.verified_prior_submissions = verified_prior_submissions
        self.ttl_seconds = ttl_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ExecutionError("clock must return an aware datetime")
        return now

    def _day_key(self, snapshot: Snapshot, now: datetime) -> str:
        return state.day_key_for(self.limits.day_timezone, now=now)

    def _validate_deferred_combo(self, legs: Sequence[PreparedLeg]) -> None:
        """A deferred-coverage combo requires a fill_proof provider at BOTH
        preflight and confirm; otherwise the stock-first combo is unsafe."""
        if any(leg.coverage_deferred for leg in legs) and self.fill_proof is None:
            raise UnsafeComboError(
                "deferred-coverage combo requires a fill_proof provider that "
                "returns a structured FillProof only for an exact full-quantity "
                "Filled proof")

    def _call_resource_plan(self, snapshot: Snapshot,
                            legs: Sequence[PreparedLeg],
                            aggregate: Mapping[str, Any]) -> ResourcePlan:
        """Invoke the callback on the FRESH snapshot under the lock.

        Canonical contract: ``resource_plan(snapshot, legs, aggregate)``.
        For compatibility with an adapter written before this signature was
        finalised, callbacks that accept fewer positional arguments are still
        supported: a 2-arg ``(snapshot, reservations)`` callback gets the
        outstanding aggregate via the ``reservations`` keyword, and a 1-arg
        ``(snapshot,)`` callback is called with just the fresh snapshot.  The
        return value may be a :class:`ResourcePlan` or a plain mapping.
        """
        fn = self.resource_plan
        try:
            params = list(inspect.signature(fn).parameters.values())
            positional = [p for p in params if p.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD)]
            names = {p.name for p in params}
        except (ValueError, TypeError):
            positional, names = [], set()
        n = len(positional)
        if n >= 3:
            result = fn(snapshot, legs, aggregate)
        elif n == 2:
            result = (fn(snapshot, reservations=aggregate)
                      if "reservations" in names else fn(snapshot, legs))
        elif n == 1:
            result = fn(snapshot)
        else:
            result = fn()
        return _coerce_resource_plan(result)

    @staticmethod
    def _validate_plan(plan: ResourcePlan, legs: Sequence[PreparedLeg],
                       snapshot: Snapshot) -> None:
        """Validate the WHOLE package is neither MISSING nor UNDER-SIZED.

        The engine INDEPENDENTLY derives EVERY leg's minimal cash / shares /
        close / exposure requirements from the approved legs + fresh snapshot
        (it does NOT trust the callback and does NOT skip risk-DECREASING legs).
        Every derived requirement must be covered by the package's reserved
        ``items`` (per key, summed), with a non-zero amount.  A single positive
        item for a package of two overspending legs — or an empty package for a
        package of closes — therefore fails closed BEFORE any send.
        """
        if not isinstance(plan, ResourcePlan):
            raise ExecutionError("resource_plan must return a ResourcePlan")
        deferred = any(leg.coverage_deferred for leg in legs)
        if plan.overrides and not deferred:
            raise UnsafeComboError(
                "resource overrides are only allowed for a deferred-coverage "
                "combo (they represent post-fill availability)")
        if deferred and not plan.overrides:
            raise UnsafeComboError(
                "deferred-coverage combo requires override availability for the "
                "full package budget; refusing unsafe stock-first combo")
        if deferred and not all(isinstance(k, tuple) and len(k) == 2
                                for k in plan.overrides):
            raise ExecutionError("override keys must be (currency, key)")
        resources = dict(plan.resources)
        for key, value in resources.items():
            if not (isinstance(key, tuple) and len(key) == 2
                    and all(isinstance(k, str) and k.strip() for k in key)):
                raise ExecutionError("resource keys must be (currency, key)")
            _nonneg_amount(value, f"resource amount {key!r}")
        for key, value in plan.overrides.items():
            _nonneg_amount(value, f"override amount {key!r}")
        # Validate EVERY reserved item and SUM the whole package per key.
        seen: dict[Any, Decimal] = {}
        for item in plan.items:
            if not isinstance(item, RiskReservationSpec):
                raise ExecutionError("resource_plan items must be RiskReservationSpec")
            amount = _dec_amount(item.amount, f"item {item.resource_key!r}")
            key = (item.currency, item.resource_key)
            seen[key] = seen.get(key, Decimal(0)) + amount
            if key not in resources and key not in plan.overrides:
                raise ExecutionError(
                    f"reserved resource {item.currency}:{item.resource_key} "
                    "has no fresh-snapshot available amount")
        # INDEPENDENT requirement derivation, never trusting the callback.
        required = _independent_requirements(legs, snapshot)
        for key, need in required.items():
            if key[0] == "__unverifiable__":
                raise UnsafeComboError(
                    "a leg has no verifiable bounded resource requirement "
                    "(unbounded MKT price / missing FX / unrecognised "
                    "instrument); refusing (package cannot prove coverage)")
            provided = seen.get(key, Decimal(0))
            if provided < need:
                raise UnsafeComboError(
                    f"full-package reservation under-sized for {key[0]}:{key[1]}: "
                    f"derived requirement {need}, package reserved {provided}; "
                    "refusing (missing/under-sized increments allow overspend)")
        # An EMPTY package must never be accepted for ANY real leg: every leg
        # (increasing OR reducing) consumes at least one verifiable resource.
        if legs and not seen and not deferred:
            raise UnsafeComboError(
                "legs have no full-package incremental resource reservation; "
                "refusing (an empty package would allow unreserved risk)")
        return None

    def preflight(self, legs: Sequence[PreparedLeg], *, account: str,
                  environment: str) -> Preflight:
        """Persist a fresh UUID intent and return its review code.

        Read-only with respect to the broker: it validates the canonical legs,
        binds EVERY order field plus the config fingerprint into the intent
        payload, and persists it.  No snapshot refresh, no reservation, no
        ``place_order``.  Re-preflighting identical terms creates a NEW intent.
        """
        _validate_legs(legs, account)
        self._validate_deferred_combo(legs)
        now = self._now()
        is_candidate_reduction = bool(legs) and all(not leg.increases_risk() for leg in legs)
        payload = {
            "schema": 1,
            "kind": "candidate_risk_reduction" if is_candidate_reduction else "prepared_legs",
            "account": account,
            "environment": environment,
            "legs": [leg.to_canonical() for leg in legs],
            "ttl_seconds": self.ttl_seconds,
            "limits_enabled": self.limits.enabled,
        }
        created = self.ledger.create_intent(
            account=account, environment=environment, payload=payload,
            config=self.config, ttl_seconds=self.ttl_seconds, now=now)
        return Preflight(
            intent_id=created["intent_id"], approval_code=created["approval_code"],
            full_digest=created["full_digest"],
            config_digest=created["config_digest"],
            expires_at=created["expires_at"], account=account,
            environment=environment,
            day_key=state.day_key_for(self.limits.day_timezone, now=now),
            leg_count=len(legs))

    def _check_digest_and_expiry(self, intent_id: str, code: str,
                                 account: str, environment: str,
                                 now: datetime,
                                 legs: Sequence[PreparedLeg]) -> Mapping[str, Any]:
        """Re-verify canonical payload/config binding, approval code, expiry,
        AND that the PASSED legs EXACTLY equal the APPROVED stored payload.

        A preflight of qty=2 must NOT be confirmable with qty=100 (or any other
        drift in the canonical legs, account, environment, ttl or limits flag):
        the review was for a SPECIFIC package.  This FULL comparison happens
        BEFORE the claim / callback / send, under the account lock.
        """
        row = self.ledger.get_intent(intent_id)
        if row is None:
            raise ExecutionError("unknown intent_id (no legacy _code fallback)")
        if row["account"] != account or row["environment"] != environment:
            raise ExecutionError("intent account/environment mismatch")
        payload = json.loads(row["payload"])
        recomputed = state.compute_full_digest(
            intent_id=row["intent_id"], account=row["account"],
            environment=row["environment"], payload=payload,
            config=self.config)
        if recomputed != row["full_digest"]:
            raise ExecutionError(
                "canonical payload/config digest mismatch (config changed or "
                "intent tampered); review is no longer valid")
        # FULL comparison of the CURRENTLY-PASSED canonical package vs the
        # approved stored payload (NOT just the stored digest).  The legs,
        # account, environment and limits flag must be IDENTICAL.  TTL is a
        # lifetime property enforced by the intent's own expiry check below +
        # the full digest binding, not part of the approved order terms.
        approved = {
            "schema": payload.get("schema"),
            "kind": payload.get("kind"),
            "account": account,
            "environment": environment,
            "legs": [leg.to_canonical() for leg in legs],
            "limits_enabled": self.limits.enabled,
        }
        stored_terms = {
            "schema": payload.get("schema"),
            "kind": payload.get("kind"),
            "account": payload.get("account"),
            "environment": payload.get("environment"),
            "legs": payload.get("legs"),
            "limits_enabled": payload.get("limits_enabled"),
        }
        if approved != stored_terms:
            raise ExecutionError(
                "confirmed legs/config/account/environment do NOT match the "
                "approved preflight payload (re-run preflight for the current "
                "package); refusing before any claim or send")
        scope = f"{account}\x1f{environment}"
        if state.approval_code(row["full_digest"], scope) != code \
                or row["approval_code"] != code:
            raise ExecutionError("approval code is not bound to this intent digest")
        now_iso = now.astimezone(timezone.utc).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        if row["expires_at"] <= now_iso:
            raise ExecutionError("intent has expired; re-run preflight")
        return payload

    def _assess(self, snapshot: Snapshot, leg: PreparedLeg,
                reservations: Mapping[str, Any], now: datetime) -> Decision:
        decision = risk.assess(snapshot, leg.to_order_request(), self.limits,
                               reservations, now=now)
        if not decision.allowed:
            raise ExecutionError(
                f"risk BLOCK for leg {leg.instrument.con_id}: "
                + "; ".join(decision.reasons))
        return decision

    def _verify_net_reduction(self, legs: Sequence[PreparedLeg],
                              snapshot: Snapshot,
                              reservations: Mapping[str, Any]) -> tuple[bool, str]:
        """Independently verify that a package achieves a genuine net risk reduction.

        Requirements:
        1. Non-empty package and EVERY leg must have increases_risk() == False
           (no STK BUY, no OPT SELL, no unrecognised instruments).
        2. For OPT BUY legs:
           - Must be LMT with bounded positive limit price, valid FX rate and valid multiplier.
           - Exact conId short-close capacity: must correspond to confirmed short positions
             in the snapshot, taking into account other reservations and pending buybacks.
             Total buyback quantity for that conId must not exceed available short close capacity.
             (Buying more than the short position creates a long position, which increases risk).
           - Premium cash: total required premium across all buybacks must be covered by free cash.
        3. For STK SELL legs:
           - Must sell shares actually owned in snapshot, taking into account reserved cover
             and pending sells. Selling more than owned shares is short selling (increases risk).
        4. Multiple legs cannot reuse capacity (totals are checked cumulatively per conId / underlying).
        """
        if not legs:
            return False, "package has no legs"
        if any(leg.increases_risk() for leg in legs):
            return False, "package contains risk-increasing leg(s)"

        summary = risk.summarize(snapshot, reservations=reservations, limits=self.limits)
        base = snapshot.base_currency.upper()

        close_needed: dict[int, Decimal] = {}
        total_premium_base = Decimal(0)
        shares_needed: dict[str, Decimal] = {}

        for index, leg in enumerate(legs):
            inst = leg.instrument
            qty = leg.quantity
            action = leg.action.upper()
            trade_cur = inst.currency.upper()

            if inst.sec_type == "OPT" and action == "BUY":
                if leg.order_type.upper() != "LMT" or leg.limit_price is None or leg.limit_price <= 0:
                    return False, f"leg {index}: option BUY requires a bounded positive limit price"
                try:
                    mult_dec = Decimal(str(inst.multiplier))
                except (InvalidOperation, ValueError, TypeError):
                    return False, f"leg {index}: invalid multiplier"
                if not mult_dec.is_finite() or mult_dec <= 0:
                    return False, f"leg {index}: non-positive multiplier"
                rate = Decimal(1) if trade_cur == base else snapshot.fx.get(trade_cur)
                if rate is None or rate <= 0:
                    return False, f"leg {index}: missing/invalid FX rate for {trade_cur}"
                rate_dec = Decimal(str(rate))

                cid = inst.con_id
                close_needed[cid] = close_needed.get(cid, Decimal(0)) + qty
                total_premium_base += qty * mult_dec * leg.limit_price * rate_dec

            elif inst.sec_type == "STK" and action == "SELL":
                uk = f"{inst.underlying_id}:{trade_cur}"
                shares_needed[uk] = shares_needed.get(uk, Decimal(0)) + qty

            else:
                return False, f"leg {index}: unrecognised or non-risk-reducing leg {inst.sec_type} {action}"

        # Verify close capacity
        for cid, needed in close_needed.items():
            avail = Decimal(str(summary.get("short_close_capacity", {}).get(str(cid), "0")))
            if needed > avail:
                return False, (f"option BUY {needed} exceeds confirmed short close capacity {avail} "
                               f"for conId {cid} (cannot create long option)")

        # Verify free cash for premiums directly using shared summary metrics
        if total_premium_base > Decimal(0):
            if not summary.get("cash_confirmed", False):
                return False, "cash balance not confirmed in broker snapshot"
            free_cash = Decimal(str(summary.get("free_cash_base", "0")))
            if free_cash < total_premium_base:
                return False, f"option BUY premium {total_premium_base} BASE exceeds free cash {free_cash} BASE"

        # Verify shares availability
        for uk, needed in shares_needed.items():
            avail_shares = Decimal(str(summary.get("shares", {}).get(uk, "0")))
            if needed > avail_shares:
                return False, (f"stock SELL {needed} exceeds available shares {avail_shares} "
                               f"for {uk} (cannot short sell)")

        return True, ""

    def _prepare_budget(self, *, account: str, environment: str,
                        day_key: str) -> tuple[bool, str]:
        """Conservative first-day migration handling.  Returns (frozen, why).

        Disabled P3 uses a generous INTERNAL cap but NEVER clobbers an already
        established day row and NEVER resets a frozen/enabled day: an existing
        row is left untouched (additive only).
        """
        if self.limits.enabled:
            try:
                self.ledger.prepare_day_budget(
                    account=account, environment=environment, day_key=day_key,
                    limit_count=self.limits.max_daily_submissions,
                    verified_prior_submissions=self.verified_prior_submissions)
            except state.MigrationFreeze as exc:
                return True, str(exc)
        else:
            existing = self.ledger.get_day_budget(
                account=account, environment=environment, day_key=day_key)
            if not existing["exists"]:
                self.ledger.set_daily_limit(
                    account=account, environment=environment, day_key=day_key,
                    limit_count=_DISABLED_INTERNAL_BUDGET)
        return False, ""

    def _observed_scope(self, snapshot: Any, account: str,
                        environment: str, label: str) -> Snapshot:
        """STRICTLY validate a refreshed snapshot's account/env + data quality.

        EVERY refreshed snapshot (initial, deferred, and any handoff evidence)
        must pass this check: an OTHER/live snapshot must never authorise a
        paper submission, and a low-quality snapshot must never pass.
        """
        return check_observed_scope(snapshot, account, environment, label)

    def _leg_increments(self, legs: Sequence[PreparedLeg],
                        snapshot: Snapshot) -> list[Mapping[str, Any]]:
        """Per-leg incremental reservations derived INDEPENDENTLY by the engine.

        Used so each leg's assessment sees the existing outstanding
        reservations + the OTHER legs' increments ONLY (never its own, which
        would double-count).  A leg with no derivable increments contributes an
        empty mapping.
        """
        result: list[Mapping[str, Any]] = []
        for leg in legs:
            reqs = _independent_requirements([leg], snapshot)
            out: dict[str, Any] = {"cash_base": Decimal(0), "shares": {},
                                   "close": {}, "exposure": {}}
            for (currency, key), amount in reqs.items():
                if currency == "__unverifiable__":
                    continue
                if key == "cash_base":
                    out["cash_base"] += amount
                elif key.startswith("shares:"):
                    uk = key.split(":", 2)[1] + ":" + key.split(":", 2)[2]
                    out["shares"][uk] = out["shares"].get(uk, Decimal(0)) + amount
                elif key.startswith("close:"):
                    cid = int(key.split(":", 1)[1])
                    out["close"][cid] = out["close"].get(cid, Decimal(0)) + amount
                elif key.startswith("exposure:"):
                    uk = key.split(":", 2)[1] + ":" + key.split(":", 2)[2]
                    out["exposure"][uk] = out["exposure"].get(uk, Decimal(0)) + amount
            result.append(out)
        return result

    def _verify_fill_proof(self, proof: Any, prior_leg: PreparedLeg,
                           prior_ref: str, account: str,
                           environment: str) -> None:
        """Independently verify a STRUCTURED FillProof against the APPROVED leg.

        A bare ``"Filled"`` string (or any generic status) is rejected.  The
        engine checks account/env/conId/stable ref/status/finite EXACT full
        quantity; 0, partial, overfill, NaN, float or a wrong ref all fail.
        """
        if proof is None:
            raise UnsafeComboError("no structured FillProof for the dependency leg")
        if not isinstance(proof, FillProof):
            raise UnsafeComboError(
                "fill_proof must return a structured FillProof (a bare status "
                "string is not sufficient evidence)")
        if proof.status != "Filled":
            raise UnsafeComboError("FillProof status is not an actual Filled")
        if proof.account != account or proof.environment != environment:
            raise UnsafeComboError("FillProof account/environment mismatch")
        if proof.order_ref != prior_ref:
            raise UnsafeComboError("FillProof stable orderRef does not match "
                                   "the approved dependency leg")
        if type(proof.con_id) is not int or proof.con_id != prior_leg.instrument.con_id:
            raise UnsafeComboError("FillProof conId does not match the approved "
                                   "dependency leg")
        qty = proof.filled_quantity
        if isinstance(qty, bool) or isinstance(qty, float) \
                or not isinstance(qty, Decimal) or not qty.is_finite() \
                or qty != prior_leg.quantity:
            raise UnsafeComboError(
                "FillProof filled_quantity must be a finite Decimal EXACTLY "
                "equal to the approved full leg quantity (0/partial/overfill/"
                "NaN/float are all refused)")

    def _stored_leg_canonical(self, intent_id: str,
                              leg_index: int) -> Mapping[str, Any]:
        """Read the APPROVED canonical leg terms from the stored intent payload.

        The approved terms are the AUTHORITATIVE comparison target for a
        positive handoff — never the caller's freshly-constructed objects.  A
        missing / malformed / out-of-range stored leg fails closed.
        """
        return approved_leg_canonical(self.ledger, intent_id, leg_index)

    def _recheck_send_time(self, *, intent_id: str, code: str,
                           account: str, environment: str,
                           legs: Sequence[PreparedLeg],
                           snapshot: Snapshot,
                           reserved_day_key: str) -> datetime:
        """Re-read the clock AFTER the in-lock refresh and BEFORE each send.

        Re-verifies TTL/expiry, snapshot freshness and the configured natural
        day.  A cross-day drift refuses BEFORE the send (no under-counting an
        actual attempt on the OLD day: we simply do not count one at all).  An
        operator can re-preflight for the new day.  Returns the fresh ``now``.
        """
        now = self._now()
        self._check_digest_and_expiry(intent_id, code, account, environment,
                                      now, legs)
        # Snapshot freshness against the CURRENT clock (not a stale one).
        if self.limits.max_snapshot_age_seconds is not None:
            age = (now - snapshot.captured_at).total_seconds()
            if age < 0 or age > self.limits.max_snapshot_age_seconds:
                raise ExecutionError(
                    "refreshed snapshot is no longer fresh at send time; "
                    "re-run preflight")
        current_day = self._day_key(snapshot, now)
        if current_day != reserved_day_key:
            raise ExecutionError(
                "configured natural day changed since reservation "
                f"({reserved_day_key} -> {current_day}); refusing to count an "
                "attempt on the old day. Re-run preflight for the new day.")
        return now

    def reconcile_positive(self, *, account: str, environment: str,
                           order_ref: str, evidence: Any,
                           perm_id: Any = None, order_id: Any = None) -> str:
        """SEPARATE, STRUCTURED positive broker-snapshot handoff releases ONE leg.

        A bare ``broker_status`` string can NO LONGER release risk.  ``evidence``
        must be a :class:`PositiveHandoff` carrying a FRESH, strictly scoped
        ``Snapshot`` (at most 30s old / 5s in the future relative to the
        in-lock clock, or a stricter configured cap) that MECHANICALLY maps the
        SAME stable ``order_ref`` (and bound ``perm_id``/``order_id``) to
        EXACTLY ONE real WORKING order whose FULL economic identity — conId,
        action, order_type, bounded limit_price and ``remaining`` — matches the
        APPROVED stored leg.  ``remaining`` must equal the approved quantity
        EXACTLY; a partial/filled handoff is CONSERVATIVELY UNSUPPORTED (no
        guessing attribution from a fresh position).  Two working orders with
        the same ref are an ambiguous mapping and refuse.  All of this runs
        under the SAME account lock; an OTHER/live ref can never release a
        paper reservation.  Release is PER-LEG only: unmapped legs of the same
        package keep their reserved risk (state._refresh_reservation keeps the
        package outstanding until every leg is terminal).
        """
        if not isinstance(evidence, PositiveHandoff):
            raise ExecutionError(
                "positive handoff requires structured PositiveHandoff evidence "
                "(a bare broker status string is not sufficient)")
        if evidence.order_ref != order_ref:
            raise ExecutionError("handoff evidence orderRef mismatch")
        if evidence.account != account or evidence.environment != environment:
            raise ExecutionError("handoff evidence account/environment mismatch")
        if evidence.broker_status not in state.POSITIVE_BROKER_STATES:
            raise ExecutionError(
                "handoff requires a positive broker status from a fresh snapshot, "
                "not a local callback or absent open order")
        # A Filled order has NO working order: attribution cannot be proven from
        # a fresh position.  Stay blocked until an explicit fill/position bridge.
        if evidence.broker_status in ("Filled", "PartiallyFilled", "Cancelled",
                                      "ApiCancelled", "Inactive"):
            raise ExecutionError(
                "positive handoff for a Filled/terminal order is conservatively "
                "unsupported: no working order can prove the specific ref was "
                "absorbed by the snapshot (no fill/position bridge evidence)")
        with state.account_scope(self.lock_dir, account, environment):
            # In-transaction scope validation: a foreign ref must not release
            # this scope's risk.  get_leg is strictly account/environment scoped.
            leg = self.ledger.get_leg(order_ref, account=account,
                                      environment=environment)
            if leg is None:
                raise ExecutionError(
                    "handoff orderRef is unknown or belongs to a different "
                    "account/environment (scope mismatch)")
            if leg.state not in ("submitted", "submitting", "unknown"):
                raise ExecutionError(
                    f"leg {order_ref!r} is not in a releasable state "
                    f"({leg.state})")
            # IDs must not contradict the ledger's bound / supplied anchors.
            for label, supplied, bound in (
                    ("orderId", order_id, leg.order_id),
                    ("permId", perm_id, leg.perm_id)):
                if supplied is not None and bound not in (None, "") \
                        and str(supplied) != bound:
                    raise ExecutionError(
                        f"handoff {label} conflicts with the ledger's bound id")
            approved = approved_leg_canonical(
                self.ledger, leg.intent_id, leg.leg_index)
            order = verify_observed_working_order(
                order_ref=order_ref, account=account, environment=environment,
                leg=leg, approved=approved, snapshot=evidence.observed_snapshot,
                now=self._now(),
                max_snapshot_age_seconds=self.limits.max_snapshot_age_seconds,
                con_id=evidence.con_id, perm_id=perm_id, order_id=order_id)
            return self.ledger.confirm_leg(
                order_ref, broker_status=evidence.broker_status,
                perm_id=perm_id if perm_id is not None else order.perm_id,
                order_id=order_id if order_id is not None else order.order_id)

    def reconcile_filled(self, *, account: str, environment: str,
                         order_ref: str, fill: Any,
                         perm_id: Any = None, order_id: Any = None) -> str:
        """Release ONE leg using a broker EXECUTION record (the fill bridge).

        :meth:`reconcile_positive` deliberately refuses a Filled/terminal order
        because no WORKING order exists to map.  Without this path a filled leg
        leaves the account blocked forever (the P0 lock).  The bridge is not a
        shortcut: ``fill`` must be a :class:`PositiveFill` derived from a real
        ``Execution`` row naming the SAME stable ``orderRef``, and every field
        (account / conId / side / shares / cumQty / execution time) is verified
        against the APPROVED stored leg.  Partial fills, foreign accounts, other
        refs and pre-attempt executions all refuse.  Runs under the same
        account lock and is PER-LEG; the day submission count is retained.
        """
        if not isinstance(fill, PositiveFill):
            raise ExecutionError(
                "fill reconciliation requires structured PositiveFill evidence "
                "(a bare status or a position delta is not proof)")
        with state.account_scope(self.lock_dir, account, environment):
            leg = self.ledger.get_leg(order_ref, account=account,
                                      environment=environment)
            if leg is None:
                raise ExecutionError(
                    "fill orderRef is unknown or belongs to a different "
                    "account/environment (scope mismatch)")
            if leg.state not in ("submitted", "submitting", "unknown"):
                raise ExecutionError(
                    f"leg {order_ref!r} is not in a releasable state "
                    f"({leg.state})")
            for label, supplied, bound in (
                    ("orderId", order_id, leg.order_id),
                    ("permId", perm_id, leg.perm_id)):
                if supplied is not None and bound not in (None, "") \
                        and str(supplied) != bound:
                    raise ExecutionError(
                        f"fill {label} conflicts with the ledger's bound id")
            approved = approved_leg_canonical(
                self.ledger, leg.intent_id, leg.leg_index)
            verify_fill_against_approved(fill=fill, leg=leg, approved=approved,
                                         account=account,
                                         environment=environment)
            return self.ledger.confirm_leg(
                order_ref, broker_status="Filled",
                perm_id=perm_id if perm_id is not None else fill.perm_id,
                order_id=order_id if order_id is not None else fill.order_id)

    @staticmethod
    def _handoff_decimal(value: Any, label: str) -> Optional[Decimal]:
        """Backwards-compatible alias of :func:`handoff_decimal`."""
        return handoff_decimal(value, label)

    def confirm(self, *, intent_id: str, code: str,
                legs: Sequence[PreparedLeg], account: str,
                environment: str) -> ConfirmResult:
        """Explicitly confirm a preflight and (if safe) send the legs.

        Requires BOTH ``intent_id`` and ``code``.  Runs the entire refresh /
        digest / expiry / claim / assess / resource_plan / reserve / send
        sequence inside ONE account-scoped ``fcntl`` lock, without holding a SQL
        transaction across the network boundary.  Every failure before the first
        send aborts the reservation; after any send the ledger stays
        conservatively unresolved.  A positive callback status maps the leg to
        ``submitted`` (retaining risk) but NEVER to ``confirmed``; release
        requires :meth:`reconcile_positive` from a fresh broker snapshot.
        """
        if not intent_id or not code:
            raise ExecutionError("explicit intent_id AND approval code are required")
        _validate_legs(legs, account)
        self._validate_deferred_combo(legs)
        with state.account_scope(self.lock_dir, account, environment):
            # Conservative whole-account block: never speculatively release
            # risk that may still be live at the broker.  This applies to ALL
            # new submissions, including risk-reducing closes — an independent
            # close path is deliberately unsupported (see module docstring).
            if self.ledger.has_unresolved_submissions(
                    account=account, environment=environment):
                raise ExecutionError(
                    "account has unresolved submissions (submitting/submitted/"
                    "unknown); new risk is blocked until positive broker mapping "
                    "or explicit operator resolution")
            # (1) FULL comparison of the PASSED canonical legs/config/account/
            #     env against the APPROVED payload BEFORE claim/callback/send.
            now = self._now()
            self._check_digest_and_expiry(intent_id, code, account, environment,
                                          now, legs)
            stored = self.ledger.get_intent(intent_id)
            self.ledger.claim_intent(
                account=account, environment=environment, intent_id=intent_id,
                full_digest=stored["full_digest"], code=code, now=now)
            # (4) EVERY refreshed snapshot is strictly account/env scoped.
            snapshot = self._observed_scope(
                self.snapshot_provider(), account, environment,
                "refreshed snapshot")
            reservations = aggregate_reservations(self.ledger, account, environment)
            plan = self._call_resource_plan(snapshot, legs, reservations)
            # (2) INDEPENDENTLY validate the whole package is neither missing
            #     nor under-sized (never trusting the callback).
            self._validate_plan(plan, legs, snapshot)
            # (3) Each leg is assessed with the existing outstanding
            #     reservations + the OTHER legs' increments ONLY (self excluded,
            #     so cash360 buy360 is not double-counted).  The full package is
            #     still reserved up front via plan.items before any send.
            leg_increments = self._leg_increments(legs, snapshot)
            others = _merge_reservations(reservations,
                                         _sum_increments(leg_increments))
            reasons: list[str] = []
            for index, leg in enumerate(legs):
                if not leg.coverage_deferred:
                    without_self = _subtract_increments(others,
                                                       leg_increments[index])
                    reasons.extend(self._assess(
                        snapshot, leg, without_self, now).reasons)
            # (7) Re-read the clock AFTER the in-lock refresh and re-verify
            #     TTL / snapshot freshness / day BEFORE the reservation.
            now = self._recheck_send_time(
                intent_id=intent_id, code=code, account=account,
                environment=environment, legs=legs, snapshot=snapshot,
                reserved_day_key=self._day_key(snapshot, now))
            day_key = self._day_key(snapshot, now)

            # Check approved audited risk-reduction exception
            stored_kind = (stored.get("kind") or
                           json.loads(stored["payload"]).get("kind"))
            is_candidate = stored_kind in ("risk_reduction", "candidate_risk_reduction")
            is_net_reduction, reduction_gap = self._verify_net_reduction(
                legs, snapshot, reservations)
            approved_risk_reduction = (is_candidate and is_net_reduction)

            frozen, explanation = self._prepare_budget(
                account=account, environment=environment, day_key=day_key)
            if frozen and not approved_risk_reduction:
                return ConfirmResult(
                    intent_id=intent_id, reservation_id="", day_key=day_key,
                    decision="BLOCK", reasons=tuple(reasons), legs=(),
                    frozen=True, explanation=explanation,
                    verification={
                        "candidate_classification": is_candidate,
                        "verified_net_reduction": is_net_reduction,
                        "reduction_details": reduction_gap,
                        "exception_applied": False,
                    })
            elif frozen and approved_risk_reduction:
                frozen = False

            # Authoritative available amounts from the FRESH snapshot, then
            # whole-package post-fill overrides for deferred coverage.
            for (currency, resource_key), amount in dict(plan.resources).items():
                self.ledger.set_resource(
                    account=account, environment=environment,
                    currency=currency, resource_key=resource_key, amount=amount)
            for (currency, resource_key), amount in dict(plan.overrides).items():
                self.ledger.set_resource(
                    account=account, environment=environment,
                    currency=currency, resource_key=resource_key, amount=amount)
            reservation_kind = "risk_reduction" if approved_risk_reduction else "new_risk"
            verification_evidence = {
                "verified_reduction": approved_risk_reduction,
                "candidate_approved": is_candidate,
                "snapshot_captured_at": snapshot.captured_at.isoformat(),
                "reason": reduction_gap if not is_net_reduction else "verified_net_reduction",
            } if approved_risk_reduction else None

            reservation_id = self.ledger.reserve(
                account=account, environment=environment,
                intent_id=intent_id, day_key=day_key,
                items=tuple(plan.items),
                kind=reservation_kind,
                day_submission_slots=len(legs),
                verification_evidence=verification_evidence,
                now=now)
            leg_docs = [{"contract_id": str(leg.instrument.con_id),
                         "action": leg.action.upper(),
                         "quantity": str(leg.quantity)} for leg in legs]
            refs = self.ledger.record_legs(
                intent_id=intent_id, reservation_id=reservation_id,
                legs=leg_docs, now=now)
            outcomes: list[LegOutcome] = []
            attempted = False
            try:
                for index, (leg, ref) in enumerate(zip(legs, refs)):
                    if leg.coverage_deferred:
                        prior = leg.requires_filled_leg
                        prior_leg, prior_ref = legs[prior], refs[prior]
                        ids = {"order_id": outcomes[prior].order_id or "",
                               "perm_id": outcomes[prior].perm_id or ""}
                        proof = self.fill_proof(self.broker, prior_ref, ids) \
                            if self.fill_proof else None
                        # (6) STRUCTURED proof, independently verified against
                        #     the APPROVED dependency leg (never a bare string).
                        try:
                            self._verify_fill_proof(
                                proof, prior_leg, prior_ref, account, environment)
                        except UnsafeComboError as exc:
                            outcomes.append(LegOutcome(
                                order_ref=ref, leg_index=index, state="skipped",
                                reason=f"invalid structured Filled proof: {exc}"))
                            continue
                        # (4) Deferred fresh snapshot is STRICTLY scoped too.
                        try:
                            fresh = self._observed_scope(
                                self.snapshot_provider(), account, environment,
                                "deferred fresh snapshot")
                        except ExecutionError as exc:
                            outcomes.append(LegOutcome(
                                order_ref=ref, leg_index=index, state="skipped",
                                reason=f"fresh snapshot unavailable/low quality: {exc}"))
                            continue
                        try:
                            # (3) Deferred assessment: the FULL package was
                            #     ALREADY reserved before any send, so `live`
                            #     (prior obligations + the whole reserved
                            #     package) reflects it.  Exclude ONLY this
                            #     leg's own increment (no re-merging the whole
                            #     package — that would double-count and needlessly
                            #     block).  Real OLD obligations and the OTHER
                            #     legs' increments are kept, never released.
                            live = aggregate_reservations(
                                self.ledger, account, environment)
                            fresh_increments = self._leg_increments(legs, fresh)
                            without_self = _subtract_increments(
                                live, fresh_increments[index])
                            reasons.extend(self._assess(
                                fresh, leg, without_self, self._now()).reasons)
                        except ExecutionError as exc:
                            outcomes.append(LegOutcome(
                                order_ref=ref, leg_index=index, state="skipped",
                                reason=f"fresh risk assessment blocked: {exc}"))
                            continue
                        # (7) Re-verify TTL/freshness/day BEFORE this send too.
                        send_now = self._recheck_send_time(
                            intent_id=intent_id, code=code, account=account,
                            environment=environment, legs=legs,
                            snapshot=fresh, reserved_day_key=day_key)
                    else:
                        # (7) Re-verify TTL/freshness/day BEFORE EACH send.
                        send_now = self._recheck_send_time(
                            intent_id=intent_id, code=code, account=account,
                            environment=environment, legs=legs,
                            snapshot=snapshot, reserved_day_key=day_key)
                    # Durable pre-send attempt IMMEDIATELY before place_order.
                    self.ledger.begin_leg_attempt(ref, now=send_now)
                    attempted = True
                    receipt = self.place_order(self.broker, leg, ref)
                    if not isinstance(receipt, BrokerReceipt):
                        raise ExecutionError("place_order must return a BrokerReceipt")
                    oid = None if receipt.order_id is None else str(receipt.order_id)
                    pid = None if receipt.perm_id is None else str(receipt.perm_id)
                    if oid or pid:
                        self.ledger.bind_broker_ids(ref, order_id=oid, perm_id=pid)
                    if receipt.status in state.POSITIVE_BROKER_STATES:
                        # LOCAL callback only: retain risk as "submitted".
                        # NEVER confirm_leg here — release requires a separate
                        # positive broker-snapshot handoff via reconcile_positive.
                        self.ledger.record_leg_result(ref, state="submitted")
                        final_state = "submitted"
                    else:
                        # Unknown: may have been sent.  NEVER retry / release.
                        self.ledger.record_leg_result(ref, state="unknown")
                        final_state = "unknown"
                    outcomes.append(LegOutcome(
                        order_ref=ref, leg_index=index, state=final_state,
                        order_id=oid, perm_id=pid))
                self.ledger.skip_unsent_legs(reservation_id, now=self._now())
            except Exception as exc:
                if attempted:
                    # A send may have happened: keep the ledger unresolved
                    # rather than speculatively releasing risk.
                    raise ExecutionError(
                        f"submission interrupted after a send; ledger left "
                        f"conservatively unresolved (reservation {reservation_id}): "
                        f"{exc}") from exc
                self.ledger.abort_reservation(reservation_id, now=self._now())
                raise
            return ConfirmResult(
                intent_id=intent_id, reservation_id=reservation_id,
                day_key=day_key, decision="ALLOW", reasons=tuple(reasons),
                legs=tuple(outcomes),
                verification={
                    "candidate_classification": is_candidate,
                    "verified_net_reduction": is_net_reduction,
                    "reduction_details": reduction_gap,
                    "exception_applied": approved_risk_reduction,
                })


def _package_aggregate(items: Sequence[RiskReservationSpec]) -> Mapping[str, Any]:
    """Convert package RiskReservationSpec increments into risk.py buckets."""
    out: dict[str, Any] = {"cash_base": Decimal(0), "shares": {},
                           "close": {}, "exposure": {}}
    for item in items:
        value = Decimal(item.amount)
        key = item.resource_key
        if key == "cash_base":
            out["cash_base"] += value
        elif key.startswith("shares:"):
            parts = key.split(":", 2)
            uk = f"{parts[1]}:{parts[2]}"
            out["shares"][uk] = out["shares"].get(uk, Decimal(0)) + value
        elif key.startswith("close:"):
            cid = int(key.split(":", 1)[1])
            out["close"][cid] = out["close"].get(cid, Decimal(0)) + value
        elif key.startswith("exposure:"):
            parts = key.split(":", 2)
            uk = f"{parts[1]}:{parts[2]}"
            out["exposure"][uk] = out["exposure"].get(uk, Decimal(0)) + value
        else:
            raise ExecutionError(f"unrecognised package resource key {key!r}")
    return out


def _merge_reservations(base: Mapping[str, Any],
                        extra: Mapping[str, Any]) -> Mapping[str, Any]:
    """Overlay package increments onto the outstanding-reservation aggregate."""
    out: dict[str, Any] = {
        "cash_base": base["cash_base"] + extra["cash_base"],
        "shares": dict(base["shares"]), "close": dict(base["close"]),
        "exposure": dict(base["exposure"]),
    }
    for bucket in ("shares", "close", "exposure"):
        for key, value in extra[bucket].items():
            out[bucket][key] = out[bucket].get(key, Decimal(0)) + value
    return out


def _sum_increments(maps: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Sum a sequence of per-leg increment mappings into one mapping."""
    out: dict[str, Any] = {"cash_base": Decimal(0), "shares": {},
                           "close": {}, "exposure": {}}
    for m in maps:
        out["cash_base"] += m["cash_base"]
        for bucket in ("shares", "close", "exposure"):
            for key, value in m[bucket].items():
                out[bucket][key] = out[bucket].get(key, Decimal(0)) + value
    return out


def _subtract_increments(base: Mapping[str, Any],
                         minus: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return ``base - minus`` (exclude ONE leg's own increments).

    Results are clamped at zero so a malformed/over-subtracted increment can
    never create NEGATIVE reserved amounts (which risk.py rejects as invalid).
    """
    out: dict[str, Any] = {
        "cash_base": max(Decimal(0), base["cash_base"] - minus["cash_base"]),
        "shares": dict(base["shares"]), "close": dict(base["close"]),
        "exposure": dict(base["exposure"]),
    }
    for bucket in ("shares", "close", "exposure"):
        for key, value in minus[bucket].items():
            out[bucket][key] = max(
                Decimal(0), out[bucket].get(key, Decimal(0)) - value)
    return out


__all__ = [
    "ExecutionError", "UnsafeComboError", "ResourcePlan", "PreparedLeg",
    "BrokerReceipt", "Preflight", "LegOutcome", "ConfirmResult", "FillProof",
    "PositiveHandoff", "ExecutionEngine", "aggregate_reservations",
]
