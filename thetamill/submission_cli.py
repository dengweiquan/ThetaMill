"""Shared CLI submission adapter: prepare canonical legs and run the engine.

This is the ONE bridge between the argparse/CLI world (``cli.py``, owned by
main) and the offline execution engine (``execution.py``, ``state.py``,
``risk.py``).  Main passes a sequence of ``(IB contract, IB order)`` pairs; this
module converts them into canonical :class:`~thetamill.execution.PreparedLeg`
objects using the caller's *actual* confirmed contract identity, then runs the
engine's ``preflight`` / ``confirm`` protocol.

Design boundaries (deliberate, do not weaken)
---------------------------------------------
* It NEVER calls a legacy ``_code`` / ``_consume_code`` path and NEVER calls
  ``ib.placeOrder`` directly: the engine's ``place_order`` callback (supplied
  here as :func:`_broker_submit`) is the ONLY network boundary, and the engine
  persists a stable ``orderRef`` BEFORE that callback runs.
* Confirmation requires BOTH an explicit ``--intent`` and ``--code``.  Neither
  alone does anything; with neither, a fresh ``preflight`` is produced.  There
  is no ``--force`` and no fabricated ``verified_prior_submissions``.
* Deliverable evidence is loaded via ``roll_cli.load_delivery_evidence``
  (lazy import; fail closed if unavailable).  STK is intrinsically stock;
  EVERY OPT (including a read-only buy-to-close) needs its OWN option conId in
  the evidence set.  If the engine currently requires ``deliverable_verified``
  on every leg and an OPT leg lacks evidence, the leg is BLOCKED — never
  bypassed and never auto-verified.
* The ledger lives at ``cli._data_dir()/state.sqlite3``.  A brand-new database
  is NOT presented as zero submission history: legacy consumed codes / audit
  history are imported read-only via the state API, and an enabled-P3 fresh
  ledger is left to the engine's conservative ``MigrationFreeze``.
* The broker callback builds an IB ``Order`` from the canonical leg and, because
  the engine treats the contract as opaque, verifies the broker contract's FULL
  identity equals the leg's confirmed :class:`Instrument` before sending.
* ``fill_proof`` proves an actual ``Filled`` status AND a finite, EXACT filled
  quantity equal to the approved leg quantity, matched by the stable orderRef.
  Generic positive statuses / partial fills / cancellations are NOT proof.
  Unknown receipts are terminal — never retried.

No real broker, network, config file, git or process is touched by import; the
CLI supplies ``ib``, ``cfg`` and ``args``.  Tests use mocks + temp DB only.
"""
from __future__ import annotations

import json
from contextlib import nullcontext as _nullcontext
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from thetamill import cli
from thetamill import execution, planning, risk, state
from thetamill.broker_state import FxQuote, Instrument, PriceQuote, Snapshot, SnapshotError

# --------------------------------------------------------------------------- #
# Return codes (CLI-facing)
# --------------------------------------------------------------------------- #
RC_PREFLIGHT = 2      # a fresh review intent was produced (or confirmation blocked)
RC_CONFIRMED = 0      # every attempted leg mapped to a positive broker state
RC_UNKNOWN = 3        # a send may have happened but was not positively mapped
RC_SKIPPED = 1        # deferred leg(s) skipped: no positive filled proof


class SubmissionCliError(Exception):
    """Fail-closed adapter error (never degrades to an unsafe submission)."""


@dataclass(frozen=True)
class ApprovedLeg:
    """Identity of one approved dependency leg, keyed by its stable orderRef.

    ``fill_proof`` surfaces these as a structured ``execution.FillProof`` so the
    engine can re-verify account/environment/conId/stableRef/exact quantity
    against the APPROVED leg rather than trusting a bare status string.
    """

    quantity: Decimal
    account: str
    environment: str
    con_id: int


