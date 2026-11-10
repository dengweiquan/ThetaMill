"""Tests for the neutral stock-only delivery evidence loader and identity matcher."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from thetamill import delivery_evidence, roll_cli, submission_cli
from thetamill.broker_state import Instrument

NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)


def make_opt(
    con_id: int = 2001,
    *,
    multiplier: int | str = 100,
    strike: int | str = 180,
    underlying_id: int = 1001,
    symbol: str = "AAPL",
    currency: str = "USD",
    sec_type: str = "OPT",
    exchange: str = "SMART",
    right: str = "P",
    expiry: str = "20261120",
    trading_class: str = "AAPL",
) -> Instrument:
    return Instrument(
        con_id=con_id,
        symbol=symbol,
        currency=currency,
        sec_type=sec_type,
        exchange=exchange,
        underlying_id=underlying_id,
        multiplier=Decimal(str(multiplier)),
        right=right,
        strike=Decimal(str(strike)),
        expiry=expiry,
        trading_class=trading_class,
    )


def make_record(
    inst: Instrument,
    *,
    stock_only: bool = True,
    source: str = "IB contract details + human review",
    verified_at: str | None = None,
    expires_at: str | None = None,
    **identity_overrides,
) -> dict:
    identity = delivery_evidence._identity_for(inst)
    identity.update(identity_overrides)
    return {
        "instrument": identity,
        "stock_only": stock_only,
        "source": source,
        "verified_at": verified_at or (NOW - timedelta(hours=1)).isoformat(),
        "expires_at": expires_at or (NOW + timedelta(hours=24)).isoformat(),
    }


def write_evidence(path: Path | str, records: list[dict], schema: str = delivery_evidence.DELIVERABLES_SCHEMA) -> str:
    doc = {"schema": schema, "records": records}
    Path(path).write_text(json.dumps(doc), encoding="utf-8")
    return str(path)


class TestDeliveryEvidenceIdentityCompatibility(unittest.TestCase):
    def test_roll_cli_aliases_and_error_identity_compatibility(self):
        """Prove same function/error identity compatibility between roll_cli and delivery_evidence."""
        self.assertIs(
            roll_cli.load_delivery_evidence,
            delivery_evidence.load_delivery_evidence,
        )
        self.assertIs(
            roll_cli.DeliverableEvidenceError,
            delivery_evidence.DeliverableEvidenceError,
        )
        self.assertEqual(
            roll_cli.DELIVERABLES_SCHEMA,
            delivery_evidence.DELIVERABLES_SCHEMA,
        )
        self.assertTrue(
            issubclass(delivery_evidence.DeliverableEvidenceError, ValueError)
        )
        self.assertIs(roll_cli._finite, delivery_evidence._finite)
        self.assertIs(roll_cli._freeze, delivery_evidence._freeze)
        self.assertIs(roll_cli._identity_for, delivery_evidence._identity_for)
        self.assertIs(roll_cli._identity_matches, delivery_evidence._identity_matches)
        self.assertIs(roll_cli._aware, delivery_evidence._aware)


class TestMultiplierFidelity(unittest.TestCase):
    def test_multipliers_100_200_500_matching_and_rejection(self):
        """Prove exact multiplier fidelity for standard 100, mini/adjusted 200, 500 contracts."""
        opt100 = make_opt(con_id=2001, multiplier=100)
        opt200 = make_opt(con_id=2002, multiplier=200)
        opt500 = make_opt(con_id=2003, multiplier=500)

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "evidence.json"
            write_evidence(p, [
                make_record(opt100),
                make_record(opt200),
                make_record(opt500),
            ])

            confirmed = delivery_evidence.load_delivery_evidence(p, [opt100, opt200, opt500], NOW)
            self.assertEqual(set(confirmed.keys()), {2001, 2002, 2003})
            self.assertEqual(confirmed[2001]["instrument"]["multiplier"], "100")
            self.assertEqual(confirmed[2002]["instrument"]["multiplier"], "200")
            self.assertEqual(confirmed[2003]["instrument"]["multiplier"], "500")

    def test_decimal_spelling_equivalence_for_multiplier(self):
        """Decimal '100.0' matches multiplier Decimal('100')."""
        opt100 = make_opt(con_id=2001, multiplier=100)
        rec = make_record(opt100, multiplier="100.0")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "evidence.json"
            write_evidence(p, [rec])
            confirmed = delivery_evidence.load_delivery_evidence(p, [opt100], NOW)
            self.assertEqual(set(confirmed.keys()), {2001})

    def test_multiplier_mismatch_rejected(self):
        """Evidence specifying multiplier 100 rejects instrument with multiplier 200 or 500."""
        opt200 = make_opt(con_id=2002, multiplier=200)
        rec_with_100 = make_record(opt200, multiplier="100")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "evidence.json"
            write_evidence(p, [rec_with_100])
            with self.assertRaises(delivery_evidence.DeliverableEvidenceError) as ctx:
                delivery_evidence.load_delivery_evidence(p, [opt200], NOW)
            self.assertIn("identity/multiplier does not match", str(ctx.exception))


class TestFullMatching(unittest.TestCase):
    def test_full_identity_matching_all_fields(self):
        """Every field in _IDENTITY_FIELDS must match; unrequested conId is ignored (empty), others fail closed."""
        opt = make_opt(con_id=2001)

        # Unrequested conId in evidence yields an empty confirmed mapping, not an error
        rec_unrequested = make_record(opt, con_id=9999)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "tampered_con_id.json"
            write_evidence(p, [rec_unrequested])
            confirmed = delivery_evidence.load_delivery_evidence(p, [opt], NOW)
            self.assertEqual(dict(confirmed), {})

        # Tampering any other field for the requested conId raises DeliverableEvidenceError
        fields_to_tamper = {
            "underlying_id": 9999,
            "symbol": "MSFT",
            "currency": "EUR",
            "sec_type": "STK",
            "exchange": "CBOE",
            "multiplier": "200",
            "right": "C",
            "strike": "200",
            "expiry": "20270101",
            "trading_class": "OTHER",
        }
        for field, bad_val in fields_to_tamper.items():
            rec = make_record(opt, **{field: bad_val})
            with tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / f"tampered_{field}.json"
                write_evidence(p, [rec])
                with self.assertRaises(delivery_evidence.DeliverableEvidenceError):
                    delivery_evidence.load_delivery_evidence(p, [opt], NOW)

    def test_identity_matches_direct_con_id_and_fields(self):
        """Direct test of _identity_matches asserting False on con_id or term mismatch."""
        opt = make_opt(con_id=2001)
        valid_rec = delivery_evidence._identity_for(opt)
        self.assertTrue(delivery_evidence._identity_matches(valid_rec, opt))

        tampered_con_id = dict(valid_rec, con_id=9999)
        self.assertFalse(delivery_evidence._identity_matches(tampered_con_id, opt))

        tampered_strike = dict(valid_rec, strike="999")
        self.assertFalse(delivery_evidence._identity_matches(tampered_strike, opt))

    def test_missing_identity_field_rejected(self):
        opt = make_opt(con_id=2001)
        rec = make_record(opt)
        del rec["instrument"]["trading_class"]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "missing_field.json"
            write_evidence(p, [rec])
            with self.assertRaises(delivery_evidence.DeliverableEvidenceError):
                delivery_evidence.load_delivery_evidence(p, [opt], NOW)


class TestDuplicateIdentities(unittest.TestCase):
    def test_conflicting_requested_identities_rejected(self):
        """Duplicate instruments with the same con_id but conflicting terms raise DeliverableEvidenceError."""
        opt_a = make_opt(con_id=2001, strike=180)
        opt_b = make_opt(con_id=2001, strike=190)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "evidence.json"
            write_evidence(p, [make_record(opt_a)])
            with self.assertRaises(delivery_evidence.DeliverableEvidenceError) as ctx:
                delivery_evidence.load_delivery_evidence(p, [opt_a, opt_b], NOW)
            self.assertIn("conflicting identity for conId 2001", str(ctx.exception))

    def test_identical_requested_duplicates_accepted(self):
        """Duplicate instruments with identical terms for the same con_id are accepted."""
        opt = make_opt(con_id=2001)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "evidence.json"
            write_evidence(p, [make_record(opt)])
            confirmed = delivery_evidence.load_delivery_evidence(p, [opt, opt], NOW)
            self.assertEqual(set(confirmed.keys()), {2001})


class TestExpiredFutureMissingImmutability(unittest.TestCase):
    def test_expired_evidence_rejected(self):
        """Evidence where expires_at <= now is rejected with DeliverableEvidenceError."""
        opt = make_opt(con_id=2001)
        rec = make_record(
            opt,
            verified_at=(NOW - timedelta(days=2)).isoformat(),
            expires_at=(NOW - timedelta(minutes=1)).isoformat(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "expired.json"
            write_evidence(p, [rec])
            with self.assertRaises(delivery_evidence.DeliverableEvidenceError) as ctx:
                delivery_evidence.load_delivery_evidence(p, [opt], NOW)
            self.assertIn("evidence has expired", str(ctx.exception))

    def test_future_evidence_rejected(self):
        """Evidence where verified_at > now is rejected with DeliverableEvidenceError."""
        opt = make_opt(con_id=2001)
        rec = make_record(
            opt,
            verified_at=(NOW + timedelta(minutes=5)).isoformat(),
            expires_at=(NOW + timedelta(days=1)).isoformat(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "future.json"
            write_evidence(p, [rec])
            with self.assertRaises(delivery_evidence.DeliverableEvidenceError) as ctx:
                delivery_evidence.load_delivery_evidence(p, [opt], NOW)
            self.assertIn("verified_at is in the future", str(ctx.exception))

    def test_missing_file_or_none_returns_empty_mapping(self):
        """Missing file path or None path returns an empty mapping without error."""
        opt = make_opt(con_id=2001)
        self.assertEqual(dict(delivery_evidence.load_delivery_evidence(None, [opt], NOW)), {})
        self.assertEqual(dict(delivery_evidence.load_delivery_evidence("/no/such/file.json", [opt], NOW)), {})

    def test_json_read_error_raises_from_none(self):
        """Malformed JSON raises DeliverableEvidenceError with __cause__ suppressed."""
        opt = make_opt(con_id=2001)
        with tempfile.TemporaryDirectory() as tmp:
            bad_json = Path(tmp) / "bad.json"
            bad_json.write_text("{malformed", encoding="utf-8")
            with self.assertRaises(delivery_evidence.DeliverableEvidenceError) as ctx:
                delivery_evidence.load_delivery_evidence(bad_json, [opt], NOW)
            self.assertIn("cannot read delivery evidence: JSONDecodeError", str(ctx.exception))
            self.assertIsNone(ctx.exception.__cause__)

    def test_immutability_of_returned_and_nested_mappings(self):
        """Return mapping and all nested dictionaries are recursively immutable MappingProxyType."""
        opt = make_opt(con_id=2001)
        rec = make_record(opt)
        rec["tags"] = ["tag1", "tag2"]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "immut.json"
            write_evidence(p, [rec])
            confirmed = delivery_evidence.load_delivery_evidence(p, [opt], NOW)

            self.assertIsInstance(confirmed, MappingProxyType)
            with self.assertRaises(TypeError):
                confirmed[9999] = {}

            entry = confirmed[2001]
            self.assertIsInstance(entry, MappingProxyType)
            with self.assertRaises(TypeError):
                entry["source"] = "tampered"

            self.assertIsInstance(entry["instrument"], MappingProxyType)
            with self.assertRaises(TypeError):
                entry["instrument"]["strike"] = "999"

            self.assertIsInstance(entry["tags"], tuple)


class TestSiblingConIdAuthorization(unittest.TestCase):
    def test_sibling_conid_sharing_underlying_not_authorized(self):
        """Confirming standard conId 2001 does NOT authorize sibling conId 2002 even with same underlying."""
        std_opt = make_opt(con_id=2001, underlying_id=1001, trading_class="AAPL")
        sibling_opt = make_opt(con_id=2002, underlying_id=1001, trading_class="AAPL")

        self.assertEqual(std_opt.key, sibling_opt.key)
        self.assertEqual(std_opt.underlying_id, sibling_opt.underlying_id)
        self.assertNotEqual(std_opt.con_id, sibling_opt.con_id)

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "std_only.json"
            write_evidence(p, [make_record(std_opt)])

            res_sibling = delivery_evidence.load_delivery_evidence(p, [sibling_opt], NOW)
            self.assertEqual(dict(res_sibling), {})

            res_both = delivery_evidence.load_delivery_evidence(p, [std_opt, sibling_opt], NOW)
            self.assertEqual(set(res_both.keys()), {2001})
            self.assertNotIn(2002, res_both)
            self.assertNotIn(1001, res_both)


class TestSubmissionLoaderNormalization(unittest.TestCase):
    def test_submission_loader_normalize_strict_mapping(self):
        """Prove submission loader expects mapping of strict int>0 nonbool keys to actual evidence mapping."""
        opt = make_opt(con_id=2001)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "evidence.json"
            write_evidence(p, [make_record(opt)])
            result = submission_cli.load_delivery_evidence(p, [opt], NOW)
            self.assertIn(2001, result)
            self.assertIsInstance(result[2001], (dict, MappingProxyType))

    def test_submission_loader_rejects_permissive_set_or_list(self):
        """Loader returns empty dict if underlying returns set or list (no permissive fallback)."""
        opt = make_opt(con_id=2001)
        with patch.object(delivery_evidence, "load_delivery_evidence", return_value={2001}):
            self.assertEqual(submission_cli.load_delivery_evidence("dummy", [opt], NOW), {})

        with patch.object(delivery_evidence, "load_delivery_evidence", return_value=[2001]):
            self.assertEqual(submission_cli.load_delivery_evidence("dummy", [opt], NOW), {})

    def test_submission_loader_filters_non_int_non_positive_and_dummy_values(self):
        """Keys must be strict int>0 non-bool; values must be actual evidence mappings."""
        opt = make_opt(con_id=2001)
        mock_evidence = {
            2001: {"source": "ok"},
            "2002": {"source": "str_key_rejected"},
            True: {"source": "bool_key_rejected"},
            0: {"source": "zero_key_rejected"},
            -1: {"source": "negative_key_rejected"},
            2003: True,
            2004: "non-mapping",
        }
        with patch.object(delivery_evidence, "load_delivery_evidence", return_value=mock_evidence):
            res = submission_cli.load_delivery_evidence("dummy", [opt], NOW)
            self.assertEqual(set(res.keys()), {2001})
            self.assertEqual(res[2001], {"source": "ok"})

    def test_submission_loader_fails_closed_on_error(self):
        """On exception / missing file / malformed JSON, returns empty mapping."""
        opt = make_opt(con_id=2001)
        self.assertEqual(submission_cli.load_delivery_evidence(None, [opt], NOW), {})
        self.assertEqual(submission_cli.load_delivery_evidence("/no/file.json", [opt], NOW), {})

        with tempfile.TemporaryDirectory() as tmp:
            bad_json = Path(tmp) / "bad.json"
            bad_json.write_text("{malformed", encoding="utf-8")
            self.assertEqual(submission_cli.load_delivery_evidence(bad_json, [opt], NOW), {})
