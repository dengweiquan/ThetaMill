"""Tests for stage-D1 offline read-only health diagnostics (thetamill.doctor)."""
from __future__ import annotations

import builtins
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from thetamill import cli, doctor
from thetamill.delivery_evidence import DELIVERABLES_SCHEMA
from thetamill.state import SubmissionLedger

FIXED_NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)


def _dir_manifest(root: Path) -> dict[str, bytes | None]:
    """Capture complete directory listing including file contents and sidecars."""
    manifest: dict[str, bytes | None] = {}
    if not root.exists():
        return manifest
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_dir():
            manifest[rel] = None
        else:
            manifest[rel] = p.read_bytes()
    return manifest


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="thetamill-doctor-test-")
        self.tmp_path = Path(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_doctor_missing_ledger_unchanged(self):
        missing_db = self.tmp_path / "nonexistent.sqlite"
        before = _dir_manifest(self.tmp_path)

        report = doctor.diagnose(ledger_path=missing_db, now=FIXED_NOW)

        after = _dir_manifest(self.tmp_path)
        self.assertEqual(before, after)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["ledger"]["status"], "missing")
        self.assertIn("file_not_found", report["ledger"]["issues"])

    def test_doctor_corrupt_ledger_unchanged(self):
        corrupt_db = self.tmp_path / "corrupt.sqlite"
        corrupt_db.write_bytes(b"not a valid sqlite database header at all")
        before = _dir_manifest(self.tmp_path)

        report = doctor.diagnose(ledger_path=corrupt_db, now=FIXED_NOW)

        after = _dir_manifest(self.tmp_path)
        self.assertEqual(before, after)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["ledger"]["status"], "corrupt")
        self.assertIn("schema_error", report["ledger"]["issues"])

    def test_doctor_real_temp_ledger_readonly(self):
        db_path = self.tmp_path / "ledger.sqlite"
        ledger = SubmissionLedger(db_path, "UTC")
        del ledger

        before = _dir_manifest(self.tmp_path)
        report = doctor.diagnose(ledger_path=db_path, now=FIXED_NOW)
        after = _dir_manifest(self.tmp_path)

        self.assertEqual(before, after)
        self.assertEqual(report["ledger"]["status"], "ok")
        self.assertEqual(report["ledger"]["issues"], [])

    def test_doctor_ledger_sidecar_present_refused(self):
        db_path = self.tmp_path / "ledger.sqlite"
        ledger = SubmissionLedger(db_path, "UTC")
        del ledger

        wal_file = self.tmp_path / "ledger.sqlite-wal"
        wal_file.write_bytes(b"active wal sidecar")

        before = _dir_manifest(self.tmp_path)
        report = doctor.diagnose(ledger_path=db_path, now=FIXED_NOW)
        after = _dir_manifest(self.tmp_path)

        self.assertEqual(before, after)
        self.assertEqual(report["ledger"]["status"], "sidecar_present_refused")
        self.assertIn("sidecar_present_refused", report["ledger"]["issues"])

    def test_doctor_ledger_readonly_directory(self):
        ro_dir = self.tmp_path / "ro_dir"
        ro_dir.mkdir()
        db_path = ro_dir / "ledger.sqlite"
        ledger = SubmissionLedger(db_path, "UTC")
        del ledger

        try:
            os.chmod(ro_dir, 0o500)
            report = doctor.diagnose(ledger_path=db_path, now=FIXED_NOW)
            self.assertEqual(report["ledger"]["status"], "ok")
        finally:
            os.chmod(ro_dir, 0o700)

    def test_doctor_ledger_foreign_schema_missing_columns(self):
        db_path = self.tmp_path / "foreign.sqlite"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
        conn.execute("INSERT INTO meta (key, value) VALUES ('day_timezone', 'UTC')")
        conn.execute("CREATE TABLE intents (intent_id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE legacy_codes (code TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE day_budget (scope TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE resources (scope TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE reservations (reservation_id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE reservation_items (reservation_id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE legs (intent_id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE reconciliation (observation_id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()

        before = _dir_manifest(self.tmp_path)
        report = doctor.diagnose(ledger_path=db_path, now=FIXED_NOW)
        after = _dir_manifest(self.tmp_path)

        self.assertEqual(before, after)
        self.assertEqual(report["ledger"]["status"], "foreign_schema")
        self.assertIn("missing_columns", report["ledger"]["issues"])

    def test_doctor_ledger_invalid_timezone_redacted(self):
        db_path = self.tmp_path / "bad_tz.sqlite"
        ledger = SubmissionLedger(db_path, "UTC")
        del ledger

        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE meta SET value='SECRET_INVALID_ZONE_999' WHERE key='day_timezone'")
        conn.commit()
        conn.close()

        report = doctor.diagnose(ledger_path=db_path, now=FIXED_NOW)
        self.assertEqual(report["ledger"]["status"], "invalid_metadata")
        self.assertIn("invalid_day_timezone", report["ledger"]["issues"])

        json_out = json.dumps(report)
        text_out = doctor.format_text(report)
        self.assertNotIn("SECRET_INVALID_ZONE_999", json_out)
        self.assertNotIn("SECRET_INVALID_ZONE_999", text_out)

    def test_doctor_connect_uri_immutable_captured(self):
        db_path = self.tmp_path / "ledger.sqlite"
        ledger = SubmissionLedger(db_path, "UTC")
        del ledger

        captured_uris: list[str] = []
        real_connect = sqlite3.connect

        def spy_connect(database, *args, **kwargs):
            captured_uris.append(str(database))
            return real_connect(database, *args, **kwargs)

        before = _dir_manifest(self.tmp_path)
        with mock.patch("sqlite3.connect", side_effect=spy_connect):
            report = doctor.diagnose(ledger_path=db_path, now=FIXED_NOW)
        after = _dir_manifest(self.tmp_path)

        self.assertEqual(before, after)
        self.assertEqual(report["ledger"]["status"], "ok")
        self.assertEqual(len(captured_uris), 1)
        self.assertIn("mode=ro", captured_uris[0])
        self.assertIn("immutable=1", captured_uris[0])

    def test_doctor_config_redaction(self):
        secret_cfg = self.tmp_path / "secret_cfg.json"
        secret_cfg.write_text(json.dumps({
            "account": "U_SECRET_ACC_12345",
            "secret_token": "RAW_SUPER_SECRET_TOKEN",
            "risk": {"enabled": False},
        }), encoding="utf-8")

        report = doctor.diagnose(config_path=secret_cfg, now=FIXED_NOW)
        json_out = json.dumps(report)
        text_out = doctor.format_text(report)

        self.assertNotIn("U_SECRET_ACC_12345", json_out)
        self.assertNotIn("U_SECRET_ACC_12345", text_out)
        self.assertNotIn("RAW_SUPER_SECRET_TOKEN", json_out)
        self.assertNotIn("RAW_SUPER_SECRET_TOKEN", text_out)
        self.assertNotIn(str(secret_cfg), json_out)
        self.assertNotIn(str(secret_cfg), text_out)
        self.assertNotIn(secret_cfg.name, json_out)
        self.assertNotIn(secret_cfg.name, text_out)

    def test_doctor_config_malformed_and_non_mapping(self):
        bad_json = self.tmp_path / "bad.json"
        bad_json.write_text("{broken json", encoding="utf-8")
        r1 = doctor.diagnose(config_path=bad_json, now=FIXED_NOW)
        self.assertEqual(r1["config"]["status"], "malformed")
        self.assertIn("json_decode_error", r1["config"]["issues"])

        list_root = self.tmp_path / "list_root.json"
        list_root.write_text("[1, 2, 3]", encoding="utf-8")
        r2 = doctor.diagnose(config_path=list_root, now=FIXED_NOW)
        self.assertEqual(r2["config"]["status"], "malformed")
        self.assertIn("root_not_a_mapping", r2["config"]["issues"])

    def test_doctor_config_huge_int_timeout_overflow(self):
        cfg = self.tmp_path / "huge_timeout.json"
        cfg.write_text(json.dumps({
            "request_timeout": 10 ** 400,
            "connect_timeout": -1,
            "command_timeout": True,
        }), encoding="utf-8")

        report = doctor.diagnose(config_path=cfg, now=FIXED_NOW)
        self.assertEqual(report["config"]["status"], "invalid")
        self.assertIn("invalid_request_timeout", report["config"]["issues"])
        self.assertIn("invalid_connect_timeout", report["config"]["issues"])
        self.assertIn("invalid_command_timeout", report["config"]["issues"])

    def test_doctor_config_risk_validation(self):
        cfg = self.tmp_path / "bad_risk.json"
        cfg.write_text(json.dumps({
            "risk": {"enabled": True, "day_timezone": "INVALID_TZ"},
        }), encoding="utf-8")

        report = doctor.diagnose(config_path=cfg, now=FIXED_NOW)
        self.assertEqual(report["config"]["status"], "invalid")
        self.assertIn("invalid_risk_config", report["config"]["issues"])

    def test_doctor_invalid_now_raises_value_error(self):
        with self.assertRaises(ValueError):
            doctor.diagnose(now=datetime(2026, 11, 9, 12, 0, 0))  # naive

        with self.assertRaises(ValueError):
            doctor.diagnose(now="not-a-datetime")  # wrong type

    def test_doctor_no_roll_cli_import(self):
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if "roll_cli" in name:
                raise AssertionError(f"Forbidden import of roll_cli: {name}")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=guarded_import):
            doctor.diagnose(now=FIXED_NOW)

    def test_doctor_json_no_nan(self):
        report = doctor.diagnose(now=FIXED_NOW)
        dumped = json.dumps(report, allow_nan=False)
        self.assertNotIn("NaN", dumped)
        self.assertNotIn("Infinity", dumped)

    def test_doctor_delivery_evidence_structural(self):
        ev_path = self.tmp_path / "evidence.json"
        ev_path.write_text(json.dumps({
            "schema": DELIVERABLES_SCHEMA,
            "records": [{
                "con_id": 12345,
                "stock_only": True,
                "source": "IB contract details",
                "verified_at": "2026-11-09T10:00:00+00:00",
                "expires_at": "2026-11-09T18:00:00+00:00",
            }],
        }), encoding="utf-8")

        r1 = doctor.diagnose(delivery_evidence_path=ev_path, now=FIXED_NOW)
        self.assertEqual(r1["delivery_evidence"]["status"], "structural_only")
        self.assertEqual(r1["delivery_evidence"]["validation"], "not_checked")
        self.assertEqual(r1["delivery_evidence"]["record_count"], 1)

        bad_ev = self.tmp_path / "bad_evidence.json"
        bad_ev.write_text(json.dumps({
            "schema": DELIVERABLES_SCHEMA,
            "records": [{
                "con_id": 12345,
                "stock_only": False,
                "source": "test",
                "verified_at": "2026-11-09T10:00:00+00:00",
                "expires_at": "2026-11-09T18:00:00+00:00",
            }],
        }), encoding="utf-8")

        r2 = doctor.diagnose(delivery_evidence_path=bad_ev, now=FIXED_NOW)
        self.assertEqual(r2["delivery_evidence"]["status"], "malformed")
        self.assertEqual(r2["delivery_evidence"]["validation"], "not_checked")

    def test_doctor_format_text_no_overclaiming(self):
        bad_db = self.tmp_path / "bad.sqlite"
        bad_db.write_bytes(b"corrupt")
        report = doctor.diagnose(ledger_path=bad_db, now=FIXED_NOW)
        text = doctor.format_text(report)
        self.assertIn("offline stopped-copy only; no concurrent consistency guarantee", text)
        self.assertNotIn("read-only verified", text)
        self.assertNotIn("authentic authorization", text)
        self.assertNotIn("full instrument validation", text)

    def test_cli_early_dispatch(self):
        temp_cwd = self.tmp_path / "scratch_cwd"
        temp_cwd.mkdir()
        malformed_cfg = temp_cwd / "config.json"
        malformed_cfg.write_text("{malformed config: not valid json at all!", encoding="utf-8")

        def forbidden_load_cfg(*args, **kwargs):
            raise AssertionError("FORBIDDEN: cli.load_cfg was called during doctor early dispatch!")

        def forbidden_connect(*args, **kwargs):
            raise AssertionError("FORBIDDEN: cli.connect was called during doctor early dispatch!")

        before = _dir_manifest(temp_cwd)
        out = io.StringIO()

        orig_cwd = os.getcwd()
        os.chdir(temp_cwd)
        try:
            with mock.patch.object(cli, "load_cfg", side_effect=forbidden_load_cfg), \
                 mock.patch.object(cli, "connect", side_effect=forbidden_connect), \
                 redirect_stdout(out):
                with self.assertRaises(SystemExit) as cm:
                    cli.main(["doctor", "--format", "json"])
                self.assertIn(cm.exception.code, (0, 1))
        finally:
            os.chdir(orig_cwd)

        captured = out.getvalue()
        data = json.loads(captured)
        self.assertEqual(data.get("doctor"), "thetamill.doctor.v1")
        self.assertIn("offline stopped-copy only; no concurrent consistency guarantee", data.get("caveat", ""))

        after = _dir_manifest(temp_cwd)
        self.assertEqual(before, after)
        subdirs = [p.name for p in temp_cwd.iterdir() if p.is_dir()]
        self.assertEqual(subdirs, [])


if __name__ == "__main__":
    unittest.main()
