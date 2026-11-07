"""Stage E: recommendation-only roll planning. Pure engine, no side effects.

This module is the P4 first-version "建议" (recommendation) engine. It:

* consumes an immutable :class:`thetamill.broker_state.Snapshot` and caller-supplied
  candidate :class:`~thetamill.broker_state.Instrument` objects plus timestamped quotes;
* emits a **separate, versioned** ``thetamill.roll.v1`` document;
* never places, cancels or modifies orders, never connects to a broker, never reads or
  writes files, never starts a process and never runs automatically. It has no
  ``placeOrder``/``cancel``/``execute``/``run`` entry point at all;
* is deliberately NOT the ordinary ``plan`` schema, so ``run`` cannot mistake a roll
  file for an executable plan. ``validate_recommendation`` enforces that boundary.

The two legs remain independent (buy-to-close the old short, then sell-to-open the new
one). The old short's obligations are treated as **not released**: risk assessment is
called with the original, unmodified snapshot, and the net estimate is computed with each
contract's own multiplier (equal contract counts are never treated as an equal roll).

Deliverable policy (per-CONTRACT evidence): ``confirmed_deliverables`` is the set of
**option conIds** (NOT underlying conIds) for which the caller has separately verified,
from contract details / human review, that THIS SPECIFIC option contract settles into
plain stock only. Both the existing short's conId and EVERY candidate's conId must be
members; sharing an underlying conId, tradingClass or multiplier with a confirmed
contract conveys NO authorization. This is essential because an adjusted option on the
same underlying can carry a different cash/basket deliverable. Nothing is ever inferred
from ``trading_class`` or ``multiplier``.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from typing import Callable, Iterable, Sequence

from .broker_state import Instrument, Snapshot

SCHEMA = "thetamill.roll.v1"
RECOMMENDATION_ONLY = True
EXECUTABLE = False

# Small allowance for clock skew between the quote source and this host. Clearly-future
# timestamps (well beyond this) are still rejected.
_CLOCK_SKEW = timedelta(seconds=5)


class RollError(ValueError):
    """A top-level roll request is invalid or cannot be satisfied safely."""


# --------------------------------------------------------------------------- #
# Input value objects (all frozen / immutable)
# --------------------------------------------------------------------------- #

def _finite_decimal(value, label: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise RollError(f"{label}: expected a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise RollError(f"{label}: expected a finite number") from None
    if not result.is_finite() or (positive and result <= 0):
        raise RollError(f"{label}: expected a {'positive ' if positive else ''}finite number")
    return result


def _normalize_price(value, label: str) -> Decimal | None:
    """Return a usable, finite, non-negative price or None (missing, never fabricated)."""
    if value is None:
        return None
    return _finite_decimal(value, label)  # also rejects bool / NaN / Inf
    # note: a price of exactly 0 is a real, usable price (worthless option), allowed


@dataclass(frozen=True)
class Quote:
    """A timestamped two-sided quote. ``bid``/``ask`` may be None (price unknown)."""

    con_id: int
    bid: Decimal | None
    ask: Decimal | None
    as_of: datetime
    source: str = ""

    def __post_init__(self):
        if isinstance(self.con_id, bool) or type(self.con_id) is not int or self.con_id <= 0:
            raise RollError("Quote requires a positive integer conId")
        if not isinstance(self.as_of, datetime):
            raise RollError("Quote requires a datetime as_of")
        if not isinstance(self.source, str):
            raise RollError("Quote source must be a string")
        bid = _normalize_price(self.bid, "quote bid")
        ask = _normalize_price(self.ask, "quote ask")
        if bid is not None and bid < 0:
            raise RollError("quote bid must be non-negative")
        if ask is not None and ask < 0:
            raise RollError("quote ask must be non-negative")
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)


@dataclass(frozen=True)
class RollConstraints:
    """Business constraints. Invalid config fails closed (bool/NaN/Inf/negative)."""

    min_dte: int | None = None
    max_dte: int | None = None
    cost_floor: Decimal | None = None
    max_entry: Decimal | None = None
    allowed_symbols: frozenset = frozenset()   # universe whitelist; empty = no filter
    max_quote_age: timedelta = timedelta(minutes=5)
    valid_for: timedelta = timedelta(minutes=15)  # recommendation lifetime

    def __post_init__(self):
        for name in ("min_dte", "max_dte"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or type(value) is not int or value < 0):
                raise RollError(f"{name} must be a non-negative integer or None")
        if (self.min_dte is not None and self.max_dte is not None
                and self.min_dte > self.max_dte):
            raise RollError("min_dte cannot exceed max_dte")
        cost_floor = None
        if self.cost_floor is not None:
            cost_floor = _finite_decimal(self.cost_floor, "cost_floor")
        object.__setattr__(self, "cost_floor", cost_floor)
        max_entry = None
        if self.max_entry is not None:
            max_entry = _finite_decimal(self.max_entry, "max_entry", positive=True)
        object.__setattr__(self, "max_entry", max_entry)

        syms = self.allowed_symbols
        if syms is None:
            syms = frozenset()
        if isinstance(syms, str) or not isinstance(syms, (list, tuple, set, frozenset)):
            raise RollError("allowed_symbols must be a collection of symbol strings")
        normalized = set()
        for s in syms:
            if not isinstance(s, str) or not s.strip():
                raise RollError("allowed_symbols entries must be non-empty strings")
            normalized.add(s.strip().upper())
        object.__setattr__(self, "allowed_symbols", frozenset(normalized))

        for name in ("max_quote_age", "valid_for"):
            value = getattr(self, name)
            if not isinstance(value, timedelta) or value.total_seconds() <= 0:
                raise RollError(f"{name} must be a positive timedelta")


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #

def _s(value: Decimal | None):
    """Decimal -> canonical string, None -> None. Never emits NaN/Inf."""
    return str(value) if value is not None else None


def _mask_account(account: str) -> str:
    account = str(account)
    return "***" + account[-3:] if len(account) > 3 else "***"


def _expiry_date(value: str):
    return datetime.strptime(value, "%Y%m%d").date()


def _now_utc(now: datetime | None) -> datetime:
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        raise RollError("now must be a datetime")
    if now.tzinfo is None:
        raise RollError("now must include a timezone")
    return now.astimezone(timezone.utc)


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def instrument_data(inst: Instrument) -> dict:
    """Full, explicit identity (independently of report.py / cli.py)."""
    return {
        "con_id": inst.con_id,
        "underlying_id": inst.underlying_id,
        "symbol": inst.symbol,
        "currency": inst.currency,
        "sec_type": inst.sec_type,
        "exchange": inst.exchange,
        "multiplier": str(_finite_decimal(inst.multiplier, "multiplier", positive=True)),
        "right": inst.right,
        "strike": str(_finite_decimal(inst.strike, "strike", positive=True)),
        "expiry": inst.expiry,
        "trading_class": inst.trading_class,
    }


def _validate_instrument(inst) -> str | None:
    """Return a rejection reason, or None when the full identity is sound."""
    if not isinstance(inst, Instrument):
        return "candidate is not a broker_state.Instrument"
    if isinstance(inst.con_id, bool) or type(inst.con_id) is not int or inst.con_id <= 0:
        return "invalid conId"
    if inst.sec_type != "OPT":
        return "candidate is not an option"
    if not isinstance(inst.symbol, str) or not inst.symbol.strip():
        return "missing symbol"
    if not isinstance(inst.currency, str) or not inst.currency.strip():
        return "missing currency"
    if not isinstance(inst.exchange, str) or not inst.exchange.strip():
        return "missing exchange"
    if (isinstance(inst.underlying_id, bool) or type(inst.underlying_id) is not int
            or inst.underlying_id <= 0):
        return "invalid underlying conId"
    for field, label in (("multiplier", "multiplier"), ("strike", "strike")):
        try:
            _finite_decimal(getattr(inst, field), label, positive=True)
        except RollError as exc:
            return f"invalid {field}: {exc}"
    if inst.right not in {"C", "P"}:
        return "invalid option right"
    if not (isinstance(inst.expiry, str) and len(inst.expiry) == 8
            and inst.expiry.isascii() and inst.expiry.isdigit()):
        return "expiry must be YYYYMMDD"
    try:
        _expiry_date(inst.expiry)
    except ValueError:
        return "expiry is not a valid date"
    if not isinstance(inst.trading_class, str):
        return "invalid tradingClass"
    return None


def _finite_quantity(value, label: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise RollError(f"{label}: missing quantity")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise RollError(f"{label}: non-finite quantity") from None
    if not result.is_finite():
        raise RollError(f"{label}: non-finite quantity")
    return result


def _validate_quantity(quantity) -> int:
    """Only a real, positive Python int. Rejects bool, float, Decimal, fraction, str."""
    if isinstance(quantity, bool) or type(quantity) is not int or quantity <= 0:
        raise RollError("quantity must be a positive finite integer (bool/fraction rejected)")
    return quantity


def _resolve_short(snapshot: Snapshot, con_id: int, qty: int) -> Instrument:
    """Net short on the exact conId, less pending BUY-to-close remaining. Fail closed."""
    matching = [p for p in snapshot.positions
                if isinstance(p.instrument, Instrument)
                and p.instrument.con_id == con_id
                and p.instrument.sec_type == "OPT"]
    if not matching:
        raise RollError(f"no existing option short for conId={con_id}")
    old = matching[0].instrument
    reason = _validate_instrument(old)
    if reason:
        raise RollError(f"existing short has incomplete identity: {reason}")

    short = Decimal(0)
    for p in matching:
        short += _finite_quantity(p.quantity, "position quantity")
    if short >= 0:
        raise RollError(f"conId={con_id} is not a net short (net quantity {short})")
    if short != short.to_integral_value():
        raise RollError("existing short quantity is not an integer number of contracts")

    pending_buy = Decimal(0)
    for o in snapshot.orders:
        if (isinstance(o.instrument, Instrument)
                and o.instrument.con_id == con_id
                and o.instrument.sec_type == "OPT"
                and str(o.action).upper() == "BUY"):
            pending_buy += _finite_quantity(o.remaining, "pending BUY remaining")
    if pending_buy < 0:
        raise RollError("negative pending BUY remaining")
    available = -short - pending_buy
    if available < qty:
        raise RollError(
            f"insufficient closable quantity: net short {-short} minus pending BUY "
            f"{pending_buy} leaves {available}, requested {qty}")
    return old


def _quote_freshness(q: Quote, now_utc: datetime, constraints: RollConstraints) -> str | None:
    if q.as_of.tzinfo is None:
        return "quote timestamp has no timezone"
    as_of = q.as_of.astimezone(timezone.utc)
    delta = as_of - now_utc
    if delta > _CLOCK_SKEW:
        return "quote timestamp is in the future"
    if -delta > constraints.max_quote_age:
        return "quote is stale/expired (older than max_quote_age)"
    return None


def _close_price(q: Quote | None, con_id: int, now_utc: datetime,
                 constraints: RollConstraints) -> tuple[Decimal | None, list[str]]:
    """Buy-to-close uses the OLD contract's ask. Missing/unusable -> None + warning."""
    if q is None:
        return None, [f"no timestamped close quote for old short conId={con_id}; net unknown"]
    if q.con_id != con_id:
        return None, ["close quote conId mismatch; net unknown"]
    fresh = _quote_freshness(q, now_utc, constraints)
    if fresh:
        return None, [f"close quote unusable ({fresh}); net unknown"]
    if q.ask is None:
        return None, ["close ask missing; net unknown (not fabricated)"]
    return q.ask, []


