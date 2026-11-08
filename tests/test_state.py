#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline tests for thetamill.state — the SQLite submission ledger.

All state lives in temporary directories; no broker, network or account
operations are performed.  Concurrency tests use real ``multiprocessing``
processes (``spawn``) racing on the same database file.
"""
import json
import multiprocessing as mp
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from thetamill import state
from thetamill.state import (
    BudgetError, ClaimError, LockUnavailableError, RiskReservationSpec,
    SchemaError, StateError, SubmissionLedger, account_scope, day_key_for,
)

TZ = "America/New_York"
ACCOUNT = "DU1234567"
ENV = "paper"
PAYLOAD = {"action": "SELL", "conId": 265598, "qty": 2, "otype": "LMT",
           "px": "3.50", "tif": "DAY", "preset": "covered-call"}
CONFIG = {"risk.enabled": True, "max_daily_submissions": 5, "day_timezone": TZ,
          "config_version": "p3-1"}


def _aware(*, day=2026, month=11, hour=14, minute=30):
    return datetime(day, month, 7 if month == 11 else 1, hour, minute,
                    tzinfo=timezone.utc)


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = self.root / "ledger.sqlite3"
        self.ledger = SubmissionLedger(self.db, TZ)

    def _intent(self, **overrides):
        kwargs = dict(account=ACCOUNT, environment=ENV, payload=PAYLOAD,
                      config=CONFIG, ttl_seconds=600, now=_aware())
        kwargs.update(overrides)
        return self.ledger.create_intent(**kwargs)

    def _claim(self, intent, **overrides):
        kwargs = dict(account=ACCOUNT, environment=ENV,
                      intent_id=intent["intent_id"],
                      full_digest=intent["full_digest"],
                      code=intent["approval_code"], now=_aware())
        kwargs.update(overrides)
        return self.ledger.claim_intent(**kwargs)

    def _prepare_day(self, *, limit=5, day_key="2026-11-07"):
        self.ledger.set_daily_limit(account=ACCOUNT, environment=ENV,
                                    day_key=day_key, limit_count=limit)
        self.ledger.set_resource(account=ACCOUNT, environment=ENV,
                                 currency="USD", resource_key="cash_base",
                                 amount="100000.00")
        return day_key

    def _reserve(self, intent, *, items=None, slots=1, day_key="2026-11-07",
                 now=None, kind="new_risk"):
        if items is None:
            items = [RiskReservationSpec("USD", "cash_base", "1000")]
        return self.ledger.reserve(
            account=ACCOUNT, environment=ENV, intent_id=intent["intent_id"],
            day_key=day_key, items=items, day_submission_slots=slots,
            now=now or _aware(), kind=kind)

    def _legs(self, intent, reservation_id, count=1, **overrides):
        legs = [dict(contract_id=str(1000 + i), action="SELL", quantity="2")
                for i in range(count)]
        kwargs = dict(intent_id=intent["intent_id"],
                      reservation_id=reservation_id, legs=legs, now=_aware())
        kwargs.update(overrides)
        return self.ledger.record_legs(**kwargs)


# -- versioned schema & fail-closed state ---------------------------------

class TestSchema(LedgerTestCase):
    def test_fresh_db_is_versioned_and_reopenable(self):
        self.assertTrue(self.db.exists())
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], str(state.SCHEMA_VERSION))
        reopened = SubmissionLedger(self.db, TZ)
        missing = reopened.get_day_budget(
            account=ACCOUNT, environment=ENV, day_key="2026-11-07")
        # A missing row reports exists=False + unknown counts, NEVER a verified
        # zero historic count (read-only report must not assert "0 submitted").
        self.assertFalse(missing["exists"])
        for key in ("limit_count", "reserved_count", "submitted_count",
                    "remaining", "migration_frozen"):
            self.assertIsNone(missing[key])

    def test_corrupt_db_raises_schema_error_not_empty(self):
        self.db.write_bytes(b"not a sqlite database" * 10)
        with self.assertRaises(SchemaError):
            SubmissionLedger(self.db, TZ)
        with self.assertRaises(SchemaError):
            self.ledger.get_resource(account=ACCOUNT, environment=ENV,
                                     currency="USD", resource_key="cash_base")

    def test_foreign_sqlite_db_refused(self):
        # A real SQLite file that is not our ledger (no required tables).
        foreign = self.root / "foreign.sqlite3"
        conn = sqlite3.connect(foreign)
        conn.execute("CREATE TABLE whatever(x INT)")
        conn.commit()
        conn.close()
        with self.assertRaises(SchemaError):
            SubmissionLedger(foreign, TZ)

    def test_unsupported_schema_version_refused(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        with self.assertRaises(SchemaError):
            SubmissionLedger(self.db, TZ)
        with self.assertRaises(SchemaError):
            self.ledger.create_intent(account=ACCOUNT, environment=ENV,
                                      payload=PAYLOAD, config=CONFIG,
                                      ttl_seconds=600, now=_aware())

    def test_unwritable_db_fail_closed_on_write_and_read(self):
        self._prepare_day()  # populate a row so a naive empty read would pass
        os.chmod(self.db, 0o444)
        self.addCleanup(lambda: self.db.exists() and os.chmod(self.db, 0o644))
        with self.assertRaises(SchemaError):
            self.ledger.set_resource(account=ACCOUNT, environment=ENV,
                                     currency="USD", resource_key="cash_base",
                                     amount="1")
        with self.assertRaises(SchemaError):
            self.ledger.get_day_budget(account=ACCOUNT, environment=ENV,
                                       day_key="2026-11-07")
        with self.assertRaises(SchemaError):
            self.ledger.outstanding_reservations(account=ACCOUNT, environment=ENV)
        with self.assertRaises(SchemaError):
            SubmissionLedger(self.db, TZ)

    def test_read_only_mode_never_initializes_or_migrates(self):
        # A touched empty file must NOT be turned into a legitimate ledger by a
        # read-only report open; it stays empty and is rejected as foreign.
        empty = self.root / "touched-empty.sqlite3"
        empty.write_bytes(b"")
        before = empty.stat().st_size
        with self.assertRaises(SchemaError):
            state.open_readonly(empty, TZ)
        self.assertEqual(empty.stat().st_size, before)  # never wrote schema

    def test_read_only_mode_rejects_corrupt_and_foreign_db(self):
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"garbage" * 8)
        with self.assertRaises(SchemaError):
            state.open_readonly(corrupt, TZ)
        foreign = self.root / "foreign-ro.sqlite3"
        conn = sqlite3.connect(foreign)
        conn.execute("CREATE TABLE whatever(x INT)")
        conn.commit()
        conn.close()
        with self.assertRaises(SchemaError):
            state.open_readonly(foreign, TZ)

    def test_read_only_mode_queries_existing_ledger_but_refuses_writes(self):
        self._prepare_day()
        self.ledger.set_resource(account=ACCOUNT, environment=ENV,
                                 currency="USD", resource_key="cash_base",
                                 amount="1000")
        ro = state.open_readonly(self.db, TZ)
        budget = ro.get_day_budget(account=ACCOUNT, environment=ENV,
                                   day_key="2026-11-07")
        self.assertTrue(budget["exists"])
        self.assertEqual(ro.get_resource(account=ACCOUNT, environment=ENV,
                                         currency="USD",
                                         resource_key="cash_base"), "1000")
        # State-changing operations are refused on a read-only ledger.
        with self.assertRaises(SchemaError):
            ro.set_resource(account=ACCOUNT, environment=ENV, currency="USD",
                            resource_key="cash_base", amount="5")
        # No journal / WAL side files were created by the read-only open.
        self.assertFalse((self.root / (self.db.name + "-wal")).exists())

    def test_read_only_mode_unsupported_version_refused(self):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
        conn.commit()
        conn.close()
        with self.assertRaises(SchemaError):
            state.open_readonly(self.db, TZ)

    def test_unknown_timezone_fails_closed(self):
        with self.assertRaises(StateError):
            SubmissionLedger(self.root / "bad.db", "Mars/Olympus")
        with self.assertRaises(StateError):
            day_key_for("Not/AZone", now=_aware())


# -- day keys / UTC timestamps --------------------------------------------

class TestDayKey(unittest.TestCase):
    def test_utc_rollover_uses_iana_day(self):
        # 03:30 UTC is still the previous local day in New York.
        self.assertEqual(day_key_for(TZ, datetime(2026, 11, 7, 3, 30,
                                                  tzinfo=timezone.utc)), "2026-11-06")
        # November is EST (UTC-5): 05:01 UTC is just past local midnight.
        self.assertEqual(day_key_for(TZ, datetime(2026, 11, 7, 5, 1,
                                                  tzinfo=timezone.utc)), "2026-11-07")

    def test_dst_transition(self):
        # 2026-03-08 spring forward in America/New_York.
        a = day_key_for(TZ, datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc))
        b = day_key_for(TZ, datetime(2026, 3, 8, 7, 1, tzinfo=timezone.utc))
        self.assertEqual(a, "2026-03-08")
        self.assertEqual(b, "2026-03-08")
        self.assertEqual(ZoneInfo(TZ).key, TZ)

    def test_naive_datetime_rejected(self):
        with self.assertRaises(StateError):
            day_key_for(TZ, datetime(2026, 11, 7, 12, 0))

    def test_timestamps_are_utc_iso(self):
        with tempfile.TemporaryDirectory() as d:
            led = SubmissionLedger(Path(d) / "s.db", "UTC")
            i = led.create_intent(account=ACCOUNT, environment=ENV,
                                  payload=PAYLOAD, config=CONFIG,
                                  ttl_seconds=600, now=_aware())
            self.assertTrue(i["expires_at"].endswith("Z"))
            expires = datetime.fromisoformat(i["expires_at"].replace("Z", "+00:00"))
            # expiry is exactly ttl_seconds after the supplied UTC instant
            self.assertEqual(expires, _aware() + timedelta(seconds=600))
            self.assertEqual(expires.tzinfo, timezone.utc)
            self.assertEqual(led.get_intent(i["intent_id"])["expires_at"],
                             i["expires_at"])


# -- intents, digests, approval code, scoping ------------------------------

class TestIntents(LedgerTestCase):
    def test_intent_has_uuid_digest_and_cfg_digest(self):
        a = self._intent()
        b = self._intent()  # same business terms -> fresh intent, not blocked
        self.assertEqual(len(a["full_digest"]), 64)
        self.assertNotEqual(a["intent_id"], b["intent_id"])
        self.assertNotEqual(a["full_digest"], b["full_digest"])
        self.assertEqual(a["config_digest"], state.config_digest(CONFIG))
        self.assertEqual(len(a["approval_code"]), 8)
        self.assertEqual(a["approval_code"],
                         state.approval_code(a["full_digest"], a["scope"]))

    def test_full_digest_recomputable_and_config_sensitive(self):
        a = self._intent()
        recomputed = state.compute_full_digest(
            intent_id=a["intent_id"], account=ACCOUNT, environment=ENV,
            payload=PAYLOAD, config=CONFIG)
        self.assertEqual(recomputed, a["full_digest"])
        changed = dict(CONFIG, max_daily_submissions=6)
        self.assertNotEqual(
            state.compute_full_digest(intent_id=a["intent_id"], account=ACCOUNT,
                                     environment=ENV, payload=PAYLOAD, config=changed),
            a["full_digest"])

    def test_claim_wrong_code_digest_scope_or_expiry_rejected(self):
        a = self._intent()
        with self.assertRaises(ClaimError):
            self._claim(a, code="DEADBEEF")
        with self.assertRaises(ClaimError):
            self._claim(a, full_digest="0" * 64)
        with self.assertRaises(ClaimError):
            self._claim(a, account="DU9999999")
        with self.assertRaises(ClaimError):
            self._claim(a, environment="live")
        with self.assertRaises(ClaimError):
            self._claim(a, now=_aware() + timedelta(seconds=601))
        # still pending after all failed attempts
        self.assertEqual(self.ledger.get_intent(a["intent_id"])["state"], "pending")
        self._claim(a)
        with self.assertRaises(ClaimError):  # replay
            self._claim(a)
        with self.assertRaises(ClaimError):  # changed config digest
            self._claim(a, full_digest=state.compute_full_digest(
                intent_id=a["intent_id"], account=ACCOUNT, environment=ENV,
                payload=PAYLOAD, config=dict(CONFIG, day_timezone="UTC")))

    def test_account_environment_isolation(self):
        paper = self._intent()
        live = self._intent(environment="live")
        other = self._intent(account="DU9999999")
        self._claim(paper)
        self.assertEqual(self.ledger.get_intent(paper["intent_id"])["state"], "claimed")
        self.assertEqual(self.ledger.get_intent(live["intent_id"])["state"], "pending")
        self.assertEqual(self.ledger.get_intent(other["intent_id"])["state"], "pending")
        with self.assertRaises(ClaimError):
            self.ledger.reserve(account="DU9999999", environment=ENV,
                                intent_id=paper["intent_id"], day_key="2026-11-07",
                                items=[], day_submission_slots=1)
        self.assertEqual(len(self.ledger.list_legs(account="DU9999999",
                                                   environment=ENV)), 0)

    def test_legacy_code_import_blocks_claim_and_never_clears(self):
        # Derive the code a future intent would get by pre-importing it.
        future = self._intent()
        self.assertEqual(
            self.ledger.import_legacy_codes(account=ACCOUNT, environment=ENV,
                                            codes=[future["approval_code"]]), 1)
        # second import is a no-op; nothing is deleted
        self.assertEqual(
            self.ledger.import_legacy_codes(account=ACCOUNT, environment=ENV,
                                            codes=[future["approval_code"]]), 0)
        self.assertIn(future["approval_code"],
                      self.ledger.legacy_codes(account=ACCOUNT, environment=ENV))
        with self.assertRaises(ClaimError):
            self._claim(future)
        # legacy dict / jsonl shapes and scope isolation
        self.assertEqual(
            self.ledger.import_legacy_records(
                account=ACCOUNT, environment=ENV,
                records=[{"LEGACY1": "ts"}, json.dumps({"code": "LEGACY2"}),
                         "LEGACY3", "LEGACY1"]), 3)
        self.assertEqual(len(self.ledger.legacy_codes(account=ACCOUNT,
                                                      environment=ENV)), 4)
        self.assertEqual(self.ledger.legacy_codes(account=ACCOUNT,
                                                  environment="live"), [])


# -- atomic reservation & day budget --------------------------------------

class TestReservation(LedgerTestCase):
    def test_reserve_uses_exact_decimal_strings(self):
        day = self._prepare_day(limit=5)
        a = self._intent()
        self._claim(a)
        rid = self._reserve(a, items=[RiskReservationSpec("USD", "cash_base",
                                                          "0.10")], slots=2)
        view = self.ledger.get_reservation(rid)
        # Decimal text keeps the caller's precision; no float conversion.
        self.assertEqual(view.items, (("USD", "cash_base", "0.10"),))
        self.assertEqual(view.day_submission_slots, 2)
        self.assertEqual(self.ledger.get_resource(account=ACCOUNT, environment=ENV,
                                                  currency="USD",
                                                  resource_key="cash_base"), "100000.00")
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV, day_key=day),
            {"exists": True, "limit_count": 5, "reserved_count": 2, "submitted_count": 0,
             "remaining": 3, "migration_frozen": False})

    def test_float_amount_rejected(self):
        self._prepare_day()
        with self.assertRaises(StateError):
            self.ledger.set_resource(account=ACCOUNT, environment=ENV,
                                     currency="USD", resource_key="cash_base",
                                     amount=1.5)
        with self.assertRaises(StateError):
            RiskReservationSpec("USD", "cash_base", 1.5)  # type: ignore[arg-type]
            self._reserve(self._intent(), items=[RiskReservationSpec(
                "USD", "cash_base", 1.5)])  # type: ignore[arg-type]

    def test_missing_or_insufficient_resource_fails_closed(self):
        self._prepare_day()
        a = self._intent()
        self._claim(a)
        with self.assertRaises(BudgetError):
            self._reserve(a, items=[RiskReservationSpec("USD", "shares:265598:USD",
                                                        "10")])
        with self.assertRaises(BudgetError):
            self._reserve(a, items=[RiskReservationSpec("USD", "cash_base",
                                                        "100000.01")])
        # failed reservations left the budget untouched
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV, day_key="2026-11-07")["reserved_count"], 0)

    def test_day_budget_exhausted_and_persistent_counts(self):
        day = self._prepare_day(limit=1)
        a = self._intent()
        b = self._intent()
        self._claim(a)
        self._claim(b)
        rid = self._reserve(a, slots=1)
        with self.assertRaises(BudgetError):
            self._reserve(b, slots=1)  # second intent cannot spend the same slot
        self._legs(a, rid)
        self.ledger.begin_submission(rid)
        self.ledger.record_submission(rid, state="rejected")
        # rejected attempts stay counted; no refund, no retry
        budget = self.ledger.get_day_budget(account=ACCOUNT, environment=ENV,
                                            day_key=day)
        self.assertEqual(budget["submitted_count"], 1)
        self.assertEqual(budget["remaining"], 0)
        with self.assertRaises(BudgetError):
            self._reserve(b, slots=1)
        with self.assertRaises(StateError):
            self.ledger.record_submission(rid, state="submitted")
        with self.assertRaises(BudgetError):
            self.ledger.set_daily_limit(account=ACCOUNT, environment=ENV,
                                        day_key=day, limit_count=0)

    def test_pre_send_abort_releases_risk_and_slot(self):
        day = self._prepare_day(limit=3)
        a = self._intent()
        self._claim(a)
        rid = self._reserve(a, slots=2)
        self.ledger.abort_reservation(rid)
        self.assertEqual(self.ledger.get_reservation(rid).state, "aborted")
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV, day_key=day),
            {"exists": True, "limit_count": 3, "reserved_count": 0, "submitted_count": 0,
             "remaining": 3, "migration_frozen": False})
        self.assertEqual(self.ledger.outstanding_reservations(
            account=ACCOUNT, environment=ENV), [])
        # intent can be reserved again after a clean abort
        rid2 = self._reserve(a, slots=2)
        self.assertNotEqual(rid, rid2)
        with self.assertRaises(StateError):  # abort only while reserved
            self.ledger.abort_reservation(rid)

    def test_decimal_boundary_no_float_slack(self):
        self.ledger.set_resource(account=ACCOUNT, environment=ENV, currency="USD",
                                 resource_key="cash_base", amount="0.30")
        self._prepare_day(limit=5)
        self.ledger.set_resource(account=ACCOUNT, environment=ENV, currency="USD",
                                 resource_key="cash_base", amount="0.30")
        a, b = self._intent(), self._intent()
        self._claim(a)
        self._claim(b)
        self._reserve(a, items=[RiskReservationSpec("USD", "cash_base", "0.10")])
        self._reserve(b, items=[RiskReservationSpec("USD", "cash_base", "0.20")])
        c = self._intent()
        self._claim(c)
        with self.assertRaises(BudgetError):
            self._reserve(c, items=[RiskReservationSpec("USD", "cash_base", "0.01")])


# -- legs / transitions / unknown / reconciliation -------------------------

class TestLifecycle(LedgerTestCase):
    def test_per_leg_attempts_count_actual_sends_before_network(self):
        """BLOCKER 2: attempts are counted per leg at the network boundary,
        not all upfront; only leg 0 sends, leg 1 is skipped and refunded."""
        day = self._prepare_day(limit=5)
        a = self._intent()
        self._claim(a)
        rid = self._reserve(a, slots=2)
        refs = self._legs(a, rid, count=2)
        self.assertEqual(len(refs), 2)
        # durable anchor on disk BEFORE any (future) network call
        conn = sqlite3.connect(self.db)
        try:
            on_disk = conn.execute(
                "SELECT order_ref,state FROM legs ORDER BY leg_index").fetchall()
        finally:
            conn.close()
        self.assertEqual([r[1] for r in on_disk], ["reserved", "reserved"])
        # leg 0: pre-send attempt persists BEFORE placeOrder
        attempt = self.ledger.begin_leg_attempt(refs[0])
        self.assertEqual(attempt["leg_index"], 0)
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV, day_key=day),
            {"exists": True, "limit_count": 5, "reserved_count": 1,
             "submitted_count": 1, "remaining": 3, "migration_frozen": False})
        self.assertEqual([l.state for l in self.ledger.list_legs(
            account=ACCOUNT, environment=ENV, intent_id=a["intent_id"])],
            ["submitting", "reserved"])
        self.ledger.bind_broker_ids(refs[0], order_id=42, perm_id=987654321)
        self.ledger.record_leg_result(refs[0], state="submitted")
        # leg 1 never sent: skip it and refund ONLY its unspent slot
        self.assertEqual(self.ledger.skip_unsent_legs(rid), 1)
        final = self.ledger.list_legs(account=ACCOUNT, environment=ENV,
                                      intent_id=a["intent_id"])
        self.assertEqual([l.state for l in final], ["submitted", "skipped"])
        self.assertEqual(final[0].order_id, "42")
        self.assertEqual(final[0].perm_id, "987654321")
        # only ONE actual attempt counted, one slot refunded
        budget = self.ledger.get_day_budget(account=ACCOUNT, environment=ENV,
                                            day_key=day)
        self.assertEqual(budget["submitted_count"], 1)
        self.assertEqual(budget["reserved_count"], 0)
        self.assertEqual(budget["remaining"], 4)
        # BLOCKER 1: submitted KEEPS risk outstanding (not released)
        self.assertEqual(self.ledger.get_reservation(rid).state, "submitted")
        self.assertEqual(len(self.ledger.outstanding_reservations(
            account=ACCOUNT, environment=ENV)), 1)
        self.assertTrue(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))
        # absent-open-order does NOT release it
        self.ledger.reconcile(account=ACCOUNT, environment=ENV, order_ref=refs[0],
                              status="absent_open_order")
        self.assertEqual(self.ledger.get_reservation(rid).state, "submitted")
        # only a POSITIVE broker status releases risk
        self.ledger.confirm_leg(refs[0], broker_status="Filled", order_id=42,
                                perm_id=987654321)
        self.assertEqual(self.ledger.get_reservation(rid).state, "rejected")
        self.assertEqual(self.ledger.outstanding_reservations(
            account=ACCOUNT, environment=ENV), [])
        self.assertFalse(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV, day_key=day)["submitted_count"], 1)

    def test_sequential_multi_leg_attempts_all_sendable(self):
        """A package may send leg 0 (submitted) and STILL attempt leg 1.

        The reservation stays outstanding (submitted) between legs; the per-leg
        state is the replay guard.  An 'unknown' leg is terminal and BLOCKS any
        remaining leg attempt (conservative — never continue a package after an
        unknown send).
        """
        day = self._prepare_day(limit=5)
        a = self._intent()
        self._claim(a)
        rid = self._reserve(a, slots=2)
        refs = self._legs(a, rid, count=2)
        self.ledger.begin_leg_attempt(refs[0])
        self.ledger.record_leg_result(refs[0], state="submitted")
        # reservation is now 'submitted' but leg 1 is still 'reserved'
        self.assertEqual(self.ledger.get_reservation(rid).state, "submitted")
        self.ledger.begin_leg_attempt(refs[1])  # must NOT raise
        self.ledger.record_leg_result(refs[1], state="submitted")
        budget = self.ledger.get_day_budget(account=ACCOUNT, environment=ENV,
                                            day_key=day)
        self.assertEqual(budget["submitted_count"], 2)
        self.assertEqual(budget["reserved_count"], 0)
        self.assertEqual(self.ledger.skip_unsent_legs(rid), 0)

    def test_unknown_leg_blocks_remaining_attempts(self):
        day = self._prepare_day(limit=5)
        a = self._intent()
        self._claim(a)
        rid = self._reserve(a, slots=2)
        refs = self._legs(a, rid, count=2)
        self.ledger.begin_leg_attempt(refs[0])
        self.ledger.record_leg_result(refs[0], state="unknown")
        self.assertEqual(self.ledger.get_reservation(rid).state, "unknown")
        with self.assertRaises(StateError):
            self.ledger.begin_leg_attempt(refs[1])
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key=day)["submitted_count"], 1)

    def test_confirm_leg_rejects_non_positive_broker_status(self):
        self._prepare_day()
        a = self._intent()
        self._claim(a)
        rid = self._reserve(a, slots=1)
        ref = self._legs(a, rid)[0]
        self.ledger.begin_leg_attempt(ref)
        self.ledger.record_leg_result(ref, state="submitted")
        for bad in ("absent_open_order", "Unknown", "PendingSubmit", "", None):
            with self.assertRaises(StateError):
                self.ledger.confirm_leg(ref, broker_status=bad)
        self.assertEqual(self.ledger.get_reservation(rid).state, "submitted")

    def test_leg_count_must_match_reserved_slots(self):
        """BLOCKER 3: no 0-slot/1-leg bypass, no mismatch."""
        self._prepare_day(limit=5)
        a = self._intent()
        self._claim(a)
        rid = self._reserve(a, slots=2)
        with self.assertRaises(StateError):
            self._legs(a, rid, count=1)
        with self.assertRaises(StateError):
            self._legs(a, rid, count=3)
        # also reject a 1-leg reservation with 0 slots carrying a leg
        b = self._intent()
        self._claim(b)
        rid0 = self._reserve(b, slots=0)
        with self.assertRaises(StateError):
            self._legs(b, rid0, count=1)
        # correct count succeeds
        self.assertEqual(len(self._legs(a, rid, count=2)), 2)

    def test_legs_immutable_anchor_no_reindex_or_reanchor(self):
        """BLOCKER 4: legs carry reservation_id, UNIQUE(reservation_id,
        leg_index); an aborted reservation's refs cannot anchor a later one."""
        self._prepare_day(limit=5)
        a = self._intent()
        self._claim(a)
        r1 = self._reserve(a, slots=1)
        ref = self._legs(a, r1)[0]
        legs = self.ledger.list_legs(account=ACCOUNT, environment=ENV,
                                     intent_id=a["intent_id"])
        self.assertEqual(legs[0].reservation_id, r1)
        self.assertEqual(legs[0].leg_index, 0)
        with self.assertRaises(StateError):  # cannot record twice / repeat index 0
            self._legs(a, r1, count=1)
        self.ledger.abort_reservation(r1)
        self.assertEqual(self.ledger.get_reservation(r1).state, "aborted")
        r2 = self._reserve(a, slots=1)
        self.assertNotEqual(r1, r2)
        with self.assertRaises(StateError):  # old ref cannot anchor new reservation
            self.ledger.record_legs(intent_id=a["intent_id"], reservation_id=r2,
                                    legs=[dict(contract_id="1", action="SELL",
                                               quantity="1", order_ref=ref)])
        # new reservation gets its own fresh immutable ref
        new_ref = self._legs(a, r2)[0]
        self.assertNotEqual(new_ref, ref)

    def test_bind_ids_immutable_and_reconcile_conflicts_rejected(self):
        """BLOCKER 5: bind_broker_ids never overwrites; reconcile rejects
        conflicting supplied perm/order IDs."""
        self._prepare_day()
        a = self._intent()
        self._claim(a)
        rid = self._reserve(a, slots=1)
        ref = self._legs(a, rid)[0]
        self.ledger.bind_broker_ids(ref, order_id=42, perm_id=987654321)
        self.ledger.bind_broker_ids(ref, order_id=42, perm_id=987654321)  # idempotent
        with self.assertRaises(StateError):
            self.ledger.bind_broker_ids(ref, order_id=999)
        with self.assertRaises(StateError):
            self.ledger.bind_broker_ids(ref, perm_id=111)
        with self.assertRaises(StateError):
            self.ledger.reconcile(account=ACCOUNT, environment=ENV,
                                  order_ref=ref, order_id=999, status="filled")
        with self.assertRaises(StateError):
            self.ledger.reconcile(account=ACCOUNT, environment=ENV,
                                  order_ref=ref, perm_id=111, status="filled")
        # same IDs are fine and idempotent
        one = self.ledger.reconcile(account=ACCOUNT, environment=ENV,
                                    order_ref=ref, order_id=42, perm_id=987654321,
                                    status="filled", broker_status="Filled")
        two = self.ledger.reconcile(account=ACCOUNT, environment=ENV,
                                    order_ref=ref, order_id=42, perm_id=987654321,
                                    status="filled", broker_status="Filled")
        self.assertTrue(two["duplicate"])
        self.assertEqual(one["observation_id"], two["observation_id"])

    def test_bulk_api_single_leg_only_and_identical_semantics(self):
        self._prepare_day(limit=5)
        a = self._intent()
        self._claim(a)
        rid = self._reserve(a, slots=1)
        self._legs(a, rid)
        self.ledger.begin_submission(rid)  # legacy wrapper, 1 leg OK
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV, day_key="2026-11-07")["submitted_count"], 1)
        self.ledger.record_submission(rid, state="submitted")
        self.assertEqual(self.ledger.get_reservation(rid).state, "submitted")
        # multi-leg bulk is refused
        b = self._intent()
        self._claim(b)
        rid2 = self._reserve(b, slots=2)
        self._legs(b, rid2, count=2)
        with self.assertRaises(StateError):
            self.ledger.begin_submission(rid2)
        with self.assertRaises(StateError):
            self.ledger.record_submission(rid2, state="submitted")

    def test_begin_submission_requires_recorded_legs(self):
        self._prepare_day()
        a = self._intent()
        self._claim(a)
        rid = self._reserve(a)
        with self.assertRaises(StateError):
            self.ledger.begin_submission(rid)
        with self.assertRaises(StateError):
            self.ledger.begin_leg_attempt("nope")

    def test_unknown_never_auto_released_or_retried(self):
        day = self._prepare_day(limit=5)
        a = self._intent()
        self._claim(a)
        rid = self._reserve(a, slots=1)
        ref = self._legs(a, rid)[0]
        self.ledger.begin_leg_attempt(ref)
        self.ledger.record_leg_result(ref, state="unknown")
        self.assertEqual(self.ledger.get_reservation(rid).state, "unknown")
        self.assertEqual(len(self.ledger.outstanding_reservations(
            account=ACCOUNT, environment=ENV)), 1)
        self.assertTrue(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))
        with self.assertRaises(StateError):
            self.ledger.begin_leg_attempt(ref)  # never retried
        with self.assertRaises(StateError):
            self.ledger.abort_reservation(rid)
        with self.assertRaises(StateError):
            self.ledger.confirm_leg(ref, broker_status="absent_open_order")
        obs = self.ledger.reconcile(account=ACCOUNT, environment=ENV,
                                    order_ref=ref, status="absent_open_order")
        self.assertFalse(obs["duplicate"])
        self.assertEqual(self.ledger.get_reservation(rid).state, "unknown")
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV, day_key=day)["submitted_count"], 1)
        # explicit operator resolution is the only release path
        self.ledger.resolve_unknown(rid, state="released")
        self.assertEqual(self.ledger.get_reservation(rid).state, "released")
        self.assertFalse(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV, day_key=day)["submitted_count"], 1)

    def test_cancelled_or_rejected_attempt_never_refunds_day_count(self):
        day = self._prepare_day(limit=5)
        a = self._intent()
        self._claim(a)
        rid = self._reserve(a, slots=1)
        ref = self._legs(a, rid)[0]
        self.ledger.begin_leg_attempt(ref)
        self.ledger.record_leg_result(ref, state="submitted")
        self.ledger.reconcile(account=ACCOUNT, environment=ENV, order_ref=ref,
                              status="cancelled", broker_status="ApiCancelled")
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV, day_key=day)["submitted_count"], 1)
        self.assertEqual(self.ledger.get_reservation(rid).state, "submitted")
        b = self._intent()
        self._claim(b)
        with self.assertRaises(BudgetError):
            self._reserve(b, slots=5)  # only 4 of the 5 slots remain

    def test_rejected_leg_frees_risk_but_keeps_attempt_count(self):
        day = self._prepare_day(limit=5)
        a = self._intent()
        self._claim(a)
        rid = self._reserve(a, slots=1)
        ref = self._legs(a, rid)[0]
        self.ledger.begin_leg_attempt(ref)
        self.assertEqual(self.ledger.record_leg_result(ref, state="rejected"),
                         "rejected")
        self.assertEqual(self.ledger.get_reservation(rid).state, "rejected")
        self.assertEqual(self.ledger.outstanding_reservations(
            account=ACCOUNT, environment=ENV), [])
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV, day_key=day)["submitted_count"], 1)

    def test_reconciliation_idempotent_by_ref_and_perm_id(self):
        self._prepare_day()
        a = self._intent()
        self._claim(a)
        rid = self._reserve(a)
        ref = self._legs(a, rid)[0]
        one = self.ledger.reconcile(account=ACCOUNT, environment=ENV,
                                    order_ref=ref, perm_id=555, status="filled",
                                    broker_status="Filled")
        two = self.ledger.reconcile(account=ACCOUNT, environment=ENV,
                                    order_ref=ref, perm_id=555, status="filled",
                                    broker_status="Filled")
        by_perm = self.ledger.reconcile(account=ACCOUNT, environment=ENV,
                                        perm_id=555, status="filled",
                                        broker_status="Filled")
        self.assertFalse(one["duplicate"])
        self.assertTrue(two["duplicate"])
        self.assertTrue(by_perm["duplicate"])
        self.assertEqual(one["observation_id"], two["observation_id"])
        self.assertEqual(one["observation_id"], by_perm["observation_id"])
        self.assertEqual(len(self.ledger.list_reconciliations(
            account=ACCOUNT, environment=ENV)), 1)
        with self.assertRaises(StateError):
            self.ledger.reconcile(account="DU9999999", environment=ENV,
                                  order_ref=ref, status="filled")
        with self.assertRaises(StateError):
            self.ledger.reconcile(account=ACCOUNT, environment=ENV,
                                  order_ref="nope", status="filled")

    def test_cross_day_unknown_reservations_retained(self):
        self._prepare_day(limit=5, day_key="2026-11-06")
        self.ledger.set_daily_limit(account=ACCOUNT, environment=ENV,
                                    day_key="2026-11-07", limit_count=5)
        a = self._intent(now=_aware(day=2026, month=11))
        self._claim(a, now=_aware())
        rid = self._reserve(a, day_key="2026-11-06", slots=1)
        ref = self._legs(a, rid)[0]
        self.ledger.begin_leg_attempt(ref)
        self.ledger.record_leg_result(ref, state="unknown")
        # a new day has its own quota, but yesterday's unknown risk remains
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV, day_key="2026-11-07")["remaining"], 5)
        outstanding = self.ledger.outstanding_reservations(
            account=ACCOUNT, environment=ENV)
        self.assertEqual(len(outstanding), 1)
        self.assertEqual(outstanding[0].day_key, "2026-11-06")
        self.assertTrue(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))
        b = self._intent()
        self._claim(b)
        with self.assertRaises(BudgetError):
            self._reserve(b, day_key="2026-11-07",
                          items=[RiskReservationSpec("USD", "cash_base", "99999")])


