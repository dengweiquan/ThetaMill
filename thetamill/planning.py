"""Pure cumulative-budget planning for ordinary multi-leg plans.

This module is the shared, OFFLINE planning layer for a normal (non-roll) plan.
It is deliberately NOT an execution engine and NOT a second risk model: every
per-order gate is :func:`thetamill.risk.assess`, and every aggregate figure is
:func:`thetamill.risk.summarize`.  What it adds on top is the *sequential*
accumulation of the budget that each ALLOWED order consumes, so a plan with
several legs cannot double-spend the same shares / cash / close capacity.

Design contract
---------------
* Pure: no I/O, no broker, no ledger, no files, no network, no globals, no
  mutation of the Snapshot / RiskLimits / OrderRequests / reservations passed
  in.  All arithmetic uses :class:`~decimal.Decimal`.
* For each candidate :class:`~thetamill.risk.OrderRequest` in order:
  1. run a STRICT timestamped-evidence gate (see :func:`validate_snapshot_evidence`);
  2. call ``risk.assess(snapshot, request, limits, running_reservations, now=now)``
     using the ORIGINAL snapshot plus the reservations accumulated by every
     *previously ALLOWED* request (old obligations are NEVER released);
  3. only when the Decision is ALLOW, compute that request's OWN incremental
     requirement (exact Decimal, same formulas as risk.py's ``_baseline``) and
     add it to the running reservations.  A BLOCKED request consumes NOTHING and
     is never presented as executable.
* Duplicate candidate conIds are rejected (a second order may not spend the
  same shares / cash / close capacity twice); quantities are never truncated
  or silently adjusted.
* The returned document is DATA ONLY: an ALLOW row means "this order is
  admissible against this snapshot and the configured limits", NOT an
  authorization to place, submit or execute anything.  It carries no
  ``placeOrder`` / ``orders_to_place`` / ``submit`` fields.

Reservation buckets (identical to risk.py and execution._reservation_aggregate)
------------------------------------------------------------------------------
    {"cash_base": Decimal, "shares": {underlying_key: Decimal},
     "close": {con_id: Decimal}, "exposure": {underlying_key: Decimal}}
``underlying_key`` is ``"<underlying_id>:<CURRENCY>"`` (Instrument.key).
Positive values always CONSUME budget (cash / shares / close / exposure), never
release it.  ``shares`` values are share counts; ``exposure`` values are in BASE units
converted by the snapshot's base-per-unit FX; ``close`` values are exact
contract counts.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from . import risk
from .broker_state import FxQuote, PriceQuote, Snapshot
from .risk import OrderRequest, RiskLimits

ZERO = Decimal(0)
ONE = Decimal(1)

# One shared evidence policy (NOT three copies).  Matches cli._risk_snapshot's
# _timestamped_mid: live, timestamped bid/ask midpoint quotes only.
QUOTE_SOURCES = frozenset({"IB timestamped bid/ask midpoint"})
DEFAULT_QUOTE_MAX_AGE = timedelta(seconds=30)
DEFAULT_SNAPSHOT_MAX_AGE = timedelta(seconds=30)
FUTURE_SKEW = timedelta(seconds=5)


class PlanningError(ValueError):
    """A planning input is malformed; never silently coerced."""


# --------------------------------------------------------------------------- #
# Small strict helpers (no mutation, bool/NaN/Inf rejected)
# --------------------------------------------------------------------------- #

def _dec(value: Any, label: str, *, positive: bool = False,
         minimum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PlanningError(f"{label}: expected a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise PlanningError(f"{label}: expected a finite number") from None
    if not result.is_finite() or (positive and result <= 0):
        raise PlanningError(f"{label}: expected a positive finite number")
    if minimum is not None and result < minimum:
        raise PlanningError(f"{label}: expected a non-negative finite number")
    return result


def _price(value: Any) -> Decimal | None:
    try:
        return _dec(value, "price", positive=True)
    except PlanningError:
        return None


def _rate(snapshot: Snapshot, currency: str) -> Decimal | None:
    if currency == snapshot.base_currency:
        return ONE
    return _price(snapshot.fx.get(currency))


def _now_utc(now: datetime | None) -> datetime:
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise PlanningError("now must be a timezone-aware datetime")
    return now.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# Shared timestamped snapshot evidence gate (single source of truth)
# --------------------------------------------------------------------------- #

def quote_evidence_problem(quote, expected_value: Decimal, *, key: str | int,
                           now: datetime,
                           max_age: timedelta = DEFAULT_QUOTE_MAX_AGE) -> str | None:
    """Validate ONE PriceQuote/FxQuote against the scalar the snapshot consumes.

    Returns a single human reason, or ``None`` when the quote is acceptable.
    Checks: real quote object, identity, finite positive value EXACTLY equal to
    ``expected_value`` (no drift), non-empty allowlisted source, aware timestamp,
    age <= ``max_age``, future skew <= :data:`FUTURE_SKEW`.  Pure; never mutates.
    """
    if quote is None:
        return f"missing timestamped quote evidence for {key}"
    if not isinstance(quote, (PriceQuote, FxQuote)):
        return f"quote evidence for {key} is not a broker_state quote"
    if isinstance(quote, PriceQuote):
        if quote.con_id != key:
            return f"price quote identity mismatch for conId {key}"
        value = quote.price
    else:
        if quote.currency != key:
            return f"FX quote identity mismatch for {key}"
        value = quote.rate
    try:
        if _dec(value, f"quote {key}", positive=True) != _dec(
                expected_value, f"snapshot value {key}", positive=True):
            return f"quote value for {key} does not match the snapshot scalar"
    except PlanningError:
        return f"quote value for {key} is unusable"
    if not isinstance(quote.source, str) or quote.source not in QUOTE_SOURCES:
        return f"quote source for {key} is not an approved timestamped source"
    stamp = quote.as_of
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
        return f"quote timestamp for {key} is not timezone-aware"
    age = now - stamp.astimezone(timezone.utc)
    if age < -FUTURE_SKEW:
        return f"quote timestamp for {key} is in the future"
    if age > max_age:
        return f"quote for {key} is stale (older than {int(max_age.total_seconds())}s)"
    return None


def required_evidence_currencies(snapshot: Snapshot,
                                 extra: Iterable[str] = ()) -> frozenset[str]:
    """Foreign currencies ACTUALLY consumed by the shared cash / obligations.

    Covers every nonzero cash balance and the currency of every position and
    working order, plus any ``extra`` currencies a caller's candidate orders
    will introduce.  The base currency needs no FX quote (its rate is 1).
    """
    risk._validate_snapshot(snapshot)
    base = snapshot.base_currency
    currencies: set[str] = set()
    for cur, amount in snapshot.cash.items():
        if cur != base and _dec(amount, f"cash {cur}") != ZERO:
            currencies.add(cur)
    for p in snapshot.positions:
        if p.instrument.currency != base:
            currencies.add(p.instrument.currency)
    for o in snapshot.orders:
        if o.instrument.currency != base:
            currencies.add(o.instrument.currency)
    for cur in extra:
        if not isinstance(cur, str) or not cur.strip():
            raise PlanningError("extra currencies must be non-empty strings")
        cur = cur.strip().upper()
        if cur != base:
            currencies.add(cur)
    return frozenset(currencies)


def validate_snapshot_evidence(snapshot: Snapshot, limits: RiskLimits, now: datetime,
                               *, needed_currencies: Iterable[str] = (),
                               need_stock_prices: bool | None = None,
                               require_all_evidence: bool = False) -> tuple[str, ...]:
    """STRICT, separate gate for timestamped price / FX + snapshot freshness.

    This is intentionally INDEPENDENT of :func:`thetamill.risk.assess`: adapters
    (normal plan, roll_cli, submission_cli) call it with the evidence their
    specific request needs, then call ``assess``.  It NEVER mutates the
    snapshot.  Returns a tuple of reason strings; empty means evidence is OK.

    * Snapshot freshness: ``now - captured_at`` must be in
      ``[-5s, max_age]`` where ``max_age`` is
      ``limits.max_snapshot_age_seconds`` (validated) or a 30s default.  A
      stale/future snapshot is a hard gate regardless of quote freshness.
    * FX: for every currency returned by :func:`required_evidence_currencies`
      (all nonzero cash / position / order currencies + ``needed_currencies``),
      ``snapshot.fx`` must hold a finite positive rate AND ``snapshot.fx_quotes``
      must hold a matching, allowlisted, timestamped :class:`FxQuote` whose
      ``rate`` equals that scalar exactly.  Missing evidence blocks the FX.
    * Stock prices: required when ``limits.has_exposure_caps`` (caps consume
      stock valuation), ``require_all_evidence``, or ``need_stock_prices`` is
      True. An explicit False cannot relax mandatory evidence. For every POSITIVE stock
      position, ``p.price`` must be a finite positive number matching a
      timestamped, allowlisted :class:`PriceQuote` for that conId.  A base put
      with caps OFF therefore needs NO unrelated stock quote.
    * ``account`` / ``environment`` are validated as one snapshot; nothing here
      mixes data across accounts or environments.
    """
    moment = _now_utc(now)
    if not isinstance(limits, RiskLimits):
        raise PlanningError("limits must be a validated RiskLimits")

    max_age = (timedelta(seconds=limits.max_snapshot_age_seconds)
               if limits.max_snapshot_age_seconds is not None
               else DEFAULT_SNAPSHOT_MAX_AGE)
    reasons: list[str] = []
    risk._validate_snapshot(snapshot)  # structural numbers, no mutation

    captured = snapshot.captured_at
    if not isinstance(captured, datetime) or captured.tzinfo is None \
            or captured.utcoffset() is None:
        reasons.append("snapshot timestamp is not timezone-aware")
    else:
        age = moment - captured.astimezone(timezone.utc)
        if age < -FUTURE_SKEW:
            reasons.append("snapshot timestamp is in the future")
        elif age > max_age:
            reasons.append(f"snapshot is stale (older than {int(max_age.total_seconds())}s)")

    # ---- FX evidence: only currencies actually consumed -------------------- #
    for cur in sorted(required_evidence_currencies(snapshot, needed_currencies)):
        rate = _rate(snapshot, cur)
        if rate is None:
            reasons.append(f"missing FX rate for consumed currency {cur}")
            continue
        problem = quote_evidence_problem(snapshot.fx_quotes.get(cur), rate,
                                        key=cur, now=moment,
                                        max_age=DEFAULT_QUOTE_MAX_AGE)
        if problem:
            reasons.append(problem)

    # ---- Stock valuation evidence: only when it is consumed --------------- #
    # An explicit override may ADD evidence requirements, never turn off
    # evidence that configured caps or require_all_evidence actually consume.
    need_stock_prices = bool(limits.has_exposure_caps or require_all_evidence
                             or need_stock_prices)
    if need_stock_prices:
        for p in snapshot.positions:
            if p.instrument.sec_type != "STK":
                continue
            if _dec(p.quantity, "stock quantity") <= ZERO:
                continue
            price = _price(p.price)
            if price is None:
                reasons.append(f"missing stock price for conId {p.instrument.con_id}")
                continue
            problem = quote_evidence_problem(
                snapshot.price_quotes.get(p.instrument.con_id), price,
                key=p.instrument.con_id, now=moment,
                max_age=DEFAULT_QUOTE_MAX_AGE)
            if problem:
                reasons.append(problem)

    return tuple(reasons)


# --------------------------------------------------------------------------- #
# Reservation accumulation
# --------------------------------------------------------------------------- #

def empty_reservations() -> dict[str, Any]:
    return {"cash_base": ZERO, "shares": {}, "close": {}, "exposure": {}}


def _normalize_reservations(reservations: Mapping | None) -> dict[str, Any]:
    """Copy + strictly validate the 4 buckets (never mutate the input)."""
    reservations = {} if reservations is None else reservations
    if not isinstance(reservations, Mapping):
        raise PlanningError("reservations must be a mapping")
    unknown = set(reservations) - risk.RESERVATION_BUCKETS
    if unknown:
        raise PlanningError(f"unknown reservation buckets {sorted(unknown)}")
    out = empty_reservations()
    out["cash_base"] = _dec(reservations.get("cash_base", ZERO),
                            "reservation cash_base", minimum=ZERO)
    for bucket in ("shares", "close", "exposure"):
        section = reservations.get(bucket, {})
        if not isinstance(section, Mapping):
            raise PlanningError(f"reservations {bucket} must be a mapping")
        for key, value in section.items():
            if bucket == "close":
                if isinstance(key, bool) or not (
                        isinstance(key, int) or
                        (isinstance(key, str) and key.isdigit())):
                    raise PlanningError(f"invalid close conId key {key!r}")
            else:
                if (not isinstance(key, str) or ":" not in key or
                        not key.split(":", 1)[0].isdigit() or
                        not key.split(":", 1)[1]):
                    raise PlanningError(f"invalid underlying key {key!r}")
            amount = _dec(value, f"reservation {bucket}[{key}]", minimum=ZERO)
            # JSON conId strings and int keys name the SAME capacity. Merge
            # aliases before accumulating, so export cannot discard a debit.
            canonical_key = int(key) if bucket == "close" else key
            out[bucket][canonical_key] = out[bucket].get(canonical_key, ZERO) + amount
    return out


def _add(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    """Accumulate non-negative consumption; only ever INCREASES usage."""
    target["cash_base"] += source.get("cash_base", ZERO)
    for bucket in ("shares", "close", "exposure"):
        for key, value in source.get(bucket, {}).items():
            target[bucket][key] = target[bucket].get(key, ZERO) + value


def _requirement(snapshot: Snapshot, request: OrderRequest) -> dict[str, Any]:
    """Exact incremental budget ONE ALLOWED order must reserve.

    Mirrors risk.py's ``_baseline`` formulas with real multipliers / strikes /
    FX.  Nothing here may release an existing obligation: stock SELL consumes
    shares, option BUY consumes exact close capacity + bounded premium cash,
    and no order ever lowers exposure.  A missing/unbounded required value
    raises PlanningError (the order should already have been blocked by
    ``assess``, so this is a fail-closed integrity check).
    """
    inst = request.instrument
    qty = _dec(request.quantity, "request quantity", positive=True)
    mult = (_dec(inst.multiplier, "multiplier", positive=True)
            if inst.sec_type == "OPT" else ONE)
    key = inst.key
    out = empty_reservations()

    if inst.sec_type == "STK" and request.action == "BUY":
        limit = _price(request.limit_price)
        rate = _rate(snapshot, inst.currency)
        if request.order_type.upper() != "LMT" or limit is None or rate is None:
            raise PlanningError("unbounded stock BUY cannot be budgeted")
        notional = qty * limit * rate
        out["cash_base"] = notional
        out["exposure"][key] = notional

    elif inst.sec_type == "STK" and request.action == "SELL":
        out["shares"][key] = qty  # consumes cover; never reduces exposure

    elif inst.sec_type == "OPT" and request.action == "SELL":
        if inst.right.upper() == "C":
            out["shares"][key] = qty * mult
        elif inst.right.upper() == "P":
            strike = _dec(inst.strike, "strike", positive=True)
            rate = _rate(snapshot, inst.currency)
            if rate is None:
                raise PlanningError("missing FX for short put obligation")
            obligation = qty * strike * mult
            out["cash_base"] = obligation * rate
            out["exposure"][key] = obligation * rate
        else:
            raise PlanningError("unrecognised option right")

    elif inst.sec_type == "OPT" and request.action == "BUY":
        out["close"][inst.con_id] = qty
        limit = _price(request.limit_price)
        rate = _rate(snapshot, inst.currency)
        if request.order_type.upper() != "LMT" or limit is None or rate is None:
            raise PlanningError("unbounded option BUY cannot be budgeted")
        out["cash_base"] = qty * mult * limit * rate
    else:
        raise PlanningError("unrecognised instrument/action; cannot budget")
    return out


# --------------------------------------------------------------------------- #
# Result value objects (immutable, JSON-friendly)
# --------------------------------------------------------------------------- #

def _freeze(value):
    """Own a recursively immutable copy, including nested risk details."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