def _net_estimate(old: Instrument, cand: Instrument, qty: int,
                  close_ask: Decimal | None, open_bid: Decimal | None) -> dict:
    """Cash estimate, each leg by its OWN multiplier. Missing price -> null, never faked."""
    old_mult = _finite_decimal(old.multiplier, "multiplier", positive=True)
    new_mult = _finite_decimal(cand.multiplier, "multiplier", positive=True)
    n = Decimal(qty)
    close_amount = close_ask * old_mult * n if close_ask is not None else None
    open_amount = open_bid * new_mult * n if open_bid is not None else None
    if close_amount is not None and open_amount is not None:
        net = open_amount - close_amount
    else:
        net = None
    equivalent = old_mult == new_mult
    notes = [
        "fees/commissions unknown: net is NOT net of any fee",
        "two independent legs (buy-to-close, then sell-to-open); no atomicity",
    ]
    if not equivalent:
        notes.append(
            f"different multipliers (old {old_mult} / new {new_mult}): "
            "each obligation measured separately; equal contract count is NOT an equal roll")
    return {
        "close_ask": _s(close_ask),
        "open_bid": _s(open_bid),
        "close_amount": _s(close_amount),
        "open_amount": _s(open_amount),
        "net": _s(net),
        "currency": cand.currency.upper(),
        "fees": None,
        "fees_unknown": True,
        "delivery_equivalent": equivalent,
        "notes": notes,
    }