# -- conservative first-day migration freeze -------------------------------

class TestMigrationFreeze(LedgerTestCase):
    def test_fresh_ledger_cannot_infer_zero_historic_submissions(self):
        self.ledger.set_resource(account=ACCOUNT, environment=ENV, currency="USD",
                                 resource_key="cash_base", amount="100000")
        with self.assertRaises(state.MigrationFreeze) as ctx:
            self.ledger.prepare_day_budget(
                account=ACCOUNT, environment=ENV, day_key="2026-11-07",
                limit_count=5)
        self.assertIn("cannot prove zero", ctx.exception.explanation)
        budget = self.ledger.get_day_budget(account=ACCOUNT, environment=ENV,
                                            day_key="2026-11-07")
        self.assertTrue(budget["migration_frozen"])
        self.assertEqual(budget["remaining"], 0)
        # reserve is actually BLOCKED, not just reported
        a = self._intent()
        self._claim(a)
        with self.assertRaises(state.MigrationFreeze):
            self._reserve(a, day_key="2026-11-07", slots=1)

    def test_verified_prior_count_unlocks_and_is_conservative(self):
        self.ledger.set_resource(account=ACCOUNT, environment=ENV, currency="USD",
                                 resource_key="cash_base", amount="100000")
        result = self.ledger.prepare_day_budget(
            account=ACCOUNT, environment=ENV, day_key="2026-11-07",
            limit_count=5, verified_prior_submissions=2)
        self.assertFalse(result["migration_frozen"])
        self.assertEqual(result["budget"]["submitted_count"], 2)
        self.assertEqual(result["budget"]["remaining"], 3)
        # a later smaller import never lowers the conservative count
        self.ledger.prepare_day_budget(
            account=ACCOUNT, environment=ENV, day_key="2026-11-07",
            limit_count=5, verified_prior_submissions=1)
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key="2026-11-07")["submitted_count"], 2)
        a = self._intent()
        self._claim(a)
        self._reserve(a, day_key="2026-11-07", slots=1)  # now allowed

    def test_next_configured_day_is_not_frozen(self):
        self.ledger.set_resource(account=ACCOUNT, environment=ENV, currency="USD",
                                 resource_key="cash_base", amount="100000")
        with self.assertRaises(state.MigrationFreeze):
            self.ledger.prepare_day_budget(account=ACCOUNT, environment=ENV,
                                           day_key="2026-11-07", limit_count=5)
        # a DIFFERENT configured natural day gets its own fresh budget
        result = self.ledger.prepare_day_budget(
            account=ACCOUNT, environment=ENV, day_key="2026-11-08",
            limit_count=5, verified_prior_submissions=0)
        self.assertFalse(result["migration_frozen"])
        self.assertEqual(result["budget"]["remaining"], 5)

    def test_disabled_p3_can_use_generous_internal_budget(self):
        # Disabled P3 callers use set_daily_limit directly (no implicit user
        # limit, no freeze) — this is an internal generous cap, not a user one.
        self.ledger.set_daily_limit(account=ACCOUNT, environment=ENV,
                                    day_key="2026-11-07", limit_count=10 ** 6)
        self.assertFalse(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key="2026-11-07")["migration_frozen"])

    # -- migration freeze: scope must be genuinely established, then a LATER
    #    configured natural day unlocks a controlled zero start ---------------

    def test_consecutive_days_first_frozen_second_zero_without_verified(self):
        # No explicit verified count is EVER supplied.  The first configured
        # day freezes; once the SAME scope is established and the clock rolls
        # to a LATER day, prepare_day_budget must allow a controlled zero start.
        self.ledger.set_resource(account=ACCOUNT, environment=ENV, currency="USD",
                                 resource_key="cash_base", amount="100000")
        with self.assertRaises(state.MigrationFreeze):
            self.ledger.prepare_day_budget(account=ACCOUNT, environment=ENV,
                                           day_key="2026-11-07", limit_count=5)
        self.assertTrue(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key="2026-11-07")["migration_frozen"])
        result = self.ledger.prepare_day_budget(
            account=ACCOUNT, environment=ENV, day_key="2026-11-08", limit_count=5)
        self.assertFalse(result["migration_frozen"])
        self.assertEqual(result["budget"]["submitted_count"], 0)
        self.assertEqual(result["budget"]["remaining"], 5)
        self.assertFalse(result["budget"]["migration_frozen"])
        # The old frozen day is NOT retroactively cleared.
        self.assertTrue(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key="2026-11-07")["migration_frozen"])

    def test_other_scope_established_row_cannot_unlock_this_scope(self):
        # A DIFFERENT account/environment having an established day must NOT be
        # used to infer a controlled zero start for a brand-new scope.
        self.ledger.set_resource(account=ACCOUNT, environment=ENV, currency="USD",
                                 resource_key="cash_base", amount="100000")
        self.ledger.set_daily_limit(account="DU9999999", environment=ENV,
                                    day_key="2026-11-07", limit_count=5)
        with self.assertRaises(state.MigrationFreeze):
            self.ledger.prepare_day_budget(account=ACCOUNT, environment=ENV,
                                           day_key="2026-11-07", limit_count=5)
        self.assertTrue(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key="2026-11-07")["migration_frozen"])

    def test_future_old_row_cannot_unlock_new_scope(self):
        # A stale/future day row that predates scope establishment (or is the
        # ONLY row) must not be treated as proof of a later natural day.
        self.ledger.set_resource(account=ACCOUNT, environment=ENV, currency="USD",
                                 resource_key="cash_base", amount="100000")
        self.ledger.set_daily_limit(account=ACCOUNT, environment=ENV,
                                    day_key="2026-11-09", limit_count=5)
        # brand-new scope still freezes on its first seen day
        with self.assertRaises(state.MigrationFreeze):
            self.ledger.prepare_day_budget(account=ACCOUNT, environment=ENV,
                                           day_key="2026-11-07", limit_count=5)
        self.assertTrue(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key="2026-11-07")["migration_frozen"])

    def test_clock_rollback_to_earlier_day_never_unlocks(self):
        # Once a scope is established on 2026-11-08, a rolled-back (earlier)
        # 2026-11-07 must NOT be granted a controlled zero start.
        self.ledger.set_resource(account=ACCOUNT, environment=ENV, currency="USD",
                                 resource_key="cash_base", amount="100000")
        with self.assertRaises(state.MigrationFreeze):
            self.ledger.prepare_day_budget(account=ACCOUNT, environment=ENV,
                                           day_key="2026-11-08", limit_count=5)
        with self.assertRaises(state.MigrationFreeze):
            self.ledger.prepare_day_budget(account=ACCOUNT, environment=ENV,
                                           day_key="2026-11-07", limit_count=5)
        self.assertTrue(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key="2026-11-07")["migration_frozen"])
        # The actual next day still unlocks.
        result = self.ledger.prepare_day_budget(
            account=ACCOUNT, environment=ENV, day_key="2026-11-09", limit_count=5)
        self.assertFalse(result["migration_frozen"])

    def test_old_unresolved_reservation_not_cleared_by_new_day(self):
        # A controlled zero start must NOT clear an older unresolved (unknown)
        # reservation/risk.  Day quota is per-day, but outstanding risk stays.
        self.ledger.set_resource(account=ACCOUNT, environment=ENV, currency="USD",
                                 resource_key="cash_base", amount="100000")
        self.ledger.set_daily_limit(account=ACCOUNT, environment=ENV,
                                    day_key="2026-11-07", limit_count=5)
        a = self._intent()
        self._claim(a)
        rid = self._reserve(a, day_key="2026-11-07", slots=1)
        ref = self._legs(a, rid)[0]
        self.ledger.begin_leg_attempt(ref)
        self.ledger.record_leg_result(ref, state="unknown")
        self.assertTrue(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))
        # Mark the scope established on the first day, then advance a day.
        self.ledger.prepare_day_budget(
            account=ACCOUNT, environment=ENV, day_key="2026-11-07",
            limit_count=5, verified_prior_submissions=0)
        self.ledger.prepare_day_budget(
            account=ACCOUNT, environment=ENV, day_key="2026-11-08", limit_count=5)
        self.assertTrue(self.ledger.has_unresolved_submissions(
            account=ACCOUNT, environment=ENV))
        self.assertEqual(len(self.ledger.outstanding_reservations(
            account=ACCOUNT, environment=ENV)), 1)
        self.assertEqual(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV,
            day_key="2026-11-08")["remaining"], 5)


