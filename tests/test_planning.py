"""Offline tests for pure cumulative-budget planning.

No broker, network, git, files, agents or shared/production writes.  All inputs
are immutable broker_state / risk value objects; snapshots are direct-constructed
(and verified to be unchanged after planning).  The layer reuses risk.assess /
risk.summarize rather than maintaining a second risk model.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import unittest

from thetamill.broker_state import (FxQuote, Instrument, Position, PriceQuote,
                                    Snapshot, WorkingOrder)
from thetamill import planning as p
from thetamill import risk
from thetamill.risk import OrderRequest, validate_risk_config

NOW = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
EXPIRY = "20261218"


def stock(con_id=10, *, currency="USD", mult=D(1)):
    return Instrument(con_id, "AAPL", currency, "STK", "SMART", con_id,
                      mult)


def call(con_id=20, *, mult=D(100), strike=D(40), underlying_id=10, currency="USD"):
    return Instrument(con_id, "AAPL", currency, "OPT", "SMART", underlying_id,
                      mult, "C", strike, EXPIRY, "AAPL")


def put(con_id=30, *, mult=D(100), strike=D(40), underlying_id=10, currency="USD"):
    return Instrument(con_id, "AAPL", currency, "OPT", "SMART", underlying_id,
                      mult, "P", strike, EXPIRY, "AAPL")


def snap(*, positions=(), orders=(), cash=None, fx=None, base="USD",
         captured_at=NOW, issues=(), net_liq=D(200000),
         price_quotes=None, fx_quotes=None, **kw):
    return Snapshot("DU1234567", "paper", captured_at, base,
                    cash if cash is not None else {"USD": D(200000)},
                    net_liq, tuple(positions), tuple(orders),
                    fx if fx is not None else {"USD": D(1)},
                    tuple(issues),
                    price_quotes or {}, fx_quotes or {}, **kw)


def fx_quote(cur, rate, *, as_of=NOW, source="IB timestamped bid/ask midpoint"):
    return FxQuote(cur, D(rate), as_of, source)


def price_quote(con_id, price, *, as_of=NOW, source="IB timestamped bid/ask midpoint"):
    return PriceQuote(con_id, D(price), as_of, source)


def pos(inst, qty, price=None):
    return Position(inst, D(qty), price)


def order(inst, action, remaining, *, limit=None, otype="LMT"):
    return WorkingOrder(inst, action, D(remaining), 1, 2, "tm-ref",
                        D(limit) if limit is not None else None, otype)


def req(inst, action, qty, *, limit=None, otype="LMT"):
    return OrderRequest(inst, action, D(qty), otype,
                        D(limit) if limit is not None else None, "DAY")


def enabled(**overrides):
    raw = {"enabled": True, "day_timezone": "UTC", "max_daily_submissions": 10}
    raw.update(overrides)
    return validate_risk_config({"risk": raw})


def disabled():
    return validate_risk_config({"risk": {"enabled": False}})


def plan(snapshot, requests, limits=None, reservations=None, now=NOW, **kw):
    return p.assess_plan(snapshot, requests, limits or enabled(),
                         reservations, now=now, **kw)


# --------------------------------------------------------------------------- #
# Shared evidence gate
# --------------------------------------------------------------------------- #
class EvidenceGateTests(unittest.TestCase):
    def test_clean_snapshot_has_no_evidence_reasons(self):
        s = snap()
        self.assertEqual(p.validate_snapshot_evidence(s, enabled(), NOW), ())

    def test_caps_off_base_put_needs_no_unrelated_stock_quote(self):
        # A positive stock position with NO price quote: caps off -> irrelevant.
        s = snap(positions=(pos(stock(), 100, None),))
        reasons = p.validate_snapshot_evidence(s, enabled(), NOW)
        self.assertEqual(reasons, ())

    def test_caps_on_require_stock_price_evidence(self):
        s = snap(positions=(pos(stock(), 100, D(50)),),
                 price_quotes={10: price_quote(10, 50)})
        caps = enabled(per_underlying={"amount": 100000})
        self.assertEqual(p.validate_snapshot_evidence(s, caps, NOW), ())

    def test_caps_on_missing_stock_price_evidence_blocks(self):
        s = snap(positions=(pos(stock(), 100, D(50)),))
        caps = enabled(per_underlying={"amount": 100000})
        reasons = p.validate_snapshot_evidence(s, caps, NOW)
        self.assertTrue(any("quote evidence" in r or "stock price" in r
                            for r in reasons))

    def test_foreign_fx_quote_required_when_consumed(self):
        s = snap(cash={"USD": D(200000), "EUR": D(1000)},
                 fx={"USD": D(1), "EUR": D("1.1")})
        reasons = p.validate_snapshot_evidence(s, enabled(), NOW)
        self.assertTrue(any("EUR" in r for r in reasons))
        s2 = replace(s, fx_quotes={"EUR": fx_quote("EUR", "1.1")})
        self.assertEqual(p.validate_snapshot_evidence(s2, enabled(), NOW), ())

    def test_extra_candidate_currency_is_checked(self):
        s = snap(cash={"USD": D(200000), "EUR": D(1000)},
                 fx={"USD": D(1), "EUR": D("1.1")},
                 fx_quotes={"EUR": fx_quote("EUR", "1.1")})
        # Introduce a new foreign currency not yet in the snapshot.
        s = replace(s, fx={**dict(s.fx), "HKD": D("0.13")})
        reasons = p.validate_snapshot_evidence(
            s, enabled(), NOW, needed_currencies=("HKD",))
        self.assertTrue(any("HKD" in r for r in reasons))

    def test_fx_quote_value_must_match_snapshot_scalar_exactly(self):
        s = snap(cash={"USD": D(200000), "EUR": D(1000)},
                 fx={"USD": D(1), "EUR": D("1.1")},
                 fx_quotes={"EUR": fx_quote("EUR", "1.2")})
        reasons = p.validate_snapshot_evidence(s, enabled(), NOW)
        self.assertTrue(any("does not match" in r for r in reasons))

    def test_fx_quote_source_allowlist(self):
        for source in ("delayed feed", "random", "IB delayed"):
            s = snap(cash={"USD": D(200000), "EUR": D(1000)},
                     fx={"USD": D(1), "EUR": D("1.1")},
                     fx_quotes={"EUR": fx_quote("EUR", "1.1", source=source)})
            reasons = p.validate_snapshot_evidence(s, enabled(), NOW)
            self.assertTrue(any("source" in r for r in reasons))

    def test_fx_quote_stale_and_future_rejected(self):
        stale = fx_quote("EUR", "1.1", as_of=NOW - timedelta(seconds=31))
        future = fx_quote("EUR", "1.1", as_of=NOW + timedelta(seconds=6))
        for q in (stale, future):
            s = snap(cash={"USD": D(200000), "EUR": D(1000)},
                     fx={"USD": D(1), "EUR": D("1.1")},
                     fx_quotes={"EUR": q})
            self.assertTrue(p.validate_snapshot_evidence(s, enabled(), NOW))

    def test_snapshot_stale_blocks(self):
        s = snap(captured_at=NOW - timedelta(seconds=31))
        reasons = p.validate_snapshot_evidence(s, enabled(), NOW)
        self.assertTrue(any("stale" in r for r in reasons))

    def test_configured_snapshot_age(self):
        s = snap(captured_at=NOW - timedelta(seconds=100))
        limits = enabled(max_snapshot_age_seconds=120)
        self.assertEqual(p.validate_snapshot_evidence(s, limits, NOW), ())

    def test_snapshot_future_blocks(self):
        s = snap(captured_at=NOW + timedelta(seconds=6))
        self.assertTrue(p.validate_snapshot_evidence(s, enabled(), NOW))

    def test_required_evidence_currencies_ignores_zero_cash_and_base(self):
        s = snap(cash={"USD": D(200000), "EUR": D(0), "HKD": D(5)},
                 fx={"USD": D(1), "HKD": D("0.13")})
        self.assertEqual(p.required_evidence_currencies(s), frozenset({"HKD"}))

    def test_quote_evidence_problem_returns_none_for_good_quote(self):
        q = price_quote(10, 50)
        self.assertIsNone(p.quote_evidence_problem(q, D(50), key=10, now=NOW))

    def test_quote_evidence_problem_detects_value_mismatch(self):
        q = price_quote(10, 50)
        problem = p.quote_evidence_problem(q, D(51), key=10, now=NOW)
        self.assertIsNotNone(problem)
        self.assertIn("does not match", problem)

    def test_quote_evidence_problem_detects_missing_quote(self):
        self.assertIn("missing", p.quote_evidence_problem(
            None, D(50), key=10, now=NOW))

    def test_explicit_need_stock_prices_forces_gate_without_caps(self):
        s = snap(positions=(pos(stock(), 100, D(50)),))
        # No caps, but caller explicitly forces stock valuation evidence.
        reasons = p.validate_snapshot_evidence(
            s, disabled(), NOW, need_stock_prices=True)
        self.assertTrue(any("quote evidence" in r for r in reasons))
        s2 = replace(s, price_quotes={10: price_quote(10, 50)})
        self.assertEqual(p.validate_snapshot_evidence(
            s2, disabled(), NOW, need_stock_prices=True), ())

    def test_require_all_evidence_forces_stock_gate(self):
        s = snap(positions=(pos(stock(), 100, D(50)),))
        reasons = p.validate_snapshot_evidence(
            s, disabled(), NOW, require_all_evidence=True)
        self.assertTrue(any("quote evidence" in r for r in reasons))

    def test_evidence_gate_does_not_mutate_snapshot(self):
        s = snap(cash={"USD": D(200000), "EUR": D(1000)},
                 fx={"USD": D(1), "EUR": D("1.1")},
                 fx_quotes={"EUR": fx_quote("EUR", "1.1")})
        before = (dict(s.cash), dict(s.fx), dict(s.fx_quotes),
                  tuple(s.positions))
        p.validate_snapshot_evidence(s, enabled(), NOW)
        self.assertEqual((dict(s.cash), dict(s.fx), dict(s.fx_quotes),
                          tuple(s.positions)), before)

    def test_naive_now_rejected(self):
        with self.assertRaises(p.PlanningError):
            p.validate_snapshot_evidence(snap(), enabled(),
                                         datetime(2026, 11, 7, 20))


# --------------------------------------------------------------------------- #
# Sequential accumulation & requirements
# --------------------------------------------------------------------------- #
class CumulativeBudgetTests(unittest.TestCase):
    def test_two_short_puts_share_one_cash_pool(self):
        s = snap()
        r1 = req(put(con_id=30), "SELL", 2)       # 2*40*100 = 8000
        r2 = req(put(con_id=31), "SELL", 3)       # 3*40*100 = 12000
        a = plan(s, [r1, r2])
        self.assertEqual(a.allowed_count, 2)
        self.assertEqual(a.reservations["cash_base"], D(20000))
        self.assertEqual(a.reservations["exposure"]["10:USD"], D(20000))
        # Second order saw the first's 8000 already committed.
        self.assertEqual(a.summary["free_cash_base"], "180000")

    def test_exact_100_200_500_multipliers(self):
        s = snap()
        rs = [req(put(con_id=30, mult=D(100)), "SELL", 1),
              req(put(con_id=31, mult=D(200)), "SELL", 1),
              req(put(con_id=32, mult=D(500)), "SELL", 1)]
        a = plan(s, rs)
        self.assertEqual(a.allowed_count, 3)
        self.assertEqual(a.reservations["cash_base"], D(40 * (100 + 200 + 500)))

    def test_mixed_multiplier_put_obligation_uses_actual_mult(self):
        s = snap()
        a = plan(s, [req(put(con_id=30, mult=D(200), strike=D(50)), "SELL", 2)])
        self.assertEqual(a.reservations["cash_base"], D(2 * 50 * 200))

    def test_short_call_consumes_shares_qty_times_mult(self):
        s = snap(positions=(pos(stock(), 1000, D(10)),))
        a = plan(s, [req(call(con_id=20, mult=D(100)), "SELL", 3)])
        self.assertEqual(a.allowed_count, 1)
        self.assertEqual(a.reservations["shares"]["10:USD"], D(300))

    def test_second_call_cannot_double_spend_same_shares(self):
        s = snap(positions=(pos(stock(), 200, D(10)),))
        r1 = req(call(con_id=20, mult=D(100)), "SELL", 2)   # needs 200
        r2 = req(call(con_id=21, mult=D(100)), "SELL", 1)   # needs 100 more
        a = plan(s, [r1, r2])
        self.assertEqual(a.allowed_count, 1)
        self.assertEqual(a.blocked_count, 1)
        self.assertFalse(a.items[1].allowed)
        self.assertIsNone(a.items[1].requirement)
        self.assertEqual(a.reservations["shares"]["10:USD"], D(200))

    def test_stock_buy_lmt_bounds_cash_and_exposure(self):
        s = snap()
        a = plan(s, [req(stock(), "BUY", 10, limit=50, otype="LMT")])
        self.assertEqual(a.allowed_count, 1)
        self.assertEqual(a.reservations["cash_base"], D(500))
        self.assertEqual(a.reservations["exposure"]["10:USD"], D(500))

    def test_market_stock_buy_is_blocked_and_consumes_nothing(self):
        s = snap()
        a = plan(s, [req(stock(), "BUY", 10, limit=50, otype="MKT")])
        self.assertEqual(a.allowed_count, 0)
        self.assertIsNone(a.items[0].requirement)
        self.assertEqual(a.reservations, p.empty_reservations())

    def test_stock_sell_consumes_shares_never_lowers_exposure(self):
        s = snap(positions=(pos(stock(), 500, D(10)),))
        a = plan(s, [req(stock(), "SELL", 200)])
        self.assertEqual(a.allowed_count, 1)
        self.assertEqual(a.reservations["shares"]["10:USD"], D(200))
        self.assertEqual(a.reservations["exposure"], {})

    def test_option_buy_consumes_exact_close_and_bounded_premium(self):
        s = snap(positions=(pos(put(con_id=30), -2, D(2)),))
        a = plan(s, [req(put(con_id=30), "BUY", 1, limit=2, otype="LMT")])
        self.assertEqual(a.allowed_count, 1)
        self.assertEqual(a.reservations["close"][30], D(1))
        self.assertEqual(a.reservations["cash_base"], D(1 * 100 * 2))  # premium

    def test_option_buy_does_not_release_old_obligations(self):
        # Held short put 2 x 40 x 100 = 8000 stays locked; a 1-lot buyback
        # consumes close capacity + premium but must NOT free cash/cover.
        s = snap(positions=(pos(put(con_id=30), -2, D(2)),))
        a = plan(s, [req(put(con_id=30), "BUY", 1, limit=2, otype="LMT")])
        self.assertEqual(a.allowed_count, 1)
        self.assertNotIn("shares", {k: v for k, v in a.reservations.items()
                                    if k == "cash_base" and v < D(0)})
        self.assertEqual(a.reservations["cash_base"], D(200))
        # The original 8000 obligation is still in the summary pool.
        self.assertEqual(a.summary["free_cash_base"], "191800")

    def test_duplicate_conid_is_rejected_no_double_spend(self):
        s = snap()
        r = req(put(con_id=30), "SELL", 1)
        a = plan(s, [r, r])
        self.assertEqual(a.allowed_count, 1)
        self.assertEqual(a.blocked_count, 1)
        self.assertIn("duplicate", " ".join(a.items[1].evidence_reasons))
        self.assertEqual(a.reservations["cash_base"], D(4000))

    def test_blocked_item_does_not_consume_budget(self):
        s = snap(cash={"USD": D(5000)})
        okay = req(put(con_id=30, strike=D(40)), "SELL", 1)   # 4000
        too_big = req(put(con_id=31, strike=D(40)), "SELL", 1)  # another 4000
        # A later 1000-put still fits ONLY because the blocked 4000 consumed
        # nothing; had it wrongly consumed budget even this would be blocked.
        after = req(put(con_id=32, strike=D(10)), "SELL", 1)   # 1000
        a = plan(s, [okay, too_big, after])
        self.assertEqual(a.allowed_count, 2)
        self.assertFalse(a.items[1].allowed)
        self.assertIsNone(a.items[1].requirement)
        self.assertEqual(a.reservations["cash_base"], D(5000))
        self.assertTrue(a.items[2].allowed)

    def test_blocked_item_never_frees_budget_for_a_later_big_order(self):
        s = snap(cash={"USD": D(5000)})
        okay = req(put(con_id=30, strike=D(40)), "SELL", 1)
        blocked = req(put(con_id=31, strike=D(40)), "SELL", 1)
        big = req(put(con_id=32, strike=D(40)), "SELL", 1)
        a = plan(s, [okay, blocked, big])
        self.assertEqual(a.allowed_count, 1)
        self.assertFalse(a.items[1].allowed)
        self.assertFalse(a.items[2].allowed)
        self.assertEqual(a.reservations["cash_base"], D(4000))

    def test_no_truncation_or_silent_qty_adjust(self):
        s = snap(cash={"USD": D(5000)})
        a = plan(s, [req(put(con_id=30, strike=D(40)), "SELL", 5)])
        self.assertEqual(a.allowed_count, 0)
        self.assertEqual(a.reservations, p.empty_reservations())

    def test_existing_reservations_are_conserved_and_not_mutated(self):
        s = snap(cash={"USD": D(12000)})
        existing = {"cash_base": D(9000), "shares": {}, "close": {},
                    "exposure": {}}
        a = plan(s, [req(put(con_id=30), "SELL", 1)], reservations=existing)
        self.assertEqual(a.allowed_count, 0)
        self.assertEqual(existing, {"cash_base": D(9000), "shares": {},
                                    "close": {}, "exposure": {}})
        self.assertEqual(a.reservations["cash_base"], D(9000))

    def test_existing_close_reservation_consumes_capacity(self):
        s = snap(positions=(pos(put(con_id=30), -2, D(2)),))
        existing = {"cash_base": D(0), "shares": {}, "close": {30: D(1)},
                    "exposure": {}}
        a = plan(s, [req(put(con_id=30), "BUY", 2, limit=2)],
                 reservations=existing)
        self.assertEqual(a.allowed_count, 0)  # only 1 close left
        self.assertEqual(a.reservations["close"][30], D(1))

    def test_snapshot_is_never_mutated(self):
        s = snap(positions=(pos(stock(), 500, D(10)),
                            pos(put(con_id=30), -2, D(2))))
        before = (dict(s.cash), dict(s.fx), tuple(s.positions),
                  tuple(s.orders), dict(s.price_quotes), dict(s.fx_quotes))
        plan(s, [req(stock(), "SELL", 100),
                 req(put(con_id=31), "SELL", 1)])
        self.assertEqual((dict(s.cash), dict(s.fx), tuple(s.positions),
                          tuple(s.orders), dict(s.price_quotes),
                          dict(s.fx_quotes)), before)

    def test_unknown_fx_blocks_candidate_without_guessing_zero(self):
        eur_put = put(con_id=30, currency="EUR", mult=D(100), strike=D(40))
        s = snap(cash={"USD": D(200000)}, fx={"USD": D(1)})
        a = plan(s, [req(eur_put, "SELL", 1)])
        self.assertEqual(a.allowed_count, 0)
        self.assertEqual(a.reservations, p.empty_reservations())

    def test_mixed_currencies_share_one_cash_pool(self):
        eur_put = put(con_id=30, currency="EUR", mult=D(100), strike=D(40))
        s = snap(cash={"USD": D(6000), "EUR": D(1000)},
                 fx={"USD": D(1), "EUR": D("2")},
                 fx_quotes={"EUR": fx_quote("EUR", "2")})
        # 1 EUR put = 4000 EUR = 8000 BASE; cash = 6000 + 2000 = 8000 BASE.
        a = plan(s, [req(eur_put, "SELL", 1)])
        self.assertEqual(a.allowed_count, 1)
        self.assertEqual(a.reservations["cash_base"], D(8000))
        self.assertEqual(a.reservations["exposure"]["10:EUR"], D(8000))

    def test_stale_snapshot_blocks_every_increasing_order(self):
        s = snap(captured_at=NOW - timedelta(seconds=31))
        a = plan(s, [req(put(con_id=30), "SELL", 1)])
        self.assertEqual(a.allowed_count, 0)
        self.assertTrue(any("stale" in r for r in a.evidence_reasons))
        self.assertTrue(any("stale" in r for r in a.items[0].evidence_reasons))

    def test_caps_enforced_cumulatively(self):
        s = snap()
        limits = enabled(per_underlying={"amount": 5000})
        # first put 4000 OK, second would push to 8000 > 5000 cap.
        a = plan(s, [req(put(con_id=30), "SELL", 1),
                     req(put(con_id=31), "SELL", 1)], limits)
        self.assertEqual(a.allowed_count, 1)
        self.assertEqual(a.blocked_count, 1)
        self.assertEqual(a.reservations["cash_base"], D(4000))

    def test_caps_off_base_put_no_unrelated_stock_quote_needed(self):
        s = snap(positions=(pos(stock(), 100, None),))  # unpriced stock
        a = plan(s, [req(put(con_id=30), "SELL", 1)], disabled())
        self.assertEqual(a.allowed_count, 1)

    def test_plan_document_is_data_only(self):
        s = snap()
        a = plan(s, [req(put(con_id=30), "SELL", 1)])
        doc = p.plan_document(a)
        self.assertFalse(doc["executable"])
        self.assertTrue(doc["recommendation_only"])
        for forbidden in ("items_to_place", "orders_to_place", "placeOrder", "submit"):
            self.assertNotIn(forbidden, doc)
        self.assertFalse(doc["items"][0]["executable"])
        self.assertFalse(any(i.executable for i in a.items))

    def test_blocked_item_never_marked_executable(self):
        s = snap(cash={"USD": D(1000)})
        a = plan(s, [req(put(con_id=30), "SELL", 1)])
        self.assertEqual(a.allowed_count, 0)
        self.assertFalse(a.items[0].allowed)
        self.assertFalse(a.items[0].executable)

    def test_input_validation_rejects_bad_types(self):
        s = snap()
        with self.assertRaises(p.PlanningError):
            p.assess_plan(s, [object()], enabled(), now=NOW)
        with self.assertRaises(p.PlanningError):
            p.assess_plan(s, "not a sequence", enabled(), now=NOW)
        with self.assertRaises(p.PlanningError):
            p.assess_plan(s, [], "not limits", now=NOW)
        with self.assertRaises(p.PlanningError):
            p.assess_plan(s, [], enabled(), reservations={"bogus": D(1)}, now=NOW)

    def test_summary_reuses_risk_summarize(self):
        s = snap()
        a = plan(s, [req(put(con_id=30), "SELL", 1)])
        expected = risk.summarize(s, a.reservations, a.limits)
        # Immutable tuples/mappings in the assessment become ordinary JSON
        # containers only at the display boundary; values remain identical.
        self.assertEqual(p.plan_document(a)["summary"], dict(expected))

    def test_reservations_view_is_json_friendly(self):
        s = snap()
        a = plan(s, [req(put(con_id=30), "SELL", 1)])
        view = p.plan_document(a)["reservations"]
        self.assertEqual(view["cash_base"], "4000")
        self.assertEqual(view["exposure"], {"10:USD": "4000"})
        self.assertEqual(view["close"], {})

    def test_same_underlying_different_currency_not_mixed(self):
        eur = put(con_id=30, currency="EUR", mult=D(100), strike=D(40))
        s = snap(cash={"USD": D(200000), "EUR": D(100000)},
                 fx={"USD": D(1), "EUR": D("2")},
                 fx_quotes={"EUR": fx_quote("EUR", "2")})
        a = plan(s, [req(eur, "SELL", 1),
                     req(put(con_id=31), "SELL", 1)])
        self.assertEqual(a.allowed_count, 2)
        self.assertEqual(a.reservations["exposure"]["10:EUR"], D(8000))
        self.assertEqual(a.reservations["exposure"]["10:USD"], D(4000))
        self.assertEqual(a.reservations["cash_base"], D(12000))

    def test_float_str_decimal_inputs_normalized(self):
        s = snap(cash={"USD": 200000}, fx={"USD": 1})
        a = plan(s, [req(put(con_id=30), "SELL", 1)])
        self.assertEqual(a.allowed_count, 1)
        self.assertEqual(a.reservations["cash_base"], D(4000))

    def test_strict_bad_reservation_key_rejected(self):
        s = snap()
        with self.assertRaises(p.PlanningError):
            p.assess_plan(s, [], enabled(),
                          reservations={"shares": {1: D(100)}}, now=NOW)


class ReviewRegressionTests(unittest.TestCase):
    def test_close_aliases_accumulate_and_json_roundtrip_retains_capacity(self):
        s = snap(positions=(pos(put(), -3, D(2)),))
        existing = {"close": {"30": D(1), "030": D(1)}}
        a = plan(s, [req(put(), "BUY", 1, limit=2)], reservations=existing)
        self.assertEqual(a.allowed_count, 1)
        self.assertEqual(dict(a.reservations["close"]), {30: D(3)})
        view = p.plan_document(a)["reservations"]
        self.assertEqual(view["close"], {"30": "3"})
        after = plan(s, [req(put(), "BUY", 1, limit=2)], reservations=view)
        self.assertEqual(after.allowed_count, 0)
        self.assertEqual(existing, {"close": {"30": D(1), "030": D(1)}})

    def test_output_mappings_are_recursively_immutable(self):
        a = plan(snap(), [req(put(), "SELL", 1)])
        for mapping, key in ((a.reservations, "cash_base"),
                             (a.reservations["exposure"], "10:USD"),
                             (a.items[0].requirement, "cash_base"),
                             (a.items[0].requirement["exposure"], "10:USD"),
                             (a.items[0].decision.details, "quantity"),
                             (a.summary, "free_cash_base"),
                             (a.summary["headroom"], "portfolio_amount_base")):
            with self.subTest(key=key), self.assertRaises(TypeError):
                mapping[key] = "forged"
        self.assertIsInstance(a.summary["unknown"], tuple)
        self.assertEqual(a.summary["reserved_cash_base"], "4000")

    def test_document_is_independent_json_copy(self):
        import json
        a = plan(snap(), [req(put(), "SELL", 1)])
        first, second = p.plan_document(a), p.plan_document(a)
        first["summary"]["headroom"]["portfolio_amount_base"] = "forged"
        first["summary"]["unknown"].append("forged")
        first["reservations"]["exposure"]["10:USD"] = "0"
        self.assertIsNone(a.summary["headroom"]["portfolio_amount_base"])
        self.assertEqual(a.summary["unknown"], ())
        self.assertIsNone(second["summary"]["headroom"]["portfolio_amount_base"])
        self.assertEqual(second["summary"]["unknown"], [])
        self.assertEqual(second["reservations"]["exposure"]["10:USD"], "4000")
        self.assertIsInstance(json.loads(json.dumps(second)), dict)

    def test_false_override_cannot_disable_mandatory_stock_evidence(self):
        for caps, require_all in ((True, False), (False, True), (True, True)):
            limits = enabled(portfolio={"amount": 10000}) if caps else disabled()
            for quotes in ({}, {10: price_quote(10, 50, as_of=NOW - timedelta(days=1))}):
                with self.subTest(caps=caps, require_all=require_all, quotes=quotes):
                    s = snap(positions=(pos(stock(), 100, D(50)),), price_quotes=quotes)
                    a = plan(s, [req(put(), "SELL", 1)], limits,
                             need_stock_prices=False, require_all_evidence=require_all)
                    self.assertEqual(a.allowed_count, 0)
                    self.assertTrue(a.evidence_reasons)

    def test_evidence_failure_document_withholds_available_budgets_not_raw_summary(self):
        s = snap(positions=(pos(stock(), 100, D(50)), pos(put(), -2, D(2))),
                 captured_at=NOW - timedelta(days=1))
        a = plan(s, [req(put(con_id=31), "SELL", 1)],
                 enabled(portfolio={"amount": 100000}), need_stock_prices=False)
        self.assertEqual(a.allowed_count, 0)
        self.assertTrue(a.summary["cash_confirmed"])
        self.assertEqual(a.summary["free_cash_base"], "192000")
        doc = p.plan_document(a)
        self.assertFalse(doc["budget_confirmed"])
        summary = doc["summary"]
        self.assertFalse(summary["cash_confirmed"])
        self.assertFalse(summary["exposure_confirmed"])
        for key in ("free_cash_base", "cash_pool_base", "exposure_portfolio_base", "headroom"):
            self.assertIsNone(summary[key])
        self.assertEqual(summary["shares"], {"10:USD": None})
        self.assertIsNone(summary["call_cover"]["10:USD"]["available_shares"])
        self.assertEqual(summary["short_close_capacity"], {"30": None})
        self.assertTrue(all(v is None for v in summary["exposure_by_underlying"].values()))
        self.assertEqual(summary["put_cash_obligations"], {"USD": "8000"})
        self.assertEqual(summary["reserved_cash_base"], "0")

    def test_result_construction_owns_nested_copies(self):
        details = {"nested": {"values": ["original"]}}
        requirement = p.empty_reservations()
        decision = risk.Decision(risk.ALLOW, ("ok",), details)
        item = p.PlannedOrder(0, decision, requirement, ())
        summary = {"nested": {"values": ["original"]}}
        assessment = p.PlanAssessment([item], requirement, summary, enabled(), NOW, [])
        details["nested"]["values"].append("changed")
        summary["nested"]["values"].append("changed")
        requirement["shares"]["10:USD"] = D(100)
        self.assertEqual(item.decision.details["nested"]["values"], ("original",))
        self.assertEqual(assessment.summary["nested"]["values"], ("original",))
        self.assertEqual(dict(item.requirement["shares"]), {})
        self.assertEqual(dict(assessment.reservations["shares"]), {})
        self.assertIsInstance(assessment.items, tuple)

    def test_all_close_key_aliases_merge_before_export(self):
        a = plan(snap(), [], reservations={"close": {30: D(1), "30": D(2), "030": D(3)}})
        self.assertEqual(dict(a.reservations["close"]), {30: D(6)})
        self.assertEqual(p.plan_document(a)["reservations"]["close"], {"30": "6"})

    def test_missing_or_stale_fx_withholds_document_budget(self):
        for quotes in ({}, {"EUR": fx_quote("EUR", 2, as_of=NOW - timedelta(seconds=31))}):
            with self.subTest(quotes=quotes):
                s = snap(cash={"USD": D(10000), "EUR": D(1000)},
                         fx={"USD": D(1), "EUR": D(2)}, fx_quotes=quotes)
                a = plan(s, [req(put(), "SELL", 1)])
                self.assertEqual(a.allowed_count, 0)
                self.assertEqual(a.summary["free_cash_base"], "12000")
                doc = p.plan_document(a)
                self.assertFalse(doc["budget_confirmed"])
                self.assertIsNone(doc["summary"]["free_cash_base"])
                self.assertIsNone(doc["summary"]["headroom"])

    def test_false_override_preserves_irrelevant_stock_exception(self):
        s = snap(positions=(pos(stock(), 100, None),))
        a = plan(s, [req(put(), "SELL", 1)], disabled(), need_stock_prices=False)
        self.assertEqual(a.allowed_count, 1)
        self.assertEqual(a.evidence_reasons, ())

    def test_confirmed_document_keeps_verified_budgets(self):
        doc = p.plan_document(plan(snap(), [req(put(), "SELL", 1)]))
        self.assertTrue(doc["budget_confirmed"])
        self.assertEqual(doc["summary"]["free_cash_base"], "196000")


if __name__ == "__main__":
    unittest.main()