def _quote_data(q: Quote | None) -> dict | None:
    if q is None:
        return None
    return {"con_id": q.con_id, "bid": _s(q.bid), "ask": _s(q.ask),
            "as_of": q.as_of.isoformat(), "source": q.source}


def _safe_assess(assess, snapshot: Snapshot, cand: Instrument, qty: int) -> tuple[bool, str]:
    try:
        result = assess(snapshot, cand, qty)
    except Exception as exc:  # fail closed on any risk-engine problem
        return False, f"risk assessment raised {type(exc).__name__}; conservatively blocked"
    if not (isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool)):
        return False, "invalid risk assessment result; conservatively blocked"
    ok, reason = result
    if ok:
        return True, ""
    return False, str(reason) if reason else "no reason supplied"


# --------------------------------------------------------------------------- #
# Main pure engine
# --------------------------------------------------------------------------- #

def build_roll_recommendation(
    snapshot: Snapshot, *,
    con_id: int,
    quantity: int,
    candidates: Sequence[Instrument],
    quotes: Mapping[int, Quote],
    constraints: RollConstraints,
    confirmed_deliverables: Iterable[int],
    assess: Callable[[Snapshot, Instrument, int], tuple[bool, str]],
    now: datetime | None = None,
) -> dict:
    """Build a recommendation-only roll document. Pure: no broker, IO, network, or orders.

    ``assess(snapshot, candidate, quantity) -> (bool, reason)`` is called with the
    ORIGINAL snapshot (old short obligations NOT released) for the conservative
    new-open assessment.

    ``confirmed_deliverables`` is the caller's explicit set of **OPTION conIds**
    (NOT underlying conIds), each one independently verified as plain-stock-only
    delivery. The existing short's conId AND every candidate's conId must appear;
    sharing an underlying with a confirmed contract grants no authorization, because
    an adjusted contract on that same underlying can settle into cash/basket. Delivery
    is never inferred from ``trading_class`` or ``multiplier``.
    """
    now_utc = _now_utc(now)

    # ---- top-level validation (invalid request -> RollError) ---------------- #
    if not isinstance(snapshot, Snapshot):
        raise RollError("snapshot must be a broker_state.Snapshot")
    if isinstance(con_id, bool) or type(con_id) is not int or con_id <= 0:
        raise RollError("conId must be a positive integer")
    qty = _validate_quantity(quantity)
    if isinstance(candidates, (str, bytes, Mapping)) or not hasattr(candidates, "__iter__"):
        raise RollError("candidates must be a sequence of Instruments")
    candidates = list(candidates)
    if not isinstance(quotes, Mapping):
        raise RollError("quotes must be a mapping of conId -> Quote")
    for key, val in quotes.items():
        if isinstance(key, bool) or type(key) is not int or key <= 0:
            raise RollError("quotes keys must be positive integer conIds")
        if val is not None and not isinstance(val, Quote):
            raise RollError("quotes values must be Quote instances")
    if not isinstance(constraints, RollConstraints):
        raise RollError("constraints must be a RollConstraints")
    if not callable(assess):
        raise RollError("assess must be callable")
    if isinstance(confirmed_deliverables, (str, bytes, Mapping)) or not hasattr(
            confirmed_deliverables, "__iter__"):
        raise RollError("confirmed_deliverables must be an iterable of OPTION conIds")
    deliverables = set()
    for d in confirmed_deliverables:
        if isinstance(d, bool) or type(d) is not int or d <= 0:
            raise RollError("confirmed_deliverables must be positive integer OPTION conIds")
        deliverables.add(d)
    deliverables = frozenset(deliverables)

    old = _resolve_short(snapshot, con_id, qty)
    # The close leg itself needs per-contract evidence. Confirming a candidate, or the
    # shared underlying, does NOT establish the old short's deliverable structure.
    if con_id not in deliverables:
        raise RollError(
            f"existing short option conId={con_id} is not in confirmed_deliverables: "
            "per-contract stock-only evidence is required for the close leg (do not "
            "authorize it by underlying conId, tradingClass or multiplier)")
    close_ask, close_warnings = _close_price(quotes.get(con_id), con_id, now_utc, constraints)
    issues = list(close_warnings)

    created_at = now_utc
    expires_at = now_utc + constraints.valid_for

    doc = {
        "schema": SCHEMA,
        "kind": "roll_recommendation",
        "recommendation_only": RECOMMENDATION_ONLY,
        "executable": EXECUTABLE,
        "disclaimer": (
            "仅供人工复核的展期建议；非投资建议。本文件不可执行：不会下单/撤单，"
            "也不得被 run 当作普通 plan 执行。"),
        "account": _mask_account(snapshot.account),
        "environment": snapshot.environment,
        "base_currency": snapshot.base_currency,
        "snapshot_captured_at": snapshot.captured_at.isoformat(),
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "valid_for_seconds": int(constraints.valid_for.total_seconds()),
        "short_con_id": con_id,
        "close_leg": {
            "action": "BUY",                      # buy-to-close the old short
            "quantity": str(qty),
            "instrument": instrument_data(old),
            "multiplier": str(_finite_decimal(old.multiplier, "multiplier", positive=True)),
        },
        "open_action": "SELL",                    # sell-to-open the new short
        "new_open_quantity": str(qty),
        "risk_assessment": {
            "basis": "original_snapshot",
            "old_obligations_released": False,
            "note": ("新开仓风控基于原始快照评估；旧仓义务尚未释放，"
                     "不因在途买回提前释放担保或额度。"),
        },
        "constraints": {
            "min_dte": constraints.min_dte,
            "max_dte": constraints.max_dte,
            "cost_floor": _s(constraints.cost_floor),
            "max_entry": _s(constraints.max_entry),
            "allowed_symbols": sorted(constraints.allowed_symbols),
            "max_quote_age_seconds": int(constraints.max_quote_age.total_seconds()),
        },
        # confirmed_deliverables is a set of OPTION conIds with independently verified
        # stock-only delivery. Never reuse an underlying conId as evidence for a
        # different option (an adjusted contract may settle into cash/basket).
        "confirmed_deliverables": sorted(deliverables),
        "confirmed_deliverables_scope": "option_conIds_per_contract",
        "deliverable_policy": (
            "stock-only delivery confirmed per OPTION conId for the old short and each "
            "candidate independently; never inferred from underlying conId, "
            "tradingClass or multiplier"),
        "close_quote": _quote_data(quotes.get(con_id)),
        "fees": None,
        "fees_note": "手续费/佣金/行权费用未知；净额估算未扣除任何费用。",
        "candidates": [],
        "recommended_count": 0,
        "rejected_count": 0,
        "issues": [],
        "assumptions": [
            "旧仓买回按旧合约 ask 估算现金流出；新仓卖出按新合约 bid 估算现金流入。",
            "两腿按各自乘数计量义务；张数相同不等于等额展期。",
            "股票交割结构按每个 OPTION conId 单独确认（旧仓与每个候选各自独立），"
            "不按 underlying conId、tradingClass 或乘数推断；同一标的的调整合约不自动获权。",
            "建议不代表成交价；实盘价格、费用与担保以券商为准。",
        ],
    }

    seen: set[int] = set()
    for cand in candidates:
        # A non-Instrument candidate gets a minimal, fully-rejected row.
        if not isinstance(cand, Instrument):
            doc["candidates"].append({
                "con_id": None, "symbol": None, "expiry": None,
                "status": "rejected", "reasons": ["candidate is not a broker_state.Instrument"],
                "instrument": None, "quantity": str(qty), "multiplier": None,
                "days_to_expiry": None, "quote": None, "net_estimate": None,
            })
            continue

        reasons: list[str] = []
        ident = _validate_instrument(cand)
        dte = None
        open_bid: Decimal | None = None
        net_estimate = None

        if ident:
            reasons.append(ident)
        else:
            if cand.con_id in seen:
                reasons.append("duplicate candidate conId")
            seen.add(cand.con_id)
            if cand.con_id == con_id:
                reasons.append("candidate is the same contract as the existing short")
            # ---- same confirmed underlying / currency / right ------------- #
            if cand.underlying_id != old.underlying_id:
                reasons.append("underlying conId does not match the existing short")
            if cand.currency.upper() != old.currency.upper():
                reasons.append("currency does not match the existing short")
            if cand.right != old.right:
                reasons.append("option right does not match the existing short")
            if cand.symbol.upper() != old.symbol.upper():
                reasons.append("symbol does not match the existing short")
            # ---- later expiry + DTE window -------------------------------- #
            cand_date, old_date = _expiry_date(cand.expiry), _expiry_date(old.expiry)
            if cand_date <= old_date:
                reasons.append("candidate expiry is not later than the existing short")
            if cand_date <= now_utc.date():
                reasons.append("candidate contract already expired")
            else:
                dte = (cand_date - now_utc.date()).days
                if constraints.min_dte is not None and dte < constraints.min_dte:
                    reasons.append(f"DTE {dte} below min_dte {constraints.min_dte}")
                if constraints.max_dte is not None and dte > constraints.max_dte:
                    reasons.append(f"DTE {dte} above max_dte {constraints.max_dte}")
            # ---- explicit, per-contract confirmed stock-only deliverable --- #
            # Must be THIS candidate's own OPTION conId. An adjusted contract can
            # share the underlying conId, symbol, tradingClass and multiplier yet
            # settle into cash/basket; sharing those fields grants no authorization.
            if cand.con_id not in deliverables:
                reasons.append(
                    "deliverable not confirmed for this option conId by caller "
                    "(stock-only only; per-CONTRACT evidence required; never inferred "
                    "from underlying conId/tradingClass/multiplier)")
            # ---- fresh, timestamped quote for the open leg ----------------- #
            q = quotes.get(cand.con_id)
            if q is None:
                reasons.append("missing timestamped quote for candidate")
            elif q.con_id != cand.con_id:
                reasons.append("candidate quote conId mismatch")
            else:
                fresh = _quote_freshness(q, now_utc, constraints)
                if fresh:
                    reasons.append(f"quote unusable: {fresh}")
                else:
                    open_bid = q.bid  # may be None -> net becomes null, no fake return
            # ---- universe whitelist --------------------------------------- #
            if (constraints.allowed_symbols
                    and cand.symbol.upper() not in constraints.allowed_symbols):
                reasons.append("symbol not in the approved universe whitelist")
            # ---- max entry (assignment / cost price) ---------------------- #
            cand_strike = _finite_decimal(cand.strike, "strike", positive=True)
            if constraints.max_entry is not None and cand_strike > constraints.max_entry:
                reasons.append("strike exceeds max_entry (willingness-to-own / cost ceiling)")
            # ---- net estimate + cost floor -------------------------------- #
            net_estimate = _net_estimate(old, cand, qty, close_ask, open_bid)
            if constraints.cost_floor is not None:
                if net_estimate["net"] is None:
                    reasons.append("cannot verify cost_floor: net unknown (missing price, not fabricated)")
                elif Decimal(net_estimate["net"]) < constraints.cost_floor:
                    reasons.append("net estimate below cost_floor")

        # ---- conservative new-open risk assessment (last gate) ------------ #
        # Called only for otherwise-viable candidates, with the ORIGINAL snapshot.
        if not reasons:
            ok, why = _safe_assess(assess, snapshot, cand, qty)
            if not ok:
                reasons.append(f"risk assessment blocked: {why}")

        status = "rejected" if reasons else "recommended"
        q = quotes.get(cand.con_id) if ident is None else None
        doc["candidates"].append({
            "con_id": cand.con_id,
            "symbol": cand.symbol,
            "expiry": cand.expiry,
            "status": status,
            "reasons": list(reasons),
            "instrument": instrument_data(cand) if ident is None else None,
            "quantity": str(qty),
            "multiplier": str(_finite_decimal(cand.multiplier, "multiplier", positive=True))
            if ident is None else None,
            "days_to_expiry": dte,
            "quote": _quote_data(q) if isinstance(q, Quote) else None,
            "net_estimate": net_estimate if status == "recommended" else None,
        })

    doc["recommended_count"] = sum(1 for c in doc["candidates"] if c["status"] == "recommended")
    doc["rejected_count"] = sum(1 for c in doc["candidates"] if c["status"] == "rejected")
    if doc["recommended_count"] == 0:
        issues.append("no candidate passed every filter; constraints are NOT relaxed automatically")
    doc["issues"] = list(dict.fromkeys(issues))
    return doc