# -- account-scoped lock ---------------------------------------------------

class TestAccountLock(LedgerTestCase):
    def test_lock_is_account_scoped_and_fail_closed_when_unsupported(self):
        with account_scope(self.root, ACCOUNT, ENV) as lock:
            self.assertTrue(lock.path.exists())
        # same (account, env) share a lock file; other scopes get their own
        p1 = state.AccountLock(self.root, ACCOUNT, ENV).path
        p2 = state.AccountLock(self.root, ACCOUNT, ENV).path
        p3 = state.AccountLock(self.root, ACCOUNT, "live").path
        self.assertEqual(p1, p2)
        self.assertNotEqual(p1, p3)
        # a path whose parent is a regular file cannot be used as lock dir
        blocker = self.root / "blocker"
        blocker.write_text("x", encoding="utf-8")
        with self.assertRaises(LockUnavailableError):
            with account_scope(blocker / "child", ACCOUNT, ENV):
                pass
        # simulate an unsupported platform: construction succeeds, use fails
        original = state.fcntl
        try:
            state.fcntl = None
            with self.assertRaises(LockUnavailableError):
                with account_scope(self.root, ACCOUNT, ENV):
                    pass
        finally:
            state.fcntl = original


# -- real multiprocessing races -------------------------------------------

def _claim_worker(db_path, intent, out, start):
    from thetamill.state import ClaimError, SubmissionLedger
    led = SubmissionLedger(db_path, TZ)
    start.wait()
    try:
        led.claim_intent(account=ACCOUNT, environment=ENV,
                         intent_id=intent["intent_id"],
                         full_digest=intent["full_digest"],
                         code=intent["approval_code"],
                         now=_aware())
        out.put(("ok", os.getpid()))
    except ClaimError as exc:
        out.put(("rejected", str(exc)))
    except Exception as exc:  # pragma: no cover - surface unexpected failures
        out.put(("error", f"{type(exc).__name__}: {exc}"))


