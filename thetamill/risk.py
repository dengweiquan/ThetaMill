"""Pure, offline risk assessment for account-scoped broker snapshots.

This module performs no I/O: it never connects to a broker, reads no files, writes
no ledger, and imports nothing from the CLI.  It is *pure*: it never mutates the
Snapshot / OrderRequest / reservation objects passed to it.  All arithmetic uses
``Decimal`` and all money is converted to the account ``base_currency`` before it
is pooled.  A single global cash pool is shared by every currency (positive and
negative balances alike); ``BASE`` is only a display aggregate and is never
counted as spendable cash.  Held/pending short-put collateral AND bounded
pending/proposed LMT stock buys and option buyback premiums draw on this same
pool (converted across all currencies). MKT purchases have no verifiable debit
bound and are refused; option purchases must be confirmed short closes.
FX uses broker_state's documented convention: **base units per one unit of the
foreign currency** (``amount_in_cur * fx[cur] == amount_in_base``).

Underlying identity is the confirmed ``Instrument.underlying_id`` (the real
``underConId``), never the display symbol.  Underlying keys therefore have the
form ``"<underlying_id>:<CURRENCY>"`` (``Instrument.key``).

Local reservations
------------------
``assess``/``summarize`` accept an optional ``reservations`` mapping.  The state
ledger (owned elsewhere) aggregates its still-open reservations into these four
buckets; callers pass an empty mapping / ``None`` when there are none.  All
values must be finite, non-bool numbers (``Decimal``/``int``/``str``/``float``
are coerced losslessly via ``Decimal(str(value))``)::

    {
      "cash_base":    Decimal,   # base-currency cash already committed
      "shares":       {underlying_key: Decimal},  # shares committed (deducted from cover)
      "close":        {con_id:    Decimal},       # exact conId short close capacity used
      "exposure":     {underlying_key: Decimal},  # base-currency exposure committed
    }

Unrecognised reservation buckets are rejected (fail closed) so a future ledger
schema change cannot silently free risk budget.  ``shares`` / ``exposure`` keys
MUST be ``"<underlying_id>:<CURRENCY>"`` strings; ``close`` keys MUST be an
int conId or a decimal digit-string.  Bare integers / symbols / non-conId
strings are rejected rather than silently ignored (otherwise a reservation
would fail to reserve its budget and an order could pass the baseline gate).
``close`` values are always *consumed* from close capacity; a future buyback
reservation must never release cover or collateral before it is reflected in
the broker snapshot.  Shares and cash reservations likewise only ever *reduce*
available budget.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from thetamill.broker_state import Instrument, Position, Snapshot, WorkingOrder

ZERO = Decimal(0)
ONE = Decimal(1)
ALLOW = "allow"
BLOCK = "block"
RESERVATION_BUCKETS = frozenset({"cash_base", "shares", "close", "exposure"})


class RiskConfigError(ValueError):
    """Risk configuration is invalid; never degrade to 'no limits'."""


class RiskDataError(ValueError):
    """A consumed snapshot / request / reservation value is not safely usable."""


def _dec(value: Any, label: str, *, positive: bool = False,
         minimum: Decimal | None = None) -> Decimal:
    """Strictly coerce to a finite Decimal; bools and NaN/Inf are rejected.

    ``float``/``str`` are converted via ``str`` (lossless for the values this
    module accepts).  This is used both for structural validation (raise) and to
    normalise every number consumed in arithmetic, so a hand-built Snapshot
    storing an ``int`` or ``str`` multiplier/quantity can never corrupt a sum.
    """
    if isinstance(value, bool) or value is None:
        raise RiskDataError(f"{label}: expected a finite Decimal number")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, (str, float)):
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            raise RiskDataError(f"{label}: expected a finite Decimal number") from None
    else:
        raise RiskDataError(f"{label}: expected a finite Decimal number")
    if not result.is_finite():
        raise RiskDataError(f"{label}: expected a finite Decimal number")
    if positive and result <= 0:
        raise RiskDataError(f"{label}: expected a positive Decimal number")
    if minimum is not None and result < minimum:
        raise RiskDataError(f"{label}: expected a non-negative Decimal number")
    return result


def _price(value: Any) -> Decimal | None:
    """Soft sanitizer for price / limit_price / netliq; bad -> unknown (None)."""
    try:
        return _dec(value, "price", positive=True)
    except RiskDataError:
        return None


def _rate(snapshot: Snapshot, currency: str) -> Decimal | None:
    """Base units per one unit of ``currency``; None when not confirmed."""
    if currency == snapshot.base_currency:
        return ONE
    value = snapshot.fx.get(currency)
    if value is None:
        return None
    try:
        return _dec(value, f"FX {currency}", positive=True)
    except RiskDataError:
        return None


def _opt(inst: Instrument, label: str) -> None:
    """Validate that an option carries the identity needed for obligation math."""
    if inst.sec_type != "OPT" or inst.right.upper() not in {"C", "P"}:
        raise RiskDataError(f"{label}: unrecognised option right / security type")
    _dec(inst.multiplier, f"{label} multiplier", positive=True)
    if inst.right.upper() == "P":
        _dec(inst.strike, f"{label} strike", positive=True)


def _validate_instrument(inst: Instrument, label: str) -> None:
    if not isinstance(inst, Instrument):
        raise RiskDataError(f"{label}: expected an Instrument")
    if inst.sec_type not in {"STK", "OPT"}:
        raise RiskDataError(f"{label}: unrecognised security type")
    if type(inst.con_id) is not int or inst.con_id <= 0:
        raise RiskDataError(f"{label}: invalid conId")
    if type(inst.underlying_id) is not int or inst.underlying_id <= 0:
        raise RiskDataError(f"{label}: unconfirmed underlying identity")
    if not isinstance(inst.currency, str) or not inst.currency:
        raise RiskDataError(f"{label}: missing currency")
    if inst.sec_type == "STK" and inst.underlying_id != inst.con_id:
        # A stock IS its own underlying; a mismatched underConId means the
        # identity is unverified and must not be used for cover aggregation.
        raise RiskDataError(
            f"{label}: stock underlying_id must equal conId "
            f"({inst.underlying_id} != {inst.con_id})")
    if inst.sec_type == "OPT":
        _opt(inst, label)


def _validate_snapshot(snapshot: Snapshot) -> None:
    """Direct-constructed snapshots are NOT trusted; validate structural numbers.

    The collector validates its own output, but ``Snapshot`` is a plain public
    dataclass whose constructor accepts arbitrary objects.  Every *structural*
    number (cash, fx, quantity, remaining, multiplier, strike) is validated and
    raises on malformed input.  Soft fields (market price, limit price, net
    liquidation) are sanitised at their read sites to ``None`` (unknown, never
    zero) instead of raising.  This function never mutates its argument.
    """
    if not isinstance(snapshot, Snapshot):
        raise RiskDataError("expected a Snapshot")
    if not isinstance(snapshot.account, str) or not snapshot.account:
        raise RiskDataError("snapshot: missing account")
    if snapshot.environment not in {"paper", "live"}:
        raise RiskDataError("snapshot: invalid environment")
    if not isinstance(snapshot.captured_at, datetime) or snapshot.captured_at.tzinfo is None:
        raise RiskDataError("snapshot: timestamp must be timezone-aware")
    if not isinstance(snapshot.base_currency, str) or not snapshot.base_currency:
        raise RiskDataError("snapshot: missing base currency")
    for name, mapping, positive in (("cash", snapshot.cash, False),
                                    ("fx", snapshot.fx, True)):
        if not isinstance(mapping, Mapping):
            raise RiskDataError(f"snapshot: {name} must be a mapping")
        for key, value in mapping.items():
            if not isinstance(key, str):
                raise RiskDataError(f"snapshot: invalid {name} key")
            _dec(value, f"snapshot {name} {key}", positive=positive)
    if snapshot.base_currency in snapshot.fx and \
            _dec(snapshot.fx[snapshot.base_currency], "base FX") != ONE:
        raise RiskDataError("snapshot: base-currency FX must equal one")
    for i, p in enumerate(snapshot.positions):
        if not isinstance(p, Position):
            raise RiskDataError(f"snapshot position {i}: expected a Position")
        _validate_instrument(p.instrument, f"snapshot position {i}")
        _dec(p.quantity, f"snapshot position {i} quantity")
    for i, o in enumerate(snapshot.orders):
        if not isinstance(o, WorkingOrder):
            raise RiskDataError(f"snapshot order {i}: expected a WorkingOrder")
        _validate_instrument(o.instrument, f"snapshot order {i}")
        if not isinstance(o.action, str) or o.action.upper() not in {"BUY", "SELL"}:
            raise RiskDataError(f"snapshot order {i}: invalid action")
        _dec(o.remaining, f"snapshot order {i} remaining", minimum=ZERO)


def _reserved(reservations: Mapping | None) -> dict[str, Any]:
    """Normalise and strictly validate the local reservation mapping (no mutation)."""
    reservations = {} if reservations is None else reservations
    if not isinstance(reservations, Mapping):
        raise RiskDataError("reservations: expected a mapping")
    unknown = set(reservations) - RESERVATION_BUCKETS
    if unknown:
        raise RiskDataError(f"reservations: unknown bucket(s) {sorted(unknown)}")
    out: dict[str, Any] = {
        "cash_base": ZERO, "shares": {}, "close": {}, "exposure": {},
    }
    out["cash_base"] = _dec(reservations.get("cash_base", ZERO),
                            "reservation cash_base", minimum=ZERO)
    for bucket in ("shares", "close", "exposure"):
        section = reservations.get(bucket, {})
        if not isinstance(section, Mapping):
            raise RiskDataError(f"reservations {bucket}: expected a mapping")
        for key, value in section.items():
            if bucket == "close":
                # conId keys: int, or a decimal digit-string (JSON-friendly).
                if isinstance(key, bool) or not (
                        isinstance(key, int) or
                        (isinstance(key, str) and key.isdigit())):
                    raise RiskDataError(
                        f"reservations close: invalid conId key {key!r}")
            else:
                # shares / exposure MUST use the underlying key format
                # "<underlying_id>:<CURRENCY>" (never a bare conId or symbol).
                if (not isinstance(key, str) or ":" not in key or
                        not key.split(":", 1)[0].isdigit() or
                        not key.split(":", 1)[1]):
                    raise RiskDataError(
                        f"reservations {bucket}: invalid underlying key {key!r}")
            out[bucket][key] = _dec(value, f"reservation {bucket}[{key}]",
                                    minimum=ZERO)
    return out


@dataclass(frozen=True)
class OrderRequest:
    """One proposed order.  ``quantity`` must be a positive, finite Decimal.

    Bools and strings are rejected outright (they are ambiguous coercions);
    ``Decimal`` / ``int`` / ``float`` are normalised to ``Decimal`` losslessly.
    ``limit_price`` bounds the notional of a stock BUY; ``order_type`` may be
    ``MKT`` (unbounded -> refused for stock buys) or ``LMT`` etc.
    """
    instrument: Instrument
    action: str                 # "BUY" / "SELL"
    quantity: Decimal
    order_type: str             # "MKT" / "LMT" / ...
    limit_price: Decimal | None = None
    tif: str = "DAY"

    def __post_init__(self):
        _validate_instrument(self.instrument, "request")
        if not isinstance(self.action, str) or self.action.upper() not in {"BUY", "SELL"}:
            raise RiskDataError("request: action must be BUY or SELL")
        if not isinstance(self.order_type, str) or not self.order_type:
            raise RiskDataError("request: missing order type")
        if not isinstance(self.tif, str) or not self.tif:
            raise RiskDataError("request: missing TIF")
        if isinstance(self.quantity, bool):
            raise RiskDataError("request: quantity must not be a bool")
        if isinstance(self.quantity, str):
            raise RiskDataError("request: quantity must be a number, not a string")
        q = _dec(self.quantity, "request quantity", positive=True)
        if self.instrument.sec_type == "OPT" and q != q.to_integral_value():
            raise RiskDataError("request: option quantity must be a whole number")
        object.__setattr__(self, "action", self.action.upper())
        object.__setattr__(self, "order_type", self.order_type.upper())
        object.__setattr__(self, "quantity", q)
        if self.limit_price is not None:
            object.__setattr__(self, "limit_price",
                               _dec(self.limit_price, "request limit price", positive=True))

    @property
    def increases_risk(self) -> bool:
        inst = self.instrument
        if inst.sec_type == "STK":
            return self.action == "BUY"
        if inst.sec_type == "OPT":
            return self.action == "SELL"
        return True  # fail closed for unrecognised instruments


@dataclass(frozen=True)
class RiskLimits:
    """Validated risk configuration.  Amounts are in ``base_currency``.

    An ``enabled=False`` (legacy / missing) config still enforces the baseline
    safety rules in :func:`assess`; only the *optional* P3 caps are disabled,
    surfaced via the ``display`` string.
    """
    enabled: bool
    day_timezone: str
    max_daily_submissions: int
    per_underlying_amount: Decimal | None = None
    portfolio_amount: Decimal | None = None
    per_underlying_ratio: Decimal | None = None
    portfolio_ratio: Decimal | None = None
    max_snapshot_age_seconds: int | None = None
    display: str = "P3 risk limits enabled"

    @property
    def per_underlying(self) -> Mapping[str, Decimal | None]:
        return {"amount": self.per_underlying_amount, "ratio": self.per_underlying_ratio}

    @property
    def portfolio(self) -> Mapping[str, Decimal | None]:
        return {"amount": self.portfolio_amount, "ratio": self.portfolio_ratio}

    @property
    def has_exposure_caps(self) -> bool:
        return any(v is not None for v in (
            self.per_underlying_amount, self.portfolio_amount,
            self.per_underlying_ratio, self.portfolio_ratio))


def _cfg_amount(section: Mapping, key: str, label: str) -> Decimal | None:
    if key not in section or section[key] is None:
        return None  # optional cap absent; never treated as zero
    value = section[key]
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise RiskConfigError(f"{label}: expected a finite positive number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise RiskConfigError(f"{label}: expected a finite positive number") from None
    if not result.is_finite() or result <= 0:
        raise RiskConfigError(f"{label}: expected a finite positive number")
    return result


def validate_risk_config(doc: Mapping | None) -> RiskLimits:
    """Validate a config mapping; explicit invalid values are never softened.

    ``doc`` is the whole config dict; its ``"risk"`` sub-mapping holds the P3
    settings.  A missing / ``None`` / ``risk.enabled: false`` config yields a
    disabled :class:`RiskLimits` whose ``display`` says P3 is off, but whose
    baseline safety checks are still applied by :func:`assess`.  When explicitly
    enabled, ``day_timezone`` (IANA) and a positive ``max_daily_submissions``
    are required; per-underlying / portfolio amount and ratio caps are optional,
    and when both an amount and a ratio are given both must be satisfied.
    """
    if doc is None:
        doc = {}
    if not isinstance(doc, Mapping):
        raise RiskConfigError("config: expected a mapping")
    raw = doc.get("risk")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise RiskConfigError("risk: expected a mapping")
    enabled_raw = raw.get("enabled", False)
    if not isinstance(enabled_raw, bool):
        # A strict bool is required: 'false'/0/'1'/None etc. must not silently
        # enable or disable P3 limits (no ambiguous coercions).
        raise RiskConfigError("risk.enabled: expected a boolean (strict)")
    if not enabled_raw:
        return RiskLimits(False, "UTC", 0, display="P3 risk limits disabled (未启用)")

    tz = raw.get("day_timezone")
    if not isinstance(tz, str) or not tz:
        raise RiskConfigError("risk.day_timezone: expected an IANA timezone name")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise RiskConfigError(f"risk.day_timezone: unknown IANA timezone {tz!r}") from None

    daily = raw.get("max_daily_submissions")
    if isinstance(daily, bool) or not isinstance(daily, int) or daily <= 0:
        raise RiskConfigError("risk.max_daily_submissions: expected a positive integer")

    per = raw.get("per_underlying")
    port = raw.get("portfolio")
    for name, section in (("per_underlying", per), ("portfolio", port)):
        if section is not None and not isinstance(section, Mapping):
            raise RiskConfigError(f"risk.{name}: expected a mapping")
    per = per or {}
    port = port or {}
    per_amount = _cfg_amount(per, "amount", "risk.per_underlying.amount")
    port_amount = _cfg_amount(port, "amount", "risk.portfolio.amount")
    per_ratio = _cfg_amount(per, "ratio", "risk.per_underlying.ratio")
    port_ratio = _cfg_amount(port, "ratio", "risk.portfolio.ratio")
    age = raw.get("max_snapshot_age_seconds")
    if age is not None:
        if isinstance(age, bool) or not isinstance(age, int) or age <= 0:
            raise RiskConfigError("risk.max_snapshot_age_seconds: expected a positive integer")
    return RiskLimits(True, tz, daily, per_amount, port_amount, per_ratio,
                      port_ratio, age, display="P3 risk limits enabled")


def day_key(snapshot: Snapshot, tz_name: str, *, now: datetime | None = None) -> str:
    """Natural-day key: ``account|environment|YYYY-MM-DD`` in the configured zone.

    Uses a real IANA ``ZoneInfo`` so daylight-saving transitions are honoured;
    this is a user-configured calendar day, not a market trading day.
    """
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise RiskConfigError(f"unknown IANA timezone {tz_name!r}") from None
    moment = now or snapshot.captured_at
    if moment.tzinfo is None:
        raise RiskDataError("day_key: now must be timezone-aware")
    return f"{snapshot.account}|{snapshot.environment}|{moment.astimezone(zone).date().isoformat()}"


@dataclass
class _State:
    """Fully validated intermediate account risk state (all money in BASE)."""
    cash_by_currency: dict[str, Decimal] = field(default_factory=dict)
    cash_base: Decimal = ZERO
    cash_known: bool = True
    shares: dict[str, Decimal] = field(default_factory=dict)
    # Call cover: shares already committed to short calls / call sells / stock
    # sells, per underlying key, for report breakdown (no new math elsewhere).
    call_cover_required: dict[str, Decimal] = field(default_factory=dict)
    # Put cash obligations (strike*mult*qty) by trade currency, for report.
    put_obligations_by_currency: dict[str, Decimal] = field(default_factory=dict)
    close: dict[int, Decimal] = field(default_factory=dict)
    exposure: dict[str, Decimal] = field(default_factory=dict)
    exposure_known: bool = True
    reserved_cash: Decimal = ZERO
    reserved_exposure: dict[str, Decimal] = field(default_factory=dict)
    unknown: list[str] = field(default_factory=list)

    def bump(self, key: str, delta: Decimal) -> None:
        self.shares[key] = self.shares.get(key, ZERO) + delta

    def cover_required(self, key: str, delta: Decimal) -> None:
        self.call_cover_required[key] = self.call_cover_required.get(key, ZERO) + delta

    def put_obligation(self, cur: str, delta: Decimal) -> None:
        self.put_obligations_by_currency[cur] = \
            self.put_obligations_by_currency.get(cur, ZERO) + delta

    def exposure_add(self, key: str, delta: Decimal) -> None:
        self.exposure[key] = self.exposure.get(key, ZERO) + delta

    @property
    def exposure_total(self) -> Decimal:
        return sum(self.exposure.values(), ZERO) + sum(self.reserved_exposure.values(), ZERO)

    def exposure_for(self, key: str) -> Decimal:
        return self.exposure.get(key, ZERO) + self.reserved_exposure.get(key, ZERO)


def _stock_prices(snapshot: Snapshot, state: _State) -> dict[str, Decimal | None]:
    """Per-underlying stock valuation basis (BASE per share), None if unconfirmed."""
    priced: dict[str, Decimal | None] = {}
    for p in snapshot.positions:
        if p.instrument.sec_type != "STK" or _dec(p.quantity, "qty") <= 0:
            continue
        inst = p.instrument
        price = _price(p.price)
        if price is None:
            state.exposure_known = False
            state.unknown.append(f"Missing stock price for conId {inst.con_id}")
            priced[inst.key] = None
            continue
        rate = _rate(snapshot, inst.currency)
        if rate is None:
            state.exposure_known = False
            state.unknown.append(f"Missing FX for {inst.currency}")
            priced[inst.key] = None
            continue
        per_share = price * rate
        prev = priced.get(inst.key)
        if inst.key in priced and prev is None:
            pass  # already unconfirmed
        elif prev is not None and prev != per_share:
            priced[inst.key] = None
            state.exposure_known = False
            state.unknown.append(f"Inconsistent stock prices for {inst.key}")
        else:
            priced[inst.key] = per_share
    return priced


def _check_signed_rows(snapshot: Snapshot) -> None:
    """Conflicting long/short rows for one conId are contradictory data.

    Netting a -1 and a +1 row to zero would let a phantom close capacity appear
    (or hide a real obligation).  Reject rather than aggregate across opposite
    signs; clean same-sign duplicate rows are still netted by _build_state.
    """
    signs: dict[int, set[int]] = {}
    for p in snapshot.positions:
        qty = _dec(p.quantity, "position quantity")
        if qty == 0:
            continue
        signs.setdefault(p.instrument.con_id, set()).add(1 if qty > 0 else -1)
    for con_id, present in signs.items():
        if len(present) > 1:
            raise RiskDataError(
                f"Conflicting signed position rows for conId {con_id}")


def _validate_identity(snapshot: Snapshot,
                       request_instrument: Instrument | None = None) -> None:
    """Reject inconsistent identity across positions, orders and the request.

    The proposed order's exact conId must match any held/working instrument
    with that conId, otherwise a BUY could be treated as a close of a
    completely different contract.
    """
    identity: dict[int, Instrument] = {}
    for p in snapshot.positions:
        inst = p.instrument
        prev = identity.get(inst.con_id)
        if prev is not None and prev != inst:
            raise RiskDataError(
                f"Inconsistent instrument identity for conId {inst.con_id}")
        identity[inst.con_id] = inst
    for o in snapshot.orders:
        inst = o.instrument
        prev = identity.get(inst.con_id)
        if prev is not None and prev != inst:
            raise RiskDataError(
                f"Inconsistent instrument identity for conId {inst.con_id}")
        identity[inst.con_id] = inst
    if request_instrument is not None:
        con_id = request_instrument.con_id
        prev = identity.get(con_id)
        if prev is not None and prev != request_instrument:
            raise RiskDataError(
                f"Request instrument conflicts with conId {con_id} in snapshot")


def _build_state(snapshot: Snapshot, reservations: Mapping | None,
                 request_instrument: Instrument | None = None) -> _State:
    """Single source of truth for collateral, cover, close capacity, exposure.

    Used by both :func:`assess` and :func:`summarize` so the report module never
    needs to duplicate any collateral / exposure calculation.  Every numeric
    input is normalised through ``_dec`` at its read site, so int/str/Decimal
    values in a hand-built Snapshot all behave identically and no input object
    is mutated.
    """
    res = _reserved(reservations)
    state = _State()
    state.reserved_cash = res["cash_base"]
    state.reserved_exposure = dict(res["exposure"])

    # ---- cash: ONE global pool, every currency + negative balances, never BASE.
    for currency, amount in snapshot.cash.items():
        cur = currency.upper()
        if cur == "BASE":
            continue
        value = _dec(amount, f"cash {cur}")
        rate = _rate(snapshot, cur)
        if rate is None:
            state.cash_known = False
            state.unknown.append(f"Missing FX for cash {cur}")
            continue
        state.cash_by_currency[cur] = state.cash_by_currency.get(cur, ZERO) + value
        state.cash_base += value * rate
    if not state.cash_by_currency:
        state.cash_known = False
        state.unknown.append("No detailed cash balances")

    # Identity must be consistent before any aggregation.
    _check_signed_rows(snapshot)
    _validate_identity(snapshot, request_instrument)
    stock_prices = _stock_prices(snapshot, state)
    for rkey, val in res["shares"].items():
        state.bump(rkey, -val)  # keys pre-validated as "<underlying_id>:<CUR>"

    def add_put(cur: str, amount: Decimal) -> None:
        # Record the trade-currency obligation for the report breakdown (the
        # same figure used to reduce the shared cash pool).
        state.put_obligation(cur, amount)
        rate = _rate(snapshot, cur)
        if rate is None:
            state.cash_known = False
            state.unknown.append(f"Missing FX for put obligation {cur}")
            return
        state.cash_base -= amount * rate

    def cover_deduct(key: str, shares: Decimal) -> None:
        # Short call / call sell obligations consume cover; track "required"
        # separately while reducing the net available shares.
        state.cover_required(key, shares)
        state.bump(key, -shares)

    def put_exposure(cur: str, key: str, amount: Decimal) -> None:
        rate = _rate(snapshot, cur)
        if rate is None:
            state.exposure_known = False
            state.unknown.append(f"Missing FX for put exposure {cur}")
        else:
            state.exposure_add(key, amount * rate)

    # ---- Positions ---------------------------------------------------------
    for p in snapshot.positions:
        inst = p.instrument
        qty = _dec(p.quantity, "position quantity")
        if inst.sec_type == "STK":
            state.bump(inst.key, qty)
            if qty > 0:
                per_share = stock_prices.get(inst.key)
                if per_share is None:
                    state.exposure_known = False
                    state.unknown.append(f"Unvalued stock position {inst.key}")
                else:
                    state.exposure_add(inst.key, qty * per_share)
        elif inst.sec_type == "OPT":
            mult = _dec(inst.multiplier, "position multiplier", positive=True)
            if qty < 0:
                net_short = -qty
                if inst.right.upper() == "C":
                    cover_deduct(inst.key, net_short * mult)
                else:
                    strike = _dec(inst.strike, "position strike", positive=True)
                    obligation = net_short * strike * mult
                    add_put(inst.currency, obligation)
                    put_exposure(inst.currency, inst.key, obligation)
                state.close[inst.con_id] = state.close.get(inst.con_id, ZERO) + net_short
            elif qty > 0:
                # A long / unrecognised option is a fail-closed condition for
                # any risk-increasing request (enforced in _baseline); its
                # payoff is unbounded so exposure is also unverifiable.
                state.exposure_known = False
                state.unknown.append(f"Long/unrecognised option conId {inst.con_id}")

    # ---- Working orders ---------------------------------------------------
    for o in snapshot.orders:
        inst = o.instrument
        rem = _dec(o.remaining, "order remaining", minimum=ZERO)
        action = o.action.upper()
        if inst.sec_type == "STK":
            if action == "SELL":
                state.bump(inst.key, -rem)  # pending stock sells remove cover
            else:  # BUY
                # A bounded notional exists ONLY for a limit order carrying a
                # real limit price.  A MKT order (even with a stray
                # lmtPrice) has no verifiable upper bound.
                bounded = (o.order_type.upper() == "LMT" and
                           _price(o.limit_price) is not None)
                if not bounded:
                    state.exposure_known = False
                    state.cash_known = False
                    state.unknown.append(
                        f"Unbounded pending stock buy {inst.key} "
                        "(requires order_type LMT with a limit price)")
                    continue
                limit = _price(o.limit_price)
                rate = _rate(snapshot, inst.currency)
                if rate is None:
                    state.exposure_known = False
                    state.cash_known = False
                    state.unknown.append(f"Missing FX for pending buy {inst.currency}")
                    continue
                notional_base = rem * limit * rate
                # A pending stock BUY spends the SAME global cash pool as put
                # collateral (converted across all currencies), and counts as
                # potential holding exposure.
                state.cash_base -= notional_base
                state.exposure_add(inst.key, notional_base)
        elif inst.sec_type == "OPT":
            mult = _dec(inst.multiplier, "order multiplier", positive=True)
            if action == "SELL":
                if inst.right.upper() == "C":
                    cover_deduct(inst.key, rem * mult)
                else:
                    strike = _dec(inst.strike, "order strike", positive=True)
                    obligation = rem * strike * mult
                    add_put(inst.currency, obligation)
                    put_exposure(inst.currency, inst.key, obligation)
            else:  # BUY: a future buyback never releases cover / collateral
                if rem == ZERO:
                    continue
                capacity = state.close.get(inst.con_id, ZERO)
                state.close[inst.con_id] = capacity - rem
                if rem > capacity:
                    state.cash_known = False
                    state.exposure_known = False
                    state.unknown.append(
                        f"Pending option BUY exceeds short close capacity for conId {inst.con_id}")
                limit = _price(o.limit_price)
                if o.order_type.upper() != "LMT" or limit is None:
                    state.cash_known = False
                    state.exposure_known = False
                    state.unknown.append(
                        f"Unbounded pending option buy conId {inst.con_id} "
                        "(requires order_type LMT with a limit price)")
                    continue
                rate = _rate(snapshot, inst.currency)
                if rate is None:
                    state.cash_known = False
                    state.exposure_known = False
                    state.unknown.append(
                        f"Missing FX for pending option buy {inst.currency}")
                    continue
                # Premium is an additional debit, not a replacement for the
                # held short's full cover/collateral until an actual fill.
                state.cash_base -= rem * mult * limit * rate

    # ---- Local reservations: exact conId close capacity is CONSUMED only ---
    for cid, val in res["close"].items():
        con_id = int(cid) if isinstance(cid, str) else cid
        state.close[con_id] = state.close.get(con_id, ZERO) - val

    if snapshot.issues:
        state.unknown.extend(snapshot.issues)
        state.exposure_known = False
    return state


def _snapshot_fresh(snapshot: Snapshot, limits: RiskLimits,
                    now: datetime, state: _State) -> bool:
    if limits.max_snapshot_age_seconds is None:
        return True
    age = (now - snapshot.captured_at).total_seconds()
    if age < 0 or age > limits.max_snapshot_age_seconds:
        state.unknown.append(f"Snapshot age {age:.0f}s exceeds freshness limit")
        return False
    return True


def _baseline(snapshot: Snapshot, request: OrderRequest, limits: RiskLimits,
              state: _State, now: datetime, details: dict) -> list[str]:
    """Baseline execution safety; ALWAYS enforced, even when P3 is disabled."""
    reasons: list[str] = []
    inst, qty, action = request.instrument, request.quantity, request.action
    key = inst.key
    increasing = request.increases_risk

    if not _snapshot_fresh(snapshot, limits, now, state) and increasing:
        reasons.append("snapshot is stale or its age cannot be confirmed")

    # Fail closed on unknown data quality / long options for any increase.
    if increasing:
        for p in snapshot.positions:
            if p.instrument.sec_type == "OPT" and _dec(p.quantity, "qty") > 0:
                reasons.append(
                    f"long/unrecognised option conId {p.instrument.con_id} blocks "
                    "risk-increasing order")
                break
        if snapshot.issues:
            reasons.append("snapshot quality unconfirmed (" +
                           "; ".join(snapshot.issues) + "); blocking risk increase")

    # A genuine short close bypasses optional P3 exposure caps, not baseline
    # funding: its bounded premium must fit after all old obligations remain.
    if inst.sec_type == "OPT" and action == "BUY":
        capacity = state.close.get(inst.con_id, ZERO)
        details["short_close_capacity"] = str(capacity)
        if qty > capacity:
            reasons.append(
                f"BUY {qty} exceeds confirmed short close capacity {capacity} "
                f"for conId {inst.con_id} (pending buybacks and reservations deducted)")
        if request.order_type != "LMT" or request.limit_price is None:
            reasons.append(
                "option BUY has no bounded premium; requires order_type LMT "
                "with a limit price")
        else:
            rate = _rate(snapshot, inst.currency)
            if rate is None:
                reasons.append(f"missing FX for {inst.currency}; cannot bound option BUY")
            else:
                premium = (qty * _dec(inst.multiplier, "request multiplier", positive=True)
                           * request.limit_price * rate)
                free = state.cash_base - state.reserved_cash
                details["option_buy_premium_base"] = str(premium)
                details["free_cash_base"] = str(free)
                if not state.cash_known:
                    reasons.append("cash pool or FX incomplete; cannot fund option BUY")
                elif free < premium:
                    reasons.append(
                        f"option BUY needs {premium} BASE, free cash {free} BASE "
                        "(held/pending collateral, purchase premiums and reservations retained)")
        return reasons

    if inst.sec_type == "STK" and action == "SELL":
        available = state.shares.get(key, ZERO)
        details["available_shares"] = str(available)
        if qty > available:
            reasons.append(
                f"stock SELL {qty} exceeds uncovered shares {available} for {key} "
                "(short calls, call sells and pending stock sells deducted)")

    elif inst.sec_type == "OPT" and action == "SELL":
        mult = _dec(inst.multiplier, "request multiplier", positive=True)
        if inst.right.upper() == "C":
            need = qty * mult
            available = state.shares.get(key, ZERO)
            details["call_shares_required"] = str(need)
            details["available_shares"] = str(available)
            if available < need:
                reasons.append(
                    f"covered call needs {need} shares for {key}, only {available} "
                    "uncovered (pending stock sells and short call sells deducted)")
        else:  # PUT
            strike = _dec(inst.strike, "request strike", positive=True)
            required = qty * strike * mult
            rate = _rate(snapshot, inst.currency)
            if rate is None:
                reasons.append(f"missing FX for {inst.currency}; cannot confirm put cash")
            else:
                required_base = required * rate
                free = state.cash_base - state.reserved_cash
                details["put_required"] = str(required)
                details["put_required_currency"] = inst.currency
                details["put_required_base"] = str(required_base)
                details["free_cash_base"] = str(free)
                # The proposed short put is also potential holding exposure.
                state.exposure_add(key, required_base)
                if not state.cash_known:
                    reasons.append("cash pool or FX incomplete; cannot confirm put collateral")
                elif free < required_base:
                    reasons.append(
                        f"cash-secured put needs {required} {inst.currency} "
                        f"({required_base} BASE), free cash {free} BASE "
                        "(all currencies share one pool; held/pending puts deducted)")

    elif inst.sec_type == "STK" and action == "BUY":
        # A verifiable notional bound exists ONLY for a LMT order with a real
        # limit price.  MKT (even carrying a stray limit_price) has no bound.
        bounded = (request.order_type == "LMT" and
                   request.limit_price is not None)
        if not bounded:
            reasons.append(
                "stock BUY has no bounded notional; requires order_type LMT "
                "with a limit price (MKT buys are refused)")
        else:
            rate = _rate(snapshot, inst.currency)
            if rate is None:
                reasons.append(f"missing FX for {inst.currency}; cannot bound stock BUY")
            else:
                notional = qty * request.limit_price * rate
                free = state.cash_base - state.reserved_cash
                details["stock_buy_notional_base"] = str(notional)
                details["free_cash_base"] = str(free)
                state.exposure_add(key, notional)  # bounded by the limit price
                if not state.cash_known:
                    reasons.append("cash pool or FX incomplete; cannot fund stock BUY")
                elif free < notional:
                    reasons.append(
                        f"stock BUY needs {notional} BASE, free cash {free} BASE "
                        "(single shared pool; pending buys and put obligations deducted)")

    return reasons


def _exposure_caps(snapshot: Snapshot, request: OrderRequest, limits: RiskLimits,
                   state: _State, details: dict) -> list[str]:
    reasons: list[str] = []
    if not limits.enabled or not request.increases_risk:
        return reasons
    key = request.instrument.key
    per_now = state.exposure_for(key)
    total_now = state.exposure_total
    details["exposure_underlying_base"] = str(per_now)
    details["exposure_portfolio_base"] = str(total_now)

    if not limits.has_exposure_caps:
        return reasons  # no exposure cap to enforce; baseline safety still applies
    if not state.exposure_known:
        reasons.append("exposure cannot be confirmed (missing price/FX/netliq); "
                       "blocking limit check rather than assuming zero")
        return reasons

    netliq_base = _price(snapshot.net_liquidation)
    if netliq_base is not None:
        details["net_liquidation_base"] = str(netliq_base)
    if (limits.per_underlying_ratio is not None or limits.portfolio_ratio is not None) \
            and netliq_base is None:
        reasons.append("ratio limits configured but net liquidation is missing/unverifiable")

    def check(label: str, current: Decimal, amount: Decimal | None,
              ratio: Decimal | None) -> None:
        if amount is not None and current > amount:
            reasons.append(f"{label} exposure {current} BASE exceeds amount cap {amount}")
        if ratio is not None and netliq_base is not None:
            cap = netliq_base * ratio
            if current > cap:
                reasons.append(f"{label} exposure {current} BASE exceeds ratio cap {cap}")

    check("per-underlying", per_now, limits.per_underlying_amount,
          limits.per_underlying_ratio)
    check("portfolio", total_now, limits.portfolio_amount, limits.portfolio_ratio)
    return reasons


@dataclass(frozen=True)
class Decision:
    """Explicit, explainable outcome of :func:`assess`."""
    decision: str                 # ALLOW / BLOCK
    reasons: tuple[str, ...]
    details: Mapping[str, str]
    limits: RiskLimits | None = None
    day_key: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW

    @property
    def display(self) -> str:
        return "ALLOW" if self.allowed else "BLOCK"


def assess(snapshot: Snapshot, request: OrderRequest, limits: RiskLimits,
           reservations: Mapping | None = None, *, now: datetime | None = None) -> Decision:
    """Pure risk decision for one proposed order.

    ``limits`` is the validated :class:`RiskLimits` (an invalid/legacy config is
    represented by ``enabled=False`` and still enforces baseline safety).
    ``reservations`` is the local ledger's aggregate mapping documented at the
    top of this module.  ``now`` is injectable for deterministic DST/freshness
    tests; it defaults to ``datetime.now(timezone.utc)``.
    """
    _validate_snapshot(snapshot)
    if not isinstance(request, OrderRequest):
        raise RiskDataError("expected an OrderRequest")
    if not isinstance(limits, RiskLimits):
        raise RiskConfigError("expected a validated RiskLimits")
    moment = now or datetime.now(timezone.utc)
    details: dict[str, str] = {
        "account": snapshot.account,
        "environment": snapshot.environment,
        "base_currency": snapshot.base_currency,
        "underlying_key": request.instrument.key,
        "action": request.action,
        "quantity": str(request.quantity),
        "limits_display": limits.display,
    }
    state = _build_state(snapshot, reservations, request.instrument)
    details["cash_pool_base"] = str(state.cash_base)
    details["reserved_cash_base"] = str(state.reserved_cash)
    details["snapshot_issues"] = "; ".join(state.unknown) if state.unknown else "none"

    reasons = _baseline(snapshot, request, limits, state, moment, details)
    reasons += _exposure_caps(snapshot, request, limits, state, details)

    day = None
    if limits.enabled and limits.day_timezone:
        day = day_key(snapshot, limits.day_timezone, now=moment)
        details["day_key"] = day
        # Daily submissions are an authoritative-ledger concern; the pure layer
        # surfaces the key and the configured cap for the execution layer to
        # atomically claim against, and never pretends to count submissions.
        details["max_daily_submissions"] = str(limits.max_daily_submissions)

    decision = BLOCK if reasons else ALLOW
    if not reasons:
        reasons = ["baseline safety and P3 limits satisfied"]
    return Decision(decision, tuple(reasons), details, limits, day)


def summarize(snapshot: Snapshot, reservations: Mapping | None = None,
              limits: RiskLimits | None = None) -> Mapping:
    """Read-only risk summary reusing the exact :func:`assess` internals.

    Returns a plain mapping (JSON / ``report.py`` friendly) with base-currency
    amounts as strings.  No collateral / exposure math is duplicated here: the
    report module (owned elsewhere) consumes this mapping directly.  Unrelated
    missing prices are reported via ``exposure_confirmed=False`` rather than
    hidden; ``None``-valued data is never shown as zero.  The input Snapshot is
    never mutated.

    ``call_cover`` gives required/available shares per underlying key and
    ``put_cash_obligations`` gives ``strike*mult*qty`` per trade currency, both
    derived from the same state used by :func:`assess`.  When ``limits`` is
    supplied, ``limits``/``headroom`` add the configured amount caps and
    remaining budget (ratio caps are reported separately as they need netliq).
    """
    _validate_snapshot(snapshot)
    if limits is not None and not isinstance(limits, RiskLimits):
        raise RiskConfigError("expected a validated RiskLimits")
    state = _build_state(snapshot, reservations)
    # Keep the identities of unvalued holdings/orders: an absent valuation is
    # unknown, not a zero exposure with the entire limit available as headroom.
    keys = sorted(set(state.exposure) | set(state.reserved_exposure) |
                  {p.instrument.key for p in snapshot.positions
                   if _dec(p.quantity, "quantity") != ZERO} |
                  {o.instrument.key for o in snapshot.orders
                   if _dec(o.remaining, "remaining") != ZERO})
    cover_keys = sorted(set(state.shares) | set(state.call_cover_required))
    call_cover = {
        k: {"required_shares": str(state.call_cover_required.get(k, ZERO)),
            "available_shares": str(state.shares.get(k, ZERO))}
        for k in cover_keys
    }
    result: dict[str, Any] = {
        "account": snapshot.account,
        "environment": snapshot.environment,
        "captured_at": snapshot.captured_at.isoformat(),
        "base_currency": snapshot.base_currency,
        "cash_by_currency": {c: str(v) for c, v in state.cash_by_currency.items()},
        "cash_pool_base": str(state.cash_base),
        "free_cash_base": str(state.cash_base - state.reserved_cash),
        "reserved_cash_base": str(state.reserved_cash),
        "cash_confirmed": state.cash_known,
        "shares": {k: str(v) for k, v in state.shares.items()},
        "call_cover": call_cover,
        "put_cash_obligations": {c: str(v) for c, v in sorted(
            state.put_obligations_by_currency.items())},
        "short_close_capacity": {str(k): str(v) for k, v in state.close.items()},
        "exposure_by_underlying": {
            k: str(state.exposure_for(k)) if state.exposure_known else None for k in keys},
        "exposure_portfolio_base": (str(state.exposure_total)
                                    if state.exposure_known else None),
        "exposure_confirmed": state.exposure_known,
        "unknown": list(state.unknown),
        "snapshot_issues": list(snapshot.issues),
        "net_liquidation": (None if _price(snapshot.net_liquidation) is None
                            else str(_price(snapshot.net_liquidation))),
    }
    if limits is not None:
        per_amt = limits.per_underlying_amount
        port_amt = limits.portfolio_amount
        result["limits"] = {
            "enabled": limits.enabled,
            "display": limits.display,
            "per_underlying_amount_base": (None if per_amt is None else str(per_amt)),
            "portfolio_amount_base": (None if port_amt is None else str(port_amt)),
            "per_underlying_ratio": (None if limits.per_underlying_ratio is None
                                     else str(limits.per_underlying_ratio)),
            "portfolio_ratio": (None if limits.portfolio_ratio is None
                                else str(limits.portfolio_ratio)),
        }
        netliq = _price(snapshot.net_liquidation)
        per_head = (None if per_amt is None else
                    {k: (str(per_amt - state.exposure_for(k))
                         if state.exposure_known else None) for k in keys})
        result["headroom"] = {
            "per_underlying_amount_base": per_head,
            "portfolio_amount_base": (None if port_amt is None or not state.exposure_known
                                      else str(port_amt - state.exposure_total)),
            "net_liquidation_base": (None if netliq is None else str(netliq)),
        }
    return result


__all__ = [
    "ALLOW", "BLOCK", "RESERVATION_BUCKETS",
    "RiskConfigError", "RiskDataError",
    "OrderRequest", "RiskLimits", "Decision",
    "validate_risk_config", "day_key", "assess", "summarize",
]