# --------------------------------------------------------------------------- #
# Validation + rendering (data only; nothing here can execute an order)
# --------------------------------------------------------------------------- #

def validate_recommendation(doc, *, now: datetime | None = None) -> tuple[bool, list[str]]:
    """Validate schema/lifetime. A valid result is still DATA ONLY and cannot execute.

    Detects wrong schema (e.g. an ordinary ``plan`` file, so ``run`` must not consume
    roll files), expired recommendations, and any doc claiming to be executable.
    """
    problems: list[str] = []
    if not isinstance(doc, Mapping):
        return False, ["recommendation is not a mapping"]
    if doc.get("schema") != SCHEMA:
        return False, [
            f"schema mismatch (expected {SCHEMA}): this is not a roll recommendation "
            "and must not be executed by run"]
    if doc.get("kind") != "roll_recommendation":
        problems.append("kind mismatch")
    if doc.get("recommendation_only") is not True:
        problems.append("recommendation_only must be true (a recommendation cannot execute)")
    if doc.get("executable") is not False:
        problems.append("executable must be false (no placeOrder/cancel/auto-run capability)")
    if any(key in doc for key in ("items", "placeOrder", "orders_to_place", "submit")):
        problems.append("document carries executable order fields; roll recommendations carry none")

    created = _parse_iso(doc.get("created_at"))
    expires = _parse_iso(doc.get("expires_at"))
    for field, parsed in (("created_at", created), ("expires_at", expires)):
        if parsed is None or parsed.tzinfo is None:
            problems.append(f"{field} is missing or not an aware ISO timestamp")
    if created and expires and created.tzinfo and expires.tzinfo and expires <= created:
        problems.append("expires_at must be after created_at")

    now_utc = _now_utc(now)
    if expires is not None and expires.tzinfo is not None:
        if now_utc >= expires.astimezone(timezone.utc):
            problems.append("recommendation has expired; regenerate before acting (do not act on it)")

    return (not problems), problems


