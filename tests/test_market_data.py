"""Tests for pure market data quote validation."""
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from thetamill.market_data import (
    DEFAULT_SOURCE,
    ROLL_POLICY,
    QuotePolicy,
    ValidatedQuote,
    validate_ticker,
)

NOW = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
_UNSET = object()


def _ticker(
    con_id=10,
    bid=10.0,
    ask=10.1,
    *,
    time=_UNSET,
    market_data_type=1,
    contract=_UNSET,
    **extra,
):
    if contract is _UNSET and con_id is not None:
        contract = SimpleNamespace(conId=con_id)
    t = SimpleNamespace(
        contract=contract if contract is not _UNSET else None,
        time=NOW if time is _UNSET else time,
        marketDataType=market_data_type,
        bid=bid,
        ask=ask,
    )
    for k, v in extra.items():
        setattr(t, k, v)
    return t


class QuotePolicyTests(unittest.TestCase):
    def test_default_policy_attributes(self):
        p = QuotePolicy()
        self.assertEqual(p.max_age_seconds, 30)
        self.assertEqual(p.future_skew_seconds, 5)
        self.assertTrue(p.require_both)
        self.assertFalse(p.allow_zero)
        self.assertFalse(p.allow_strings)

    def test_roll_policy_attributes(self):
        self.assertIsNone(ROLL_POLICY.max_age_seconds)
        self.assertEqual(ROLL_POLICY.future_skew_seconds, 5)
        self.assertFalse(ROLL_POLICY.require_both)
        self.assertTrue(ROLL_POLICY.allow_zero)
        self.assertTrue(ROLL_POLICY.allow_strings)

    def test_policy_immutability(self):
        p = QuotePolicy()
        with self.assertRaises(FrozenInstanceError):
            p.max_age_seconds = 10

    def test_invalid_policy_bounds_rejected(self):
        for val in (Decimal("1e400"), 10**1000, float("inf"), float("nan"), -1, True, "30"):
            with self.assertRaises(ValueError):
                QuotePolicy(max_age_seconds=val)
        for val in (Decimal("1e400"), 10**1000, float("inf"), float("nan"), -5, False, None, "5"):
            with self.assertRaises(ValueError):
                QuotePolicy(future_skew_seconds=val)
        for name in ("require_both", "allow_zero", "allow_strings"):
            with self.assertRaises(ValueError):
                QuotePolicy(**{name: 1})
            with self.assertRaises(ValueError):
                QuotePolicy(**{name: None})


class ValidatedQuoteTests(unittest.TestCase):
    def test_midpoint_calculation(self):
        q = ValidatedQuote(1, Decimal("10.0"), Decimal("10.5"), NOW, "reqTickers")
        self.assertEqual(q.midpoint, Decimal("10.25"))

    def test_midpoint_none_when_single_sided_or_crossed(self):
        q1 = ValidatedQuote(1, Decimal("10.0"), None, NOW, "reqTickers")
        self.assertIsNone(q1.midpoint)
        q2 = ValidatedQuote(1, None, Decimal("10.5"), NOW, "reqTickers")
        self.assertIsNone(q2.midpoint)
        q3 = ValidatedQuote(1, None, None, NOW, "reqTickers")
        self.assertIsNone(q3.midpoint)
        q4 = ValidatedQuote(1, Decimal("11.0"), Decimal("10.0"), NOW, "reqTickers")
        self.assertIsNone(q4.midpoint)

    def test_quote_immutability(self):
        q = ValidatedQuote(1, Decimal("10.0"), Decimal("10.5"), NOW, "reqTickers")
        with self.assertRaises(FrozenInstanceError):
            q.bid = Decimal("11.0")

    def test_quote_validation_checks(self):
        for cid in (0, -1, True, "1", None):
            with self.assertRaises(ValueError):
                ValidatedQuote(cid, Decimal("1"), Decimal("2"), NOW, "reqTickers")
        naive = datetime(2026, 11, 7, 20, 0)
        for t in (naive, None, "2026-11-07"):
            with self.assertRaises(ValueError):
                ValidatedQuote(1, Decimal("1"), Decimal("2"), t, "reqTickers")
        for s in ("", "   ", None, 123):
            with self.assertRaises(ValueError):
                ValidatedQuote(1, Decimal("1"), Decimal("2"), NOW, s)
        for bad_p in (-1, True, float("inf"), float("nan"), "invalid"):
            with self.assertRaises(ValueError):
                ValidatedQuote(1, bad_p, Decimal("2"), NOW, "reqTickers")


