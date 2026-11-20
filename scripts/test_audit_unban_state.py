#!/usr/bin/env python3
"""Offline checks for the read-only bulk-unban audit."""
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.audit_unban_state import audit


NOW = datetime(2026, 11, 20, 20)


def account(**changes):
    row = {"id": "emby-7", "name": "u7", "tg": 7, "source": "emby", "lv": "a",
           "ex": NOW + timedelta(days=30), "managed": True, "blocked_reason": None,
           "periods": [{"id": 1, "starts_at": NOW - timedelta(days=1),
                        "ends_at": NOW + timedelta(days=30), "tier": "vip", "kind": "month"}]}
    return {**row, **changes}


def remote(user_id="emby-7", **policy):
    return {"Id": user_id, "Name": "u7",
            "Policy": {"IsDisabled": False, "IsAdministrator": False, **policy}}


class UnbanAuditTests(unittest.TestCase):
    def test_active_vip_with_stale_expiry_reason_is_not_flagged(self):
        report = audit([account(blocked_reason="expiry")], [remote()], NOW)
        self.assertEqual(report["rows"], [])
        self.assertEqual(report["summary"]["active_vip"], 1)
        self.assertEqual(report["summary"]["needs_review"], 0)

    def test_manual_blocks_still_enabled_are_flagged(self):
        for reason in ("policy", "admin", "activity", "legacy_unknown"):
            with self.subTest(reason=reason):
                report = audit([account(lv="c", blocked_reason=reason)], [remote()], NOW)
                row = report["rows"][0]
                self.assertEqual(row["tg"], 7)
                self.assertTrue(row["requires_review"])
                self.assertFalse(row["remote_is_disabled"])
                self.assertEqual(row["blocked_reason"], reason)
                self.assertIn("blocked_but_enabled", row["issues"])

    def test_expired_managed_legacy_and_emby2_accounts_are_distinguished(self):
        old = NOW - timedelta(days=5)
        period = {"id": 1, "starts_at": old - timedelta(days=30),
                  "ends_at": old, "tier": "vip", "kind": "legacy"}
        for source, managed in (("emby", True), ("emby", False), ("emby2", False)):
            with self.subTest(source=source, managed=managed):
                report = audit([account(source=source, managed=managed, lv="b", ex=old,
                                        blocked_reason="expiry" if managed else None,
                                        periods=[period] if managed else [])], [remote()], NOW)
                row = report["rows"][0]
                self.assertEqual(row["source"], source)
                self.assertTrue(row["requires_review"])
                self.assertIn("expired_but_enabled", row["issues"])
                if managed:
                    self.assertEqual(row["last_period_tier"], "vip")
                    self.assertEqual(row["last_period_end"], old.isoformat())

    def test_unknown_accounts_cannot_be_classified_from_current_state(self):
        report = audit([], [remote("unknown"), remote("admin", IsAdministrator=True)], NOW)
        self.assertEqual(report["summary"]["unknown"], 1)
        self.assertEqual(report["summary"]["remote_administrators"], 1)
        row = report["rows"][0]
        self.assertEqual(row["source"], "unknown")
        self.assertTrue(row["requires_review"])
        self.assertIn("no_database_record", row["issues"])

    def test_future_raw_expiry_with_old_ledger_requires_redemption_review(self):
        old = NOW - timedelta(days=5)
        period = {"id": 1, "starts_at": old - timedelta(days=30),
                  "ends_at": old, "tier": "vip", "kind": "legacy"}
        report = audit([account(blocked_reason="expiry", periods=[period])], [remote()], NOW)
        row = report["rows"][0]
        self.assertIn("projection_ledger_mismatch", row["issues"])
        self.assertNotIn("expired_but_enabled", row["issues"])
        self.assertNotIn("should_be_disabled", row)

    def test_redemption_after_expiry_is_reviewed_when_bulk_overwrote_raw_expiry(self):
        old = NOW - timedelta(days=5)
        period = {"id": 1, "starts_at": old - timedelta(days=30),
                  "ends_at": old, "tier": "vip", "kind": "legacy"}
        codes = [{"kind": "renew", "us": 90, "usedtime": NOW - timedelta(minutes=2)}]
        report = audit([account(lv="c", ex=old, blocked_reason="expiry", periods=[period],
                                redemptions=codes)], [remote()], NOW)
        row = report["rows"][0]
        self.assertIn("redeemed_renewal_requires_review", row["issues"])
        self.assertNotIn("expired_but_enabled", row["issues"])

    def test_report_does_not_copy_password_token_or_policy_fields(self):
        secret = "must-never-appear"
        db = account(lv="c", blocked_reason="policy", pwd=secret, db_pwd=secret)
        user = {**remote(), "AccessToken": secret, "Password": secret}
        user["Policy"]["PrivateApiToken"] = secret
        report = audit([db], [user], NOW)
        output = json.dumps(report)
        self.assertNotIn(secret, output)
        self.assertNotIn("AccessToken", output)
        self.assertNotIn("PrivateApiToken", output)
        self.assertNotIn("pwd", output)
        self.assertNotIn("should_be_disabled", output)

    def test_only_latest_redemption_evidence_is_reported_without_code(self):
        old = NOW - timedelta(days=1)
        secret = "consumed-code-must-never-appear"
        records = [
            {"kind": "renew", "us": 30, "usedtime": old, "code": secret},
            {"kind": "whitelist", "us": 0, "usedtime": NOW, "code": secret},
            {"kind": "renew", "us": 90, "usedtime": NOW, "code": secret},
            {"kind": "whitelist", "us": 0, "usedtime": old, "code": secret},
        ]
        report = audit([account(lv="c", blocked_reason="policy", redemptions=records)],
                       [remote()], NOW)
        row = report["rows"][0]
        self.assertEqual(row["last_renewal_days"], 90)
        self.assertEqual(row["last_renewal_at"], NOW.isoformat())
        self.assertEqual(row["last_whitelist_at"], NOW.isoformat())
        self.assertNotIn(secret, json.dumps(report))

    def test_rows_are_limited_without_hiding_total_review_count(self):
        report = audit([], [remote(str(i)) for i in range(105)], NOW)
        self.assertEqual(len(report["rows"]), 100)
        self.assertEqual(report["summary"]["needs_review"], 105)
        self.assertEqual(report["summary"]["rows_omitted"], 5)


if __name__ == "__main__":
    unittest.main()
