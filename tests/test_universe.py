"""外部准入白名单契约的单元测试（不连接网关）。"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from thetamill import universe


def good_doc(**over):
    doc = {
        "schema": "thetamill-universe/1",
        "generated": "2026-11-07T12:00:00",
        "valid_until": (date.today() + timedelta(days=90)).isoformat(),
        "approved_by": "人工签字",
        "signed_at": "2026-11-07T12:00:00",
        "items": [{
            "symbol": "AAPL", "name": "Apple", "market": "US", "currency": "USD",
            "tier": "ELIGIBLE", "score": 82.2, "as_of": "2026Q3",
            "source": "manual", "max_entry": 300.0, "demo": False,
        }],
    }
    doc.update(over)
    return doc


class ValidateTests(unittest.TestCase):
    def test_valid_doc_passes(self):
        ok, problems = universe.validate(good_doc())
        self.assertTrue(ok, problems)

    def test_expired_is_rejected(self):
        ok, problems = universe.validate(good_doc(valid_until="2020-01-01"))
        self.assertFalse(ok)
        self.assertTrue(any("过期" in p for p in problems))

    def test_unsigned_is_rejected(self):
        ok, problems = universe.validate(good_doc(approved_by=""))
        self.assertFalse(ok)

    def test_bad_schema_is_rejected(self):
        ok, _ = universe.validate(good_doc(schema="something/else"))
        self.assertFalse(ok)

    def test_demo_entry_is_rejected(self):
        doc = good_doc()
        doc["items"][0]["demo"] = True
        ok, _ = universe.validate(doc)
        self.assertFalse(ok)

    def test_invalid_max_entry_is_rejected(self):
        doc = good_doc()
        doc["items"][0]["max_entry"] = 0
        ok, problems = universe.validate(doc)
        self.assertFalse(ok)
        self.assertTrue(any("max_entry 无效" in p for p in problems))

    def test_nan_max_entry_is_rejected(self):
        doc = good_doc()
        doc["items"][0]["max_entry"] = float("nan")
        ok, problems = universe.validate(doc)
        self.assertFalse(ok)
        self.assertTrue(any("NaN 或 Inf" in p for p in problems))

    def test_inf_max_entry_is_rejected(self):
        doc = good_doc()
        doc["items"][0]["max_entry"] = float("inf")
        ok, problems = universe.validate(doc)
        self.assertFalse(ok)
        self.assertTrue(any("NaN 或 Inf" in p for p in problems))

    def test_invalid_valid_until_format_is_rejected(self):
        ok, problems = universe.validate(good_doc(valid_until="invalid-date-format"))
        self.assertFalse(ok)
        self.assertTrue(any("格式无效" in p for p in problems))


class GateTests(unittest.TestCase):
    def setUp(self):
        self.amap = universe.universe_allowed(good_doc())

    def test_unlisted_symbol_blocked(self):
        ok, why = universe.universe_gate("MSFT", "cash-put", 200, self.amap)
        self.assertFalse(ok)
        self.assertIn("不在准入白名单", why)

    def test_strike_above_max_entry_blocked(self):
        ok, why = universe.universe_gate("AAPL", "cash-put", 325, self.amap)
        self.assertFalse(ok)
        self.assertIn("愿意接货价", why)

    def test_strike_within_max_entry_allowed(self):
        ok, _ = universe.universe_gate("AAPL", "cash-put", 290, self.amap)
        self.assertTrue(ok)

    def test_sell_call_not_constrained_by_max_entry(self):
        ok, _ = universe.universe_gate("AAPL", "sell-call", 400, self.amap)
        self.assertTrue(ok)

    def test_meta_keeps_signature_and_items(self):
        m = universe.meta(good_doc())
        ok, problems = universe.validate(m)
        self.assertTrue(ok, problems)


if __name__ == "__main__":
    unittest.main()
