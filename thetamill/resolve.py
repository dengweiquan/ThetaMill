"""Operator release path for UNRESOLVED submissions (the P0 account lock).

The execution engine conservatively blocks the WHOLE account from new risk as
soon as ONE leg may have reached the broker (``submitting`` / ``submitted`` /
``unknown``).  That block is correct — but until this module existed there was
no production entry point that could ever clear it: :meth:`SubmissionLedger.
resolve_unknown` and :meth:`ExecutionEngine.reconcile_positive` had no callers,
so a single ambiguous send locked the account permanently and the documented
remedy (``docs/OPERATIONS.zh-CN.md``: reconcile, never delete the ledger) was
unimplementable.

This module is that exit.  It is deliberately NARROW:

* ``confirmed`` requires POSITIVE, STRUCTURED broker evidence for EVERY
  unresolved leg of the reservation, obtained from a live READ-ONLY broker
  connection: either

    - a fresh, strictly-scoped :class:`~thetamill.broker_state.Snapshot` whose
      working orders mechanically map the SAME stable ``orderRef`` to exactly
      one order with the APPROVED full economic identity, or
    - an :class:`~thetamill.execution.PositiveFill` derived from a broker
      EXECUTION row naming the SAME ``orderRef``, with account / conId / side /
      exact filled quantity / execution time all matching the approved leg
      (the fill bridge — a Filled order has no working order to map).

  "It is not in open orders" is NOT evidence and never releases anything.
* ``released`` is only for a reservation where NO leg was ever attempted.  Any
  attempted leg means a send MAY have happened; the risk stays held.

Risk is released per leg through :meth:`SubmissionLedger.confirm_leg` (or
:meth:`SubmissionLedger.abort_reservation` for the provable no-send case), so
the ledger always records WHICH positive broker status justified each release.
The persistent day submission count is never refunded.

No CLI import, no config read and no ``ib_async`` import at module level: the
caller supplies either raw rows or a live broker object.  Merely listing or
gathering evidence uses the READ-ONLY handle, so a mistyped command can never
create or migrate a database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional, Sequence, Tuple

from thetamill import execution, state
from thetamill.execution import (
    ExecutionError,
    PositiveFill,
    approved_leg_canonical,
    verify_fill_against_approved,
    verify_observed_working_order,
)


class ResolveError(Exception):
    """Fail-closed operator-resolution error (nothing was released)."""


# ``submitted`` means "a positive LOCAL callback said the order was accepted";
# it is still unresolved because no broker observation has mapped it yet.
UNRESOLVED_LEG_STATES = ("submitting", "submitted", "unknown")

# An execution older than this is not a usable current observation.
_MAX_EXECUTION_AGE = timedelta(minutes=30)
_MAX_FUTURE_SKEW = timedelta(seconds=5)


@dataclass(frozen=True)
class BlockedReservation:
    """One reservation that is currently holding risk (why the account blocks)."""

    reservation_id: str
    intent_id: str
    day_key: str
    state: str
    legs: Tuple[Tuple[str, str, str, str], ...]  # (order_ref, action, qty, state)

    @property
    def states(self) -> Tuple[str, ...]:
        return tuple(sorted({leg[3] for leg in self.legs}))


@dataclass(frozen=True)
class Observations:
    """A single live read-only broker observation set.

    ``executions`` returns raw broker execution/fill rows for the SAME account
    (ib_async: ``ib.fills()`` plus ``ib.executions()``).  ``snapshot`` is a
    fresh account-scoped :class:`~thetamill.broker_state.Snapshot` or ``None``.
    Both are observations, never instructions.
    """

    executions: Callable[..., Sequence[Any]]
    snapshot: Optional[Any] = None


@dataclass(frozen=True)
class ResolutionOutcome:
    reservation_id: str
    status: str                        # "confirmed" | "released"
    released: Tuple[str, ...] = ()     # order refs whose risk was released
    evidence: Tuple[str, ...] = ()     # justification recorded per leg


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #

def unresolved_summary(ledger: Any, *, account: str,
                       environment: str) -> list[BlockedReservation]:
    """Every reservation for this scope that still holds risk, with its legs."""
    legs_by_res: dict[str, list[tuple[str, str, str, str]]] = {}
    for leg in ledger.list_legs(account=account, environment=environment):
        legs_by_res.setdefault(leg.reservation_id, []).append(
            (leg.order_ref, leg.action, leg.quantity, leg.state))
    return [BlockedReservation(
        reservation_id=res.reservation_id, intent_id=res.intent_id,
        day_key=res.day_key, state=res.state,
        legs=tuple(legs_by_res.get(res.reservation_id, ())))
        for res in ledger.outstanding_reservations(
            account=account, environment=environment)]


def never_sent_provable(ledger: Any, reservation_id: str, *, account: str,
                        environment: str) -> bool:
    """True ONLY when no leg of this reservation was ever attempted.

    A ``reserved`` / ``skipped`` leg cannot have reached the broker (the ledger
    persists the attempt durably BEFORE the network call), so those legs may be
    released.  Anything else means a send may have happened.
    """
    res = ledger.get_reservation(reservation_id)
    if res is None:
        raise ResolveError("unknown reservation")
    if res.state not in state._UNRESOLVED_STATES | {"reserved"}:
        raise ResolveError(f"reservation is not outstanding (state={res.state})")
    legs = [leg for leg in ledger.list_legs(account=account,
                                            environment=environment)
            if leg.reservation_id == reservation_id]
    return all(leg.state in ("reserved", "skipped") for leg in legs)


# --------------------------------------------------------------------------- #
# Evidence adapters
# --------------------------------------------------------------------------- #

def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool) or isinstance(value, float):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def fill_from_row(row: Any, *, account: str) -> Optional[PositiveFill]:
    """Turn ONE broker execution/fill row into structured evidence, or ``None``.

    Accepts a raw ``Execution`` (``execId`` / ``acctNumber`` / ``orderRef`` /
    ``permId`` / ``orderId`` / ``conId`` / ``side`` / ``shares`` / ``price`` /
    ``time`` / ``cumQty``) and an ib_async ``Fill`` (``.execution`` +
    ``.contract``).  Anything unreadable returns ``None`` so the caller can
    never release on a partially-understood row.
    """
    execution_row = getattr(row, "execution", row)
    contract = getattr(row, "contract", None)
    exec_id = getattr(execution_row, "execId", None)
    if not isinstance(exec_id, str) or not exec_id.strip():
        return None
    if getattr(execution_row, "acctNumber", None) != account:
        return None
    order_ref = getattr(execution_row, "orderRef", None)
    if not isinstance(order_ref, str) or not order_ref.strip():
        return None
    # ib_async's ``Execution`` has NO conId field (see the dataclass fields);
    # only the paired ``Fill.contract`` carries it.  So the contract is the
    # authoritative source, and an execution-level conId may only CORROBORATE.
    exec_con = getattr(execution_row, "conId", None)
    contract_con = getattr(contract, "conId", None)
    if type(contract_con) is int:
        if type(exec_con) is int and exec_con != contract_con:
            return None  # two disagreeing identities are never evidence
        con_id = contract_con
    elif type(exec_con) is int:
        con_id = exec_con
    else:
        return None
    side = getattr(execution_row, "side", None)
    if not isinstance(side, str):
        return None
    shares = _decimal_or_none(getattr(execution_row, "shares", None))
    price = _decimal_or_none(getattr(execution_row, "price", None))
    raw_cum = getattr(execution_row, "cumQty", None)
    cum = shares if raw_cum is None else _decimal_or_none(raw_cum)
    stamp = getattr(execution_row, "time", None)
    if shares is None or price is None or cum is None:
        return None
    if not isinstance(stamp, datetime) or stamp.tzinfo is None \
            or stamp.utcoffset() is None:
        return None
    perm = getattr(execution_row, "permId", None)
    order_id = getattr(execution_row, "orderId", None)
    return PositiveFill(
        exec_id=exec_id.strip(), account=account, order_ref=order_ref.strip(),
        con_id=con_id, side=side, shares=shares, price=price, cum_qty=cum,
        as_of=stamp,
        perm_id=perm if type(perm) is int else None,
        order_id=order_id if type(order_id) is int else None)


def _check_execution_time(stamp: datetime, now: datetime) -> None:
    if stamp - now > _MAX_FUTURE_SKEW:
        raise ResolveError("broker execution time is in the future (clock skew)")
    if now - stamp > _MAX_EXECUTION_AGE:
        raise ResolveError(
            "broker execution is stale; re-read executions and try again")


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

def _verify_leg(ledger: Any, leg: Any, *, account: str, environment: str,
                observations: Observations, now: datetime) -> str:
    """Establish positive evidence for ONE unresolved leg; returns the reason.

    Every branch either returns a description of evidence that PASSED the
    shared identity checks in :mod:`thetamill.execution`, or raises.  There is
    no "assume it worked" branch.
    """
    approved = approved_leg_canonical(ledger, leg.intent_id, leg.leg_index)
    # (a) A still-working order with the approved identity releases the leg.
    if observations.snapshot is not None:
        try:
            order = verify_observed_working_order(
                order_ref=leg.order_ref, account=account,
                environment=environment, leg=leg, approved=approved,
                snapshot=observations.snapshot, now=now)
        except ExecutionError:
            order = None
        if order is not None:
            ledger.confirm_leg(leg.order_ref, broker_status="Submitted",
                               perm_id=order.perm_id, order_id=order.order_id)
            return (f"working order {order.order_id}/{order.perm_id} with "
                    f"remaining {order.remaining} matches the approved leg")
    # (b) A broker EXECUTION row for this exact ref and full quantity.
    if not callable(observations.executions):
        raise ResolveError("no broker execution observation was supplied")
    raw = observations.executions(account=account)
    if not isinstance(raw, (list, tuple)):
        raise ResolveError("broker execution observation is not a list")
    matches: list[PositiveFill] = []
    for row in raw:
        fill = fill_from_row(row, account=account)
        if fill is None or fill.order_ref != leg.order_ref:
            continue
        _check_execution_time(fill.as_of, now)
        try:
            verify_fill_against_approved(fill=fill, leg=leg, approved=approved,
                                         account=account,
                                         environment=environment)
        except ExecutionError as exc:
            raise ResolveError(
                f"execution {fill.exec_id} for {leg.order_ref} cannot release "
                f"this leg: {exc}") from exc
        matches.append(fill)
    if len(matches) > 1:
        raise ResolveError(
            f"ambiguous: multiple executions match orderRef {leg.order_ref}; "
            "refusing to release risk")
    if not matches:
        raise ResolveError(
            f"no execution and no working order proves orderRef {leg.order_ref} "
            "reached the broker; the risk stays held")
    fill = matches[0]
    ledger.confirm_leg(leg.order_ref, broker_status="Filled",
                       perm_id=fill.perm_id, order_id=fill.order_id)
    return (f"execution {fill.exec_id} filled {fill.shares} @ {fill.price} "
            f"for {fill.order_ref}")


def resolve_reservation(ledger: Any, reservation_id: str, *,
                        executions: Callable[..., Sequence[Any]],
                        snapshot_provider: Optional[Callable[[], Any]] = None,
                        now: Optional[datetime] = None) -> ResolutionOutcome:
    """Release ONE reservation using positive broker evidence, or refuse.

    ``confirmed`` needs positive evidence for EVERY unresolved leg; anything
    less raises :class:`ResolveError` and NOTHING is released.  ``released`` is
    only reachable through :func:`release_never_sent`.
    """
    stamp = now or datetime.now(timezone.utc)
    if not isinstance(stamp, datetime) or stamp.tzinfo is None \
            or stamp.utcoffset() is None:
        raise ResolveError("now must be an aware datetime")
    res = ledger.get_reservation(reservation_id)
    if res is None:
        raise ResolveError("unknown reservation")
    if res.state not in state._UNRESOLVED_STATES:
        raise ResolveError(f"reservation is not unresolved (state={res.state})")
    account, environment = scope_parts(res.scope)
    legs = [leg for leg in ledger.list_legs(account=account,
                                            environment=environment)
            if leg.reservation_id == reservation_id]
    if not legs:
        raise ResolveError("reservation has no recorded legs")
    unresolved = [leg for leg in legs if leg.state in UNRESOLVED_LEG_STATES]
    if not unresolved:
        raise ResolveError("reservation has no unresolved legs to release")
    snapshot = snapshot_provider() if snapshot_provider is not None else None
    if snapshot is not None:
        try:
            execution.check_observed_scope(snapshot, account, environment,
                                           "resolve observed snapshot")
        except ExecutionError as exc:
            raise ResolveError(str(exc)) from exc
    observations = Observations(executions=executions, snapshot=snapshot)
    released: list[str] = []
    evidence: list[str] = []
    for leg in unresolved:
        evidence.append(_verify_leg(ledger, leg, account=account,
                                    environment=environment,
                                    observations=observations, now=stamp))
        released.append(leg.order_ref)
    return ResolutionOutcome(reservation_id=reservation_id, status="confirmed",
                             released=tuple(released), evidence=tuple(evidence))


def release_never_sent(ledger: Any, reservation_id: str, *,
                       why: str) -> ResolutionOutcome:
    """Release a reservation that provably never reached the broker.

    Only valid when NO leg was attempted; a written reason is required.  This
    refunds the reserved day slots (they were never spent) but never a spent
    submission count.
    """
    if not isinstance(why, str) or not why.strip():
        raise ResolveError("a written reason is required to release a reservation")
    res = ledger.get_reservation(reservation_id)
    if res is None:
        raise ResolveError("unknown reservation")
    account, environment = scope_parts(res.scope)
    if not never_sent_provable(ledger, reservation_id, account=account,
                               environment=environment):
        raise ResolveError(
            "at least one leg was already attempted; a send may have reached "
            "the broker, so this reservation cannot be released")
    ledger.abort_reservation(reservation_id)
    return ResolutionOutcome(reservation_id=reservation_id, status="released")


def scope_parts(scope: str) -> Tuple[str, str]:
    """Split a ledger scope string back into ``(account, environment)``."""
    parts = str(scope).split("\x1f")
    if len(parts) != 2 or not all(part.strip() for part in parts):
        raise ResolveError("reservation scope is malformed")
    return parts[0], parts[1]


# --------------------------------------------------------------------------- #
# Ledger / observation handles
# --------------------------------------------------------------------------- #

def open_ledger(path: Any, day_timezone: str, *, writable: bool) -> Any:
    """Open the ledger; read-only unless a release is actually being applied."""
    try:
        return state.SubmissionLedger(path, day_timezone,
                                      read_only=not writable)
    except state.StateError as exc:
        raise ResolveError(f"cannot open the submission ledger: {exc}") from exc


def live_observations(ib: Any, *, account: str) -> Observations:
    """Collect a read-only, account-scoped execution observation set.

    Never places, cancels or modifies anything.  ``ib.fills()`` is the execution
    history ib_async keeps for THIS client; ``ib.executions()`` adds rows seen
    through ``reqExecutions``.  Rows are merged and de-duplicated by execId.
    """
    rows: list[Any] = []
    seen: set[str] = set()
    for getter in ("fills", "executions"):
        fn = getattr(ib, getter, None)
        if not callable(fn):
            continue
        try:
            found = fn()
        except Exception as exc:  # broker-side failure: never guess
            raise ResolveError(f"cannot read broker executions: {exc}") from exc
        if not isinstance(found, (list, tuple)):
            raise ResolveError("broker execution observation is not a list")
        for row in found:
            execution_row = getattr(row, "execution", row)
            marker = str(getattr(execution_row, "execId", "") or "")
            if marker and marker in seen:
                continue
            if marker:
                seen.add(marker)
            rows.append(row)

    def executions(*, account: str = account) -> Sequence[Any]:
        return tuple(rows)

    return Observations(executions=executions)
