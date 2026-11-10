"""Broker session management, quote retrieval, and account snapshotting."""
from __future__ import annotations

import os
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Sequence

from ib_async import Contract

from .broker_requests import safe_req_tickers
from .broker_state import (
    FxQuote,
    PriceQuote,
    Snapshot,
    collect_snapshot,
    select_account as _state_select_account,
)
from .market_data import validate_ticker


def select_account(
    ib: Any,
    cfg: Any = None,
    args: Any = None,
    *,
    requested: str | None = None,
) -> str:
    """Resolve target managed account adhering strictly to single/explicit selection."""
    target = requested
    if target is None:
        if args is not None:
            target = getattr(args, "account", None)
        if target is None and cfg is not None:
            if isinstance(cfg, dict):
                target = cfg.get("account")
            elif isinstance(cfg, str):
                target = cfg
    return _state_select_account(ib, target)


@contextmanager
def private_diagnostics():
    """Do not expose raw broker messages in privacy-preserving reports."""
    with open(os.devnull, "w", encoding="utf-8") as sink:
        with redirect_stdout(sink), redirect_stderr(sink):
            yield


def timestamped_mid(
    ib: Any,
    contract: Any,
    *,
    now: Any = None,
    requester: Any = None,
) -> tuple[Decimal, datetime, str] | None:
    """Return timestamped bid/ask midpoint quote or None.

    Strictly validates contract conId before any I/O, samples reception time
    after request completion, and verifies quote validity and freshness via B2 validator.
    """
    if contract is None:
        return None
    con_id = getattr(contract, "conId", None)
    if isinstance(con_id, bool) or type(con_id) is not int or con_id <= 0:
        return None

    req_fn = requester if requester is not None else safe_req_tickers
    try:
        ticks = req_fn(ib, contract)
    except Exception:
        return None

    if not isinstance(ticks, (list, tuple)) or len(ticks) != 1:
        return None
    tick = ticks[0]

    # Sample now AFTER the request returns
    if now is not None:
        sample_time = now() if callable(now) else now
    else:
        sample_time = datetime.now(timezone.utc)
    if not isinstance(sample_time, datetime) or sample_time.tzinfo is None or sample_time.utcoffset() is None:
        return None

    validated = validate_ticker(tick, con_id, sample_time)
    if validated is None or validated.midpoint is None:
        return None

    return (validated.midpoint, validated.time, "IB timestamped bid/ask midpoint")


def risk_snapshot(
    ib: Any,
    cfg: Any,
    args: Any,
    environment: str,
    *,
    required_currencies: Sequence[str] = (),
    now: Any = None,
) -> Snapshot:
    """Collect an immutable account-scoped snapshot enriched with timestamped FX and stock quotes."""
    account = select_account(ib, cfg, args)
    snapshot = collect_snapshot(ib, account=account, environment=environment)

    currencies = set(snapshot.cash)
    for currency in required_currencies:
        if (
            not isinstance(currency, str)
            or len(currency.strip()) != 3
            or not currency.strip().isascii()
            or not currency.strip().isalpha()
        ):
            raise ValueError("Required FX currency must be a three-letter code")
        currencies.add(currency.strip().upper())
    currencies.update(p.instrument.currency for p in snapshot.positions)
    currencies.update(t.instrument.currency for t in snapshot.orders)

    rates: dict[str, Decimal] = {snapshot.base_currency: Decimal(1)}
    fx_quotes: dict[str, FxQuote] = {}
    price_quotes: dict[int, PriceQuote] = {}

    for currency in currencies - {snapshot.base_currency}:
        for symbol, target, invert in (
            (currency, snapshot.base_currency, False),
            (snapshot.base_currency, currency, True),
        ):
            try:
                qualified = ib.qualifyContracts(
                    Contract(
                        secType="CASH",
                        symbol=symbol,
                        currency=target,
                        exchange="IDEALPRO",
                    )
                )
                if not isinstance(qualified, (list, tuple)) or len(qualified) != 1:
                    continue
                pair = qualified[0]
                if (
                    type(getattr(pair, "conId", None)) is not int
                    or pair.conId <= 0
                    or pair.secType != "CASH"
                    or pair.symbol != symbol
                    or pair.currency != target
                ):
                    continue
                tick = timestamped_mid(ib, pair, now=now)
                if tick is None:
                    continue
                price, stamp, source = tick
                rate = Decimal(1) / price if invert else price
                fx_quotes[currency] = FxQuote(currency, rate, stamp, source)
                rates[currency] = rate
                break
            except Exception:
                continue

    positions = []
    for position in snapshot.positions:
        if position.instrument.sec_type == "STK":
            tick = timestamped_mid(
                ib,
                Contract(
                    conId=position.instrument.con_id,
                    exchange=position.instrument.exchange or "SMART",
                ),
                now=now,
            )
            if tick is not None:
                price, stamp, source = tick
                price_quotes[position.instrument.con_id] = PriceQuote(
                    position.instrument.con_id,
                    price,
                    stamp,
                    source,
                )
                position = replace(position, price=price)
            else:
                position = replace(position, price=None)
        positions.append(position)

    return replace(
        snapshot,
        fx=rates,
        fx_quotes=fx_quotes,
        price_quotes=price_quotes,
        positions=positions,
    )
