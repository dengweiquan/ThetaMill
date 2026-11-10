"""Pure market data validation for live ticker quotes."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

DEFAULT_SOURCE = "reqTickers"


def _validate_bound(val: Any, name: str, *, allow_none: bool = False) -> None:
    if val is None:
        if allow_none:
            return
        raise ValueError(f"{name} must not be None")
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"{name} must be a non-negative finite number")
    try:
        f = float(val)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(f"{name} must be a non-negative finite number") from None
    if not math.isfinite(f) or f < 0:
        raise ValueError(f"{name} must be a non-negative finite number")


def _clean_price(
    val: Any,
    field_name: str | None = None,
    *,
    allow_zero: bool = True,
    allow_strings: bool = False,
) -> Decimal | None:
    """Validate or convert a price into a finite non-negative Decimal or None."""
    if val is None:
        return None
    if isinstance(val, bool):
        if field_name is not None:
            raise ValueError(f"{field_name} must not be a bool")
        return None
    if isinstance(val, Decimal):
        if not val.is_finite() or (val < 0 if allow_zero else val <= 0):
            if field_name is not None:
                raise ValueError(f"{field_name} must be a finite non-negative Decimal")
            return None
        return val
    if isinstance(val, int):
        if val < 0 if allow_zero else val <= 0:
            if field_name is not None:
                raise ValueError(f"{field_name} must be a finite non-negative number")
            return None
        return Decimal(val)
    if isinstance(val, float):
        if not math.isfinite(val) or (val < 0 if allow_zero else val <= 0):
            if field_name is not None:
                raise ValueError(f"{field_name} must be a finite non-negative number")
            return None
        return Decimal(str(val))
    if isinstance(val, str) and allow_strings:
        try:
            d = Decimal(val.strip())
        except (InvalidOperation, ValueError):
            return None
        if not d.is_finite() or (d < 0 if allow_zero else d <= 0):
            return None
        return d
    if field_name is not None:
        raise ValueError(f"{field_name} must be a Decimal, int, float, or None")
    return None


@dataclass(frozen=True)
class QuotePolicy:
    """Validation policy for timestamped ticker quotes."""

    max_age_seconds: float | int | None = 30
    future_skew_seconds: float | int = 5
    require_both: bool = True
    allow_zero: bool = False
    allow_strings: bool = False

    def __post_init__(self) -> None:
        _validate_bound(self.future_skew_seconds, "future_skew_seconds", allow_none=False)
        _validate_bound(self.max_age_seconds, "max_age_seconds", allow_none=True)
        for name in ("require_both", "allow_zero", "allow_strings"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a bool")


DEFAULT_POLICY = QuotePolicy()
ROLL_POLICY = QuotePolicy(
    max_age_seconds=None,
    future_skew_seconds=5,
    require_both=False,
    allow_zero=True,
    allow_strings=True,
)


@dataclass(frozen=True)
class ValidatedQuote:
    """An immutable, validated ticker quote."""

    con_id: int
    bid: Decimal | None
    ask: Decimal | None
    time: datetime
    source: str

    def __post_init__(self) -> None:
        if isinstance(self.con_id, bool) or type(self.con_id) is not int or self.con_id <= 0:
            raise ValueError("con_id must be a positive integer")
        if not isinstance(self.time, datetime) or self.time.tzinfo is None or self.time.utcoffset() is None:
            raise ValueError("time must be a timezone-aware datetime")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a non-empty string")
        norm_bid = _clean_price(self.bid, "bid")
        norm_ask = _clean_price(self.ask, "ask")
        if norm_bid is not self.bid:
            object.__setattr__(self, "bid", norm_bid)
        if norm_ask is not self.ask:
            object.__setattr__(self, "ask", norm_ask)

    @property
    def midpoint(self) -> Decimal | None:
        """Midpoint of bid and ask, or None if single-sided or crossed."""
        if self.bid is None or self.ask is None or self.bid > self.ask:
            return None
        return (self.bid + self.ask) / Decimal(2)


def validate_ticker(
    ticker: Any,
    con_id: int,
    now: datetime,
    *,
    policy: QuotePolicy = DEFAULT_POLICY,
    source: str = DEFAULT_SOURCE,
) -> ValidatedQuote | None:
    """Validate a live ticker against policy and contract identity."""
    if not isinstance(policy, QuotePolicy):
        raise ValueError("policy must be a QuotePolicy")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string")
    _validate_bound(policy.future_skew_seconds, "future_skew_seconds", allow_none=False)
    if policy.max_age_seconds is not None:
        _validate_bound(policy.max_age_seconds, "max_age_seconds", allow_none=True)

    if isinstance(con_id, bool) or type(con_id) is not int or con_id <= 0:
        return None
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return None
    if ticker is None:
        return None

    try:
        contract = getattr(ticker, "contract", None)
        if contract is None:
            return None
        tick_con_id = getattr(contract, "conId", None)
        if isinstance(tick_con_id, bool) or type(tick_con_id) is not int or tick_con_id != con_id:
            return None

        mdt = getattr(ticker, "marketDataType", None)
        if type(mdt) is not int or mdt != 1:
            return None

        stamp = getattr(ticker, "time", None)
        if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
            return None

        raw_bid = getattr(ticker, "bid", None)
        raw_ask = getattr(ticker, "ask", None)
    except Exception:
        return None

    now_utc = now.astimezone(timezone.utc)
    stamp_utc = stamp.astimezone(timezone.utc)
    age_seconds = (now_utc - stamp_utc).total_seconds()

    if age_seconds < -float(policy.future_skew_seconds):
        return None
    if policy.max_age_seconds is not None and age_seconds > float(policy.max_age_seconds):
        return None

    bid = _clean_price(raw_bid, allow_zero=policy.allow_zero, allow_strings=policy.allow_strings)
    ask = _clean_price(raw_ask, allow_zero=policy.allow_zero, allow_strings=policy.allow_strings)

    if policy.require_both:
        if bid is None or ask is None:
            return None
        if bid > ask:
            return None
    else:
        if bid is None and ask is None:
            return None
        if bid is not None and ask is not None and bid > ask:
            return None

    return ValidatedQuote(
        con_id=con_id,
        bid=bid,
        ask=ask,
        time=stamp_utc,
        source=source,
    )


__all__ = [
    "DEFAULT_POLICY",
    "DEFAULT_SOURCE",
    "ROLL_POLICY",
    "QuotePolicy",
    "ValidatedQuote",
    "validate_ticker",
]