def _reserve_worker(db_path, intent, out, start):
    from thetamill.state import BudgetError, RiskReservationSpec, SubmissionLedger
    led = SubmissionLedger(db_path, TZ)
    start.wait()
    try:
        rid = led.reserve(account=ACCOUNT, environment=ENV,
                          intent_id=intent["intent_id"], day_key="2026-11-07",
                          items=[RiskReservationSpec("USD", "cash_base", "700")],
                          day_submission_slots=1)
        out.put(("ok", rid))
    except BudgetError as exc:
        out.put(("rejected", str(exc)))
    except Exception as exc:  # pragma: no cover
        out.put(("error", f"{type(exc).__name__}: {exc}"))


class TestInterprocessRaces(LedgerTestCase):
    @staticmethod
    def _run(worker_fn, db_path, intents):
        ctx = mp.get_context("spawn")
        start = ctx.Event()
        out = ctx.Queue()
        procs = [ctx.Process(target=worker_fn, args=(db_path, i, out, start))
                 for i in intents]
        for p in procs:
            p.start()
        start.set()
        results = [out.get(timeout=30) for _ in procs]
        for p in procs:
            p.join(timeout=30)
            if p.is_alive():  # pragma: no cover
                p.terminate()
            assert p.exitcode == 0, f"worker exit={p.exitcode}"
        return results

    def test_same_intent_claimed_exactly_once_across_processes(self):
        intent = self._intent()
        results = self._run(_claim_worker, str(self.db), [intent] * 8)
        ok = [r for r in results if r[0] == "ok"]
        rejected = [r for r in results if r[0] == "rejected"]
        errors = [r for r in results if r[0] == "error"]
        self.assertEqual(errors, [], f"unexpected worker errors: {errors}")
        self.assertEqual(len(ok), 1, results)
        self.assertEqual(len(rejected), 7, results)
        self.assertEqual(self.ledger.get_intent(intent["intent_id"])["state"], "claimed")

    def test_quota_race_only_legal_reservations_succeed(self):
        self._prepare_day(limit=2)
        self.ledger.set_resource(account=ACCOUNT, environment=ENV, currency="USD",
                                 resource_key="cash_base", amount="1000")
        intents = [self._intent() for _ in range(6)]
        for i in intents:
            self._claim(i)
        results = self._run(_reserve_worker, str(self.db), intents)
        ok = [r for r in results if r[0] == "ok"]
        errors = [r for r in results if r[0] == "error"]
        self.assertEqual(errors, [], f"unexpected worker errors: {errors}")
        # 2 slots AND 1000 cash / 700 per reservation => at most 1 legal win
        self.assertEqual(len(ok), 1, results)
        budget = self.ledger.get_day_budget(account=ACCOUNT, environment=ENV,
                                            day_key="2026-11-07")
        self.assertEqual(budget["reserved_count"], 1)
        self.assertEqual(len(self.ledger.outstanding_reservations(
            account=ACCOUNT, environment=ENV)), 1)


