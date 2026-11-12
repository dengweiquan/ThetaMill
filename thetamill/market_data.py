"""Pure market data validation for live ticker quotes."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

DEFAULT_SOURCE = "reqTickers"
IB_LIVE_SOURCE = "IB live timestamped bid/ask"
IB_DELAYED_SOURCE = "IB delayed timestamped bid/ask"
IB_PROFILES = {
    "ib_live": {
        "provider": "ib",
        "market_data_type": 1,
        "max_age_seconds": 30,
        "source": IB_LIVE_SOURCE,
        "reference_only": False,
    },
    "ib_delayed": {
        "provider": "ib",
        "market_data_type": 3,
        "max_age_seconds": 1200,
        "source": IB_DELAYED_SOURCE,
        "reference_only": True,
    },
}


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
    market_data_type: int = 1

    def __post_init__(self) -> None:
        _validate_bound(self.future_skew_seconds, "future_skew_seconds", allow_none=False)
        _validate_bound(self.max_age_seconds, "max_age_seconds", allow_none=True)
        for name in ("require_both", "allow_zero", "allow_strings"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a bool")
        mdt = self.market_data_type
        if isinstance(mdt, bool) or type(mdt) is not int or mdt not in (1, 3):
            raise ValueError("market_data_type must be 1 (live) or 3 (delayed)")


DEFAULT_POLICY = QuotePolicy()
DELAYED_POLICY = QuotePolicy(
    max_age_seconds=1200,
    future_skew_seconds=5,
    require_both=True,
    allow_zero=False,
    allow_strings=False,
    market_data_type=3,
)
ROLL_POLICY = QuotePolicy(
    max_age_seconds=None,
    future_skew_seconds=5,
    require_both=False,
    allow_zero=True,
    allow_strings=True,
)


def resolve_ib_profile(name: str | None = None, *, market_data_type: str | None = None,
                       delayed: bool = False) -> dict:
    """Resolve a built-in IB profile. Unknown names fail closed.

    ``delayed`` and the legacy ``market_data_type`` config value can select the
    delayed profile, but they never invent a third source. Planning callers must
    keep using the live policy; delayed quotes are reference-only.
    """
    if not isinstance(delayed, bool):
        raise ValueError("delayed must be a bool")
    selected = name
    if selected is None and delayed:
        selected = "ib_delayed"
    if selected is None and market_data_type is not None:
        legacy = str(market_data_type).strip().lower()
        if legacy == "live":
            selected = "ib_live"
        elif legacy == "delayed":
            selected = "ib_delayed"
        elif legacy:
            raise ValueError("market_data_type must be 'live' or 'delayed'")
    if selected is None:
        selected = "ib_live"
    if not isinstance(selected, str):
        raise ValueError("market profile must be a string")
    key = selected.strip().lower().replace("-", "_")
    profile = IB_PROFILES.get(key)
    if profile is None:
        raise ValueError("market profile must be ib_live or ib_delayed")
    resolved = dict(profile)
    resolved["name"] = key
    return resolved


def policy_for_profile(profile: dict) -> QuotePolicy:
    """Return the quote policy for a resolved built-in IB profile."""
    if not isinstance(profile, dict) or profile.get("provider") != "ib":
        raise ValueError("profile must be a built-in IB profile")
    mdt = profile.get("market_data_type")
    if mdt == 1:
        return DEFAULT_POLICY
    if mdt == 3:
        return DELAYED_POLICY
    raise ValueError("IB profile market_data_type must be 1 or 3")


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
        if type(mdt) is not int or mdt != policy.market_data_type:
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
    "DELAYED_POLICY",
    "IB_DELAYED_SOURCE",
    "IB_LIVE_SOURCE",
    "IB_PROFILES",
    "ROLL_POLICY",
    "QuotePolicy",
    "ValidatedQuote",
    "policy_for_profile",
    "resolve_ib_profile",
    "validate_ticker",
]