def render_roll_recommendation(doc: Mapping, fmt: str = "terminal") -> str:
    """Render as JSON or a human table. Returns a string; no file/network side effects."""
    if fmt == "json":
        return json.dumps(dict(doc), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if fmt not in {"terminal", "markdown"}:
        raise RollError("format must be terminal, json or markdown")

    def cell(value) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    lines = [
        "# ThetaMill 展期建议（仅供人工复核，不可执行）", "",
        cell(doc.get("disclaimer", "")), "",
        f"账户：{cell(doc.get('account') or 'N/A')} | 环境：{cell(doc.get('environment') or 'N/A')}",
        f"快照时间：{cell(doc.get('snapshot_captured_at') or 'N/A')}",
        f"生成时间(UTC)：{cell(doc.get('created_at') or 'N/A')}",
        f"失效时间(UTC)：{cell(doc.get('expires_at') or 'N/A')}",
        f"建议张数：{cell(doc.get('new_open_quantity') or 'N/A')} | "
        f"schema：{cell(doc.get('schema') or 'N/A')}",
        "", "## 旧仓买回 / 新仓卖出（两步独立，不承诺原子性）",
    ]
    close = doc.get("close_leg") or {}
    ci = (close.get("instrument") or {})
    lines.append(
        f"- 买回旧仓：{cell(ci.get('symbol') or '?')} conId={cell(ci.get('con_id') or '?')} "
        f"mult={cell(ci.get('multiplier') or '?')} expiry={cell(ci.get('expiry') or '?')} "
        f"× {cell(close.get('quantity') or '?')}")
    lines.append(f"- 新开仓动作：{cell(doc.get('open_action') or 'SELL')}（见下表候选）")
    lines.append("- 手续费：未知；净额估算未扣除任何费用。")
    lines.extend(["", "## 候选", "| conId | 标的 | 到期 | 乘数 | DTE | 状态 | 净额/币种 | 过滤原因 |",
                  "|---|---|---|---:|---:|---|---|---|"])
    for c in doc.get("candidates", []):
        inst = c.get("instrument") or {}
        estimate = c.get("net_estimate")
        net = (f"{estimate['net']} {estimate['currency']}"
               if estimate and estimate.get("net") is not None
               else "N/A（缺价，不编造）")
        lines.append(
            f"| {cell(c.get('con_id'))} | {cell(c.get('symbol'))} | {cell(c.get('expiry'))} "
            f"| {cell(c.get('multiplier') or 'N/A')} | {cell(c.get('days_to_expiry') or 'N/A')} "
            f"| {cell(c.get('status'))} | {cell(net)} | {cell('; '.join(c.get('reasons', [])) or '-')} |")
    lines.extend(["", "## 约束与数据质量"])
    lines.append(f"- 已确认股票交割的 OPTION conId（逐合约，非 underlying conId）："
                 f"{cell(', '.join(map(str, doc.get('confirmed_deliverables', []))) or '无')}")
    for issue in doc.get("issues", []):
        lines.append(f"- {cell(issue)}")
    lines.append("- 风控基于原始快照，旧仓义务未释放；不自动放宽约束、不自动执行。")
    return "\n".join(lines) + "\n"