def _finite(value, label: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise SubmissionCliError(f"{label}: expected a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise SubmissionCliError(f"{label}: expected a finite number") from None
    if not result.is_finite() or (positive and result <= 0):
        raise SubmissionCliError(f"{label}: expected a {'positive ' if positive else ''}finite number")
    return result


def _mask(account: str) -> str:
    account = str(account)
    return "***" + account[-3:] if len(account) > 3 else "***"


def _broker_silence():
    """Suppress raw broker stdout/stderr (account ids leak there).

    Uses main's ``cli._private_broker_diagnostics`` context manager when
    available.  It is a PRIVACY wrapper only: its absence never relaxes any
    gate, and every exception raised inside still propagates (fail closed).
    """
    privacy = getattr(cli, "_private_broker_diagnostics", None)
    return privacy() if callable(privacy) else _nullcontext()


# --------------------------------------------------------------------------- #
# Canonical identity: the caller's ACTUAL contract, never guessed
# --------------------------------------------------------------------------- #

# Fields that must match between the broker contract and the confirmed
# Instrument.  For OPT, exchange is sometimes normalised by the broker, so we
# compare the economically load-bearing identity fields strictly.
_IDENTITY_FIELDS = ("conId", "secType", "symbol", "currency", "right",
                    "strike", "multiplier", "lastTradeDateOrContractMonth",
                    "tradingClass")


def contract_identity(contract: Any) -> Mapping[str, Any]:
    """Read the FULL identity from an IB-like contract object (no inference)."""
    def norm(value):
        if value is None:
            return None
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            return _finite(value, "contract identity", positive=False)
        if isinstance(value, str):
            text = value.strip()
            # Real IB contracts carry multiplier / strike as NUMERIC STRINGS
            # (e.g. "100", "200", "500", "180.5").  Normalise to Decimal so a
            # string "100" compares equal to the confirmed Decimal(100); never
            # reject a legitimate contract just because its multiplier is a str.
            try:
                return Decimal(text)
            except (InvalidOperation, ValueError):
                return text.upper()
        return value

    return {
        "conId": getattr(contract, "conId", None),
        "secType": str(getattr(contract, "secType", "") or "").strip().upper(),
        "symbol": str(getattr(contract, "symbol", "") or "").strip().upper(),
        "currency": str(getattr(contract, "currency", "") or "").strip().upper(),
        "right": str(getattr(contract, "right", "") or "").strip().upper(),
        "strike": norm(getattr(contract, "strike", None)),
        "multiplier": norm(getattr(contract, "multiplier", None)),
        "lastTradeDateOrContractMonth": str(
            getattr(contract, "lastTradeDateOrContractMonth", "") or "").strip(),
        "tradingClass": str(getattr(contract, "tradingClass", "") or "").strip().upper(),
        "exchange": str(getattr(contract, "exchange", "") or "").strip().upper(),
    }


def verify_contract_identity(contract: Any, instrument: Instrument) -> list[str]:
    """Return a list of mismatches; empty means the FULL identity matches.

    The engine never introspects the opaque contract.  This adapter therefore
    does it, so a mismatched/legacy/wrong contract can never be sent under a
    confirmed Instrument's review digest.
    """
    c = contract_identity(contract)
    problems: list[str] = []
    con_id = c["conId"]
    if isinstance(con_id, bool) or type(con_id) is not int or con_id != instrument.con_id:
        problems.append(f"conId {con_id!r} != {instrument.con_id}")
    if c["secType"] != instrument.sec_type.upper():
        problems.append("secType mismatch")
    if c["symbol"] != instrument.symbol.upper():
        problems.append("symbol mismatch")
    if c["currency"] != instrument.currency.upper():
        problems.append("currency mismatch")
    if c["exchange"] != instrument.exchange.upper():
        problems.append("exchange mismatch")
    if instrument.sec_type == "OPT":
        if c["right"] != instrument.right.upper():
            problems.append("right mismatch")
        if c["tradingClass"] != instrument.trading_class.upper():
            problems.append("tradingClass mismatch")
        if c["lastTradeDateOrContractMonth"] != instrument.expiry:
            problems.append("expiry mismatch")
        for name, expected in (("strike", instrument.strike),
                               ("multiplier", instrument.multiplier)):
            value = c[name]
            if not isinstance(value, Decimal) or value != _finite(expected, name):
                problems.append(f"{name} mismatch ({value!r} != {expected})")
    return problems


def canonical_instrument(contract: Any, ib: Any, account: str) -> Instrument:
    """Build a confirmed Instrument from the ACTUAL contract via broker_state.

    Uses :func:`~thetamill.broker_state._instrument` (contract-details-backed
    underlying identity) — NEVER guessed from symbol/tradingClass.  This is the
    same canonical identity the pure risk layer and the engine digest consume.
    """
    from thetamill.broker_state import _instrument
    try:
        # broker_state calls ib.reqContractDetails here; its diagnostics can
        # contain the raw account id, so suppress them (privacy only).
        with _broker_silence():
            instrument = _instrument(contract, ib, {})
    except SnapshotError as exc:
        raise SubmissionCliError(f"cannot confirm contract identity: {exc}") from exc
    if not isinstance(instrument, Instrument):
        raise SubmissionCliError("contract identity could not be confirmed")
    return instrument


# --------------------------------------------------------------------------- #
# PreparedLeg construction from a (contract, order) pair
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LegSpec:
    """One caller-supplied (contract, order) pair plus confirmation metadata."""

    contract: Any
    order: Any
    instrument: Instrument
    strategy: str = ""
    deliverable_verified: bool = False
    coverage_deferred: bool = False
    requires_filled_leg: Optional[int] = None
    canonical_extra: Mapping[str, Any] = None  # extra fields bound to digest

    def to_prepared(self, account: str) -> "execution.PreparedLeg":
        order = self.order
        action = str(getattr(order, "action", "") or "").upper()
        quantity = _finite(getattr(order, "totalQuantity", None), "totalQuantity", positive=True)
        if self.instrument.sec_type == "OPT" and quantity != quantity.to_integral_value():
            raise SubmissionCliError("option quantity must be a whole number")
        order_type = str(getattr(order, "orderType", "") or "").upper()
        limit = getattr(order, "lmtPrice", None)
        stop = getattr(order, "auxPrice", None)
        limit_d = _finite(limit, "lmtPrice", positive=True) if limit not in (None, 0, "") else None
        stop_d = _finite(stop, "auxPrice", positive=True) if stop not in (None, 0, "") else None
        extra = dict(self.canonical_extra or {})
        # Bind the exact confirmed identity into the digest so a changed contract
        # cannot be replayed under the same review code.
        extra["identity"] = {
            "con_id": self.instrument.con_id,
            "underlying_id": self.instrument.underlying_id,
            "multiplier": str(self.instrument.multiplier),
            "right": self.instrument.right,
            "strike": str(self.instrument.strike),
            "expiry": self.instrument.expiry,
            "trading_class": self.instrument.trading_class,
        }
        return execution.PreparedLeg(
            instrument=self.instrument, action=action, quantity=quantity,
            order_type=order_type, contract=self.contract,
            limit_price=limit_d, stop_price=stop_d,
            tif=str(getattr(order, "tif", "DAY") or "DAY"),
            exchange=str(self.instrument.exchange or ""),
            outside_rth=bool(getattr(order, "outsideRth", False)),
            strategy=self.strategy or str(getattr(order, "strategy", "") or ""),
            account=account, canonical=extra,
            deliverable_verified=bool(self.deliverable_verified),
            coverage_deferred=bool(self.coverage_deferred),
            requires_filled_leg=self.requires_filled_leg,
        )


def build_leg_specs(pairs: Sequence[tuple], *, ib: Any, account: str,
                    strategy: str = "",
                    requires_filled: Iterable[int] = (),
                    delivery_evidence: Optional[Mapping[int, Any]] = None,
                    evidence_now: Optional[datetime] = None) -> list[LegSpec]:
    """Convert caller (contract, order) pairs into canonical LegSpec objects.

    ``delivery_evidence`` is a mapping keyed by OPTION conId (the output of
    ``roll_cli.load_delivery_evidence``).  STK is intrinsically stock; EVERY
    OPT leg — including a buy-to-close — must have its OWN conId in that set.
    A missing OPT evidence does NOT auto-verify: it is left False so the engine
    blocks the leg safely.

    ``requires_filled`` marks indices of legs whose coverage is deferred until a
    prior leg actually fills (a covered two-leg combo).  Such a leg must point
    at an EARLIER index via ``requires_filled_leg``.
    """
    if not isinstance(pairs, Sequence) or isinstance(pairs, (str, bytes)) or not pairs:
        raise SubmissionCliError("a non-empty sequence of (contract, order) pairs is required")
    filled_set = set(requires_filled or ())
    for i in filled_set:
        if isinstance(i, bool) or type(i) is not int or i < 0 or i >= len(pairs):
            raise SubmissionCliError(f"requires_filled index out of range: {i}")
    evidence = delivery_evidence or {}
    now = evidence_now or datetime.now(timezone.utc)

    specs: list[LegSpec] = []
    for index, item in enumerate(pairs):
        if not (isinstance(item, (tuple, list)) and len(item) == 2):
            raise SubmissionCliError(f"pair {index} must be (contract, order)")
        contract, order = item
        instrument = canonical_instrument(contract, ib, account)
        mismatches = verify_contract_identity(contract, instrument)
        if mismatches:
            # Double-check: the identity above was built FROM this contract, so
            # a mismatch here would mean an internally inconsistent contract.
            raise SubmissionCliError(
                f"leg {index} contract identity is inconsistent: " + "; ".join(mismatches))

        if instrument.sec_type == "STK":
            verified = True  # stock delivery is intrinsic, not inferred
        elif instrument.sec_type == "OPT":
            # Per-OPTION-conId evidence. NEVER infer from underlying conId,
            # tradingClass, multiplier, or from a sibling standard contract.
            verified = instrument.con_id in evidence
            if verified:
                entry = evidence[instrument.con_id]
                # Evidence may carry an expiry; honor it conservatively.
                if isinstance(entry, Mapping):
                    until = entry.get("valid_until") or entry.get("expires_at")
                    if until:
                        try:
                            expiry = datetime.fromisoformat(str(until))
                            if expiry.tzinfo is None:
                                expiry = expiry.replace(tzinfo=timezone.utc)
                            if now.astimezone(timezone.utc) >= expiry.astimezone(timezone.utc):
                                verified = False
                        except ValueError:
                            verified = False
        else:
            verified = False
        deferred = index in filled_set
        prior = None
        if deferred:
            # The stock leg is the immediate preceding leg by convention; the
            # caller can override via LegSpec if different. For the covered
            # two-leg case, index 1 (call) requires index 0 (stock).
            prior = index - 1 if index > 0 else None
            if prior is None or prior in filled_set:
                raise SubmissionCliError(
                    f"deferred leg {index} has no valid earlier stock leg to require")
        specs.append(LegSpec(
            contract=contract, order=order, instrument=instrument,
            strategy=strategy, deliverable_verified=verified,
            coverage_deferred=deferred, requires_filled_leg=prior,
        ))
    return specs


# --------------------------------------------------------------------------- #
# Delivery evidence (lazy import of the risk owner's loader)
# --------------------------------------------------------------------------- #

def load_delivery_evidence(path, instruments: Sequence[Instrument],
                           now: Optional[datetime] = None) -> Mapping[int, Any]:
    """Load explicitly-confirmed stock-only OPTION conIds, keyed by OPTION conId.

    Delegates to ``roll_cli.load_delivery_evidence`` (lazy import).  If the
    loader is unavailable or rejects the evidence, we FAIL CLOSED by returning
    an empty mapping (so all OPT legs stay unverified and the engine blocks
    them).  We NEVER infer delivery from tradingClass/multiplier/underlying.

    STK instruments are intrinsically stock and are not looked up here.
    """
    now = now or datetime.now(timezone.utc)
    try:
        from thetamill import roll_cli  # lazy: owner may still be writing it
        loader = getattr(roll_cli, "load_delivery_evidence", None)
        if not callable(loader):
            return {}
        result = loader(path, instruments, now)
    except Exception:
        # A missing/broken/expired evidence source must never auto-authorize.
        return {}
    if result is None:
        return {}
    # Normalise the loader's return into a {option_conId: evidence} mapping.
    # Accept a set/frozenset/list of conIds, or a dict keyed by conId.
    out: dict[int, Any] = {}
    if isinstance(result, Mapping):
        for key, value in result.items():
            try:
                con_id = int(key)
            except (TypeError, ValueError):
                continue
            if con_id > 0:
                out[con_id] = value
    elif isinstance(result, (set, frozenset, list, tuple)):
        for value in result:
            try:
                con_id = int(value)
            except (TypeError, ValueError):
                continue
            if con_id > 0:
                out[con_id] = True
    return out


# --------------------------------------------------------------------------- #
# Snapshot + resource plan (fresh baseline via risk.summarize)
# --------------------------------------------------------------------------- #

def _fresh_snapshot(ib, cfg, args, environment: str,
                    required_currencies: Iterable[str] = ()) -> Snapshot:
    """Refresh broker state through the shared CLI helper (FX + identity).

    ``required_currencies`` carries EVERY candidate leg's currency so main's
    ``_risk_snapshot(..., required_currencies=...)`` actively collects a live
    timestamped FX quote even when a foreign currency has NO existing cash
    balance / position / working order.  Without it the snapshot would omit the
    quote and my freshness gate would (correctly, but needlessly) block.  I
    never fall back to a historical/_fx rate.  Raw broker stdout/stderr is
    suppressed via ``cli._private_broker_diagnostics`` (privacy: those messages
    can contain the account id); the helper is optional and its absence never
    leaks diagnostics by relaxing the gate.
    """
    with _broker_silence():
        try:
            snapshot = cli._risk_snapshot(ib, cfg, args, environment,
                                          required_currencies=tuple(required_currencies))
        except TypeError:
            # An older helper without the new kwarg: retry with the old call.
            # The freshness gate still fails closed if a needed FX quote is
            # missing, so this fallback can never authorize stale data.
            snapshot = cli._risk_snapshot(ib, cfg, args, environment)
    if not isinstance(snapshot, Snapshot):
        raise SubmissionCliError("_risk_snapshot did not return a Snapshot")
    return snapshot


# --------------------------------------------------------------------------- #
# Timestamped market-data freshness gate
# --------------------------------------------------------------------------- #
# A refreshed Snapshot carries ``captured_at`` (sync time) but NO timestamp for
# portfolio.marketPrice or FX rates.  Main adds OPTIONAL ``price_quotes`` /
# ``fx_quotes`` mappings of frozen PriceQuote(con_id, price, as_of, source) /
# FxQuote(currency, rate, as_of, source), default EMPTY; collect_snapshot never
# fabricates them.  Per the coordinator's rule:
#   * captured_at is NOT proof a price/FX rate is fresh; never use a bare
#     marketPrice / cli._fx float as a timestamped quote.
#   * Baseline SAME-BASE cash-secured Put / covered Call need NO arbitrary
#     underlying price when exposure caps are OFF: cash liabilities are known
#     from strikes/limit prices, not from a stock valuation.
#   * ANY converted cash/asset (non-base currency) needs an ACTUAL timestamped
#     FX quote; base currency is intrinsically 1 (no quote required).
#   * Enabled exposure caps need timestamped stock valuation for ALL relevant
#     positions; a market (unbounded) BUY is always blocked.
#   * Missing/unsourced/naive/expired data BLOCKS the affected increase/cap with
#     an explicit ``unsupported`` reason — never degraded to zero/fresh.

DEFAULT_MAX_DATA_AGE_SECONDS = 30


def _as_quote(value) -> "MarketQuote | None":
    """Normalise a PriceQuote/FxQuote/MarketQuote-like object."""
    if value is None:
        return None
    price = getattr(value, "price", None)
    if price is None:
        price = getattr(value, "rate", None)
    as_of = getattr(value, "as_of", None)
    source = getattr(value, "source", "")
    con_id = getattr(value, "con_id", None)
    currency = getattr(value, "currency", None)
    if not isinstance(as_of, datetime) or price is None:
        return None
    try:
        dec = _finite(price, "quote price")
    except SubmissionCliError:
        return None
    return MarketQuote(dec, as_of, str(source or ""), con_id=con_id, currency=currency)


@dataclass(frozen=True)
class MarketQuote:
    """One timestamped, sourced price/FX observation. ``as_of`` is aware UTC."""

    price: Decimal
    as_of: datetime
    source: str
    con_id: Optional[int] = None
    currency: Optional[str] = None


def _quote_fresh(quote, now: datetime, max_age_seconds: int) -> bool:
    if not isinstance(quote, MarketQuote):
        return False
    if not quote.source or not quote.source.strip():
        return False
    if quote.as_of.tzinfo is None:
        return False
    if not quote.price.is_finite() or quote.price < 0:
        return False
    age = (now.astimezone(timezone.utc) - quote.as_of.astimezone(timezone.utc)).total_seconds()
    return 0 <= age <= max_age_seconds


def _snapshot_quotes(snapshot: Snapshot):
    """Read the OPTIONAL, explicitly-collected timestamped quotes from Snapshot.

    Never fabricates/inferred timestamps.  Returns ({conId: MarketQuote},
    {currency: MarketQuote}).  Both are empty when the snapshot carries none.
    """
    prices: dict[int, MarketQuote] = {}
    fx: dict[str, MarketQuote] = {}
    raw_prices = getattr(snapshot, "price_quotes", None) or {}
    raw_fx = getattr(snapshot, "fx_quotes", None) or {}
    if isinstance(raw_prices, Mapping):
        for key, value in raw_prices.items():
            q = _as_quote(value)
            if q is None:
                continue
            cid = q.con_id if q.con_id is not None else key
            try:
                prices[int(cid)] = q
            except (TypeError, ValueError):
                continue
    if isinstance(raw_fx, Mapping):
        for key, value in raw_fx.items():
            q = _as_quote(value)
            if q is None:
                continue
            cur = q.currency if q.currency is not None else str(key)
            fx[str(cur).upper()] = q
    return prices, fx


def freshness_gate(snapshot: Snapshot, legs: Sequence["execution.PreparedLeg"],
                   limits: "risk.RiskLimits", *,
                   price_quotes: Optional[Mapping[int, MarketQuote]] = None,
                   fx_quotes: Optional[Mapping[str, MarketQuote]] = None,
                   now: Optional[datetime] = None,
                   max_age_seconds: Optional[int] = None) -> list[str]:
    """Return BLOCK reasons; empty means every increase/cap is properly backed.

    Uses :func:`planning.validate_snapshot_evidence` as the shared gate for
    timestamped evidence, quote freshness (30s policy independent of snapshot
    max age), future skew (<= 5s), rate/price equality, allowlisted sources,
    and exact key identity.
    """
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []

    # (1) Market (unbounded) BUY is always blocked: no verifiable amount ceiling.
    for i, leg in enumerate(legs):
        if (leg.instrument.sec_type == "STK" and leg.action.upper() == "BUY"
                and leg.limit_price is None):
            reasons.append(
                f"unsupported: leg {i} market STK BUY has no bounded verifiable "
                "amount; blocked regardless of quotes")

    merged_fx_quotes = dict(snapshot.fx_quotes)
    merged_price_quotes = dict(snapshot.price_quotes)

    if fx_quotes is not None:
        for k, v in fx_quotes.items():
            cur_key = str(k).strip().upper()
            if v is None:
                merged_fx_quotes.pop(cur_key, None)
                continue
            if isinstance(v, FxQuote):
                if v.currency != cur_key:
                    reasons.append(f"unsupported: FX quote identity mismatch for {cur_key}")
                else:
                    merged_fx_quotes[cur_key] = v
            elif isinstance(v, MarketQuote):
                if v.currency is not None and str(v.currency).upper() != cur_key:
                    reasons.append(f"unsupported: FX quote identity mismatch for {cur_key}")
                else:
                    try:
                        merged_fx_quotes[cur_key] = FxQuote(
                            currency=cur_key, rate=v.price, as_of=v.as_of, source=v.source)
                    except (SnapshotError, ValueError, TypeError) as exc:
                        reasons.append(f"unsupported: FX quote {cur_key} invalid: {exc}")
            else:
                as_of = getattr(v, "as_of", None)
                price = getattr(v, "rate", None)
                if price is None:
                    price = getattr(v, "price", None)
                source = getattr(v, "source", "")
                curr = getattr(v, "currency", None)
                if curr is not None and str(curr).upper() != cur_key:
                    reasons.append(f"unsupported: FX quote identity mismatch for {cur_key}")
                elif as_of is not None and price is not None:
                    try:
                        p_dec = _finite(price, "quote price")
                        merged_fx_quotes[cur_key] = FxQuote(
                            currency=cur_key, rate=p_dec, as_of=as_of, source=str(source or ""))
                    except Exception as exc:
                        reasons.append(f"unsupported: quote evidence for {cur_key} is unusable: {exc}")
                else:
                    reasons.append(f"unsupported: quote evidence for {cur_key} is unusable")

    if price_quotes is not None:
        for k, v in price_quotes.items():
            try:
                con_id_key = int(k)
            except (TypeError, ValueError):
                reasons.append(f"unsupported: invalid conId {k}")
                continue
            if v is None:
                merged_price_quotes.pop(con_id_key, None)
                continue
            if isinstance(v, PriceQuote):
                if v.con_id != con_id_key:
                    reasons.append(f"unsupported: price quote identity mismatch for conId {con_id_key}")
                else:
                    merged_price_quotes[con_id_key] = v
            elif isinstance(v, MarketQuote):
                if v.con_id is not None and int(v.con_id) != con_id_key:
                    reasons.append(f"unsupported: price quote identity mismatch for conId {con_id_key}")
                else:
                    try:
                        merged_price_quotes[con_id_key] = PriceQuote(
                            con_id=con_id_key, price=v.price, as_of=v.as_of, source=v.source)
                    except (SnapshotError, ValueError, TypeError) as exc:
                        reasons.append(f"unsupported: price quote conId={con_id_key} invalid: {exc}")
            else:
                as_of = getattr(v, "as_of", None)
                price = getattr(v, "price", None)
                source = getattr(v, "source", "")
                cid = getattr(v, "con_id", None)
                if cid is not None and int(cid) != con_id_key:
                    reasons.append(f"unsupported: price quote identity mismatch for conId {con_id_key}")
                elif as_of is not None and price is not None:
                    try:
                        p_dec = _finite(price, "quote price")
                        merged_price_quotes[con_id_key] = PriceQuote(
                            con_id=con_id_key, price=p_dec, as_of=as_of, source=str(source or ""))
                    except Exception as exc:
                        reasons.append(f"unsupported: quote evidence for conId {con_id_key} is unusable: {exc}")
                else:
                    reasons.append(f"unsupported: quote evidence for conId {con_id_key} is unusable")

    try:
        working_snapshot = dataclasses.replace(
            snapshot, fx_quotes=merged_fx_quotes, price_quotes=merged_price_quotes)
    except SnapshotError as exc:
        reasons.append(f"unsupported: {exc}")
        return reasons

    if max_age_seconds is not None and max_age_seconds != limits.max_snapshot_age_seconds:
        working_limits = dataclasses.replace(limits, max_snapshot_age_seconds=max_age_seconds)
    else:
        working_limits = limits

    needed_currencies = {
        str(leg.instrument.currency).upper() for leg in legs
        if getattr(getattr(leg, "instrument", None), "currency", None)
    }

    try:
        evidence_reasons = planning.validate_snapshot_evidence(
            working_snapshot, working_limits, now,
            needed_currencies=needed_currencies)
        for err in evidence_reasons:
            reasons.append(f"unsupported: {err}")
    except planning.PlanningError as exc:
        reasons.append(f"unsupported: {exc}")

    return reasons


def _resource_plan(snapshot: Snapshot, legs: Sequence["execution.PreparedLeg"],
                   reservations: Optional[Mapping] = None, *,
                   limits: "risk.RiskLimits",
                   price_quotes: Optional[Mapping[int, MarketQuote]] = None,
                   fx_quotes: Optional[Mapping[str, MarketQuote]] = None,
                   now: Optional[datetime] = None,
                   max_age_seconds: Optional[int] = None) -> "execution.ResourcePlan":
    """Build the engine's FULL-package :class:`execution.ResourcePlan`.

    Called INSIDE the account lock on the FRESH snapshot (and again with the
    validated outstanding-reservation aggregate).  Uses :func:`risk.summarize`
    (the same internals as ``risk.assess``) so no collateral/exposure math is
    duplicated.  BEFORE computing any budget it runs :func:`freshness_gate`:
    converted cash/assets need actual timestamped FX (rate equality), caps need
    timestamped stock valuation (price equality), market BUY is blocked, and a
    same-base secured Put / covered Call (caps off) needs no arbitrary price.
    """
    blocks = freshness_gate(snapshot, legs, limits, price_quotes=price_quotes,
                            fx_quotes=fx_quotes, now=now,
                            max_age_seconds=max_age_seconds)
    if blocks:
        raise SubmissionCliError("; ".join(blocks))
    # Available resources from fresh broker snapshot baseline without local ledger reserves
    # (ledger.reserve will subtract active local reserves itself).
    summary = risk.summarize(snapshot, reservations=None, limits=limits)
    base = snapshot.base_currency.upper()

    def fx_rate(currency: str) -> Decimal:
        cur = currency.upper()
        if cur == base:
            return Decimal(1)
        val = snapshot.fx.get(cur)
        if val is None:
            raise SubmissionCliError(f"missing FX rate for {cur}")
        try:
            r = Decimal(str(val))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise SubmissionCliError(f"invalid FX rate for {cur}: {exc}") from exc
        if not r.is_finite() or r <= 0:
            raise SubmissionCliError(f"non-positive FX rate for {cur}: {r}")
        return r

    items: dict[tuple[str, str], Decimal] = {}

    def add(currency: str, resource_key: str, amount: Decimal) -> None:
        if amount <= 0:
            return
        k = (currency, resource_key)
        items[k] = items.get(k, Decimal(0)) + amount

    for leg in legs:
        inst = leg.instrument
        qty = _finite(leg.quantity, "quantity", positive=True)
        mult = _finite(inst.multiplier, "multiplier", positive=True)
        trade_cur = inst.currency.upper()
        rate = fx_rate(trade_cur)
        uid = inst.underlying_id
        action = leg.action.upper()
        order_type = leg.order_type.upper()

        if inst.sec_type == "STK":
            if action == "BUY":
                if order_type != "LMT" or leg.limit_price is None:
                    raise SubmissionCliError(
                        "stock BUY requires a bounded limit price for resource reservation")
                limit_price = _finite(leg.limit_price, "limit price", positive=True)
                notional_base = qty * limit_price * rate
                add(base, "cash_base", notional_base)
                add(trade_cur, f"exposure:{uid}:{trade_cur}", notional_base)
            elif action == "SELL":
                add(trade_cur, f"shares:{uid}:{trade_cur}", qty)
            else:
                raise SubmissionCliError(f"unsupported stock action: {action}")

        elif inst.sec_type == "OPT":
            if action == "SELL":
                if inst.right.upper() == "P":
                    strike = _finite(inst.strike, "strike", positive=True)
                    collateral_base = qty * strike * mult * rate
                    add(base, "cash_base", collateral_base)
                    add(trade_cur, f"exposure:{uid}:{trade_cur}", collateral_base)
                elif inst.right.upper() == "C":
                    add(trade_cur, f"shares:{uid}:{trade_cur}", mult * qty)
                else:
                    raise SubmissionCliError(f"unrecognised option right: {inst.right}")
            elif action == "BUY":
                if order_type != "LMT" or leg.limit_price is None:
                    raise SubmissionCliError(
                        "option BUY requires a bounded limit price for resource reservation")
                limit_price = _finite(leg.limit_price, "limit price", positive=True)
                premium_base = qty * mult * limit_price * rate
                add(base, f"close:{inst.con_id}", qty)
                add(base, "cash_base", premium_base)
            else:
                raise SubmissionCliError(f"unsupported option action: {action}")
        else:
            raise SubmissionCliError(f"unrecognised instrument: {inst.sec_type}")

    available: dict[tuple[str, str], Decimal] = {}
    cash_needed = (base, "cash_base") in items
    raw_free_cash = summary.get("free_cash_base")
    cash_confirmed = bool(summary.get("cash_confirmed", True))

    if cash_needed and (not cash_confirmed or raw_free_cash is None):
        raise SubmissionCliError("unconfirmed cash balance or FX rate; cannot reserve cash")

    if raw_free_cash is None or not cash_confirmed:
        free_cash = Decimal(0)
    else:
        free_cash = _finite(raw_free_cash, "free cash")

    if cash_needed and free_cash < 0:
        raise SubmissionCliError(
            f"negative available cash ({free_cash} BASE); cannot reserve cash")

    # Baseline free cash excluding local reserves (clamped safely to non-negative for ledger)
    available[(base, "cash_base")] = max(Decimal(0), free_cash)
    for k, v in (summary.get("shares") or {}).items():
        if v is not None:
            trade_cur = k.split(":")[-1].upper()
            available[(trade_cur, f"shares:{k}")] = max(Decimal(0), _finite(v, "shares"))
    for k, v in (summary.get("short_close_capacity") or {}).items():
        if v is not None:
            available[(base, f"close:{k}")] = max(Decimal(0), _finite(v, "close capacity"))

    resources = dict(available)
    has_exposure_keys = any(key.startswith("exposure:") for _, key in items)

    caps_enabled = bool(limits.enabled and limits.has_exposure_caps)
    if caps_enabled and has_exposure_keys:
        if not summary.get("exposure_confirmed", True):
            raise SubmissionCliError(
                "exposure cannot be confirmed; cannot determine headroom")

        raw_netliq = summary.get("net_liquidation")
        netliq = Decimal(str(raw_netliq)) if raw_netliq is not None else None
        has_ratio_limits = (limits.per_underlying_ratio is not None or
                            limits.portfolio_ratio is not None)
        if has_ratio_limits and netliq is None:
            raise SubmissionCliError(
                "ratio limits configured but net liquidation is missing")

        curr_port_str = summary.get("exposure_portfolio_base")
        curr_port = Decimal(str(curr_port_str)) if curr_port_str is not None else Decimal(0)
        exp_by_und = summary.get("exposure_by_underlying") or {}

        for (cur, key) in items:
            if not key.startswith("exposure:"):
                continue
            parts = key.split(":", 2)
            uid = parts[1] if len(parts) > 1 else ""
            trade_cur = parts[2] if len(parts) > 2 else cur
            und_key = f"{uid}:{trade_cur}"

            curr_und_str = exp_by_und.get(und_key)
            curr_und = Decimal(str(curr_und_str)) if curr_und_str is not None else Decimal(0)

            headrooms: list[Decimal] = []
            if limits.per_underlying_amount is not None:
                headrooms.append(limits.per_underlying_amount - curr_und)
            if limits.per_underlying_ratio is not None and netliq is not None:
                headrooms.append(netliq * limits.per_underlying_ratio - curr_und)
            if limits.portfolio_amount is not None:
                headrooms.append(limits.portfolio_amount - curr_port)
            if limits.portfolio_ratio is not None and netliq is not None:
                headrooms.append(netliq * limits.portfolio_ratio - curr_port)

            if headrooms:
                headroom = max(Decimal(0), min(headrooms))
            else:
                headroom = available[(base, "cash_base")] * max(len(legs), 1)
            resources[(cur, key)] = headroom
    else:
        # Caps not configured: exposure is not capped by limits
        cash_ceiling = available[(base, "cash_base")] * max(len(legs), 1)
        for (cur, key) in items:
            if key.startswith("exposure:") and (cur, key) not in resources:
                needed = items.get((cur, key), Decimal(0))
                resources[(cur, key)] = max(cash_ceiling, needed)

    for (cur, key) in items:
        resources.setdefault((cur, key), Decimal(0))
    for key in items:
        resources.setdefault(key, Decimal(0))

    spec_items = [state.RiskReservationSpec(cur, key, str(amount))
                  for (cur, key), amount in sorted(items.items())]

    # Deferred coverage: the second leg's collateral only exists AFTER the stock
    # leg fills.  Provide a conservative post-fill override so the WHOLE package
    # is reserved up front (engine requires overrides for deferred combos).
    overrides: dict[tuple, Decimal] = {}
    deferred = any(getattr(leg, "coverage_deferred", False) for leg in legs)
    if deferred:
        for leg in legs:
            if leg.action.upper() == "BUY" and leg.instrument.sec_type == "STK":
                qty = _finite(leg.quantity, "quantity", positive=True)
                cur = leg.instrument.currency.upper()
                uid = leg.instrument.underlying_id
                key = (cur, f"shares:{uid}:{cur}")
                # Post-fill shares = baseline shares + the newly bought shares.
                baseline = resources.get(key, Decimal(0))
                overrides[key] = baseline + qty
        if not overrides:
            raise SubmissionCliError(
                "deferred-coverage combo has no stock BUY leg to supply "
                "post-fill shares; refusing an unsafe combo")
    return execution.ResourcePlan(resources=resources, items=tuple(spec_items),
                                  overrides=overrides)


# --------------------------------------------------------------------------- #
# Broker boundary: build IB order, verify identity, bounded receipt
# --------------------------------------------------------------------------- #

def build_broker_order(leg: "execution.PreparedLeg", order_ref: str,
                       account: str) -> Any:
    """Create an IB ``Order`` from the CANONICAL leg and set ALL bound fields.

    The order is reconstructed ONLY from the canonical leg fields that are bound
    into the review digest (action / quantity / type / prices / TIF / RTH), NOT
    from a mutable template object.  This guarantees the sent order matches the
    digest: no caller-supplied object can inject a divergent field, and nothing
    non-JSON-serialisable enters the intent payload.  ``account``, ``orderRef``
    (the stable pre-persisted reference), ``tif`` and ``outsideRth`` are set
    explicitly here.
    """
    try:
        from ib_async import MarketOrder, LimitOrder, StopOrder  # type: ignore
        qty = float(leg.quantity)
        if leg.order_type.upper() == "MKT":
            order = MarketOrder(leg.action.upper(), qty)
        elif leg.order_type.upper() == "STP" and leg.stop_price is not None:
            order = StopOrder(leg.action.upper(), qty, float(leg.stop_price))
        else:
            order = LimitOrder(leg.action.upper(), qty,
                               float(leg.limit_price) if leg.limit_price else 0.0)
        if leg.stop_price is not None and leg.order_type.upper() != "STP":
            order.auxPrice = leg.stop_price
    except Exception:
        order = type("Order", (), {})()
        order.action = leg.action.upper()
        order.totalQuantity = leg.quantity
        order.orderType = leg.order_type.upper()
        order.lmtPrice = leg.limit_price
        order.auxPrice = leg.stop_price
    order.tif = leg.tif
    order.outsideRth = bool(leg.outside_rth)
    order.account = account
    order.orderRef = order_ref
    return order


def _bounded_receipt(ib, contract, order, *, max_wait_seconds: float = 5.0) -> dict:
    """Submit ONE order and observe a POSITIVE status within a bounded wait.

    Returns a dict with ``order_id`` / ``perm_id`` / ``status``.  ``status`` is
    None (unknown, may have been sent) unless a positive broker status is
    actually observed.  There is NO retry: a single ``placeOrder`` call only.
    ``ib.sleep`` is used for the bounded wait when available (mock-friendly).
    """
    # ONE placeOrder; raw broker diagnostics are privacy-suppressed, but the
    # trade object (and every exception) still propagates normally.
    with _broker_silence():
        trade = ib.placeOrder(contract, order)
    deadline = (datetime.now(timezone.utc).timestamp() + max_wait_seconds)
    status = None
    order_id = None
    perm_id = None
    # Poll a few times within the bounded window for a positive observation.
    for _ in range(3):
        st = getattr(getattr(trade, "orderStatus", None), "status", None)
        oid = getattr(getattr(trade, "order", None), "orderId", None) \
            or getattr(order, "orderId", None)
        pid = getattr(getattr(trade, "order", None), "permId", None) \
            or getattr(order, "permId", None)
        if oid:
            order_id = str(oid)
        if pid:
            perm_id = str(pid)
        if isinstance(st, str) and st in state.POSITIVE_BROKER_STATES:
            status = st
            break
        if hasattr(ib, "sleep"):
            try:
                with _broker_silence():
                    ib.sleep(min(0.5, max(0.0, max_wait_seconds / 3.0)))
            except Exception:
                pass
        if datetime.now(timezone.utc).timestamp() >= deadline:
            break
    return {"order_id": order_id, "perm_id": perm_id, "status": status}


def _broker_submit(ib, leg: "execution.PreparedLeg", order_ref: str,
                   account: str, evidence: Mapping[int, Any]) -> "execution.BrokerReceipt":
    """The engine's ONLY network boundary (opaque-contract verification here)."""
    # The engine does NOT introspect the contract; verify FULL identity here.
    contract = leg.contract
    mismatches = verify_contract_identity(contract, leg.instrument)
    if mismatches:
        raise SubmissionCliError(
            "refusing send: broker contract does not match the confirmed leg identity: "
            + "; ".join(mismatches))
    # Every OPT (including a close) requires explicit per-conId evidence.
    if leg.instrument.sec_type == "OPT" and leg.instrument.con_id not in evidence:
        raise SubmissionCliError(
            f"refusing send: option conId={leg.instrument.con_id} has no explicit "
            "delivery evidence")
    order = build_broker_order(leg, order_ref, account)
    receipt = _bounded_receipt(ib, contract, order)
    return execution.BrokerReceipt(
        order_id=receipt["order_id"], perm_id=receipt["perm_id"],
        status=receipt["status"])


# --------------------------------------------------------------------------- #
# fill_proof: actual Filled + finite exact approved qty + stable orderRef
# --------------------------------------------------------------------------- #

def _broker_trades(ib) -> list:
    """Best-effort read of current/recent broker trades (no cancel, no retry).

    Several broker getters (``openTrades``, ``trades``, ``reqAllOpenOrders``)
    commonly return the SAME trade objects; deduplicate by identity so one real
    trade is never counted three times (which would look like an ambiguous
    orderRef and wrongly fail the exact-fill proof).
    """
    trades: list = []
    seen: set[int] = set()

    def collect(found):
        if isinstance(found, (list, tuple)):
            for trade in found:
                marker = id(trade)
                if marker not in seen:
                    seen.add(marker)
                    trades.append(trade)

    for getter in ("openTrades", "trades"):
        fn = getattr(ib, getter, None)
        if callable(fn):
            try:
                with _broker_silence():
                    collect(fn())
            except Exception:
                pass
    try:
        req = getattr(ib, "reqAllOpenOrders", None)
        if callable(req):
            with _broker_silence():
                collect(req())
    except Exception:
        pass
    return trades


def _fill_proof(ib, order_ref: str, ids: Mapping[str, str], *,
                approved_by_ref: Mapping[str, "ApprovedLeg"]) -> Optional[object]:
    """Prove an actual ``Filled`` order with finite, EXACT approved quantity.

    Only a positive ``Filled`` status AND an exactly-matching finite filled
    quantity (equal to the approved leg quantity) count.  ``PartiallyFilled``,
    ``PreSubmitted``, ``Cancelled`` etc. are NOT proof.  Matching is by the
    stable ``orderRef`` (never by an absent open order), with permId/orderId as
    corroboration only.

    Returns a STRUCTURED ``execution.FillProof`` (the engine's final API: it
    carries account/environment/conId/stableRef/exact filled quantity/status and
    the engine re-verifies every field against the approved dependency leg) or
    ``None``.  If the engine build does not yet export ``FillProof`` (older
    tree), a duck-typed object with identical fields is returned instead, so the
    adapter works with both.  Never retries; never returns a bare string.
    """
    approved = approved_by_ref.get(order_ref)
    if approved is None:
        return None
    matches: list = []
    for trade in _broker_trades(ib):
        o = getattr(trade, "order", None)
        ref = str(getattr(o, "orderRef", "") or "")
        if ref and ref == order_ref:
            matches.append(trade)
    if len(matches) != 1:
        return None  # ambiguous / missing is never proof
    trade = matches[0]
    o = trade.order
    st = getattr(getattr(trade, "orderStatus", None), "status", None)
    if st != "Filled":
        return None  # generic positive statuses are NOT fill proof
    # Corroborate identities if supplied, but never relax the orderRef match.
    perm = str(ids.get("perm_id") or "")
    oid = str(ids.get("order_id") or "")
    if perm and str(getattr(o, "permId", "") or "") not in ("", perm):
        return None
    if oid and str(getattr(o, "orderId", "") or "") not in ("", oid):
        return None
    os_ = trade.orderStatus
    filled = getattr(os_, "filled", None)
    remaining = getattr(os_, "remaining", None)
    try:
        total = _finite(getattr(o, "totalQuantity", None), "totalQuantity", positive=True)
        filled_d = _finite(filled, "filled") if filled is not None else None
        remaining_d = _finite(remaining, "remaining") if remaining is not None else None
    except SubmissionCliError:
        return None  # non-finite/NaN/bool quantity is never proof (fail closed)
    # A Filled order implies filled == total == approved, remaining == 0.
    if filled_d is None or filled_d != total or filled_d != approved.quantity:
        return None
    if remaining_d is not None and remaining_d != 0:
        return None
    fields = dict(order_ref=order_ref, account=approved.account,
                  environment=approved.environment, con_id=approved.con_id,
                  filled_quantity=filled_d, status="Filled")
    proof_cls = getattr(execution, "FillProof", None)
    if proof_cls is not None:
        return proof_cls(**fields)
    # Pre-final engine tree: a duck-typed proof with the identical surface.
    return SimpleNamespace(**fields)


# --------------------------------------------------------------------------- #
# Ledger + legacy import (preserve history, never claim zero)
# --------------------------------------------------------------------------- #

def _ledger(cfg, account: str, environment: str, ib=None) -> "state.SubmissionLedger":
    """Open the temp-state SQLite ledger, importing legacy consumed codes.

    The DB path is ``cli._data_dir()/state.sqlite3``.  Legacy consumed codes /
    audit history are imported READ-ONLY through the state API so a new ledger
    never silently claims zero submission history.
    """
    data_dir = Path(cli._data_dir())
    path = data_dir / "state.sqlite3"
    tz = (cfg.get("risk", {}) or {}).get("day_timezone", "UTC") or "UTC"
    ledger = state.SubmissionLedger(path, tz)
    # Preserve/import legacy consumed codes, read-only (INSERT OR IGNORE).
    consumed = []
    cfile = data_dir / "consumed_codes.json"
    if cfile.exists():
        try:
            data = json.loads(cfile.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                consumed = [str(k) for k in data]
            elif isinstance(data, list):
                consumed = [str(x) for x in data]
        except (OSError, ValueError):
            consumed = []
    log = data_dir / "orders.jsonl"
    records = []
    if log.exists():
        try:
            for line in log.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if isinstance(rec, dict) and rec.get("code"):
                        records.append(rec)
                except ValueError:
                    continue
        except OSError:
            records = []
    try:
        if consumed:
            ledger.import_legacy_codes(account=account, environment=environment,
                                       codes=consumed, source="consumed_codes.json")
        if records:
            ledger.import_legacy_records(account=account, environment=environment,
                                         records=records, source="orders.jsonl")
    except state.StateError as exc:
        raise SubmissionCliError(f"legacy history import failed: {exc}") from exc
    return ledger


# --------------------------------------------------------------------------- #
# Top-level adapter
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AdapterResult:
    """Return value of :func:`execute_prepared` (also printed to CLI)."""

    rc: int
    outcome: str
    account: str
    environment: str
    intent_id: str
    approval_code: str
    details: tuple[str, ...] = ()

    def lines(self) -> list[str]:
        acc = _mask(self.account)
        out = [f"[{self.outcome}] account={acc} env={self.environment} rc={self.rc}"]
        if self.intent_id:
            out.append(f"  intent={self.intent_id}")
        if self.approval_code:
            out.append(f"  approval_code={self.approval_code} (review; never an order)")
        out.extend(f"  {d}" for d in self.details)
        return out


def _engine_kwargs(*, ledger, ib, cfg, args, environment, account,
                   limits, legs, lock_dir,
                   price_quotes: Optional[Mapping[int, MarketQuote]] = None,
                   fx_quotes: Optional[Mapping[str, MarketQuote]] = None,
                   clock: Optional[Callable[[], datetime]] = None,
                   now: Optional[datetime] = None,
                   max_age_seconds: Optional[int] = None):
    """Build the engine constructor kwargs for the FINAL execution.py API.

    ``resource_plan`` is a REQUIRED constructor kwarg (static ``resource_items``/
    ``resources``/``resource_overrides`` were removed).  It is a callable with
    the canonical signature ``(snapshot, legs, aggregate) -> ResourcePlan`` and
    is invoked INSIDE the account lock on the FRESH snapshot; the third argument
    is the validated outstanding-reservation aggregate ready for
    ``risk.summarize``.  No budget is fabricated and the freshness equality gate
    is applied to explicitly-collected timestamped quotes.  ``verified_prior_
    submissions`` is deliberately NEVER passed: an enabled-P3 fresh ledger stays
    conservatively frozen (no fabricated zero history).
    """
    dyn_clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))

    def plan(snapshot: Snapshot, plan_legs, aggregate):
        return _resource_plan(snapshot, plan_legs, aggregate, limits=limits,
                              price_quotes=price_quotes, fx_quotes=fx_quotes,
                              now=dyn_clock(), max_age_seconds=max_age_seconds)

    evidence = _current_evidence
    approved = _current_approved

    # EVERY candidate leg's currency is actively collected by main's refreshed
    # snapshot, even for a foreign currency with no existing balance/position.
    # The freshness gate then validates the actual timestamped quote (equality).
    candidate_currencies = tuple(sorted({
        str(leg.instrument.currency).upper() for leg in legs
        if getattr(getattr(leg, "instrument", None), "currency", None)}))

    def submit(broker, leg, ref):
        return _broker_submit(broker, leg, ref, account, evidence)

    def fill(broker, ref, ids):
        return _fill_proof(broker, ref, ids, approved_by_ref=approved)

    return dict(
        ledger=ledger,
        snapshot_provider=lambda: _fresh_snapshot(
            ib, cfg, args, environment, candidate_currencies),
        broker=ib, place_order=submit, resource_plan=plan, fill_proof=fill,
        config=dict(cfg), limits=limits, lock_dir=lock_dir,
    )


# Per-call state shared with callbacks (single-threaded, one adapter call).
_current_evidence: Mapping[int, Any] = {}
_current_approved: Mapping[str, ApprovedLeg] = {}

# Any of these durable per-leg states means placeOrder MAY already have been
# called.  NEVER present such an outcome as a no-send preflight/refusal: the
# operator must treat it as UNKNOWN and must not retry.
_MAY_HAVE_SENT_LEG_STATES = frozenset({
    "attempting", "submitting", "submitted", "unknown",
})
# The engine wraps an after-send interruption in this exact message prefix.
_AFTER_SEND_MARKER = "submission interrupted after a send"


def _may_have_sent(*, ledger, account: str, environment: str,
                   intent_id: str, exc: BaseException) -> bool:
    """Conservatively decide whether a confirm error may have followed a send.

    Two independent signals (either is enough):
    * the engine's typed ``ExecutionError`` after-send interruption message;
    * ANY durable leg of this intent in attempting/submitting/submitted/unknown.
    The ledger inspection is read-only and account/environment scoped; if it is
    itself unavailable we still honour the typed marker.  Returning True keeps
    the ledger conservative and forces RC_UNKNOWN / outcome UNKNOWN.
    """
    if _AFTER_SEND_MARKER in str(exc):
        return True
    try:
        rows = ledger.list_legs(account=account, environment=environment,
                                intent_id=intent_id)
    except Exception:
        return False
    return any(str(getattr(row, "state", "")).lower() in _MAY_HAVE_SENT_LEG_STATES
               for row in rows)


def execute_prepared(ib, cfg, args, *, pairs: Sequence[tuple],
                     strategy: str = "",
                     requires_filled: Iterable[int] = (),
                     delivery_evidence_path=None,
                     price_quotes: Optional[Mapping[int, MarketQuote]] = None,
                     fx_quotes: Optional[Mapping[str, MarketQuote]] = None,
                     now: Optional[datetime] = None,
                     max_age_seconds: Optional[int] = None,
                     clock: Optional[Callable[[], datetime]] = None) -> AdapterResult:
    """Prepare canonical legs and run the engine's preflight/confirm protocol.

    Parameters
    ----------
    ib, cfg, args
        The connected broker-like object, whole config mapping and parsed args
        (used by ``cli._risk_snapshot`` / ``cli._is_live_env`` /
        ``cli._selected_account``).
    pairs
        Sequence of ``(IB contract, IB order)`` pairs supplied by main.  For a
        covered two-leg combo, main passes the stock leg first and lists the
        covered call's index in ``requires_filled``.
    strategy
        Strategy label bound into the review digest.
    requires_filled
        Indices of deferred-coverage legs (need a prior leg's actual fill).
    delivery_evidence_path
        Path to the explicitly-confirmed delivery evidence file; loaded via
        ``roll_cli.load_delivery_evidence`` (lazy import).  None/expired/missing
        => NO OPT leg is verified (fail closed, never inferred).
    price_quotes / fx_quotes
        Optional explicit timestamped quotes that OVERRIDE the snapshot's own
        ``price_quotes`` / ``fx_quotes`` for the freshness gate (useful for
        tests).  Normally the refreshed snapshot supplies them.
    now / max_age_seconds / clock
        Deterministic time controls for the freshness gate and engine expiry.

    Confirmation semantics
    ----------------------
    ``--code`` WITHOUT ``--intent`` is REJECTED (never a legacy path).  With
    BOTH, ``engine.confirm`` is called.  With NEITHER, a fresh ``preflight``
    (review code) is produced.  There is no ``--force`` and no fabricated
    ``verified_prior_submissions``.

    Returns an :class:`AdapterResult` with rc 2 / 0 / 3 / 1 (preflight /
    confirmed / unknown / skipped) and prints details (account masked).
    """
    global _current_evidence, _current_approved
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise SubmissionCliError("now must include a timezone")

    # ---- account + environment (selected, not guessed) -------------------- #
    port, _ = cli.resolve_port(cfg, args)
    environment = "live" if cli._is_live_env(cfg, args, port) else "paper"
    try:
        account = cli._selected_account(ib, cfg, args)
    except SystemExit as exc:
        raise SubmissionCliError("cannot select a managed account") from exc
    if not account:
        raise SubmissionCliError("no account selected")

    # ---- confirm args: explicit --intent + --code only -------------------- #
    intent_id = str(getattr(args, "intent", "") or "").strip()
    code = str(getattr(args, "code", "") or "").strip()
    if code and not intent_id:
        raise SubmissionCliError(
            "--code alone is not a confirmation; an explicit --intent is required "
            "(no legacy _code path)")
    if intent_id and not code:
        raise SubmissionCliError(
            "--intent alone is not a confirmation; an explicit --code is required")
    confirm = bool(intent_id and code)

    # ---- canonical legs from ACTUAL contracts ----------------------------- #
    # We need the instruments before loading evidence (the loader takes them).
    specs0 = build_leg_specs(pairs, ib=ib, account=account, strategy=strategy,
                             requires_filled=requires_filled,
                             delivery_evidence={}, evidence_now=now)
    instruments = [s.instrument for s in specs0]
    evidence = load_delivery_evidence(delivery_evidence_path, instruments, now) \
        if delivery_evidence_path is not None else {}
    specs = build_leg_specs(pairs, ib=ib, account=account, strategy=strategy,
                            requires_filled=requires_filled,
                            delivery_evidence=evidence, evidence_now=now)
    legs = [s.to_prepared(account) for s in specs]

    # Any OPT leg without explicit evidence stays deliverable_verified=False so
    # the engine blocks it safely (never bypassed / auto-verified).
    unverified = [i for i, leg in enumerate(legs)
                  if leg.instrument.sec_type == "OPT" and not leg.deliverable_verified]
    if unverified:
        raise SubmissionCliError(
            "option legs without explicit per-conId delivery evidence are blocked "
            f"(indices {unverified}); close legs are not exempt")

    # Config/account/strategy/contracts are bound via the engine digest by
    # including the whole cfg, the selected account, strategy and identity in
    # each leg's canonical payload.
    approved = {}
    # Approved quantities are keyed by the engine's stable orderRef; we cannot
    # know the refs before preflight, so fill_proof also accepts the leg's
    # approved qty mapped lazily via the ledger after record_legs (see below).
    _current_evidence = evidence
    _current_approved = approved

    # ---- ledger + limits ------------------------------------------------- #
    try:
        limits = risk.validate_risk_config(cfg)
    except risk.RiskConfigError as exc:
        raise SubmissionCliError(f"invalid risk config: {exc}") from exc
    ledger = _ledger(cfg, account, environment, ib)
    lock_dir = Path(cli._data_dir()) / "locks"

    kwargs = _engine_kwargs(
        ledger=ledger, ib=ib, cfg=cfg, args=args, environment=environment,
        account=account, limits=limits, legs=legs, lock_dir=lock_dir,
        price_quotes=price_quotes, fx_quotes=fx_quotes, clock=clock, now=now,
        max_age_seconds=max_age_seconds)
    if clock is not None:
        kwargs["clock"] = clock
    engine = execution.ExecutionEngine(**kwargs)

    # ---- preflight (no intent/code) -------------------------------------- #
    if not confirm:
        pre = engine.preflight(legs, account=account, environment=environment)
        details = [
            f"legs={pre.leg_count} day_key={pre.day_key}",
            "review_code is NOT an order; re-run with --intent and --code to confirm",
            "no placeOrder/cancel was called",
        ]
        result = AdapterResult(
            rc=RC_PREFLIGHT, outcome="PREFLIGHT", account=account,
            environment=environment, intent_id=pre.intent_id,
            approval_code=pre.approval_code, details=tuple(details))
        for line in result.lines():
            print(line)
        return result

    # ---- confirm --------------------------------------------------------- #
    # Map stable orderRef -> approved leg quantity for fill_proof.  The engine
    # persists refs via record_legs DURING confirm (before the deferred leg's
    # fill_proof runs), so seed the map lazily on the first proof call.
    def seed_approved():
        try:
            rows = ledger.list_legs(account=account, environment=environment,
                                    intent_id=intent_id)
            for row in rows:
                if 0 <= row.leg_index < len(legs):
                    leg = legs[row.leg_index]
                    approved[row.order_ref] = ApprovedLeg(
                        quantity=leg.quantity, account=account,
                        environment=environment, con_id=leg.instrument.con_id)
        except Exception:
            pass
    # Wrap fill_proof so the approved map is seeded before the first proof call.
    base_fill = kwargs["fill_proof"]

    def seeded_fill(broker, ref, ids):
        if not approved:
            seed_approved()
        return base_fill(broker, ref, ids)

    engine.fill_proof = seeded_fill

    try:
        result = engine.confirm(intent_id=intent_id, code=code, legs=legs,
                                account=account, environment=environment)
    except (execution.ExecutionError, state.StateError,
            SubmissionCliError) as exc:
        # A blocked/digest/unresolved/freshness condition BEFORE any send is a
        # safe no-send refusal.  A SubmissionCliError can be raised inside the
        # resource_plan callback (e.g. the timestamped quote freshness gate).
        # BUT once a leg is durably attempting/submitting/submitted/unknown — or
        # the engine reports a typed after-send interruption — placeOrder MAY
        # already have happened: return RC_UNKNOWN/UNKNOWN, keep the ledger
        # conservatively unresolved, and NEVER claim preflight / refused-no-send.
        message = _mask_account_text(str(exc), account)
        if _may_have_sent(ledger=ledger, account=account,
                          environment=environment, intent_id=intent_id, exc=exc):
            details = [
                "a send may have happened; result is UNKNOWN (not a new preflight)",
                "do NOT retry; ledger left conservatively unresolved",
                f"reason: {message}",
            ]
            out = AdapterResult(rc=RC_UNKNOWN, outcome="UNKNOWN", account=account,
                                environment=environment, intent_id=intent_id,
                                approval_code="", details=tuple(details))
        else:
            details = [f"confirm refused before any send: {message}",
                       "no retry; ledger left conservative"]
            out = AdapterResult(rc=RC_PREFLIGHT, outcome="BLOCKED", account=account,
                                environment=environment, intent_id=intent_id,
                                approval_code="", details=tuple(details))
        for line in out.lines():
            print(line)
        return out

    leg_states = [o.state for o in result.legs]
    details = [
        f"decision={result.decision} day_key={result.day_key}",
        f"legs={len(result.legs)} states={leg_states}",
        "unknown receipts are terminal; never retried",
    ]
    for o in result.legs:
        if o.reason:
            details.append(f"leg {o.leg_index}: {o.reason}")
    if result.frozen:
        details.append(f"migration freeze: {result.explanation}")

    # The engine emits leg states: "submitted" (positive LOCAL callback; risk is
    # RETAINED and release needs a separate positive broker-snapshot handoff via
    # engine.reconcile_positive), "unknown" (may have been sent; never retried),
    # "skipped" (deferred leg had no exact Filled proof).  A whole-account
    # migration freeze returns decision=BLOCK with NO leg outcomes.
    if result.frozen or result.decision != "ALLOW":
        rc, outcome = RC_PREFLIGHT, "BLOCKED"
    elif any(o.state == "unknown" for o in result.legs):
        rc, outcome = RC_UNKNOWN, "UNKNOWN"
    elif any(o.state == "skipped" for o in result.legs):
        rc, outcome = RC_SKIPPED, "SKIPPED"
    elif result.legs and all(o.state == "submitted" for o in result.legs):
        # rc 0 = every attempted leg mapped to a positive broker state.  This is
        # NOT a released obligation: reconcile_positive is a separate step.
        rc, outcome = RC_CONFIRMED, "CONFIRMED"
    else:
        rc, outcome = RC_PREFLIGHT, "BLOCKED"
    out = AdapterResult(rc=rc, outcome=outcome, account=account,
                        environment=environment, intent_id=intent_id,
                        approval_code="", details=tuple(details))
    for line in out.lines():
        print(line)
    return out


def _mask_account_text(text: str, account: str) -> str:
    """Never leak the raw account id in error output."""
    return text.replace(account, _mask(account)) if account else text
