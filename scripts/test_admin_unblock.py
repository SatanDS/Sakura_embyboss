#!/usr/bin/env python3
"""Explicit administrator actions clear ledger bans without granting account time."""
import ast
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import test_legacy_code_entitlements as fixtures

HANDLERS = {
    "panel": ("bot/modules/panel/kk.py", "kk_user_ban"),
    "uinfo": ("bot/modules/extra/create.py", "uinfo_enable_cb"),
}


class AdminUnblockTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, handler, reason, *, expired=False, remote_success=True):
        fixture = fixtures.LegacyCodeEntitlementTests()
        fixture.setUp()
        try:
            if expired:
                fixture.expire_legacy()
            else:
                self.assertEqual(fixture.renew("DuSheng90Renew-test", 7)["status"], "ok")
            with fixture.sessions.begin() as session:
                user = session.get(fixture.Emby, 7)
                original_expiry = user.ex
                fixture.ent.mark_block(session, user, reason, fixtures.NOW)
                original_periods = [
                    (p.starts_at, p.ends_at, p.tier, p.source_key)
                    for p in session.query(fixture.ent.AccountPeriod).order_by(fixture.ent.AccountPeriod.id)
                ]
            with fixture.sessions() as session:
                user = session.get(fixture.Emby, 7)
            emby = SimpleNamespace(
                emby_change_policy=AsyncMock(return_value=remote_success),
            )
            call = SimpleNamespace(
                from_user=SimpleNamespace(id=1, first_name="Admin"), data="user_ban-7",
                answer=AsyncMock(), message=SimpleNamespace(edit_reply_markup=AsyncMock()),
            )
            namespace = dict(
                judge_admins=lambda tg: True, admins=(), LOGGER=Mock(), emby=emby,
                bot=SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(first_name="Viewer")),
                                    send_message=AsyncMock()),
                sql_get_emby=lambda *args, **kwargs: user, sql_get_emby2=Mock(return_value=None),
                sql_update_emby=fixture.update, Emby=fixture.Emby,
                editMessage=AsyncMock(), callAnswer=AsyncMock(), uinfo_ikb=lambda *args: None,
            )
            path, name = HANDLERS[handler]
            function = next(node for node in ast.parse((ROOT / path).read_text(encoding="utf-8")).body
                            if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
            function.decorator_list = []
            exec(compile(ast.Module(body=[function], type_ignores=[]), str(ROOT / path), "exec"), namespace)
            if handler == "uinfo":
                call.data = "uinfo_enable-emby-7"
            await namespace[name](None, call)
            emby.emby_change_policy.assert_awaited_once()
            with fixture.sessions() as session:
                user = session.get(fixture.Emby, 7)
                entitlement = fixture.ent.resolve_entitlement(session, user, fixtures.NOW)
                self.assertEqual(user.ex, original_expiry)
                periods = [(p.starts_at, p.ends_at, p.tier, p.source_key)
                           for p in session.query(fixture.ent.AccountPeriod).order_by(fixture.ent.AccountPeriod.id)]
                self.assertEqual(periods, original_periods)
                if not remote_success:
                    self.assertEqual(entitlement.blocked_reason, reason)
                    self.assertEqual(user.lv, "c")
                elif expired:
                    self.assertEqual(entitlement.blocked_reason, "expiry")
                    self.assertFalse(entitlement.allowed)
                    self.assertEqual(user.lv, "c")
                else:
                    self.assertIsNone(entitlement.blocked_reason)
                    self.assertTrue(entitlement.allowed)
                    self.assertEqual(entitlement.current_tier, "normal")
                    self.assertEqual(user.lv, "b")
                    self.assertIsNone(user.disabled_at)
        finally:
            fixture.doCleanups()

    async def test_admin_handlers_clear_admin_and_policy_bans(self):
        for handler in HANDLERS:
            for reason in ("admin", "policy"):
                with self.subTest(handler=handler, reason=reason):
                    await self.exercise(handler, reason)

    async def test_admin_handlers_do_not_extend_expired_subscriptions(self):
        for handler in HANDLERS:
            with self.subTest(handler=handler):
                await self.exercise(handler, "policy", expired=True)

    async def test_remote_enable_failure_preserves_ban(self):
        for handler in HANDLERS:
            with self.subTest(handler=handler):
                await self.exercise(handler, "admin", remote_success=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