# -- approved audited risk-reduction daily-quota exception -----------------

class TestRiskReductionQuotaException(LedgerTestCase):
    def test_risk_reduction_quota_exception_bypasses_exhausted_daily_limit_and_retains_real_attempts(self):
        day = self._prepare_day(limit=1)
        self.ledger.set_resource(account=ACCOUNT, environment=ENV, currency="USD",
                                 resource_key="close:2001", amount="5")
        a = self._intent()
        self._claim(a)
        rid_a = self._reserve(a, slots=1)
        refs_a = self._legs(a, rid_a, count=1)
        self.ledger.begin_leg_attempt(refs_a[0])
        self.ledger.record_leg_result(refs_a[0], state="submitted")

        # Quota exhausted: 1 submitted of 1 limit
        budget = self.ledger.get_day_budget(account=ACCOUNT, environment=ENV, day_key=day)
        self.assertEqual(budget["submitted_count"], 1)
        self.assertEqual(budget["remaining"], 0)

        # New risk intent is blocked
        b = self._intent()
        self._claim(b)
        with self.assertRaises(BudgetError):
            self._reserve(b, slots=1, kind="new_risk")

        # Approved audited risk-reduction intent succeeds under the quota exception
        rr_payload = dict(PAYLOAD, kind="risk_reduction")
        c = self._intent(payload=rr_payload)
        self._claim(c)
        rid_c = self.ledger.reserve(
            account=ACCOUNT, environment=ENV, intent_id=c["intent_id"],
            day_key=day, items=[RiskReservationSpec("USD", "close:2001", "1"),
                                RiskReservationSpec("USD", "cash_base", "200")],
            kind="risk_reduction", day_submission_slots=1, now=_aware())
        self.assertTrue(rid_c.startswith("res-"))

        # Real attempt count is tracked
        budget_c = self.ledger.get_day_budget(account=ACCOUNT, environment=ENV, day_key=day)
        self.assertEqual(budget_c["reserved_count"], 1)
        self.assertEqual(budget_c["submitted_count"], 1)

        refs_c = self._legs(c, rid_c, count=1)
        self.ledger.begin_leg_attempt(refs_c[0])
        budget_c2 = self.ledger.get_day_budget(account=ACCOUNT, environment=ENV, day_key=day)
        self.assertEqual(budget_c2["reserved_count"], 0)
        self.assertEqual(budget_c2["submitted_count"], 2)  # Real attempt count incremented

        # Required resource constraints are strictly retained: exceeding close capacity fails
        d = self._intent(payload=rr_payload)
        self._claim(d)
        with self.assertRaises(BudgetError):
            self.ledger.reserve(
                account=ACCOUNT, environment=ENV, intent_id=d["intent_id"],
                day_key=day, items=[RiskReservationSpec("USD", "close:2001", "100")],
                kind="risk_reduction", day_submission_slots=1, now=_aware())

    def test_risk_reduction_quota_exception_bypasses_migration_freeze(self):
        day = "2026-11-07"
        self.ledger.set_resource(account=ACCOUNT, environment=ENV, currency="USD",
                                 resource_key="cash_base", amount="100000")
        # Freeze day
        with self.assertRaises(state.MigrationFreeze):
            self.ledger.prepare_day_budget(account=ACCOUNT, environment=ENV,
                                          day_key=day, limit_count=5,
                                          verified_prior_submissions=None)
        self.assertTrue(self.ledger.get_day_budget(
            account=ACCOUNT, environment=ENV, day_key=day)["migration_frozen"])

        # New risk intent is frozen
        a = self._intent()
        self._claim(a)
        with self.assertRaises(state.MigrationFreeze):
            self.ledger.reserve(account=ACCOUNT, environment=ENV, intent_id=a["intent_id"],
                                day_key=day, items=[RiskReservationSpec("USD", "cash_base", "100")],
                                kind="new_risk", day_submission_slots=1, now=_aware())

        # Approved risk-reduction intent bypasses migration freeze
        rr_payload = dict(PAYLOAD, kind="risk_reduction")
        b = self._intent(payload=rr_payload)
        self._claim(b)
        rid = self.ledger.reserve(
            account=ACCOUNT, environment=ENV, intent_id=b["intent_id"],
            day_key=day, items=[RiskReservationSpec("USD", "cash_base", "100")],
            kind="risk_reduction", day_submission_slots=1, now=_aware())
        self.assertTrue(rid.startswith("res-"))

    def test_risk_reduction_quota_exception_refused_for_unapproved_intent(self):
        day = self._prepare_day(limit=1)
        # Normal intent without kind="risk_reduction" in payload
        a = self._intent(payload=dict(PAYLOAD, kind="prepared_legs"))
        self._claim(a)
        with self.assertRaises(ClaimError) as ctx:
            self.ledger.reserve(
                account=ACCOUNT, environment=ENV, intent_id=a["intent_id"],
                day_key=day, items=[RiskReservationSpec("USD", "cash_base", "100")],
                kind="risk_reduction", day_submission_slots=1, now=_aware())
        self.assertIn("not approved for risk_reduction", str(ctx.exception))

    def test_fresh_ledger_reduction_preserves_migration_freeze_and_counts(self):
        day = "2026-11-07"
        self.ledger.set_resource(account=ACCOUNT, environment=ENV, currency="USD",
                                 resource_key="cash_base", amount="100000")

        # Fresh ledger: reserve risk_reduction with missing day_budget
        rr_payload = dict(PAYLOAD, kind="risk_reduction")
        b = self._intent(payload=rr_payload)
        self._claim(b)
        rid = self.ledger.reserve(
            account=ACCOUNT, environment=ENV, intent_id=b["intent_id"],
            day_key=day, items=[RiskReservationSpec("USD", "cash_base", "100")],
            kind="risk_reduction", day_submission_slots=1, now=_aware())
        self.assertTrue(rid.startswith("res-"))

        # Day budget row must NEVER create verified history or unfreeze migration
        budget = self.ledger.get_day_budget(account=ACCOUNT, environment=ENV, day_key=day)
        self.assertTrue(budget["exists"])
        self.assertTrue(budget["migration_frozen"])
        self.assertEqual(budget["reserved_count"], 1)
        self.assertEqual(budget["submitted_count"], 0)

        # Subsequent new_risk intent on same day is blocked by migration freeze
        new_intent = self._intent()
        self._claim(new_intent)
        with self.assertRaises(state.MigrationFreeze):
            self.ledger.reserve(
                account=ACCOUNT, environment=ENV, intent_id=new_intent["intent_id"],
                day_key=day, items=[RiskReservationSpec("USD", "cash_base", "100")],
                kind="new_risk", day_submission_slots=1, now=_aware())

        # Calling prepare_day_budget on same day without verified prior count stays frozen
        with self.assertRaises(state.MigrationFreeze):
            self.ledger.prepare_day_budget(
                account=ACCOUNT, environment=ENV, day_key=day, limit_count=5,
                verified_prior_submissions=None)
        budget_frozen = self.ledger.get_day_budget(account=ACCOUNT, environment=ENV, day_key=day)
        self.assertTrue(budget_frozen["migration_frozen"])
        self.assertEqual(budget_frozen["reserved_count"], 1)  # counts retained
        self.assertEqual(budget_frozen["limit_count"], 5)

        # Importing verified prior count unfreezes while retaining reserved counts
        result = self.ledger.prepare_day_budget(
            account=ACCOUNT, environment=ENV, day_key=day, limit_count=5,
            verified_prior_submissions=2)
        self.assertFalse(result["migration_frozen"])
        self.assertEqual(result["budget"]["submitted_count"], 2)
        self.assertEqual(result["budget"]["reserved_count"], 1)
        self.assertEqual(result["budget"]["remaining"], 2)  # 5 - (2 + 1) = 2

        # Cross-day migration: next configured natural day unlocks controlled zero
        next_day = "2026-11-08"
        next_result = self.ledger.prepare_day_budget(
            account=ACCOUNT, environment=ENV, day_key=next_day, limit_count=5)
        self.assertFalse(next_result["migration_frozen"])
        self.assertEqual(next_result["budget"]["submitted_count"], 0)
        self.assertEqual(next_result["budget"]["remaining"], 5)

        # Day 1 counts remain retained and undisturbed
        day1_budget = self.ledger.get_day_budget(account=ACCOUNT, environment=ENV, day_key=day)
        self.assertEqual(day1_budget["submitted_count"], 2)
        self.assertEqual(day1_budget["reserved_count"], 1)

    def test_risk_reduction_trust_boundary_and_verification_evidence(self):
        day = self._prepare_day(limit=5)
        # Intent with candidate_risk_reduction is accepted
        cand_payload = dict(PAYLOAD, kind="candidate_risk_reduction")
        cand = self._intent(payload=cand_payload)
        self._claim(cand)
        rid = self.ledger.reserve(
            account=ACCOUNT, environment=ENV, intent_id=cand["intent_id"],
            day_key=day, items=[RiskReservationSpec("USD", "cash_base", "100")],
            kind="risk_reduction", day_submission_slots=1, now=_aware())
        self.assertTrue(rid.startswith("res-"))

        # Invalid verification evidence is refused
        rr_payload = dict(PAYLOAD, kind="risk_reduction")
        bad_ev_intent = self._intent(payload=rr_payload)
        self._claim(bad_ev_intent)
        with self.assertRaises(ClaimError) as ctx:
            self.ledger.reserve(
                account=ACCOUNT, environment=ENV, intent_id=bad_ev_intent["intent_id"],
                day_key=day, items=[RiskReservationSpec("USD", "cash_base", "100")],
                kind="risk_reduction", day_submission_slots=1,
                verification_evidence={"verified_reduction": False}, now=_aware())
        self.assertIn("without validated verification evidence", str(ctx.exception))

        # Valid verification evidence succeeds
        good_ev_intent = self._intent(payload=rr_payload)
        self._claim(good_ev_intent)
        rid_good = self.ledger.reserve(
            account=ACCOUNT, environment=ENV, intent_id=good_ev_intent["intent_id"],
            day_key=day, items=[RiskReservationSpec("USD", "cash_base", "100")],
            kind="risk_reduction", day_submission_slots=1,
            verification_evidence={"verified_reduction": True}, now=_aware())
        self.assertTrue(rid_good.startswith("res-"))


if __name__ == "__main__":
    unittest.main()