class ValidateTickerTests(unittest.TestCase):
    def test_valid_quote_default_policy(self):
        t = _ticker(10, bid=100.0, ask=100.1)
        q = validate_ticker(t, 10, NOW)
        self.assertIsNotNone(q)
        self.assertEqual(q.con_id, 10)
        self.assertEqual(q.bid, Decimal("100.0"))
        self.assertEqual(q.ask, Decimal("100.1"))
        self.assertEqual(q.midpoint, Decimal("100.05"))
        self.assertEqual(q.time, NOW)
        self.assertEqual(q.source, DEFAULT_SOURCE)

    def test_naive_and_invalid_timestamps(self):
        naive = datetime(2026, 11, 7, 20, 0)
        self.assertIsNone(validate_ticker(_ticker(time=naive), 10, NOW))
        self.assertIsNone(validate_ticker(_ticker(time="2026-11-07"), 10, NOW))
        self.assertIsNone(validate_ticker(_ticker(time=None), 10, NOW))
        self.assertIsNone(validate_ticker(_ticker(time=123456789), 10, NOW))
        self.assertIsNone(validate_ticker(_ticker(), 10, naive))
        self.assertIsNone(validate_ticker(_ticker(), 10, None))
        self.assertIsNone(validate_ticker(_ticker(), 10, "2026-11-07"))
        self.assertIsNone(validate_ticker(_ticker(), 10, 123456789))

    def test_market_data_type_validation(self):
        self.assertIsNone(validate_ticker(_ticker(market_data_type=True), 10, NOW))
        self.assertIsNone(validate_ticker(_ticker(market_data_type=False), 10, NOW))
        for mdt in (2, 3, 4, None, 0, 1.0, "1"):
            self.assertIsNone(validate_ticker(_ticker(market_data_type=mdt), 10, NOW))

    def test_identity_and_no_fallbacks(self):
        self.assertIsNone(validate_ticker(_ticker(con_id=999), 10, NOW))
        self.assertIsNone(validate_ticker(_ticker(con_id=-1), -1, NOW))
        self.assertIsNone(validate_ticker(_ticker(con_id=True), True, NOW))

        t_alias_cid = SimpleNamespace(
            conId=10,
            contract=None,
            time=NOW,
            marketDataType=1,
            bid=10.0,
            ask=10.1,
        )
        self.assertIsNone(validate_ticker(t_alias_cid, 10, NOW))

        t_contract_no_cid = SimpleNamespace(
            conId=10,
            contract=SimpleNamespace(),
            time=NOW,
            marketDataType=1,
            bid=10.0,
            ask=10.1,
        )
        self.assertIsNone(validate_ticker(t_contract_no_cid, 10, NOW))

        t_alias_time = SimpleNamespace(
            contract=SimpleNamespace(conId=10),
            as_of=NOW,
            time=None,
            marketDataType=1,
            bid=10.0,
            ask=10.1,
        )
        self.assertIsNone(validate_ticker(t_alias_time, 10, NOW))

        t_no_time = _ticker()
        del t_no_time.time
        self.assertIsNone(validate_ticker(t_no_time, 10, NOW))

    def test_clock_skew_and_freshness(self):
        t_exact = _ticker(time=NOW)
        self.assertIsNotNone(validate_ticker(t_exact, 10, NOW))

        t_30s = _ticker(time=NOW - timedelta(seconds=30))
        self.assertIsNotNone(validate_ticker(t_30s, 10, NOW))

        t_31s = _ticker(time=NOW - timedelta(seconds=31))
        self.assertIsNone(validate_ticker(t_31s, 10, NOW))

        t_fut_5s = _ticker(time=NOW + timedelta(seconds=5))
        self.assertIsNotNone(validate_ticker(t_fut_5s, 10, NOW))

        t_fut_6s = _ticker(time=NOW + timedelta(seconds=6))
        self.assertIsNone(validate_ticker(t_fut_6s, 10, NOW))

        t_roll_old = _ticker(time=NOW - timedelta(seconds=1000))
        self.assertIsNotNone(validate_ticker(t_roll_old, 10, NOW, policy=ROLL_POLICY))

        t_roll_fut = _ticker(time=NOW + timedelta(seconds=6))
        self.assertIsNone(validate_ticker(t_roll_fut, 10, NOW, policy=ROLL_POLICY))

    def test_nan_inf_zero_negative_crossed(self):
        for bad in (float("nan"), float("inf"), float("-inf"), Decimal("nan"), Decimal("inf")):
            self.assertIsNone(validate_ticker(_ticker(bid=bad), 10, NOW))
            self.assertIsNone(validate_ticker(_ticker(ask=bad), 10, NOW))
        for nonpos in (0, 0.0, Decimal(0), -1, Decimal("-1.5")):
            self.assertIsNone(validate_ticker(_ticker(bid=nonpos), 10, NOW))
        for b in (True, False):
            self.assertIsNone(validate_ticker(_ticker(bid=b), 10, NOW))
            self.assertIsNone(validate_ticker(_ticker(ask=b), 10, NOW))

        t_crossed = _ticker(bid=10.5, ask=10.0)
        self.assertIsNone(validate_ticker(t_crossed, 10, NOW))
        self.assertIsNone(validate_ticker(t_crossed, 10, NOW, policy=ROLL_POLICY))

    def test_optional_single_side_and_degradation(self):
        pol_permissive = QuotePolicy(require_both=False)
        self.assertIsNone(validate_ticker(_ticker(bid=10.0, ask=None), 10, NOW))
        q_bid = validate_ticker(_ticker(bid=10.0, ask=None), 10, NOW, policy=pol_permissive)
        self.assertIsNotNone(q_bid)
        self.assertEqual(q_bid.bid, Decimal("10.0"))
        self.assertIsNone(q_bid.ask)
        self.assertIsNone(q_bid.midpoint)

        q_degrade = validate_ticker(_ticker(bid=float("nan"), ask=10.1), 10, NOW, policy=pol_permissive)
        self.assertIsNotNone(q_degrade)
        self.assertIsNone(q_degrade.bid)
        self.assertEqual(q_degrade.ask, Decimal("10.1"))

        self.assertIsNone(validate_ticker(_ticker(bid=None, ask=None), 10, NOW, policy=pol_permissive))
        self.assertIsNone(validate_ticker(_ticker(bid=float("nan"), ask=-1), 10, NOW, policy=pol_permissive))

    def test_huge_finite_decimal_price_without_float_overflow(self):
        huge_dec = Decimal("1e400")
        t = _ticker(bid=huge_dec, ask=huge_dec)
        q = validate_ticker(t, 10, NOW)
        self.assertIsNotNone(q)
        self.assertEqual(q.bid, huge_dec)
        self.assertEqual(q.ask, huge_dec)
        self.assertEqual(q.midpoint, huge_dec)

    def test_string_policy(self):
        t = _ticker(bid="10.0", ask="10.2")
        self.assertIsNone(validate_ticker(t, 10, NOW))
        q_roll = validate_ticker(t, 10, NOW, policy=ROLL_POLICY)
        self.assertIsNotNone(q_roll)
        self.assertEqual(q_roll.bid, Decimal("10.0"))
        self.assertEqual(q_roll.ask, Decimal("10.2"))

    def test_explicit_provenance(self):
        q_def = validate_ticker(_ticker(), 10, NOW)
        self.assertEqual(q_def.source, DEFAULT_SOURCE)
        q_custom = validate_ticker(_ticker(), 10, NOW, source="custom_source")
        self.assertEqual(q_custom.source, "custom_source")
        for bad_source in ("", "   ", None, 123):
            with self.assertRaises(ValueError):
                validate_ticker(_ticker(), 10, NOW, source=bad_source)
