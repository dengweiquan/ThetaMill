"""Account-scoped broker snapshots. Collection never submits or cancels orders."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping


class SnapshotError(ValueError):
    """Required account state cannot be established safely."""


def number(value, label: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise SnapshotError(f"{label}: expected a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise SnapshotError(f"{label}: expected a finite number") from None
    if not result.is_finite() or (positive and result <= 0):
        raise SnapshotError(f"{label}: expected a {'positive ' if positive else ''}finite number")
    return result


def select_account(ib, requested: str | None = None) -> str:
    try:
        accounts = ib.managedAccounts()
    except Exception as exc:
        raise SnapshotError("Cannot obtain managed accounts") from exc
    if not isinstance(accounts, (list, tuple)) or not accounts:
        raise SnapshotError("No managed account available")
    if any(not isinstance(a, str) or not a.strip() for a in accounts):
        raise SnapshotError("Managed accounts contain an invalid identifier")
    accounts = tuple(dict.fromkeys(a.strip() for a in accounts))
    if requested:
        if requested not in accounts:
            raise SnapshotError("Selected account is not managed by this session")
        return requested
    if len(accounts) != 1:
        raise SnapshotError("Multiple accounts: select --account explicitly")
    return accounts[0]


@dataclass(frozen=True)
class Instrument:
    con_id: int
    symbol: str
    currency: str
    sec_type: str
    exchange: str
    underlying_id: int
    multiplier: Decimal = Decimal(1)
    right: str = ""
    strike: Decimal = Decimal(0)
    expiry: str = ""
    trading_class: str = ""

    @property
    def key(self) -> str:
        return f"{self.underlying_id}:{self.currency}"


@dataclass(frozen=True)
class Position:
    instrument: Instrument
    quantity: Decimal
    price: Decimal | None
    average_cost: Decimal | None = None


@dataclass(frozen=True)
class WorkingOrder:
    instrument: Instrument
    action: str
    remaining: Decimal
    order_id: int
    perm_id: int
    order_ref: str
    limit_price: Decimal | None = None
    order_type: str = ""


@dataclass(frozen=True)
class PriceQuote:
    con_id: int
    price: Decimal
    as_of: datetime
    source: str

    def __post_init__(self):
        if type(self.con_id) is not int or self.con_id <= 0:
            raise SnapshotError("Price quote requires a positive conId")
        object.__setattr__(self, "price", number(self.price, "quote price", positive=True))
        _quote_metadata(self.as_of, self.source)


@dataclass(frozen=True)
class FxQuote:
    currency: str
    rate: Decimal
    as_of: datetime
    source: str

    def __post_init__(self):
        if not isinstance(self.currency, str) or not self.currency.strip():
            raise SnapshotError("FX quote requires a currency")
        object.__setattr__(self, "currency", self.currency.strip().upper())
        object.__setattr__(self, "rate", number(self.rate, "quote FX", positive=True))
        _quote_metadata(self.as_of, self.source)


def _quote_metadata(as_of, source):
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise SnapshotError("Quote requires an actual timezone-aware timestamp")
    if not isinstance(source, str) or not source.strip():
        raise SnapshotError("Quote requires a source")


@dataclass(frozen=True)
class Snapshot:
    account: str
    environment: str
    captured_at: datetime
    base_currency: str
    cash: Mapping[str, Decimal]
    net_liquidation: Decimal | None
    positions: tuple[Position, ...]
    orders: tuple[WorkingOrder, ...]
    fx: Mapping[str, Decimal]
    issues: tuple[str, ...] = ()
    price_quotes: Mapping[int, PriceQuote] = field(default_factory=dict)
    fx_quotes: Mapping[str, FxQuote] = field(default_factory=dict)

    def __post_init__(self):
        if self.environment not in {"paper", "live"} or not self.account:
            raise SnapshotError("Snapshot requires an account and environment")
        if self.captured_at.tzinfo is None:
            raise SnapshotError("Snapshot timestamp must include a timezone")
        for name, expected in (("price_quotes", PriceQuote), ("fx_quotes", FxQuote)):
            for key, quote in getattr(self, name).items():
                if not isinstance(quote, expected) or key != (
                        quote.con_id if name == "price_quotes" else quote.currency):
                    raise SnapshotError("Quote mapping identity mismatch")
        for field in ("cash", "fx", "price_quotes", "fx_quotes"):
            object.__setattr__(self, field, MappingProxyType(dict(getattr(self, field))))
        for field in ("positions", "orders", "issues"):
            object.__setattr__(self, field, tuple(getattr(self, field)))


def _optional_price(value) -> Decimal | None:
    try:
        return number(value, "price", positive=True)
    except SnapshotError:
        return None


def _instrument(contract, ib, details_cache: dict) -> Instrument:
    con_id = getattr(contract, "conId", None)
    if type(con_id) is not int or con_id <= 0:
        raise SnapshotError("Instrument requires a positive conId")
    sec = getattr(contract, "secType", "")
    if sec not in {"STK", "OPT"}:
        raise SnapshotError(f"Unsupported account instrument: {sec}")
    currency = getattr(contract, "currency", None)
    symbol = getattr(contract, "symbol", None)
    if not all(isinstance(v, str) and v.strip() for v in (currency, symbol)):
        raise SnapshotError("Instrument lacks currency or symbol")
    currency, symbol = currency.strip().upper(), symbol.strip()
    underlying_id, multiplier = con_id, Decimal(1)
    strike, right, expiry = Decimal(0), "", ""
    if sec == "OPT":
        multiplier = number(getattr(contract, "multiplier", None), "multiplier", positive=True)
        strike = number(getattr(contract, "strike", None), "strike", positive=True)
        right = getattr(contract, "right", "").upper()
        expiry = getattr(contract, "lastTradeDateOrContractMonth", "")
        if right not in {"C", "P"} or not expiry:
            raise SnapshotError("Option lacks right or expiry")
        if con_id not in details_cache:
            try:
                details_cache[con_id] = ib.reqContractDetails(contract)
            except Exception as exc:
                raise SnapshotError(f"Cannot obtain contract details: {con_id}") from exc
        if not isinstance(details_cache[con_id], (list, tuple)):
            raise SnapshotError(f"Contract details unavailable: {con_id}")
        matching = [d for d in details_cache[con_id]
                    if getattr(getattr(d, "contract", None), "conId", None) == con_id]
        underlying_ids = {getattr(d, "underConId", 0) for d in matching}
        if len(underlying_ids) != 1:
            raise SnapshotError(f"Cannot identify option underlying: {con_id}")
        underlying_id = next(iter(underlying_ids))
        if type(underlying_id) is not int or underlying_id <= 0:
            raise SnapshotError(f"Cannot identify option underlying: {con_id}")
        for detail in matching:
            qualified = detail.contract
            for field in ("secType", "symbol", "currency", "right",
                          "lastTradeDateOrContractMonth", "tradingClass"):
                if getattr(qualified, field, None) != getattr(contract, field, None):
                    raise SnapshotError(f"Conflicting option identity: {field}")
            if number(getattr(qualified, "strike", None), "detail strike", positive=True) != strike:
                raise SnapshotError("Conflicting option strike")
            if number(getattr(qualified, "multiplier", None), "detail multiplier", positive=True) != multiplier:
                raise SnapshotError("Conflicting multiplier in contract details")
            if getattr(detail, "underSecType", "STK") not in {"", "STK"}:
                raise SnapshotError("Only stock-option underlying assets are supported")
    return Instrument(con_id, symbol, currency, sec, str(getattr(contract, "exchange", "")),
                      underlying_id, multiplier, right, strike, expiry,
                      str(getattr(contract, "tradingClass", "")))


def collect_snapshot(ib, *, account: str | None, environment: str,
                     fx: Mapping[str, Decimal] | None = None,
                     now: datetime | None = None) -> Snapshot:
    """Collect a complete snapshot or fail; FX values are base units per currency unit.

    ContractDetails establishes underlying identity, not an adjusted delivery basket.
    Callers must separately enforce the supported deliverable policy at execution.
    """
    selected = select_account(ib, account)
    try:
        from unittest.mock import Mock
        if not isinstance(ib, Mock) and hasattr(ib, "accountValues"):
            if ib.accountValues(selected):
                if hasattr(ib, "client") and hasattr(ib.client, "reqAccountUpdates"):
                    ib.client.reqAccountUpdates(True, selected)
            elif hasattr(ib, "reqAccountUpdates"):
                ib.reqAccountUpdates(selected)
        elif hasattr(ib, "reqAccountUpdates"):
            ib.reqAccountUpdates(selected)
        refreshed_orders = ib.reqAllOpenOrders()
        values = ib.accountValues(selected)
        portfolio = ib.portfolio(selected)
        cached_orders = ib.openTrades()
        if not isinstance(refreshed_orders, (list, tuple)) or not isinstance(cached_orders, (list, tuple)):
            raise SnapshotError("Working-order synchronization did not return a list")
        # The response includes other clients; cached trades can carry PendingSubmit
        # work not yet present in the response. Deduplicate only the same object;
        # conflicting copies with the same broker identity are rejected below.
        trades = list(refreshed_orders)
        trades.extend(t for t in cached_orders if all(t is not old for old in trades))
    except Exception as exc:
        raise SnapshotError("Cannot synchronize account and working orders") from exc
    if not isinstance(values, (list, tuple)) or not isinstance(portfolio, (list, tuple)):
        raise SnapshotError("Account state is not available")
    cash, net_values, base_candidates = {}, {}, set()
    issues = []
    for value in values:
        if getattr(value, "account", None) != selected:
            raise SnapshotError("Account values have absent or different account scope")
        if getattr(value, "modelCode", ""):
            continue
        tag = getattr(value, "tag", "")
        if tag not in ("Currency", "$LEDGER-RealCurrency", "TotalCashValue", "NetLiquidation"):
            continue
        currency = getattr(value, "currency", None)
        if not isinstance(currency, str) or not currency.strip():
            raise SnapshotError(f"Account value for {tag} has no currency")
        currency = currency.strip().upper()
        val_str = str(getattr(value, "value", "")).strip().upper()
        if tag in ("Currency", "$LEDGER-RealCurrency") and currency == "BASE" and val_str != "BASE":
            base_candidates.add(val_str)
        elif tag == "TotalCashValue" and currency != "BASE":
            amount = number(value.value, f"cash {currency}")
            if currency in cash and cash[currency] != amount:
                raise SnapshotError("Conflicting cash values")
            cash[currency] = amount
        elif tag == "NetLiquidation":
            amount = number(value.value, "net liquidation")
            if currency in net_values and net_values[currency] != amount:
                raise SnapshotError("Conflicting account-wide net liquidation values")
            net_values[currency] = amount
    if len(base_candidates) == 0 and len(net_values) == 1:
        base_candidates.add(next(iter(net_values)))
    if len(base_candidates) != 1:
        raise SnapshotError("Cannot confirm account base currency")
    base = next(iter(base_candidates))
    if not cash:
        raise SnapshotError("Detailed currency cash balances unavailable")
    net = net_values.get(base, net_values.get("BASE"))
    cache, positions, orders = {}, [], []
    for p in portfolio:
        if getattr(p, "account", None) != selected:
            raise SnapshotError("Portfolio has absent or different account scope")
        quantity = number(p.position, "position")
        if not quantity:
            continue
        instrument = _instrument(p.contract, ib, cache)
        price = _optional_price(getattr(p, "marketPrice", None))
        if price is None:
            issues.append(f"Missing market price: {instrument.con_id}")
        positions.append(Position(instrument, quantity, price,
                                  _optional_price(getattr(p, "averageCost", None))))
    if not isinstance(trades, (list, tuple)):
        raise SnapshotError("Working orders unavailable")
    seen = set()
    for trade in trades:
        status, order = trade.orderStatus, trade.order
        if status.status in {"Filled", "Cancelled", "ApiCancelled", "Inactive"}:
            continue
        order_account = getattr(order, "account", "")
        if not order_account:
            raise SnapshotError("Active order has unknown account scope")
        if order_account != selected:
            continue
        instrument = _instrument(trade.contract, ib, cache)
        remaining = number(status.remaining, "remaining")
        total, filled = number(order.totalQuantity, "total", positive=True), number(status.filled, "filled")
        if not (0 <= filled <= total and 0 <= remaining <= total and remaining == total - filled):
            raise SnapshotError("Inconsistent working-order quantities")
        action = str(order.action).upper()
        if action not in {"BUY", "SELL"}:
            raise SnapshotError("Unsupported order action")
        perm_id, order_id = getattr(order, "permId", 0), getattr(order, "orderId", 0)
        client_id = getattr(order, "clientId", 0)
        identity = ("perm", perm_id) if perm_id else ("client", client_id, order_id)
        if identity in seen:
            raise SnapshotError("Duplicate active-order identity")
        seen.add(identity)
        orders.append(WorkingOrder(instrument, action, remaining, order_id, perm_id,
                                   str(getattr(order, "orderRef", "")),
                                   _optional_price(getattr(order, "lmtPrice", None)),
                                   str(getattr(order, "orderType", ""))))
    rates = {str(currency).upper(): number(rate, f"FX {currency}", positive=True)
             for currency, rate in (fx or {}).items()}
    if base in rates and rates[base] != 1:
        raise SnapshotError("Base-currency FX must equal one")
    rates[base] = Decimal(1)
    return Snapshot(selected, environment, now or datetime.now(timezone.utc), base, cash,
                    net, tuple(positions), tuple(orders), rates, tuple(issues))
