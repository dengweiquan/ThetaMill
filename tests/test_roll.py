"""Offline tests for the Stage E recommendation-only roll engine.

No broker, network, files, git or order submission. All inputs are plain, immutable
broker_state value objects; the risk callback is a local stub.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import json
import unittest

from thetamill.broker_state import Instrument, Position, Snapshot, WorkingOrder
from thetamill import roll
from thetamill.roll import (
    Quote, RollConstraints, RollError, SCHEMA,
    build_roll_recommendation, render_roll_recommendation, validate_recommendation,
)

NOW = datetime(2026, 11, 7, 20, tzinfo=timezone.utc)
OLD_EXPIRY = "20261218"   # 41 DTE from NOW
NEW_EXPIRY = "20270115"   # 69 DTE from NOW


def old_inst(mult=D(100), **kw):
    p = dict(con_id=20, symbol="AAPL", currency="USD", sec_type="OPT",
             exchange="SMART", underlying_id=10, multiplier=mult,
             right="P", strike=D(40), expiry=OLD_EXPIRY, trading_class="AAPL")
    p.update(kw)
    return Instrument(**p)


def candidate(**kw):
    p = dict(con_id=30, symbol="AAPL", currency="USD", sec_type="OPT",
             exchange="SMART", underlying_id=10, multiplier=D(100),
             right="P", strike=D(40), expiry=NEW_EXPIRY, trading_class="AAPL")
    p.update(kw)
    return Instrument(**p)


def quote(con_id, bid=D("3"), ask=D("3.5"), *, age=timedelta(0), naive=False, source="test"):
    as_of = NOW - age
    if naive:
        as_of = as_of.replace(tzinfo=None)
    return Quote(con_id, bid, ask, as_of, source)


def make_snapshot(*, instrument=None, short=D(-5), orders=(), **kw):
    p = dict(account="DU1234567", environment="paper", captured_at=NOW,
             base_currency="USD", cash={"USD": D(16000)},
             net_liquidation=D(20000),
             positions=(Position(instrument or old_inst(), short, D(2)),),
             orders=tuple(orders), fx={"USD": D(1)})
    p.update(kw)
    return Snapshot(**p)


def pending_buy(remaining, instrument=None):
    return WorkingOrder(instrument or old_inst(), "BUY", remaining, 1, 2, "tm-close", D(2), "LMT")


def base(**overrides):
    """Default, fully-viable request. Override any keyword for a specific test."""
    calls = []
    p = dict(
        snapshot=make_snapshot(), con_id=20, quantity=2,
        candidates=[candidate()],
        quotes={20: quote(20, D("1.5"), D("2")), 30: quote(30)},
        constraints=RollConstraints(),
        # Deliverable evidence is keyed by each OPTION conId, NOT the underlying
        # conId (10). Old short = 20, candidate = 30: both must be explicitly listed.
        confirmed_deliverables=[20, 30],
        assess=lambda s, c, q: (calls.append((s, c, q)) or (True, "")),
        now=NOW,
    )
    p.update(overrides)
    return p, calls


def build(**overrides):
    params, _ = base(**overrides)
    return build_roll_recommendation(**params)


def reasons(doc, index=0):
    return doc["candidates"][index]["reasons"]


def joined_reasons(doc, index=0):
    return " ".join(reasons(doc, index))


class QuantityTests(unittest.TestCase):
    def test_positive_integer_quantity_required(self):
        for bad in (True, False, 0, -1, 2.0, D("2"), "2", None, float("nan"),
                    float("inf"), D("2.5"), 1 + 0j):
            with self.subTest(bad=bad), self.assertRaises(RollError):
                build(quantity=bad)

    def test_valid_quantity_serialized(self):
        doc = build(quantity=3)
        self.assertEqual(doc["close_leg"]["quantity"], "3")
        self.assertEqual(doc["new_open_quantity"], "3")


class ShortResolutionTests(unittest.TestCase):
    def test_missing_short_raises(self):
        with self.assertRaisesRegex(RollError, "no existing option short"):
            build(con_id=999)

    def test_net_long_is_not_a_short(self):
        with self.assertRaisesRegex(RollError, "not a net short"):
            build(snapshot=make_snapshot(short=D(5)))

    def test_fractional_short_fails_closed(self):
        with self.assertRaisesRegex(RollError, "not an integer"):
            build(snapshot=make_snapshot(short=D("-2.5")))

    def test_old_short_identity_must_be_complete(self):
        bad = old_inst(right="", expiry="2026121", multiplier=D(0))
        with self.assertRaisesRegex(RollError, "incomplete identity"):
            build(snapshot=make_snapshot(instrument=bad))

    def test_pending_buy_consumes_closable_capacity(self):
        snap = make_snapshot(orders=(pending_buy(D(4)),))
        with self.assertRaisesRegex(RollError, "insufficient closable quantity"):
            build(snapshot=snap, quantity=2)
        # Exactly the remaining 1 contract is still closable.
        doc = build(snapshot=snap, quantity=1)
        self.assertEqual(doc["recommended_count"], 1)

    def test_pending_buy_entirely_consumes_short(self):
        snap = make_snapshot(orders=(pending_buy(D(5)),))
        with self.assertRaisesRegex(RollError, "insufficient closable quantity"):
            build(snapshot=snap)

    def test_partial_close_leaves_remaining_short_untouched(self):
        # Close 2 of a 5-lot short; a partial close is a normal recommendation.
        doc = build(quantity=2)
        self.assertEqual(doc["close_leg"]["quantity"], "2")
        self.assertEqual(doc["recommended_count"], 1)
        self.assertFalse(doc["risk_assessment"]["old_obligations_released"])

    def test_only_same_conid_pending_buy_counts(self):
        other = WorkingOrder(candidate(), "BUY", D(5), 3, 4, "other", D(1), "LMT")
        doc = build(snapshot=make_snapshot(orders=(other,)), quantity=5)
        self.assertEqual(doc["recommended_count"], 1)

    def test_pending_sell_does_not_release_short_capacity(self):
        sell = WorkingOrder(old_inst(), "SELL", D(3), 3, 4, "sell", D(1), "LMT")
        doc = build(snapshot=make_snapshot(orders=(sell,)), quantity=5)
        self.assertEqual(doc["recommended_count"], 1)


class MultiplierFidelityTests(unittest.TestCase):
    def test_equal_contract_counts_with_different_multipliers_are_not_equal(self):
        doc = build(candidates=[candidate(multiplier=D(200))])
        est = doc["candidates"][0]["net_estimate"]
        # close: 2 * 100 * 2 = 400 ; open: 3 * 200 * 2 = 1200 ; net = 800
        self.assertEqual(est["close_amount"], "400")
        self.assertEqual(est["open_amount"], "1200")
        self.assertEqual(est["net"], "800")
        self.assertFalse(est["delivery_equivalent"])
        self.assertTrue(any("NOT an equal roll" in n for n in est["notes"]))

    def test_100_200_500_multipliers_are_serialized_per_leg(self):
        for old_mult, new_mult in ((100, 100), (100, 200), (200, 500), (500, 100)):
            with self.subTest(old_mult=old_mult, new_mult=new_mult):
                doc = build(
                    snapshot=make_snapshot(instrument=old_inst(D(old_mult))),
                    candidates=[candidate(multiplier=D(new_mult))])
                self.assertEqual(doc["close_leg"]["instrument"]["multiplier"], str(old_mult))
                self.assertEqual(doc["candidates"][0]["instrument"]["multiplier"], str(new_mult))
                est = doc["candidates"][0]["net_estimate"]
                self.assertEqual(est["close_amount"], str(D(old_mult) * 2 * 2))
                self.assertEqual(est["open_amount"], str(D(new_mult) * 3 * 2))

    def test_full_identity_present_on_both_legs(self):
        doc = build()
        fields = {"con_id", "underlying_id", "symbol", "currency", "sec_type",
                  "exchange", "multiplier", "right", "strike", "expiry", "trading_class"}
        self.assertEqual(set(doc["close_leg"]["instrument"]), fields)
        self.assertEqual(set(doc["candidates"][0]["instrument"]), fields)
        self.assertEqual(doc["close_leg"]["instrument"]["expiry"], OLD_EXPIRY)
        self.assertEqual(doc["candidates"][0]["instrument"]["expiry"], NEW_EXPIRY)


class CandidateIdentityTests(unittest.TestCase):
    def test_currency_mismatch_rejected(self):
        doc = build(candidates=[candidate(currency="EUR")])
        self.assertIn("currency does not match", joined_reasons(doc))
        self.assertEqual(doc["recommended_count"], 0)

    def test_right_mismatch_rejected(self):
        doc = build(candidates=[candidate(right="C")])
        self.assertIn("option right does not match", joined_reasons(doc))

    def test_underlying_mismatch_rejected(self):
        doc = build(candidates=[candidate(underlying_id=999)])
        self.assertIn("underlying conId does not match", joined_reasons(doc))

    def test_symbol_mismatch_rejected(self):
        doc = build(candidates=[candidate(symbol="MSFT")])
        self.assertIn("symbol does not match", joined_reasons(doc))

    def test_same_contract_as_short_rejected(self):
        doc = build(candidates=[candidate(con_id=20, expiry=NEW_EXPIRY)])
        self.assertIn("same contract as the existing short", joined_reasons(doc))

    def test_malformed_identity_rejected(self):
        for bad in (candidate(right=""), candidate(expiry="2026121"),
                    candidate(multiplier=D(-1)), candidate(sec_type="STK"),
                    candidate(strike=D(0)), candidate(con_id=0)):
            with self.subTest(bad=bad):
                self.assertEqual(build(candidates=[bad])["recommended_count"], 0)

    def test_non_instrument_candidate_is_rejected(self):
        doc = build(candidates=["not-an-instrument"])
        self.assertEqual(doc["candidates"][0]["status"], "rejected")
        self.assertIsNone(doc["candidates"][0]["instrument"])

    def test_duplicate_conid_rejected(self):
        c = candidate()
        doc = build(candidates=[c, c])
        self.assertIn("duplicate candidate conId", joined_reasons(doc, 1))


class DeliverablePolicyTests(unittest.TestCase):
    """Evidence is PER OPTION conId (old + every candidate), never per underlying."""

    def test_old_short_must_be_explicitly_confirmed_top_level_gate(self):
        # Confirming only the new candidate (30) does NOT authorize the close leg.
        with self.assertRaisesRegex(RollError, "existing short option conId=20 is not in"):
            build(confirmed_deliverables=[30])

    def test_confirming_underlying_conid_alone_does_not_authorize_old_short(self):
        # The underlying conId (10) is NOT an option conId; it authorizes nothing.
        with self.assertRaisesRegex(RollError, "existing short option conId=20"):
            build(confirmed_deliverables=[10])

    def test_underlying_conid_confirmation_does_not_authorize_candidate(self):
        # Old short confirmed (20) but candidate (30) is NOT: even though both share
        # underlying conId 10, the candidate must be rejected per contract.
        doc = build(confirmed_deliverables=[20, 10])
        self.assertIn("not confirmed for this option conId", joined_reasons(doc))
        self.assertEqual(doc["recommended_count"], 0)

    def test_unconfirmed_deliverable_rejected(self):
        with self.assertRaises(RollError):
            build(confirmed_deliverables=[])
        doc = build(confirmed_deliverables=[20])  # old only, candidate unconfirmed
        self.assertIn("not confirmed for this option conId", joined_reasons(doc))
        self.assertEqual(doc["recommended_count"], 0)

    def test_wrong_confirmed_deliverable_conid_rejected(self):
        doc = build(confirmed_deliverables=[20, 999])
        self.assertIn("not confirmed for this option conId", joined_reasons(doc))

    def test_matching_explicit_option_conids_are_accepted(self):
        doc = build(confirmed_deliverables=[20, 30])
        self.assertEqual(doc["recommended_count"], 1)
        self.assertEqual(doc["confirmed_deliverables"], [20, 30])
        self.assertEqual(doc["confirmed_deliverables_scope"], "option_conIds_per_contract")

    def test_same_underlying_standard_confirmed_vs_adjusted_unconfirmed_regression(self):
        # CORE REGRESSION: a standard option and an adjusted option can share the SAME
        # underlying conId (10), symbol, currency, right and even multiplier, while the
        # adjusted one settles into cash/basket. Confirming the standard candidate (30)
        # must NOT authorize the adjusted candidate (31) just because they share an
        # underlying. Evidence must be per OPTION conId.
        adjusted = candidate(con_id=31, trading_class="AAPL1", multiplier=D(100))
        standard = candidate(con_id=30)
        doc = build(
            candidates=[standard, adjusted],
            quotes={20: quote(20, D("1.5"), D("2")),
                    30: quote(30), 31: quote(31)},
            confirmed_deliverables=[20, 30])  # confirm old + STANDARD only
        by_id = {c["con_id"]: c for c in doc["candidates"]}
        self.assertEqual(by_id[30]["status"], "recommended")
        self.assertEqual(by_id[31]["status"], "rejected")
        self.assertIn("not confirmed for this option conId", " ".join(by_id[31]["reasons"]))
        self.assertEqual(adjusted.underlying_id, standard.underlying_id)  # same underlying
        self.assertEqual(doc["recommended_count"], 1)
        self.assertEqual(doc["rejected_count"], 1)

    def test_adjusted_candidate_accepted_only_with_its_own_conid_confirmed(self):
        adjusted = candidate(con_id=31, trading_class="AAPL1")
        doc = build(
            candidates=[adjusted],
            quotes={20: quote(20, D("1.5"), D("2")), 31: quote(31)},
            confirmed_deliverables=[20, 31])
        self.assertEqual(doc["recommended_count"], 1)

    def test_never_infers_delivery_from_tradingclass_or_multiplier(self):
        # Plausible tradingClass + standard 100 multiplier + same underlying are NOT
        # evidence of stock-only delivery (adjusted contracts can carry cash/baskets).
        doc = build(confirmed_deliverables=[20],
                    candidates=[candidate(trading_class="AAPL", multiplier=D(100))])
        self.assertIn("not confirmed for this option conId", joined_reasons(doc))
        self.assertIn("never inferred", joined_reasons(doc))
        self.assertEqual(doc["recommended_count"], 0)

    def test_every_candidate_needs_independent_evidence(self):
        cands = [candidate(con_id=30), candidate(con_id=31), candidate(con_id=32)]
        quotes_map = {20: quote(20, D("1.5"), D("2")),
                      30: quote(30), 31: quote(31), 32: quote(32)}
        doc = build(candidates=cands, quotes=quotes_map,
                    confirmed_deliverables=[20, 30, 31])  # 32 not confirmed
        statuses = {c["con_id"]: c["status"] for c in doc["candidates"]}
        self.assertEqual(statuses, {30: "recommended", 31: "recommended", 32: "rejected"})
        self.assertEqual(doc["recommended_count"], 2)

    def test_scope_and_policy_are_documented_in_output(self):
        doc = build()
        self.assertEqual(doc["confirmed_deliverables_scope"], "option_conIds_per_contract")
        self.assertIn("per OPTION conId", doc["deliverable_policy"])
        rendered = render_roll_recommendation(doc, "markdown")
        self.assertIn("逐合约，非 underlying conId", rendered)
        self.assertIn("20, 30", rendered)

    def test_invalid_deliverable_conids_fail_closed(self):
        for bad in ([True], ["10"], [0], [-1], "10", 10):
            with self.subTest(bad=bad), self.assertRaises(RollError):
                build(confirmed_deliverables=bad)


class QuoteTests(unittest.TestCase):
    def test_absent_close_quote_leaves_net_null_no_fake_return(self):
        doc = build(quotes={30: quote(30)})
        est = doc["candidates"][0]["net_estimate"]
        self.assertIsNone(est["net"])
        self.assertIsNone(est["close_amount"])
        self.assertEqual(est["open_amount"], "600")  # open price still known
        self.assertIn("net unknown", " ".join(doc["issues"]))

    def test_absent_candidate_quote_rejects_candidate(self):
        doc = build(quotes={20: quote(20, D("1.5"), D("2"))})
        self.assertIn("missing timestamped quote for candidate", joined_reasons(doc))
        self.assertEqual(doc["recommended_count"], 0)

    def test_missing_close_ask_leaves_net_null(self):
        doc = build(quotes={20: quote(20, D("1.5"), None), 30: quote(30)})
        self.assertIsNone(doc["candidates"][0]["net_estimate"]["net"])
        self.assertIsNone(doc["candidates"][0]["net_estimate"]["close_ask"])

    def test_missing_open_bid_leaves_net_null(self):
        doc = build(quotes={20: quote(20, D("1.5"), D("2")), 30: quote(30, None, D("3.5"))})
        self.assertIsNone(doc["candidates"][0]["net_estimate"]["net"])

    def test_expired_close_quote_is_unusable(self):
        doc = build(quotes={20: quote(20, D("1.5"), D("2"), age=timedelta(minutes=10)),
                            30: quote(30)})
        self.assertIsNone(doc["candidates"][0]["net_estimate"]["net"])
        self.assertIn("stale/expired", " ".join(doc["issues"]))

    def test_future_close_quote_is_unusable(self):
        doc = build(quotes={20: quote(20, D("1.5"), D("2"), age=-timedelta(minutes=1)),
                            30: quote(30)})
        self.assertIsNone(doc["candidates"][0]["net_estimate"]["net"])
        self.assertIn("in the future", " ".join(doc["issues"]))

    def test_expired_candidate_quote_rejects_candidate(self):
        doc = build(quotes={20: quote(20, D("1.5"), D("2")),
                            30: quote(30, age=timedelta(minutes=10))})
        self.assertIn("quote unusable", joined_reasons(doc))
        self.assertEqual(doc["recommended_count"], 0)

    def test_future_candidate_quote_rejects_candidate(self):
        doc = build(quotes={20: quote(20, D("1.5"), D("2")),
                            30: quote(30, age=-timedelta(minutes=1))})
        self.assertIn("in the future", joined_reasons(doc))

    def test_naive_quote_timestamp_rejects_candidate(self):
        doc = build(quotes={20: quote(20, D("1.5"), D("2")),
                            30: quote(30, naive=True)})
        self.assertIn("no timezone", joined_reasons(doc))

    def test_naive_close_quote_leaves_net_null(self):
        doc = build(quotes={20: quote(20, D("1.5"), D("2"), naive=True), 30: quote(30)})
        self.assertIsNone(doc["candidates"][0]["net_estimate"]["net"])

    def test_conid_mismatched_quote_rejects_candidate(self):
        doc = build(quotes={20: quote(20, D("1.5"), D("2")),
                            30: quote(999)})
        self.assertIn("quote conId mismatch", joined_reasons(doc))

    def test_quote_source_and_timestamp_are_recorded(self):
        doc = build(quotes={20: quote(20, D("1.5"), D("2"), source="OPRA"),
                            30: quote(30, source="OPRA")})
        self.assertEqual(doc["candidates"][0]["quote"]["source"], "OPRA")
        self.assertEqual(doc["close_quote"]["as_of"], (NOW).isoformat())

    def test_small_clock_skew_is_tolerated(self):
        doc = build(quotes={20: quote(20, D("1.5"), D("2"), age=-timedelta(seconds=2)),
                            30: quote(30)})
        self.assertEqual(doc["candidates"][0]["net_estimate"]["net"], "200")

    def test_quote_validation_rejects_bool_and_bad_fields(self):
        for bad in (dict(con_id=True, bid=D(1), ask=D(2), as_of=NOW),
                    dict(con_id=0, bid=D(1), ask=D(2), as_of=NOW),
                    dict(con_id=30, bid=True, ask=D(2), as_of=NOW),
                    dict(con_id=30, bid=D(1), ask=D("NaN"), as_of=NOW),
                    dict(con_id=30, bid=D(1), ask=D(2), as_of="2026-11-07")):
            with self.subTest(bad=bad), self.assertRaises(RollError):
                Quote(**bad)


class ExpiryTests(unittest.TestCase):
    def test_expiry_not_later_than_old_rejected(self):
        doc = build(candidates=[candidate(expiry=OLD_EXPIRY)])
        self.assertIn("not later than", joined_reasons(doc))

    def test_earlier_expiry_rejected(self):
        doc = build(candidates=[candidate(expiry="20261120")])
        self.assertIn("not later than", joined_reasons(doc))

    def test_already_expired_candidate_rejected(self):
        doc = build(candidates=[candidate(expiry="20261001")])
        self.assertIn("already expired", joined_reasons(doc))

    def test_dte_window_is_enforced(self):
        self.assertIn("below min_dte", joined_reasons(
            build(constraints=RollConstraints(min_dte=70))))
        self.assertIn("above max_dte", joined_reasons(
            build(constraints=RollConstraints(max_dte=60))))
        doc = build(constraints=RollConstraints(min_dte=30, max_dte=80))
        self.assertEqual(doc["candidates"][0]["days_to_expiry"], 69)


class ConstraintTests(unittest.TestCase):
    def test_max_entry_rejects_high_strike(self):
        doc = build(constraints=RollConstraints(max_entry=D("35")))
        self.assertIn("strike exceeds max_entry", joined_reasons(doc))

    def test_max_entry_passes_when_strike_within_limit(self):
        doc = build(constraints=RollConstraints(max_entry=D("45")))
        self.assertEqual(doc["recommended_count"], 1)

    def test_cost_floor_rejects_low_net(self):
        doc = build(constraints=RollConstraints(cost_floor=D("250")))
        self.assertIn("below cost_floor", joined_reasons(doc))

    def test_cost_floor_passes_when_net_above_floor(self):
        doc = build(constraints=RollConstraints(cost_floor=D("100")))
        self.assertEqual(doc["recommended_count"], 1)

    def test_cost_floor_cannot_be_verified_with_missing_price(self):
        doc = build(constraints=RollConstraints(cost_floor=D("0")),
                    quotes={30: quote(30)})
        self.assertIn("cannot verify cost_floor", joined_reasons(doc))

    def test_universe_whitelist_filters_symbols(self):
        self.assertIn("not in the approved universe", joined_reasons(
            build(constraints=RollConstraints(allowed_symbols=["MSFT"]))))
        self.assertEqual(build(constraints=RollConstraints(
            allowed_symbols=["aapl"]))["recommended_count"], 1)

    def test_multiple_reasons_reported_and_constraints_not_relaxed(self):
        doc = build(
            candidates=[candidate(currency="EUR", expiry=OLD_EXPIRY, strike=D(90))],
            quotes={20: quote(20, D("1.5"), D("2"))},
            constraints=RollConstraints(max_entry=D("35"), min_dte=70,
                                        allowed_symbols=["MSFT"]),
            confirmed_deliverables=[20])  # old short confirmed; candidate is not
        self.assertGreaterEqual(len(reasons(doc)), 4)
        self.assertEqual(doc["recommended_count"], 0)
        self.assertIn("NOT relaxed automatically", " ".join(doc["issues"]))

    def test_invalid_constraints_fail_closed(self):
        for bad in (dict(min_dte=True), dict(min_dte=1.5), dict(min_dte=-1),
                    dict(min_dte=80, max_dte=10),
                    dict(cost_floor=float("nan")), dict(cost_floor=True),
                    dict(max_entry=float("inf")), dict(max_entry=D(-1)),
                    dict(max_quote_age=timedelta(0)), dict(valid_for=timedelta(-1)),
                    dict(allowed_symbols="AAPL"), dict(allowed_symbols=[""])):
            with self.subTest(bad=bad), self.assertRaises(RollError):
                RollConstraints(**bad)


class RiskAssessmentTests(unittest.TestCase):
    def test_blocked_risk_rejects_candidate_with_reason(self):
        doc = build(assess=lambda s, c, q: (False, "daily submission limit reached"))
        self.assertEqual(doc["recommended_count"], 0)
        self.assertIn("daily submission limit reached", joined_reasons(doc))
        self.assertIn("risk assessment blocked", joined_reasons(doc))

    def test_assessment_uses_original_unreleased_snapshot(self):
        params, calls = base()
        snap = params["snapshot"]
        doc = build_roll_recommendation(**params)
        self.assertEqual(len(calls), 1)
        passed_snapshot, passed_cand, passed_qty = calls[0]
        self.assertIs(passed_snapshot, snap)              # original object, unmodified
        self.assertEqual(passed_snapshot.positions, snap.positions)  # old short intact
        self.assertIs(passed_cand, params["candidates"][0])
        self.assertEqual(passed_qty, 2)
        self.assertEqual(doc["risk_assessment"]["basis"], "original_snapshot")
        self.assertFalse(doc["risk_assessment"]["old_obligations_released"])

    def test_assessment_not_called_for_already_rejected_candidates(self):
        calls = []
        build(candidates=[candidate(currency="EUR")],
              assess=lambda s, c, q: (calls.append(1) or (True, "")))
        self.assertEqual(calls, [])

    def test_raising_assessment_fails_closed(self):
        def boom(s, c, q):
            raise RuntimeError("risk engine unavailable")
        doc = build(assess=boom)
        self.assertEqual(doc["recommended_count"], 0)
        self.assertIn("conservatively blocked", joined_reasons(doc))

    def test_malformed_assessment_result_fails_closed(self):
        for result in (None, True, (1, "x"), ("yes", ""), (True,), ()):
            with self.subTest(result=result):
                doc = build(assess=lambda s, c, q, r=result: r)
                self.assertEqual(doc["recommended_count"], 0)

    def test_non_callable_assess_rejected(self):
        with self.assertRaises(RollError):
            build(assess="not-callable")


class SchemaAndValidationTests(unittest.TestCase):
    def test_separate_versioned_schema_and_flags(self):
        doc = build()
        self.assertEqual(SCHEMA, "thetamill.roll.v1")
        self.assertEqual(doc["schema"], SCHEMA)
        self.assertEqual(doc["kind"], "roll_recommendation")
        self.assertTrue(doc["recommendation_only"])
        self.assertFalse(doc["executable"])
        self.assertNotIn("items", doc)              # not the ordinary plan schema

    def test_valid_document_passes_validation(self):
        doc = build()
        ok, problems = validate_recommendation(doc, now=NOW)
        self.assertTrue(ok, problems)
        self.assertEqual(problems, [])

    def test_utc_created_and_expires(self):
        doc = build()
        created = datetime.fromisoformat(doc["created_at"])
        expires = datetime.fromisoformat(doc["expires_at"])
        self.assertEqual(created.utcoffset(), timedelta(0))
        self.assertEqual(expires.utcoffset(), timedelta(0))
        self.assertGreater(expires, created)
        self.assertEqual(expires - created, timedelta(minutes=15))

    def test_expired_recommendation_is_detected(self):
        doc = build()
        ok, problems = validate_recommendation(
            doc, now=NOW + timedelta(minutes=16))
        self.assertFalse(ok)
        self.assertTrue(any("expired" in p for p in problems))

    def test_wrong_schema_is_rejected_so_run_cannot_consume_it(self):
        for doc in ({"schema": "thetamill-plan/1", "items": [{"i": 1}]},
                    {"schema": "thetamill.report.v1"},
                    {}, "not-a-mapping", None):
            with self.subTest(doc=doc):
                ok, problems = validate_recommendation(doc, now=NOW)
                self.assertFalse(ok)
                self.assertTrue(any("schema mismatch" in p or "not a mapping" in p
                                   for p in problems))

    def test_tampered_executable_fields_are_rejected(self):
        for change in ({"executable": True}, {"recommendation_only": False},
                       {"kind": "plan"}, {"items": [{"action": "SELL"}]},
                       {"submit": True}, {"orders_to_place": [1]},
                       {"placeOrder": "x"}):
            with self.subTest(change=change):
                doc = dict(build())
                doc.update(change)
                ok, problems = validate_recommendation(doc, now=NOW)
                self.assertFalse(ok)

    def test_bad_timestamps_are_rejected(self):
        for field, value in (("created_at", "2026-11-07T20:00:00"),
                             ("expires_at", None), ("expires_at", "garbage"),
                             ("created_at", "2026-11-07T21:00:00+00:00")):
            with self.subTest(field=field, value=value):
                doc = dict(build())
                doc[field] = value
                self.assertFalse(validate_recommendation(doc, now=NOW)[0])

    def test_render_json_is_clean_and_has_no_side_effects(self):
        doc = build()
        text = render_roll_recommendation(doc, "json")
        data = json.loads(text)
        self.assertEqual(data["schema"], SCHEMA)
        self.assertNotIn("NaN", text)
        self.assertIn("不可执行", text)

    def test_render_terminal_and_bad_format(self):
        doc = build()
        text = render_roll_recommendation(doc, "markdown")
        self.assertIn("展期建议", text)
        self.assertIn("conId", text)
        with self.assertRaises(RollError):
            render_roll_recommendation(doc, "html")

    def test_no_execution_entry_points_exist(self):
        # Strong structural guarantee: no placeOrder/cancel/execute/run/auto wiring.
        for name in ("placeOrder", "place_order", "cancelOrder", "cancel",
                     "execute", "execute_roll", "run", "auto_run", "submit",
                     "connect", "collect_snapshot"):
            self.assertFalse(hasattr(roll, name), f"unexpected execution entry point: {name}")

    def test_no_broker_or_network_imports(self):
        # The engine must be pure: no broker library, socket, subprocess, git or IO.
        import ast
        import pathlib
        source = pathlib.Path(roll.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for forbidden in ("ib_async", "socket", "subprocess", "requests", "http",
                          "sqlite3", "os", "pathlib", "shutil", "git"):
            self.assertNotIn(forbidden, imported, f"forbidden import: {forbidden}")

    def test_fees_are_explicitly_unknown_and_currency_recorded(self):
        doc = build()
        est = doc["candidates"][0]["net_estimate"]
        self.assertEqual(est["currency"], "USD")
        self.assertIsNone(est["fees"])
        self.assertTrue(est["fees_unknown"])
        self.assertIsNone(doc["fees"])

    def test_invalid_top_level_inputs_fail_closed(self):
        with self.assertRaises(RollError):
            build(snapshot="not-a-snapshot")
        with self.assertRaises(RollError):
            build(con_id=True)
        with self.assertRaises(RollError):
            build(candidates="AAPL")
        with self.assertRaises(RollError):
            build(quotes=[quote(20)])
        with self.assertRaises(RollError):
            build(quotes={20: "not-a-quote"})
        with self.assertRaises(RollError):
            build(quotes={True: quote(20)})
        with self.assertRaises(RollError):
            build(constraints="strict")
        with self.assertRaises(RollError):
            build(now=datetime(2026, 11, 7))  # naive timestamp


if __name__ == "__main__":
    unittest.main()
