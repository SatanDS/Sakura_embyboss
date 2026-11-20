#!/usr/bin/env python3
"""Recovery checks with the real SQLite entitlement ledger and fake HTTP only."""
import copy
import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import test_legacy_code_entitlements as legacy_tests
from sqlalchemy import Column, DateTime, Integer, String

USED = datetime(2026, 11, 20, 15, 16, 24)
NOW = datetime(2026, 11, 20, 20)
END = datetime(2027, 2, 18, 15, 16, 24)
RESTORE_ID = "0" * 31 + "1"
EXPIRED_IDS = [f"{i:032x}" for i in range(10, 32)]
MANUAL = [{"id": f"{i:032x}", "name": f"manual-{i}"} for i in (40, 41)]


def plan():
    return {"restore_renewal": {"id": RESTORE_ID, "tg": 7, "used_at": USED.isoformat(),
                                "days": 90, "tier": "vip", "expires_at": END.isoformat()},
            "disable_expired": EXPIRED_IDS[:], "disable_unmanaged": copy.deepcopy(MANUAL)}


class FakeResponse:
    def __init__(self, status, data=None):
        self.status_code, self.data = status, data

    def json(self):
        return copy.deepcopy(self.data)


class FakeHTTP:
    def __init__(self, users):
        self.users = {u["Id"]: copy.deepcopy(u) for u in users}
        self.writes = []
        self.trust_env = True

    def get(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False and kwargs["timeout"] == (5, 15)
        return FakeResponse(200, self.users[url.rsplit("/", 1)[1]])

    def post(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False and kwargs["timeout"] == (5, 15)
        user_id = url.split("/")[-2]
        original = self.users[user_id]["Policy"]
        policy = kwargs["json"]
        assert {k: v for k, v in original.items() if k != "IsDisabled"} == {
            k: v for k, v in policy.items() if k != "IsDisabled"}
        self.users[user_id]["Policy"] = copy.deepcopy(policy)
        self.writes.append(user_id)
        return FakeResponse(204)

    def close(self):
        pass


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        legacy_tests.LegacyCodeEntitlementTests.setUp(self)
        self.recovery = importlib.import_module("scripts.recover_unban")
        ns = {"Base": self.base, "Column": Column, "String": String,
              "DateTime": DateTime, "Integer": Integer}
        legacy_tests.load_definitions("bot/sql_helper/sql_emby2.py", ns, {"Emby2"})
        self.Emby2 = ns["Emby2"]
        self.base.metadata.create_all(self.engine)
        old = datetime(2026, 11, 7)
        with self.sessions.begin() as session:
            user = session.get(self.Emby, 7)
            user.embyid, user.lv, user.ex = RESTORE_ID, "a", old
            self.ent.ensure_legacy_period(session, user, NOW)
            user.lv = "c"
            session.get(self.ent.AccountEntitlement, 7).blocked_reason = "expiry"
            code = session.get(self.Code, "DuSheng90Renew-test")
            code.used, code.usedtime = 7, USED
            for i, user_id in enumerate(EXPIRED_IDS, 100):
                user = self.Emby(tg=i, embyid=user_id, name=f"expired-{i}", lv="a",
                                 cr=old - timedelta(days=30), ex=old)
                session.add(user)
                session.flush()
                self.ent.ensure_legacy_period(session, user, NOW)
                user.lv = "c"
                session.get(self.ent.AccountEntitlement, i).blocked_reason = "expiry"
        self.runtime = types.SimpleNamespace(Session=self.sessions, Emby=self.Emby,
                                              Emby2=self.Emby2, Code=self.Code, ent=self.ent)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {"db_backup_dir": self.temp.name, "emby_url": "http://emby:8096",
                       "emby_api": "test-token"}
        self.users = [{"Id": user_id, "Name": f"remote-{i}",
                       "Policy": {"IsAdministrator": False, "IsDisabled": False,
                                  "EnableContentDeletion": False, "EnableAllFolders": False,
                                  "EnabledFolders": ["one"], "MaxActiveSessions": 2}}
                      for i, user_id in enumerate([RESTORE_ID] + EXPIRED_IDS)]
        self.users += [{"Id": item["id"], "Name": item["name"],
                        "Policy": {"IsAdministrator": False, "IsDisabled": False}}
                       for item in MANUAL]
        self.http = FakeHTTP(self.users)

    def rows(self):
        with self.sessions() as session:
            return [{"id": u.embyid, "tg": u.tg, "name": u.name, "source": "emby",
                     "managed": True, "lv": u.lv, "ex": u.ex, "disabled_at": u.disabled_at,
                     "blocked_reason": session.get(self.ent.AccountEntitlement, u.tg).blocked_reason,
                     "periods": [{"id": p.id, "starts_at": p.starts_at, "ends_at": p.ends_at,
                                  "tier": p.tier, "kind": p.kind}
                                 for p in session.query(self.ent.AccountPeriod).filter_by(tg=u.tg)],
                     "redemptions": [{"kind": "renew", "us": 90, "usedtime": USED}]
                                    if u.tg == 7 else []}
                    for u in session.query(self.Emby)]

    def apply(self):
        normalized = self.recovery.validate_plan(plan())
        with patch.object(self.recovery, "load_runtime", return_value=self.runtime), \
                patch("requests.Session", return_value=self.http), contextlib.redirect_stdout(io.StringIO()):
            return self.recovery.apply_plan(self.config, normalized, self.rows(), self.users, NOW)

    def test_exact_90_day_restore_and_replay_never_credit_twice(self):
        result = self.apply()
        self.assertEqual(result["failed"], 0)
        with self.sessions() as session:
            user = session.get(self.Emby, 7)
            self.assertEqual((user.lv, user.ex), ("a", END))
            self.assertEqual(session.query(self.ent.AccountPeriod).filter_by(
                source_key="legacy-code:DuSheng90Renew-test").count(), 1)
            code = session.get(self.Code, "DuSheng90Renew-test")
            self.assertEqual((code.used, code.usedtime), (7, USED))
        result = self.apply()
        self.assertEqual(result["failed"], 0)
        with self.sessions() as session:
            self.assertEqual(session.get(self.Emby, 7).ex, END)
            self.assertEqual(session.query(self.ent.AccountPeriod).filter_by(tg=7).count(), 2)
        self.assertEqual(len(self.http.writes), 24)
        self.assertFalse(self.http.users[RESTORE_ID]["Policy"]["IsDisabled"])

    def test_preview_does_not_import_bot_or_write(self):
        with patch.object(self.recovery, "load_runtime", side_effect=AssertionError("import")):
            result = self.recovery.preview(self.recovery.validate_plan(plan()), self.rows(), self.users, NOW)
        self.assertEqual(result["counts"], {"restore_renewal": 1, "disable_expired": 22,
                                            "disable_unmanaged": 2})
        self.assertEqual(self.http.writes, [])
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_manual_admin_policy_and_unknown_bans_never_clear(self):
        for reason in ("admin", "policy", "activity", "legacy_unknown", None):
            with self.subTest(reason=reason):
                with self.sessions.begin() as session:
                    session.get(self.ent.AccountEntitlement, 7).blocked_reason = reason
                result = self.apply()
                self.assertGreater(result["failed"], 0)
                with self.sessions() as session:
                    self.assertEqual(session.query(self.ent.AccountPeriod).filter_by(tg=7).count(), 1)
                    self.assertEqual(session.get(self.Emby, 7).lv, "c")

    def test_wrong_used_code_time_or_multiple_matching_codes_reject_restore(self):
        with self.sessions.begin() as session:
            session.get(self.Code, "DuSheng90Renew-test").usedtime = USED + timedelta(seconds=1)
        self.assertGreater(self.apply()["failed"], 0)
        with self.sessions.begin() as session:
            session.get(self.Code, "DuSheng90Renew-test").usedtime = USED
            session.add(self.Code(code="DuSheng90Renew-other", tg=1, us=90, used=7, usedtime=USED))
        self.assertGreater(self.apply()["failed"], 0)
        with self.sessions() as session:
            self.assertEqual(session.query(self.ent.AccountPeriod).filter_by(tg=7).count(), 1)

    def test_fresh_renewal_during_apply_is_skipped_before_remote_disable(self):
        original = self.recovery.private_backup
        def backup_then_renew(*args):
            path = original(*args)
            with self.sessions.begin() as session:
                user = session.get(self.Emby, 100)
                self.ent.append_days(session, user, 30, "new-payment", NOW, tier="vip")
            return path
        with patch.object(self.recovery, "private_backup", side_effect=backup_then_renew):
            result = self.apply()
        self.assertEqual(result["failed"], 0)
        self.assertFalse(self.http.users[EXPIRED_IDS[0]]["Policy"]["IsDisabled"])
        self.assertNotIn(EXPIRED_IDS[0], self.http.writes)

    def test_unmatched_consumed_renewal_is_not_disabled(self):
        with self.sessions.begin() as session:
            session.add(self.Code(code="DuSheng30Renew-race", tg=1, us=30,
                                  used=100, usedtime=NOW - timedelta(minutes=1)))
        self.assertEqual(self.apply()["failed"], 0)
        self.assertFalse(self.http.users[EXPIRED_IDS[0]]["Policy"]["IsDisabled"])

    def test_backup_failure_means_no_database_or_http_changes(self):
        with patch.object(self.recovery, "private_backup", side_effect=OSError("private")):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(self.http.writes, [])
        with self.sessions() as session:
            self.assertEqual(session.query(self.ent.AccountPeriod).filter_by(tg=7).count(), 1)

    def test_backup_excludes_password_tokens_and_consumed_code(self):
        self.apply()
        files = list(Path(self.temp.name).glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        content = files[0].read_text(encoding="utf-8")
        self.assertNotIn("test-token", content)
        self.assertNotIn("DuSheng90Renew-test", content)
        records = [json.loads(line) for line in content.splitlines()]
        data = records[0]
        self.assertEqual(data["remote_users"][0]["Policy"], self.users[0]["Policy"])
        restored = next(r for r in records[1:] if r["action"] == "restore_renewal")
        self.assertEqual(restored["database"]["entitlement"]["blocked_reason"], "expiry")
        self.assertIn("revision", restored["database"]["entitlement"])
        self.assertEqual(len(restored["database"]["periods"]), 1)
        self.assertEqual(restored["database"]["redemptions"][0]["usedtime"], USED.isoformat())
        self.assertIn("code_sha256", restored["database"]["redemptions"][0])
        if os.name == "posix":
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)

    def test_locked_before_image_fsync_failure_prevents_each_action_write(self):
        with patch.object(self.recovery, "append_backup", side_effect=OSError("private")):
            result = self.apply()
        self.assertEqual(result["failed"], 25)
        self.assertEqual(self.http.writes, [])
        with self.sessions() as session:
            self.assertEqual(session.query(self.ent.AccountPeriod).filter_by(tg=7).count(), 1)

    def test_database_snapshot_excludes_password_and_raw_code_sources(self):
        with self.sessions.begin() as session:
            session.get(self.Emby, 7).pwd = "private-password"
        self.apply()
        content = next(Path(self.temp.name).glob("*.jsonl")).read_text(encoding="utf-8")
        self.assertNotIn("private-password", content)
        self.assertNotIn("legacy:7", content)
        self.assertIn("source_sha256", content)

    def test_fresh_locked_before_image_keeps_only_policy_and_minimal_database(self):
        original = self.recovery.private_backup
        def backup_then_change(*args):
            path = original(*args)
            with self.sessions.begin() as session:
                state = session.get(self.ent.AccountEntitlement, 7)
                state.revision, state.active_tier = 17, "normal"
                session.get(self.Emby, 7).disabled_at = NOW
            self.http.users[RESTORE_ID]["AccessToken"] = "never-copy-access-token"
            return path
        with patch.object(self.recovery, "private_backup", side_effect=backup_then_change):
            self.assertEqual(self.apply()["failed"], 0)
        content = next(Path(self.temp.name).glob("*.jsonl")).read_text(encoding="utf-8")
        records = [json.loads(line) for line in content.splitlines()]
        before = records[1]["database"]
        self.assertEqual((before["entitlement"]["revision"], before["entitlement"]["active_tier"]),
                         (17, "normal"))
        self.assertEqual(before["emby"]["disabled_at"], NOW.isoformat())
        self.assertNotIn("never-copy-access-token", content)

    def test_edited_replay_period_cannot_credit_another_90_days(self):
        self.assertEqual(self.apply()["failed"], 0)
        with self.sessions.begin() as session:
            period = session.query(self.ent.AccountPeriod).filter_by(
                source_key="legacy-code:DuSheng90Renew-test").one()
            period.ends_at = datetime(2027, 2, 19, 15, 16, 24)
        self.assertEqual(self.apply()["failed"], 1)
        with self.sessions() as session:
            self.assertEqual(session.query(self.ent.AccountPeriod).filter_by(tg=7).count(), 2)

    def test_restore_post_timeout_rolls_back_ledger_and_reports_partial(self):
        self.http.users[RESTORE_ID]["Policy"]["IsDisabled"] = True
        real_post = self.http.post
        def timeout_only_restore(url, **kwargs):
            if RESTORE_ID in url:
                raise TimeoutError("must-not-print-test-token")
            return real_post(url, **kwargs)
        with patch.object(self.http, "post", side_effect=timeout_only_restore):
            result = self.apply()
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["actions"][0]["error_type"], "TimeoutError")
        self.assertEqual(result["actions"][0]["stage"], "set_disabled")
        with self.sessions() as session:
            self.assertEqual(session.query(self.ent.AccountPeriod).filter_by(tg=7).count(), 1)
            self.assertEqual(session.get(self.Emby, 7).lv, "c")
        self.assertEqual(len(self.http.writes), 24)

    def test_fresh_admin_or_malformed_policy_aborts_before_any_write(self):
        for changes in ({"IsAdministrator": True}, {"IsAdministrator": None}, {"IsDisabled": 0}):
            with self.subTest(changes=changes):
                self.http.users[RESTORE_ID]["Policy"] = {**self.users[0]["Policy"], **changes}
                with self.assertRaises(ValueError):
                    self.apply()
                self.assertEqual(self.http.writes, [])

    def test_unmanaged_name_or_database_match_is_rejected(self):
        self.http.users[MANUAL[0]["id"]]["Name"] = "renamed"
        with self.assertRaises(ValueError):
            self.apply()
        self.http.users[MANUAL[0]["id"]]["Name"] = MANUAL[0]["name"]
        with self.sessions.begin() as session:
            session.add(self.Emby2(embyid=MANUAL[0]["id"], name=MANUAL[0]["name"], lv="b"))
        result = self.apply()
        self.assertGreater(result["failed"], 0)
        self.assertFalse(self.http.users[MANUAL[0]["id"]]["Policy"]["IsDisabled"])

    def test_plan_rejects_conflicts_unknown_keys_bad_dates_and_changed_scope(self):
        invalid = []
        p = plan(); p["extra"] = True; invalid.append(p)
        p = plan(); p["restore_renewal"]["expires_at"] = "2027-02-19T15:16:24"; invalid.append(p)
        p = plan(); p["restore_renewal"]["used_at"] += "+08:00"; invalid.append(p)
        p = plan(); p["disable_expired"][0] = RESTORE_ID; invalid.append(p)
        p = plan(); p["disable_expired"].pop(); invalid.append(p)
        p = plan(); p["disable_unmanaged"][0]["id"] = "../user"; invalid.append(p)
        p = plan(); p["restore_renewal"]["tg"] = True; invalid.append(p)
        for p in invalid:
            with self.subTest(p=p), self.assertRaises(ValueError):
                self.recovery.validate_plan(p)


if __name__ == "__main__":
    unittest.main()
