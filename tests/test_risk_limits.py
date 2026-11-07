"""Stage B/C PURE risk module regressions.

All tests are fully offline: hand-built broker_state Snapshots (no broker,
no network, no CLI, no ledger).  Money is always Decimal; boundaries use exact
one-cent / one-share differences.  Underlying identity is the real underConId.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from thetamill.broker_state import Instrument, Position, Snapshot, WorkingOrder
from thetamill import risk

D = Decimal
NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Immutable, reusable contract identities (underlying_id = real underConId).
# --------------------------------------------------------------------------
def stock(con_id=1, underlying=1, currency="USD", symbol="AAPL"):
    return Instrument(con_id, symbol, currency, "STK", "SMART", underlying)


def call(con_id=101, underlying=1, currency="USD", multiplier=100, strike=40):
    return Instrument(con_id, "AAPL", currency, "OPT", "SMART", underlying,
                      D(multiplier), "C", D(strike), "20261218", "AAPL")


def put(con_id=201, underlying=1, currency="USD", multiplier=100, strike=40):
    return Instrument(con_id, "AAPL", currency, "OPT", "SMART", underlying,
                      D(multiplier), "P", D(strike), "20261218", "AAPL")


def snap(*, cash=None, positions=(), orders=(), fx=None, netliq=None,
         base="USD", captured=NOW, issues=(), account="DU1", environment="paper"):
    return Snapshot(account, environment, captured, base,
                    cash if cash is not None else {"USD": D("100000")},
                    netliq, tuple(positions), tuple(orders),
                    fx if fx is not None else {"USD": D(1)}, tuple(issues))


def pos(inst, qty, price=None):
    return Position(inst, qty if isinstance(qty, Decimal) else D(str(qty)),
                    None if price is None else (price if isinstance(price, Decimal)
                                                else D(str(price))))


def order(inst, action, remaining, *, limit=None, oid=1, otype="LMT"):
    return WorkingOrder(inst, action, D(str(remaining)), oid, 0, f"r{oid}",
                        None if limit is None else D(str(limit)), otype)


def req(inst, action, qty, *, limit=None, otype="LMT"):
    return risk.OrderRequest(inst, action,
                             qty if isinstance(qty, Decimal) else D(str(qty)),
                             otype, None if limit is None else D(str(limit)), "DAY")


def enabled(**overrides):
    cfg = {"enabled": True, "day_timezone": "UTC", "max_daily_submissions": 10,
           "per_underlying": {"amount": 1000000},
           "portfolio": {"amount": 1000000}}
    cfg.update(overrides)
    return risk.validate_risk_config({"risk": cfg})


def disabled():
    return risk.validate_risk_config({})


def decide(snapshot, request, limits=None, reservations=None, now=NOW):
    return risk.assess(snapshot, request, limits if limits is not None else enabled(),
                       reservations, now=now)


# ==========================================================================
class ConfigTests(unittest.TestCase):
    def test_missing_config_displays_disabled_but_baseline_still_enforced(self):
        lim = disabled()
        self.assertFalse(lim.enabled)
        self.assertIn("disabled", lim.display.lower())
        # A naked call is still blocked even with P3 off.
        self.assertEqual(decide(snap(), req(call(), "SELL", 1), lim).decision,
                         risk.BLOCK)

    def test_explicit_enabled_invalid_config_fails_closed(self):
        base = {"enabled": True, "day_timezone": "UTC", "max_daily_submissions": 10,
                "per_underlying": {"amount": 1000000}}
        bad = [
            {"enabled": "maybe"},
            {"day_timezone": "Mars/Olympus"},
            {"day_timezone": ""},
            {"max_daily_submissions": 0},
            {"max_daily_submissions": -1},
            {"max_daily_submissions": True},
            {"max_daily_submissions": 1.5},
            {"max_daily_submissions": "10"},
            {"per_underlying": {"amount": True}},
            {"per_underlying": {"amount": float("nan")}},
            {"per_underlying": {"amount": float("inf")}},
            {"per_underlying": {"amount": -1}},
            {"per_underlying": {"ratio": "0.5x"}},
            {"portfolio": {"amount": 0}},
            {"max_snapshot_age_seconds": True},
            {"max_snapshot_age_seconds": 0},
        ]
        for patch in bad:
            with self.subTest(patch=patch):
                cfg = dict(base)
                cfg.update(patch)
                with self.assertRaises(risk.RiskConfigError):
                    risk.validate_risk_config({"risk": cfg})

    def test_valid_config_and_optional_caps(self):
        lim = enabled(per_underlying={"amount": 50000, "ratio": "0.25"},
                      portfolio={"amount": 200000},
                      max_snapshot_age_seconds=60)
        self.assertTrue(lim.enabled)
        self.assertEqual(lim.per_underlying_amount, D(50000))
        self.assertEqual(lim.per_underlying_ratio, D("0.25"))
        self.assertEqual(lim.portfolio_amount, D(200000))
        self.assertIsNone(lim.portfolio_ratio)
        self.assertEqual(lim.max_snapshot_age_seconds, 60)
        self.assertTrue(enabled(per_underlying=None, portfolio=None).enabled)

    def test_day_key_uses_configured_zone_and_dst(self):
        lim = enabled(day_timezone="America/New_York")
        s = snap()
        # Spring forward: 04:30Z is still 03/07, 05:30Z is already 03/08.
        before = datetime(2026, 3, 8, 4, 30, tzinfo=timezone.utc)
        after = datetime(2026, 3, 8, 5, 30, tzinfo=timezone.utc)
        self.assertTrue(risk.day_key(s, lim.day_timezone, now=before).endswith("2026-03-07"))
        self.assertTrue(risk.day_key(s, lim.day_timezone, now=after).endswith("2026-03-08"))
        # Fall back: 03:30Z is still 10/31, 04:30Z is 11/01.
        fb_before = datetime(2026, 11, 1, 3, 30, tzinfo=timezone.utc)
        fb_after = datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc)
        self.assertTrue(risk.day_key(s, lim.day_timezone, now=fb_before).endswith("2026-10-31"))
        self.assertTrue(risk.day_key(s, lim.day_timezone, now=fb_after).endswith("2026-11-01"))
        d = decide(s, req(put(), "SELL", 1), lim, now=after)
        self.assertTrue(d.day_key.endswith("2026-03-08"))

    def test_day_key_scopes_account_and_environment(self):
        a = risk.day_key(snap(account="DU1"), "UTC", now=NOW)
        b = risk.day_key(snap(account="DU2"), "UTC", now=NOW)
        c = risk.day_key(snap(environment="live"), "UTC", now=NOW)
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a.split("|")[0:2], ["DU1", "paper"])


# ==========================================================================
class CoveredCallTests(unittest.TestCase):
    def test_100_200_500_exact_and_one_share_short(self):
        for mult in (100, 200, 500):
            for shares, allowed in ((2 * mult, True), (2 * mult - 1, False)):
                with self.subTest(mult=mult, shares=shares):
                    s = snap(positions=[pos(stock(), shares, 50)])
                    d = decide(s, req(call(multiplier=mult), "SELL", 2))
                    self.assertEqual(d.allowed, allowed)
                    self.assertEqual(d.details["call_shares_required"], str(D(2 * mult)))

    def test_pending_stock_sell_deducts_cover(self):
        s = snap(positions=[pos(stock(), 200, 50)],
                 orders=[order(stock(), "SELL", 100)])
        self.assertFalse(decide(s, req(call(), "SELL", 2)).allowed)   # 100 left < 200
        self.assertTrue(decide(s, req(call(), "SELL", 1)).allowed)     # 100 left == 100

    def test_held_and_pending_short_call_sells_deduct_cover(self):
        s = snap(positions=[pos(stock(), 500, 50), pos(call(con_id=111), -1)],
                 orders=[order(call(con_id=112, multiplier=200), "SELL", 1)])
        # 500 - 100(held) - 200(pending) = 200 available.
        self.assertTrue(decide(s, req(call(con_id=113), "SELL", 2)).allowed)
        self.assertFalse(decide(s, req(call(con_id=113), "SELL", 3)).allowed)

    def test_partial_remaining_uses_remaining_not_total(self):
        s = snap(positions=[pos(stock(), 250, 50), pos(call(con_id=111), -1)],
                 orders=[order(stock(), "SELL", 50)])
        self.assertTrue(decide(s, req(call(), "SELL", 1)).allowed)   # 250-100-50=100
        self.assertFalse(decide(s, req(call(), "SELL", 2)).allowed)

    def test_pending_call_buyback_does_not_release_cover(self):
        s = snap(positions=[pos(stock(), 100, 50), pos(call(), -1)],
                 orders=[order(call(), "BUY", 1, limit=1)])
        self.assertFalse(decide(s, req(stock(), "SELL", 1)).allowed)

    def test_pending_stock_sell_cannot_uncover_existing_short_call(self):
        s = snap(positions=[pos(stock(), 100, 50), pos(call(), -1)])
        self.assertFalse(decide(s, req(stock(), "SELL", 1)).allowed)

    def test_stock_sell_beyond_uncovered_shares_rejected(self):
        s = snap(positions=[pos(stock(), 100, 50)])
        self.assertTrue(decide(s, req(stock(), "SELL", 100)).allowed)
        self.assertFalse(decide(s, req(stock(), "SELL", 101)).allowed)

    def test_covered_call_not_double_counted_in_exposure(self):
        # 200 shares @ 50 = 10000; one covered call adds no stock notional.
        lim = enabled(per_underlying={"amount": 10000}, portfolio={"amount": 10000})
        s = snap(positions=[pos(stock(), 200, 50)])
        d = decide(s, req(call(), "SELL", 2), lim)
        self.assertTrue(d.allowed)
        self.assertEqual(d.details["exposure_underlying_base"], "10000")
        self.assertEqual(d.details["exposure_portfolio_base"], "10000")

    def test_underlying_identity_is_underconid_not_symbol(self):
        a = stock(con_id=1, underlying=1, symbol="AAPL")
        b = stock(con_id=2, underlying=2, symbol="AAPL")  # same symbol, different underConId
        s = snap(positions=[pos(a, 0, 50), pos(b, 100, 50)])
        # shares live under key 2:USD; a call on underlying 1 must not use them.
        self.assertFalse(decide(s, req(call(underlying=1), "SELL", 1)).allowed)
        self.assertTrue(decide(s, req(call(underlying=2), "SELL", 1)).allowed)


# ==========================================================================
class ShortCloseTests(unittest.TestCase):
    def test_exact_conid_capacity_less_pending_buybacks(self):
        s = snap(positions=[pos(put(con_id=201), -3)],
                 orders=[order(put(con_id=201), "BUY", 2, limit=1)])
        self.assertTrue(decide(s, req(put(con_id=201), "BUY", 1, limit=1)).allowed)
        self.assertFalse(decide(s, req(put(con_id=201), "BUY", 2, limit=1)).allowed)

    def test_buyback_on_different_conid_is_not_netted(self):
        s = snap(positions=[pos(put(con_id=201), -1), pos(put(con_id=999), -1)],
                 orders=[order(put(con_id=999), "BUY", 1, limit=1)])
        self.assertTrue(decide(s, req(put(con_id=201), "BUY", 1, limit=1)).allowed)

    def test_no_short_position_means_no_opening_long(self):
        self.assertFalse(decide(snap(), req(call(), "BUY", 1)).allowed)

    def test_local_close_reservation_consumes_capacity(self):
        s = snap(positions=[pos(put(con_id=201), -3)])
        res = {"close": {201: D(2)}}
        self.assertTrue(decide(s, req(put(con_id=201), "BUY", 1, limit=1), reservations=res).allowed)
        self.assertFalse(decide(s, req(put(con_id=201), "BUY", 2, limit=1), reservations=res).allowed)
        # String conId keys are accepted too (JSON-friendly ledger).
        self.assertFalse(decide(s, req(put(con_id=201), "BUY", 2, limit=1),
                                reservations={"close": {"201": 2}}).allowed)

    def test_short_close_is_risk_reducing_and_ignores_exposure_caps(self):
        lim = enabled(per_underlying={"amount": 1}, portfolio={"amount": 1})
        s = snap(positions=[pos(put(con_id=201), -1)])
        self.assertTrue(decide(s, req(put(con_id=201), "BUY", 1, limit=1), lim).allowed)


# ==========================================================================
class CashSecuredPutTests(unittest.TestCase):
    def test_100_200_500_exact_and_one_cent_short(self):
        for mult in (100, 200, 500):
            required = D(40) * D(2) * D(mult)
            for cash, allowed in ((required, True), (required - D("0.01"), False)):
                with self.subTest(mult=mult, cash=cash):
                    d = decide(snap(cash={"USD": cash}), req(put(multiplier=mult), "SELL", 2))
                    self.assertEqual(d.allowed, allowed)
                    self.assertEqual(d.details["put_required"], str(required))

    def test_held_and_pending_put_obligations_share_one_pool(self):
        positions = [pos(put(con_id=201, multiplier=100), -1)]
        orders = [order(put(con_id=202, multiplier=200), "SELL", 1)]
        # 4000 held + 8000 pending = 12000 locked; new put needs 40*200*1=8000.
        # free = cash - 12000; exact boundary cash = 20000, one cent short = 19999.99.
        self.assertTrue(decide(snap(cash={"USD": D("20000")}, positions=positions,
                                    orders=orders),
                              req(put(con_id=203, multiplier=200), "SELL", 1)).allowed)
        self.assertFalse(decide(snap(cash={"USD": D("19999.99")}, positions=positions,
                                     orders=orders),
                                req(put(con_id=203, multiplier=200), "SELL", 1)).allowed)

    def test_negative_cash_balance_reduces_shared_pool(self):
        # +10000 USD and -16000 HKD (-2000 base at fx 0.125) => 8000 base free.
        s = snap(cash={"USD": D("10000"), "HKD": D("-16000")},
                 fx={"USD": D(1), "HKD": D("0.125")})
        self.assertTrue(decide(s, req(put(), "SELL", 2)).allowed)  # needs 8000
        self.assertFalse(decide(s, req(put(), "SELL", 2),
                                reservations={"cash_base": D("0.01")}).allowed)
        self.assertEqual(D(decide(s, req(put(), "SELL", 1)).details["free_cash_base"]),
                         D("8000"))

    def test_three_currency_pool_not_reused(self):
        fx = {"USD": D(1), "HKD": D("0.125"), "JPY": D("0.01")}
        cash = {"USD": D("10000"), "HKD": D("80000"), "JPY": D("1000000")}
        # total = 10000 + 10000 + 10000 = 30000 base.
        # Held HKD put: 40*100*1 = 4000 HKD -> 500 base obligation.
        hkd_put = put(con_id=301, currency="HKD")
        s = snap(cash=cash, fx=fx, positions=[pos(hkd_put, -1)])
        self.assertEqual(D(decide(s, req(put(), "SELL", 1)).details["free_cash_base"]),
                         D("29500"))
        # A JPY put (5000*100 = 500000 JPY -> 5000 base) draws the SAME pool.
        jpy_put = put(con_id=401, currency="JPY", strike=5000)
        self.assertEqual(D(decide(s, req(jpy_put, "SELL", 1)).details["put_required_base"]),
                         D("5000"))
        # Six such JPY puts (30000 base) exceed the 29500 base free.
        self.assertFalse(decide(s, req(jpy_put, "SELL", 6)).allowed)
        self.assertTrue(decide(s, req(jpy_put, "SELL", 5)).allowed)  # 25000 <= 29500

    def test_base_aggregate_never_counted_as_spendable_cash(self):
        s = snap(cash={"USD": D("8000"), "BASE": D("999999")},
                 fx={"USD": D(1)})
        big = put(con_id=501, strike=640)  # 640*100*1 = 64000 base
        self.assertFalse(decide(s, req(big, "SELL", 1)).allowed)

    def test_missing_fx_blocks_put_collateral_not_zero(self):
        s = snap(cash={"USD": D("100000")}, fx={"USD": D(1)})
        hkd = put(con_id=301, currency="HKD")
        d = decide(s, req(hkd, "SELL", 1))
        self.assertFalse(d.allowed)
        self.assertTrue(any("FX" in r for r in d.reasons))

    def test_reserved_cash_reduces_free_pool(self):
        s = snap(cash={"USD": D("8000")})
        self.assertTrue(decide(s, req(put(), "SELL", 2)).allowed)
        self.assertFalse(decide(s, req(put(), "SELL", 2),
                                reservations={"cash_base": D("0.01")}).allowed)

    def test_unknown_reservation_bucket_fails_closed(self):
        with self.assertRaises(risk.RiskDataError):
            decide(snap(), req(put(), "SELL", 1), reservations={"mystery": D(1)})

    def test_decimal_one_cent_precision_no_float_rounding(self):
        self.assertTrue(decide(snap(cash={"USD": D("8000.00")}),
                              req(put(), "SELL", 2)).allowed)
        self.assertFalse(decide(snap(cash={"USD": D("7999.99")}),
                                req(put(), "SELL", 2)).allowed)


# ==========================================================================
class ExposureTests(unittest.TestCase):
    def test_positive_stock_pending_buys_and_puts_in_exposure(self):
        # 100 shares @50 = 5000; pending LMT buy 10@50 = 500; short put 40*100=4000.
        s = snap(positions=[pos(stock(), 100, 50), pos(put(), -1)],
                 orders=[order(stock(), "BUY", 10, limit=50)])
        summary = risk.summarize(s)
        self.assertEqual(summary["exposure_by_underlying"]["1:USD"], "9500")
        self.assertEqual(summary["exposure_portfolio_base"], "9500")
        self.assertTrue(summary["exposure_confirmed"])

    def test_exposure_amount_boundary_exact_and_one_cent_over(self):
        s = snap(positions=[pos(stock(), 100, 50)])  # exposure 5000
        for cap, allowed in ((D(5000), True), (D("4999.99"), False)):
            lim = enabled(per_underlying={"amount": cap}, portfolio={"amount": cap})
            with self.subTest(cap=cap):
                # A covered call adds no exposure; existing 5000 is compared to cap.
                self.assertEqual(decide(s, req(call(), "SELL", 1), lim).allowed, allowed)

    def test_new_short_put_adds_potential_holding_exposure(self):
        s = snap(positions=[pos(stock(), 100, 50)])  # exposure 5000
        lim = enabled(per_underlying={"amount": 9000}, portfolio={"amount": 9000})
        # New short put 40*100 = 4000 base -> total 9000, at cap.
        self.assertTrue(decide(s, req(put(), "SELL", 1), lim).allowed)
        lim2 = enabled(per_underlying={"amount": 8999}, portfolio={"amount": 8999})
        self.assertFalse(decide(s, req(put(), "SELL", 1), lim2).allowed)

    def test_ratio_limit_uses_netliq_and_both_caps_must_pass(self):
        s = snap(positions=[pos(stock(), 100, 50)], netliq=D(20000))
        # exposure 5000; amount cap 10000 (pass), ratio 0.2*20000 = 4000 (fail).
        lim = enabled(per_underlying={"amount": 10000, "ratio": "0.2"},
                      portfolio={"amount": 100000})
        self.assertFalse(decide(s, req(call(), "SELL", 1), lim).allowed)
        lim2 = enabled(per_underlying={"amount": 10000, "ratio": "0.3"},
                       portfolio={"amount": 100000})
        self.assertTrue(decide(s, req(call(), "SELL", 1), lim2).allowed)

    def test_ratio_limit_without_netliq_blocks_instead_of_zero(self):
        s = snap(positions=[pos(stock(), 100, 50)], netliq=None)
        d = decide(s, req(call(), "SELL", 1), enabled(per_underlying={"ratio": "0.5"}))
        self.assertFalse(d.allowed)
        self.assertTrue(any("net liquidation" in r for r in d.reasons))

    def test_missing_price_blocks_exposure_cap_not_zero(self):
        s = snap(positions=[pos(stock(), 100, None)])
        d = decide(s, req(call(), "SELL", 1), enabled(per_underlying={"amount": 1000000}))
        self.assertFalse(d.allowed)
        self.assertTrue(any("cannot be confirmed" in r for r in d.reasons))

    def test_no_exposure_caps_does_not_block_unrelated_put_on_missing_price(self):
        # With P3 enabled but NO amount/ratio caps, a missing stock price is
        # reported (unknown) but must not block a put whose cash collateral is
        # otherwise fully confirmed.  Baseline safety is still enforced.
        lim = enabled(per_underlying=None, portfolio=None)
        s = snap(positions=[pos(stock(), 100, None)])
        self.assertFalse(risk.summarize(s)["exposure_confirmed"])
        d = decide(s, req(put(), "SELL", 1), lim)
        self.assertTrue(d.allowed)
        # Baseline cover is still enforced: 100 shares cannot cover 2 calls.
        self.assertFalse(decide(s, req(call(), "SELL", 2), lim).allowed)

    def test_market_stock_buy_refuses_unbounded_cost(self):
        d = decide(snap(), req(stock(), "BUY", 10, otype="MKT"))
        self.assertFalse(d.allowed)
        self.assertTrue(any("bounded notional" in r for r in d.reasons))
        d2 = decide(snap(), req(stock(), "BUY", 10, limit=50))
        self.assertTrue(d2.allowed)
        self.assertEqual(d2.details["stock_buy_notional_base"], "500")

    def test_exposure_reservation_is_added(self):
        s = snap(positions=[pos(stock(), 100, 50)])  # 5000
        res = {"exposure": {"1:USD": D(1000)}}
        lim = enabled(per_underlying={"amount": 6000}, portfolio={"amount": 6000})
        self.assertTrue(decide(s, req(call(), "SELL", 1), lim, res).allowed)
        lim2 = enabled(per_underlying={"amount": 5999}, portfolio={"amount": 5999})
        self.assertFalse(decide(s, req(call(), "SELL", 1), lim2, res).allowed)

    def test_shares_reservation_deducts_cover(self):
        s = snap(positions=[pos(stock(), 200, 50)])
        res = {"shares": {"1:USD": D(100)}}
        self.assertTrue(decide(s, req(call(), "SELL", 1), reservations=res).allowed)
        self.assertFalse(decide(s, req(call(), "SELL", 2), reservations=res).allowed)

    def test_long_option_fails_closed_for_increase(self):
        s = snap(positions=[pos(call(), 1, 5)])
        for r in (req(call(), "SELL", 1), req(put(), "SELL", 1),
                  req(stock(), "BUY", 1, limit=50)):
            with self.subTest(action=r.action):
                self.assertFalse(decide(s, r).allowed)

    def test_pending_market_buy_marks_exposure_unconfirmed(self):
        s = snap(orders=[order(stock(), "BUY", 10, limit=None, otype="MKT")])
        self.assertFalse(risk.summarize(s)["exposure_confirmed"])
        self.assertFalse(decide(s, req(put(), "SELL", 1),
                                enabled(per_underlying={"amount": 1000000})).allowed)


# ==========================================================================
class DataQualityAndValidationTests(unittest.TestCase):
    def test_stale_snapshot_blocks_increase_but_not_close(self):
        lim = enabled(max_snapshot_age_seconds=30)
        self.assertFalse(decide(snap(captured=NOW - timedelta(seconds=31)),
                                req(put(), "SELL", 1), lim).allowed)
        self.assertTrue(decide(snap(captured=NOW - timedelta(seconds=29)),
                               req(put(), "SELL", 1), lim).allowed)
        old = snap(captured=NOW - timedelta(seconds=31),
                   positions=[pos(put(con_id=201), -1)])
        self.assertTrue(decide(old, req(put(con_id=201), "BUY", 1, limit=1), lim).allowed)

    def test_snapshot_issues_block_risk_increase(self):
        s = snap(issues=("Missing market price: 1",))
        self.assertFalse(decide(s, req(put(), "SELL", 1)).allowed)
        self.assertIn("Missing market price", risk.summarize(s)["unknown"][0])

    def test_bool_and_nan_config_rejected(self):
        for value in (True, float("nan"), float("inf"), -1, "x"):
            with self.subTest(value=value):
                with self.assertRaises(risk.RiskConfigError):
                    risk.validate_risk_config({"risk": {
                        "enabled": True, "day_timezone": "UTC",
                        "max_daily_submissions": 10,
                        "per_underlying": {"amount": value}}})
        # None means the optional cap is absent (valid), never zero or unlimited.
        lim = risk.validate_risk_config({"risk": {
            "enabled": True, "day_timezone": "UTC", "max_daily_submissions": 10,
            "per_underlying": {"amount": None}, "portfolio": {"amount": None}}})
        self.assertIsNone(lim.per_underlying_amount)
        self.assertIsNone(lim.portfolio_amount)

    def test_direct_constructed_snapshot_malformed_numbers_fail_closed(self):
        # Structural NaN cash raises rather than being treated as zero.
        with self.assertRaises(risk.RiskDataError):
            decide(snap(cash={"USD": D("NaN")}), req(put(), "SELL", 1))
        # A bad market price is treated as unknown (never zero) WITHOUT mutating
        # the caller's Snapshot: assess is pure.
        s = snap(positions=[pos(stock(), 100, D("NaN"))])
        summary = risk.summarize(s)
        self.assertFalse(summary["exposure_confirmed"])
        self.assertTrue(any("price" in u.lower() for u in summary["unknown"]))
        self.assertTrue(s.positions[0].price.is_nan())  # input untouched
        d = decide(s, req(call(), "SELL", 1),
                   enabled(per_underlying={"amount": 1000000}))
        self.assertFalse(d.allowed)  # unverifiable price blocks the cap check

    def test_bool_quantity_and_non_integer_option_qty_rejected(self):
        for qty in (0, -1, D("NaN"), D("Infinity"), D("0.5")):
            with self.subTest(qty=qty):
                with self.assertRaises(risk.RiskDataError):
                    req(put(), "SELL", qty)
        # bool and string quantities are ambiguous coercions -> rejected.
        for qty in (True, False, "1"):
            with self.subTest(qty=qty):
                with self.assertRaises(risk.RiskDataError):
                    risk.OrderRequest(put(), "SELL", qty, "LMT", D(1))
        # Stock quantities may be fractional; option quantities must be whole.
        self.assertEqual(req(stock(), "BUY", D("0.5"), limit=50).quantity, D("0.5"))

    def test_inconsistent_duplicate_conid_identity_rejected(self):
        a = Instrument(101, "AAPL", "USD", "OPT", "SMART", 1, D(100), "C", D(40), "x")
        b = Instrument(101, "AAPL", "USD", "OPT", "SMART", 1, D(200), "C", D(40), "x")
        s = snap(positions=[pos(a, -1), pos(b, -1)])
        with self.assertRaises(risk.RiskDataError):
            decide(s, req(call(), "SELL", 1))

    def test_duplicate_conid_positions_aggregate_conservatively(self):
        s = snap(positions=[pos(call(con_id=101), -1), pos(call(con_id=101), -1)])
        self.assertEqual(risk.summarize(s)["short_close_capacity"]["101"], "2")

    def test_order_request_is_immutable(self):
        r = req(put(), "SELL", 1)
        with self.assertRaises(Exception):
            r.quantity = D(9)

    def test_decision_is_explicit_and_explainable(self):
        d = decide(snap(positions=[pos(stock(), 10, 50)]), req(call(), "SELL", 1))
        self.assertEqual(d.decision, risk.BLOCK)
        self.assertFalse(d.allowed)
        self.assertTrue(d.reasons)
        self.assertIn("available_shares", d.details)
        self.assertEqual(d.display, "BLOCK")

    def test_summarize_reuses_assess_state_no_duplicate_math(self):
        s = snap(cash={"USD": D("10000")},
                 positions=[pos(stock(), 100, 50), pos(put(), -1)],
                 orders=[order(stock(), "BUY", 10, limit=50)])
        summary = risk.summarize(s)
        d = decide(s, req(call(), "SELL", 1))
        self.assertEqual(summary["exposure_portfolio_base"],
                         d.details["exposure_portfolio_base"])
        self.assertEqual(summary["cash_pool_base"], d.details["cash_pool_base"])
        self.assertTrue(summary["cash_confirmed"])
        self.assertIsNone(summary["net_liquidation"])


# ==========================================================================
# Independent-review blocker regressions (all must stay red->green).
# ==========================================================================
class ReviewBlockerTests(unittest.TestCase):
    # ---- 1. Stock buys draw on the SAME shared cash pool -------------------
    def test_pending_lmt_stock_buy_reserves_shared_cash(self):
        # cash 4000 + pending LMT buy 100x40 = 4000 reserved; a 4000 put must
        # be rejected because only 0 free cash remains (not 4000).
        s = snap(cash={"USD": D("4000")},
                 orders=[order(stock(), "BUY", 100, limit=40, otype="LMT")])
        self.assertFalse(decide(s, req(put(strike=40), "SELL", 1)).allowed)
        # With 8000.01 cash it is exactly allowed.
        s2 = snap(cash={"USD": D("8000")},
                  orders=[order(stock(), "BUY", 100, limit=40, otype="LMT")])
        self.assertTrue(decide(s2, req(put(strike=40), "SELL", 1)).allowed)

    def test_pending_stock_buy_cash_converted_across_currencies(self):
        # HKD pending LMT buy 100x40 = 4000 HKD; fx 0.125 -> 500 base reserved.
        hkd = stock(con_id=2, underlying=2, currency="HKD")
        s = snap(cash={"USD": D("500")}, fx={"USD": D(1), "HKD": D("0.125")},
                 orders=[order(hkd, "BUY", 100, limit=40, otype="LMT")])
        self.assertFalse(decide(s, req(put(strike=4), "SELL", 1)).allowed)  # needs 400
        s2 = snap(cash={"USD": D("900")}, fx={"USD": D(1), "HKD": D("0.125")},
                  orders=[order(hkd, "BUY", 100, limit=40, otype="LMT")])
        self.assertTrue(decide(s2, req(put(strike=4), "SELL", 1)).allowed)  # 900-500=400

    def test_held_put_then_proposed_stock_buy_shares_one_pool(self):
        # cash 4000 + held short put 4000 obligation = 0 free; buy 100x40 must fail.
        s = snap(cash={"USD": D("4000")}, positions=[pos(put(strike=40), -1)])
        self.assertFalse(decide(s, req(stock(), "BUY", 100, limit=40)).allowed)
        s2 = snap(cash={"USD": D("8000")}, positions=[pos(put(strike=40), -1)])
        self.assertTrue(decide(s2, req(stock(), "BUY", 100, limit=40)).allowed)

    def test_zero_cash_lmt_stock_buy_rejected(self):
        s = snap(cash={"USD": D("0")})
        d = decide(s, req(stock(), "BUY", 100, limit=40))
        self.assertFalse(d.allowed)
        self.assertTrue(any("cash" in r.lower() for r in d.reasons))

    def test_proposed_buy_uses_free_cash_after_reservations(self):
        s = snap(cash={"USD": D("4000")})
        self.assertTrue(decide(s, req(stock(), "BUY", 100, limit=40)).allowed)
        self.assertFalse(decide(s, req(stock(), "BUY", 100, limit=40),
                                reservations={"cash_base": D("0.01")}).allowed)

    # ---- 2. order_type must be LMT for a real bounded notional ------------
    def test_mkt_order_with_limit_price_is_not_bounded(self):
        # A MKT order carrying a stray limit_price must NOT create a fictitious
        # bound for the request...
        r = risk.OrderRequest(stock(), "BUY", D(100), "MKT", D(40))
        self.assertFalse(decide(snap(cash={"USD": D("100000")}), r).allowed)
        # ...nor for a pending working order.
        s = snap(cash={"USD": D("4000")},
                 orders=[order(stock(), "BUY", 100, limit=40, otype="MKT")])
        self.assertFalse(risk.summarize(s)["exposure_confirmed"])
        self.assertFalse(decide(s, req(put(strike=40), "SELL", 1),
                                enabled(per_underlying={"amount": 1000000})).allowed)

    def test_lmt_order_without_limit_price_is_unbounded(self):
        r = risk.OrderRequest(stock(), "BUY", D(100), "LMT")
        self.assertFalse(decide(snap(cash={"USD": D("100000")}), r).allowed)
        s = snap(orders=[order(stock(), "BUY", 100, otype="LMT")])
        self.assertFalse(risk.summarize(s)["exposure_confirmed"])

    # ---- 3. Conflicting signed rows on the same conId ---------------------
    def test_conflicting_signed_rows_same_conid_rejected(self):
        # -1 and +1 on the same conId net to zero but are contradictory;
        # a BUY must not be allowed from a phantom close capacity.
        s = snap(positions=[pos(call(con_id=101), -1), pos(call(con_id=101), 1)])
        with self.assertRaises(risk.RiskDataError):
            decide(s, req(call(con_id=101), "BUY", 1))

    def test_net_short_rows_same_conid_aggregate_close_capacity(self):
        # Two clean short rows on one conId -> 2 contracts of close capacity.
        s = snap(positions=[pos(call(con_id=101), -1), pos(call(con_id=101), -1)])
        self.assertEqual(risk.summarize(s)["short_close_capacity"]["101"], "2")
        self.assertTrue(decide(s, req(call(con_id=101), "BUY", 2, limit=1)).allowed)
        self.assertFalse(decide(s, req(call(con_id=101), "BUY", 3, limit=1)).allowed)

    # ---- 4. Unmapped reservation keys must fail closed --------------------
    def test_integer_shares_reservation_key_rejected(self):
        s = snap(positions=[pos(stock(), 200, 50)])
        with self.assertRaises(risk.RiskDataError):
            decide(s, req(call(), "SELL", 2), reservations={"shares": {1: D(100)}})

    def test_malformed_close_reservation_key_rejected(self):
        s = snap(positions=[pos(put(con_id=201), -3)])
        with self.assertRaises(risk.RiskDataError):
            decide(s, req(put(con_id=201), "BUY", 1, limit=1),
                   reservations={"close": {"not-a-conid": D(2)}})

    def test_exposure_reservation_key_must_be_underlying_key(self):
        s = snap(positions=[pos(stock(), 100, 50)])
        with self.assertRaises(risk.RiskDataError):
            decide(s, req(call(), "SELL", 1), reservations={"exposure": {1: D(100)}})

    # ---- 5. Request instrument identity consistency -----------------------
    def test_request_conid_mismatch_with_held_short_rejected(self):
        # Held short CALL conId 101; a PUT BUY on the SAME conId must not be
        # allowed as if it were a close of that call.
        s = snap(positions=[pos(call(con_id=101), -1)])
        with self.assertRaises(risk.RiskDataError):
            decide(s, req(put(con_id=101), "BUY", 1))

    def test_request_conid_matches_held_short_allows_close(self):
        s = snap(positions=[pos(put(con_id=201), -1)])
        self.assertTrue(decide(s, req(put(con_id=201), "BUY", 1, limit=1)).allowed)

    def test_stock_instrument_requires_underlying_id_equal_conid(self):
        bad = stock(con_id=2, underlying=1)  # STK whose underConId != conId
        with self.assertRaises(risk.RiskDataError):
            risk.OrderRequest(bad, "BUY", D(1), "LMT", D(40))
        with self.assertRaises(risk.RiskDataError):
            risk.summarize(snap(positions=[pos(bad, 100, 50)]))
        # A valid STK (con_id == underlying_id) is fine.
        self.assertIsInstance(risk.OrderRequest(stock(), "BUY", D(1), "LMT", D(40)),
                              risk.OrderRequest)

    # ---- extra: enabled must be strictly bool -----------------------------
    def test_enabled_rejects_non_bool_values(self):
        for value in ("false", "true", "0", "1", 0, 1, None, "yes", [], {}):
            with self.subTest(value=value):
                with self.assertRaises(risk.RiskConfigError):
                    risk.validate_risk_config({"risk": {"enabled": value}})
        # A real bool still works.
        self.assertFalse(risk.validate_risk_config({"risk": {"enabled": False}}).enabled)
        self.assertTrue(risk.validate_risk_config({"risk": {
            "enabled": True, "day_timezone": "UTC", "max_daily_submissions": 1}}).enabled)

    # ---- report-facing summarize breakdown (no duplicated math) ----------
    def test_summarize_call_cover_and_put_cash_breakdown(self):
        st = stock()
        short_call = pos(call(con_id=101), -1)          # 100 shares required
        short_put = pos(put(con_id=201), -1)            # 4000 USD obligation
        hkd_put = Instrument(301, "AAPL", "HKD", "OPT", "SMART", 1, D(100), "P",
                             D(40), "20261218", "AAPL")
        s = snap(cash={"USD": D("50000")},
                 fx={"USD": D(1), "HKD": D("0.125")},
                 positions=[pos(st, 500, 50), short_call, short_put],
                 orders=[order(hkd_put, "SELL", 1)])
        out = risk.summarize(s)
        self.assertEqual(out["call_cover"]["1:USD"]["required_shares"], "100")
        self.assertEqual(out["call_cover"]["1:USD"]["available_shares"], "400")
        self.assertEqual(out["put_cash_obligations"]["USD"], "4000")
        self.assertEqual(out["put_cash_obligations"]["HKD"], "4000")

    def test_summarize_optional_limits_and_headroom(self):
        lim = enabled(per_underlying={"amount": 10000}, portfolio={"amount": 20000})
        s = snap(positions=[pos(stock(), 100, 50)], netliq=D(50000))
        out = risk.summarize(s, limits=lim)
        self.assertEqual(out["limits"]["per_underlying_amount_base"], "10000")
        self.assertEqual(out["limits"]["portfolio_amount_base"], "20000")
        # exposure 5000 -> per-underlying headroom 5000, portfolio 15000.
        self.assertEqual(D(out["headroom"]["per_underlying_amount_base"]["1:USD"]),
                         D("5000"))
        self.assertEqual(D(out["headroom"]["portfolio_amount_base"]), D("15000"))
        # Without limits, the extra keys are absent (backward compatible).
        self.assertNotIn("limits", risk.summarize(s))
        self.assertNotIn("headroom", risk.summarize(s))


class OptionPurchaseCashTests(unittest.TestCase):
    def test_pending_buyback_premium_consumes_shared_cash(self):
        c = call()
        s = snap(cash={"USD": D(5000)}, positions=[pos(c, -1)],
                 orders=[order(c, "BUY", 1, limit=10)])
        self.assertTrue(decide(s, req(put(), "SELL", 1), limits=disabled()).allowed)
        short = snap(cash={"USD": D("4999.99")}, positions=s.positions, orders=s.orders)
        self.assertFalse(decide(short, req(put(), "SELL", 1), limits=disabled()).allowed)
        self.assertEqual(risk.summarize(s)["free_cash_base"], "4000")

    def test_pending_and_proposed_premiums_for_100_200_500_multipliers(self):
        for mult in (100, 200, 500):
            with self.subTest(multiplier=mult):
                c = call(multiplier=mult)
                # One pending close and one proposed close each cost mult * 2.
                cash = D(mult * 4)
                s = snap(cash={"USD": cash}, positions=[pos(c, -2)],
                         orders=[order(c, "BUY", 1, limit=2)])
                request = req(c, "BUY", 1, limit=2)
                self.assertTrue(decide(s, request, limits=disabled()).allowed)
                short = snap(cash={"USD": cash - D("0.01")},
                             positions=s.positions, orders=s.orders)
                self.assertFalse(decide(short, request, limits=disabled()).allowed)

    def test_proposed_foreign_premium_uses_fx_and_requires_rate(self):
        c = call(currency="HKD", multiplier=200)
        s = snap(cash={"USD": D(250)}, positions=[pos(c, -1)],
                 fx={"USD": D(1), "HKD": D("0.125")})
        request = req(c, "BUY", 1, limit=10)
        self.assertTrue(decide(s, request).allowed)
        short = snap(cash={"USD": D("249.99")}, positions=s.positions, fx=s.fx)
        self.assertFalse(decide(short, request).allowed)
        missing = snap(cash=s.cash, positions=s.positions)
        self.assertFalse(decide(missing, request).allowed)

    def test_pending_option_premium_uses_fx_multiplier_and_remaining(self):
        c = call(currency="HKD", multiplier=200)
        s = snap(cash={"USD": D(4250)}, positions=[pos(c, -2)],
                 orders=[order(c, "BUY", 1, limit=10)],
                 fx={"USD": D(1), "HKD": D("0.125")})
        self.assertEqual(risk.summarize(s)["free_cash_base"], "4000.000")
        self.assertTrue(decide(s, req(put(), "SELL", 1), limits=disabled()).allowed)

    def test_pending_opening_long_or_unbounded_buy_is_unknown(self):
        for pending, held in ((order(call(), "BUY", 1, limit=10), ()),
                              (order(call(), "BUY", 2, limit=10), (pos(call(), -1),)),
                              (order(call(), "BUY", 1, limit=10, otype="MKT"),
                               (pos(call(), -1),)),
                              (order(call(), "BUY", 1), (pos(call(), -1),))):
            with self.subTest(order=pending, positions=held):
                s = snap(positions=held, orders=[pending])
                summary = risk.summarize(s)
                self.assertFalse(summary["cash_confirmed"])
                self.assertFalse(summary["exposure_confirmed"])
                self.assertFalse(decide(s, req(put(), "SELL", 1), limits=disabled()).allowed)

    def test_pending_option_buys_check_aggregate_close_capacity(self):
        c = call()
        s = snap(positions=[pos(c, -1)], orders=[order(c, "BUY", 1, limit=10, oid=1),
                                                order(c, "BUY", 1, limit=10, oid=2)])
        self.assertFalse(risk.summarize(s)["cash_confirmed"])
        self.assertFalse(risk.summarize(s)["exposure_confirmed"])

    def test_proposed_buyback_preserves_full_existing_put_collateral(self):
        c = call()
        positions = (pos(c, -1), pos(put(), -1))
        s = snap(cash={"USD": D(5000)}, positions=positions)
        self.assertTrue(decide(s, req(c, "BUY", 1, limit=10)).allowed)
        short = snap(cash={"USD": D("4999.99")}, positions=positions)
        self.assertFalse(decide(short, req(c, "BUY", 1, limit=10), limits=disabled()).allowed)
        # Closing a Put does not release its old collateral before a fill.
        s = snap(cash={"USD": D(4000)}, positions=[pos(put(), -1)])
        self.assertFalse(decide(s, req(put(), "BUY", 1, limit=1)).allowed)
        funded = snap(cash={"USD": D(4100)}, positions=s.positions)
        self.assertTrue(decide(funded, req(put(), "BUY", 1, limit=1)).allowed)
        pending = snap(cash={"USD": D(4100)}, positions=s.positions,
                       orders=[order(put(), "BUY", 1, limit=1)])
        self.assertEqual(risk.summarize(pending)["free_cash_base"], "0")
        self.assertEqual(risk.summarize(pending)["put_cash_obligations"]["USD"], "4000")

    def test_proposed_buyback_requires_bounded_confirmed_cash(self):
        c = call()
        s = snap(positions=[pos(c, -1)])
        for request in (req(c, "BUY", 1), req(c, "BUY", 1, limit=1, otype="MKT")):
            self.assertFalse(decide(s, request).allowed)
        unknown = snap(cash={"HKD": D(10000)}, positions=[pos(c, -1)])
        self.assertFalse(decide(unknown, req(c, "BUY", 1, limit=1)).allowed)
        self.assertFalse(decide(s, req(c, "BUY", 1, limit=1),
                                reservations={"cash_base": D(100000)}).allowed)

    def test_missing_option_premium_fx_marks_cash_and_exposure_unknown(self):
        c = call(currency="HKD")
        s = snap(positions=[pos(c, -1)], orders=[order(c, "BUY", 1, limit=1)])
        summary = risk.summarize(s)
        self.assertFalse(summary["cash_confirmed"])
        self.assertFalse(summary["exposure_confirmed"])
        self.assertFalse(decide(s, req(put(), "SELL", 1), limits=disabled()).allowed)

    def test_unknown_exposure_keeps_underlying_keys_and_unknown_headroom(self):
        s = snap(positions=[pos(stock(), 100)])
        summary = risk.summarize(s, limits=enabled(portfolio={"amount": 5000}))
        self.assertFalse(summary["exposure_confirmed"])
        self.assertIsNone(summary["headroom"]["portfolio_amount_base"])
        self.assertIn("1:USD", summary["headroom"]["per_underlying_amount_base"])
        self.assertIsNone(summary["headroom"]["per_underlying_amount_base"]["1:USD"])
        self.assertIsNone(summary["exposure_by_underlying"]["1:USD"])
        self.assertIsNone(summary["exposure_portfolio_base"])
        pending = snap(orders=[order(call(underlying=2), "BUY", 1, otype="MKT")])
        summary = risk.summarize(pending, reservations={"exposure": {"3:USD": D(100)}},
                                 limits=enabled())
        self.assertEqual(set(summary["exposure_by_underlying"]), {"2:USD", "3:USD"})
        self.assertTrue(all(v is None for v in summary["exposure_by_underlying"].values()))
        self.assertTrue(all(v is None for v in
                            summary["headroom"]["per_underlying_amount_base"].values()))


if __name__ == "__main__":
    unittest.main()