def _json_copy(value):
    """An independent JSON-friendly copy; no shared mutable descendants."""
    if isinstance(value, Mapping):
        return {str(key): _json_copy(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_copy(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


@dataclass(frozen=True)
class PlannedOrder:
    """One candidate's outcome.  ``allowed`` is an ADMISSIBILITY, not an order."""
    index: int
    decision: risk.Decision
    requirement: Mapping[str, Any] | None
    evidence_reasons: tuple[str, ...]

    def __post_init__(self):
        if self.decision is not None:
            object.__setattr__(self, "decision", replace(
                self.decision, details=_freeze(self.decision.details),
                reasons=tuple(self.decision.reasons)))
        object.__setattr__(self, "requirement", _freeze(self.requirement))
        object.__setattr__(self, "evidence_reasons", tuple(self.evidence_reasons))

    @property
    def allowed(self) -> bool:
        return (self.decision is not None and self.decision.allowed
                and not self.evidence_reasons)

    @property
    def executable(self) -> bool:
        return False  # planning is DATA ONLY; never an execution authorization


@dataclass(frozen=True)
class PlanAssessment:
    """The whole plan: per-order decisions + final aggregate + summary."""
    items: tuple[PlannedOrder, ...]
    reservations: Mapping[str, Any]
    summary: Mapping[str, Any]
    limits: RiskLimits
    now: datetime
    evidence_reasons: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "reservations", _freeze(self.reservations))
        object.__setattr__(self, "summary", _freeze(self.summary))
        object.__setattr__(self, "evidence_reasons", tuple(self.evidence_reasons))

    @property
    def allowed_count(self) -> int:
        return sum(1 for i in self.items if i.allowed)

    @property
    def blocked_count(self) -> int:
        return sum(1 for i in self.items if not i.allowed)


def _reservations_view(reservations: Mapping[str, Any]) -> Mapping[str, Any]:
    """JSON-friendly string copy (shares/exposure keys unchanged, close str)."""
    return {
        "cash_base": str(reservations["cash_base"]),
        "shares": {k: str(v) for k, v in reservations["shares"].items()},
        "close": {str(k): str(v) for k, v in reservations["close"].items()},
        "exposure": {k: str(v) for k, v in reservations["exposure"].items()},
    }


def plan_document(assessment: PlanAssessment) -> dict[str, Any]:
    """Independent DATA-ONLY JSON mapping; unverified budgets are withheld.

    The assessment retains the raw shared risk summary for diagnostics. This
    display document must not present stale/unproven figures as availability.
    """
    summary = _json_copy(assessment.summary)
    budget_confirmed = (not assessment.evidence_reasons
                        and summary.get("cash_confirmed") is True
                        and summary.get("exposure_confirmed") is True)
    if assessment.evidence_reasons:
        summary["cash_confirmed"] = False
        summary["exposure_confirmed"] = False
        for key in ("cash_pool_base", "free_cash_base", "exposure_portfolio_base", "headroom"):
            summary[key] = None
        for key in ("shares", "short_close_capacity", "exposure_by_underlying"):
            summary[key] = {identity: None for identity in summary.get(key, {})}
        for cover in summary.get("call_cover", {}).values():
            cover["available_shares"] = None
    return {
        "schema": "thetamill.planning.v1",
        "kind": "cumulative_budget_plan",
        "executable": False,
        "recommendation_only": True,
        "disclaimer": ("仅供人工复核的累计预算评估；不是执行授权，不会下单/撤单，"
                       "不得据此自动提交任何订单。"),
        "account": assessment.summary.get("account"),
        "environment": assessment.summary.get("environment"),
        "base_currency": assessment.summary.get("base_currency"),
        "now": assessment.now.isoformat(),
        "limits_display": assessment.limits.display,
        "budget_confirmed": budget_confirmed,
        "evidence_reasons": list(assessment.evidence_reasons),
        "items": [{
            "index": i.index,
            "allowed": i.allowed,
            "executable": False,
            "decision": i.decision.display if i.decision is not None else "BLOCK",
            "reasons": list(i.decision.reasons) if i.decision is not None else [],
            "evidence_reasons": list(i.evidence_reasons),
            "requirement": (_reservations_view(i.requirement)
                            if i.requirement is not None else None),
        } for i in assessment.items],
        "allowed_count": assessment.allowed_count,
        "blocked_count": assessment.blocked_count,
        "reservations": _reservations_view(assessment.reservations),
        "summary": summary,
    }


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #

def assess_plan(snapshot: Snapshot, requests: Sequence[OrderRequest],
                limits: RiskLimits,
                reservations: Mapping | None = None, *, now: datetime | None = None,
                evidence_currencies: Iterable[str] = (),
                need_stock_prices: bool | None = None,
                require_all_evidence: bool = False) -> PlanAssessment:
    """Sequentially assess candidate orders against a cumulative budget.

    Parameters
    ----------
    snapshot
        The ORIGINAL, immutable account snapshot.  It is NEVER modified or
        replaced: old obligations are not released by an early close or a stock
        sell.  Only one account/environment is ever considered.
    requests
        Ordered :class:`~thetamill.risk.OrderRequest` candidates.  Duplicate
        conIds are REJECTED (no double-spend of the same shares/cash/close).
        Quantities are never truncated or silently adjusted.
    limits
        A validated :class:`~thetamill.risk.RiskLimits`.
    reservations
        Existing aggregate local reservations (4 buckets); copied, never
        mutated.  Only ALLOWED requests add to it.
    now
        Aware datetime (injectable for deterministic tests).
    evidence_currencies
        Extra foreign currencies the candidates will introduce, for the shared
        evidence gate.  The candidates' own currencies are added automatically.
    need_stock_prices / require_all_evidence
        Passed to :func:`validate_snapshot_evidence`.

    Returns
    -------
    A :class:`PlanAssessment` with per-order :class:`PlannedOrder` decisions,
    the final aggregate reservations (use with :func:`risk.summarize`) and a
    JSON-friendly ``risk.summarize`` summary.  DATA ONLY — never execute.
    """
    moment = _now_utc(now)
    if not isinstance(limits, RiskLimits):
        raise PlanningError("limits must be a validated RiskLimits")
    if isinstance(requests, (str, bytes, Mapping)) or not isinstance(requests, Sequence):
        raise PlanningError("requests must be a sequence of OrderRequest")
    request_list = list(requests)
    for i, request in enumerate(request_list):
        if not isinstance(request, OrderRequest):
            raise PlanningError(f"requests[{i}] must be an OrderRequest")
        risk._validate_instrument(request.instrument, f"requests[{i}]")

    # Conservative input validation of the starting budget (no mutation).
    running = _normalize_reservations(reservations)

    # Shared evidence gate: one implementation for plan / roll / submission.
    candidate_currencies = {r.instrument.currency for r in request_list}
    extra = set(evidence_currencies) | candidate_currencies
    evidence_reasons = validate_snapshot_evidence(
        snapshot, limits, moment, needed_currencies=extra,
        need_stock_prices=need_stock_prices,
        require_all_evidence=require_all_evidence)
    # A snapshot-wide evidence failure blocks EVERY risk-increasing order; the
    # reasons are carried on each item so no adapter can miss them.
    evidence_block = bool(evidence_reasons)

    items: list[PlannedOrder] = []
    seen: set[int] = set()
    for index, request in enumerate(request_list):
        inst = request.instrument
        item_reasons: list[str] = []

        if inst.con_id in seen:
            items.append(PlannedOrder(
                index, None, None,
                (f"duplicate candidate conId {inst.con_id}: would double-spend "
                 "the same shares/cash/close capacity",)))
            continue
        seen.add(inst.con_id)

        if evidence_block:
            items.append(PlannedOrder(index, None, None, tuple(evidence_reasons)))
            continue

        # Sequential: the ORIGINAL snapshot + everything ALLOWED so far.
        try:
            decision = risk.assess(snapshot, request, limits, running, now=moment)
        except Exception as exc:  # fail closed on any risk-engine problem
            items.append(PlannedOrder(
                index, None, None,
                (f"risk assessment error ({type(exc).__name__}); blocked",)))
            continue

        if not decision.allowed:
            items.append(PlannedOrder(index, decision, None, ()))
            continue

        # A genuine ALLOW still needs an exact incremental requirement; if it
        # cannot be computed, fail closed rather than admitting a free order.
        try:
            requirement = _requirement(snapshot, request)
        except PlanningError as exc:
            items.append(PlannedOrder(index, decision, None, (str(exc),)))
            continue
        _add(running, requirement)
        items.append(PlannedOrder(index, decision, requirement, ()))

    summary = risk.summarize(snapshot, running, limits)
    return PlanAssessment(tuple(items), running, summary, limits, moment,
                          tuple(evidence_reasons))


__all__ = [
    "QUOTE_SOURCES", "DEFAULT_QUOTE_MAX_AGE", "DEFAULT_SNAPSHOT_MAX_AGE",
    "FUTURE_SKEW", "PlanningError", "quote_evidence_problem",
    "required_evidence_currencies", "validate_snapshot_evidence",
    "empty_reservations", "PlannedOrder", "PlanAssessment", "plan_document",
    "assess_plan",
]
