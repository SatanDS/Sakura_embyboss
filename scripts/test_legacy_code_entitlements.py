#!/usr/bin/env python3
"""Legacy redemption regressions using real SQLite models without Bot services."""
import ast
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from unittest.mock import Mock, patch

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 20, 15, 16)


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.replace(tzinfo=tz)


def load_definitions(path, namespace, names=None):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    nodes = [node for node in tree.body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef))
             and (names is None or node.name in names)]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / path), "exec"), namespace)


class LegacyCodeEntitlementTests(unittest.TestCase):
    def setUp(self):
        self.base = declarative_base()
        self.engine = create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        sql = types.ModuleType("bot.sql_helper")
        sql.Base, sql.Session = self.base, self.sessions
        bot = types.ModuleType("bot")
        bot.__path__ = [str(ROOT / "bot")]
        payments = types.ModuleType("bot.payments")
        payments.__path__ = [str(ROOT / "bot/payments")]
        self.modules = patch.dict(sys.modules, {
            "bot": bot, "bot.sql_helper": sql, "bot.payments": payments,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location(
            "bot.payments.entitlements", ROOT / "bot/payments/entitlements.py")
        self.ent = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.ent
        spec.loader.exec_module(self.ent)
        self.ent.china_now = lambda: NOW
        namespace = {"Base": self.base, "Session": self.sessions, "Column": Column,
                     "BigInteger": BigInteger, "DateTime": DateTime, "Integer": Integer,
                     "String": String, "datetime": FrozenDatetime, "LOGGER": Mock()}
        load_definitions("bot/sql_helper/sql_emby.py", namespace, {"Emby", "sql_update_emby"})
        load_definitions("bot/sql_helper/sql_code.py", namespace, {"Code"})
        self.Emby, self.Code = namespace["Emby"], namespace["Code"]
        self.update = namespace["sql_update_emby"]
        namespace.update(vars(self.ent))
        namespace.update(Session=self.sessions, Emby=self.Emby, Code=self.Code,
                         datetime=FrozenDatetime, timedelta=timedelta)
        load_definitions("bot/func_helper/utils.py", namespace, {"is_subscription_active"})
        load_definitions("bot/modules/commands/exchange.py", namespace)
        self.renew = namespace["_redeem_renew_code_atomic"]
        self.whitelist = namespace["_redeem_whitelist_code_atomic"]
        line_namespace = {"Optional": Optional, "Emby": self.Emby,
                          "Emby2": type("Emby2", (), {}), "datetime": FrozenDatetime,
                          "config": types.SimpleNamespace(payments=types.SimpleNamespace())}
        load_definitions("bot/web/api/webhook/line_report.py", line_namespace,
                         {"effective_line_entitlement", "is_user_whitelisted"})
        self.effective = line_namespace["effective_line_entitlement"]
        self.is_whitelisted = line_namespace["is_user_whitelisted"]
        self.base.metadata.create_all(self.engine)
        with self.sessions.begin() as session:
            session.add(self.Emby(tg=7, embyid="emby-7", name="u7", lv="b",
                                  cr=NOW - timedelta(days=365), ex=NOW - timedelta(days=1)))
            session.add_all([
                self.Code(code="DuSheng90Renew-test", tg=1, us=90),
                self.Code(code="DuShengWhitelist-test", tg=1, us=0),
            ])

    def expire_legacy(self):
        self.assertTrue(self.update(self.Emby.tg == 7, lv="c", disabled_at=NOW,
                                    entitlement_block_reason="expiry"))
        with self.sessions() as session:
            self.assertEqual(session.get(self.ent.AccountEntitlement, 7).blocked_reason, "expiry")

    def result(self):
        with self.sessions() as session:
            user = session.get(self.Emby, 7)
            return self.ent.resolve_entitlement(session, user, NOW)

    def assert_code_unused(self, code):
        with self.sessions() as session:
            self.assertIsNone(session.get(self.Code, code).used)
            self.assertIsNone(session.get(self.Code, code).usedtime)

    def test_expired_legacy_renewal_restores_authoritative_period(self):
        self.expire_legacy()
        redeemed = self.renew("DuSheng90Renew-test", 7)
        self.assertEqual(redeemed["status"], "ok")
        self.assertEqual(redeemed["ex_new"], NOW + timedelta(days=90))
        resolved = self.result()
        self.assertTrue(resolved.allowed)
        self.assertEqual(resolved.current_end, redeemed["ex_new"])
        with self.sessions() as session:
            user = session.get(self.Emby, 7)
            self.assertEqual(user.lv, "b")
            self.assertIsNone(user.disabled_at)
            self.assertEqual(self.effective(user).lv, "b")

    def test_expired_whitelist_renewal_restores_previous_vip_tier_without_another_code(self):
        with self.sessions.begin() as session:
            session.get(self.Emby, 7).lv = "a"
        self.expire_legacy()
        redeemed = self.renew("DuSheng90Renew-test", 7)
        self.assertEqual(redeemed["status"], "ok")
        self.assertEqual(self.result().current_tier, "vip")
        self.assertEqual(self.result().final_expiry, NOW + timedelta(days=90))
        self.assert_code_unused("DuShengWhitelist-test")
        with self.sessions() as session:
            self.assertTrue(self.is_whitelisted(self.effective(session.get(self.Emby, 7))))

    def test_explicit_normal_period_does_not_inherit_expired_vip(self):
        with self.sessions.begin() as session:
            session.get(self.Emby, 7).lv = "a"
        self.expire_legacy()
        with self.sessions.begin() as session:
            user = session.get(self.Emby, 7)
            self.ent.append_days(session, user, 30, "normal-renewal", NOW, tier="normal")
        self.assertEqual(self.result().current_tier, "normal")

    def test_expiry_renewal_then_whitelist_allows_vip_line(self):
        self.expire_legacy()
        self.assertEqual(self.renew("DuSheng90Renew-test", 7)["status"], "ok")
        self.assertEqual(self.whitelist("DuShengWhitelist-test", 7)["status"], "ok")
        with self.sessions() as session:
            projected = self.effective(session.get(self.Emby, 7))
            self.assertEqual(projected.lv, "a")
            self.assertTrue(self.is_whitelisted(projected))
        self.assertEqual(self.result().current_tier, "vip")

    def test_zero_day_admin_renewal_repairs_already_redeemed_legacy_account(self):
        self.expire_legacy()
        redeemed_expiry = NOW + timedelta(days=90)
        redeemed_at = NOW - timedelta(minutes=2)
        with self.sessions.begin() as session:
            user = session.get(self.Emby, 7)
            user.lv, user.ex = "a", redeemed_expiry
            for code in session.query(self.Code).all():
                code.used, code.usedtime = 7, redeemed_at
        with self.sessions() as session:
            self.assertEqual(self.effective(session.get(self.Emby, 7)).lv, "c")

        self.assertTrue(self.update(self.Emby.tg == 7, lv="a", ex=redeemed_expiry))
        self.assertTrue(self.update(self.Emby.tg == 7, lv="a"))
        resolved = self.result()
        self.assertTrue(resolved.allowed)
        self.assertEqual(resolved.current_tier, "vip")
        self.assertEqual(resolved.current_end, redeemed_expiry)
        self.assertEqual(resolved.final_expiry, redeemed_expiry)
        with self.sessions() as session:
            user = session.get(self.Emby, 7)
            self.assertEqual(user.ex, redeemed_expiry)
            self.assertTrue(self.is_whitelisted(self.effective(user)))
            period_count = session.query(self.ent.AccountPeriod).count()
            for code in session.query(self.Code).all():
                self.assertEqual((code.used, code.usedtime), (7, redeemed_at))
        self.assertEqual(self.renew("DuSheng90Renew-test", 7)["status"], "used")
        self.assertEqual(self.whitelist("DuShengWhitelist-test", 7)["status"], "used")
        with self.sessions() as session:
            self.assertEqual(session.query(self.ent.AccountPeriod).count(), period_count)
        self.assertEqual(self.result().final_expiry, redeemed_expiry)

    def test_zero_day_admin_renewal_does_not_clear_manual_blocks(self):
        expiry = NOW + timedelta(days=90)
        for reason in ("admin", "policy"):
            with self.subTest(reason=reason):
                with self.sessions.begin() as session:
                    user = session.get(self.Emby, 7)
                    user.lv, user.ex = "b", expiry
                    self.ent.mark_block(session, user, reason, NOW)
                self.assertFalse(self.update(self.Emby.tg == 7, lv="b", ex=expiry))
                self.assertEqual(self.result().blocked_reason, reason)

                with self.sessions.begin() as session:
                    session.get(self.Emby, 7).lv = "a"
                self.assertTrue(self.update(self.Emby.tg == 7, lv="a", ex=expiry))
                self.assertFalse(self.update(self.Emby.tg == 7, lv="a"))
                resolved = self.result()
                self.assertFalse(resolved.allowed)
                self.assertEqual(resolved.blocked_reason, reason)
                with self.sessions() as session:
                    user = session.get(self.Emby, 7)
                    self.assertEqual((user.lv, user.ex), ("c", expiry))
                    self.assertEqual(self.effective(user).lv, "c")

    def test_whitelist_changes_only_current_queued_period(self):
        with self.sessions.begin() as session:
            user = session.get(self.Emby, 7)
            first = self.ent.append_months(session, user, "normal", 1, "paid-current", NOW)
            second = self.ent.append_months(session, user, "normal", 1, "paid-queued", NOW)
            expected_end = second.ends_at
            current_end = first.ends_at
        self.assertEqual(self.whitelist("DuShengWhitelist-test", 7)["status"], "ok")
        with self.sessions() as session:
            periods = session.query(self.ent.AccountPeriod).filter(
                self.ent.AccountPeriod.kind == "month").order_by(self.ent.AccountPeriod.starts_at).all()
            self.assertEqual([p.tier for p in periods], ["vip", "normal"])
            self.assertEqual(periods[0].ends_at, current_end)
            self.assertEqual(periods[1].ends_at, expected_end)
            self.assertTrue(self.is_whitelisted(self.effective(session.get(self.Emby, 7))))

    def test_renewal_appends_after_paid_queue_and_replay_adds_nothing(self):
        with self.sessions.begin() as session:
            user = session.get(self.Emby, 7)
            self.ent.append_months(session, user, "normal", 1, "paid-current", NOW)
            queued = self.ent.append_months(session, user, "vip", 1, "paid-queued", NOW)
            queued_end = queued.ends_at
        renewed = self.renew("DuSheng90Renew-test", 7)
        self.assertEqual(renewed["status"], "ok")
        self.assertEqual(self.result().final_expiry, queued_end + timedelta(days=90))
        self.assertEqual(self.renew("DuSheng90Renew-test", 7)["status"], "used")
        with self.sessions() as session:
            periods = session.query(self.ent.AccountPeriod).filter(
                self.ent.AccountPeriod.ends_at > NOW).order_by(self.ent.AccountPeriod.starts_at).all()
            self.assertEqual([p.tier for p in periods], ["normal", "vip", "vip"])
            self.assertEqual(periods[2].starts_at, queued_end)
            self.assertEqual(periods[2].ends_at, queued_end + timedelta(days=90))

    def test_expired_bans_reject_renewal_without_consuming_code(self):
        for reason in ("admin", "policy", "activity", "legacy_unknown"):
            with self.subTest(reason=reason):
                with self.sessions.begin() as session:
                    user = session.get(self.Emby, 7)
                    user.ex = NOW - timedelta(days=1)
                    user.lv = "b"
                    self.ent.mark_block(session, user, reason, NOW)
                    code = session.get(self.Code, "DuSheng90Renew-test")
                    code.used, code.usedtime = None, None
                self.assertEqual(self.renew("DuSheng90Renew-test", 7)["status"], "blocked")
                self.assert_code_unused("DuSheng90Renew-test")
                with self.sessions() as session:
                    self.assertEqual(session.get(self.Emby, 7).lv, "c")
                    self.assertEqual(session.get(self.ent.AccountEntitlement, 7).blocked_reason, reason)

    def test_future_expiry_bans_reject_whitelist_without_consuming_code(self):
        for reason in ("admin", "policy", "activity", "legacy_unknown"):
            with self.subTest(reason=reason):
                with self.sessions.begin() as session:
                    user = session.get(self.Emby, 7)
                    user.ex = NOW + timedelta(days=30)
                    user.lv = "b"
                    self.ent.mark_block(session, user, reason, NOW)
                    code = session.get(self.Code, "DuShengWhitelist-test")
                    code.used, code.usedtime = None, None
                self.assertEqual(self.whitelist("DuShengWhitelist-test", 7)["status"], "blocked")
                self.assert_code_unused("DuShengWhitelist-test")
                with self.sessions() as session:
                    self.assertEqual(session.get(self.Emby, 7).lv, "c")
                    self.assertEqual(session.get(self.ent.AccountEntitlement, 7).blocked_reason, reason)

    def test_unmanaged_disabled_account_is_not_implicitly_unblocked(self):
        with self.sessions.begin() as session:
            session.get(self.Emby, 7).lv = "c"
        self.assertEqual(self.renew("DuSheng90Renew-test", 7)["status"], "blocked")
        self.assert_code_unused("DuSheng90Renew-test")

    def test_unmanaged_disabled_future_subscription_cannot_redeem_whitelist(self):
        with self.sessions.begin() as session:
            user = session.get(self.Emby, 7)
            user.lv, user.ex = "c", NOW + timedelta(days=30)
        self.assertEqual(self.whitelist("DuShengWhitelist-test", 7)["status"], "blocked")
        self.assert_code_unused("DuShengWhitelist-test")

    def test_invalid_renewal_days_do_not_consume_code_or_change_account(self):
        for days in (0, -1, None):
            with self.subTest(days=days):
                with self.sessions.begin() as session:
                    code = session.get(self.Code, "DuSheng90Renew-test")
                    code.us, code.used, code.usedtime = days, None, None
                    session.get(self.Emby, 7).ex = NOW - timedelta(days=1)
                self.assertEqual(self.renew("DuSheng90Renew-test", 7)["status"], "invalid_code")
                self.assert_code_unused("DuSheng90Renew-test")
                with self.sessions() as session:
                    self.assertEqual(session.get(self.Emby, 7).ex, NOW - timedelta(days=1))


if __name__ == "__main__":
    unittest.main()
