"""Renewal messages reflect whether the remote account was actually enabled."""

import ast
import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


ROOT = Path(__file__).resolve().parents[1]


class RenewalFeedbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.messages = []
        self.result = dict(status="ok", issuer_tg=1, days=90,
                           ex_new=datetime(2027, 2, 18, 15, 16),
                           embyid="emby-test", restore_policy=True)
        self.user = SimpleNamespace(embyid="emby-test")
        self.emby = SimpleNamespace(emby_change_policy=AsyncMock(return_value=True))
        self.message = SimpleNamespace(from_user=SimpleNamespace(id=7, first_name="Viewer"),
                                       chat=SimpleNamespace(id=7))
        async def send(message, text, **kwargs):
            self.messages.append((text, kwargs))
            return True
        self.namespace = dict(
            _open=SimpleNamespace(stat=False, use_whitelist_code=True),
            get_user_lock=lambda tg: asyncio.Lock(), sql_get_emby=lambda **kwargs: self.user,
            _redeem_renew_code_atomic=lambda *args: self.result,
            _redeem_whitelist_code_atomic=lambda *args: self.result,
            is_whitelist_code=lambda code: "Whitelist" in code, is_renew_code=lambda code: "Renew" in code,
            emby=self.emby, bot=SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(first_name="Admin"))),
            sendMessage=send, LOGGER=Mock(),
        )
        function = next(node for node in ast.parse(
            (ROOT / "bot/modules/commands/exchange.py").read_text(encoding="utf-8")).body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "rgs_code")
        exec(compile(ast.Module(body=[function], type_ignores=[]), "exchange_feedback", "exec"), self.namespace)

    async def redeem(self, code="DuSheng90Renew-test"):
        await self.namespace["rgs_code"](None, self.message, code)

    async def test_failed_enable_keeps_credited_expiry_but_does_not_claim_restored(self):
        self.emby.emby_change_policy.return_value = False
        await self.redeem()
        text = "\n".join(message for message, _ in self.messages)
        self.assertIn("2027-02-18 15:16", text)
        self.assertIn("联系管理员", text)
        self.assertNotIn("已解封", text)

    async def test_enable_exception_is_reported_instead_of_success(self):
        self.emby.emby_change_policy.side_effect = RuntimeError("synthetic outage")
        await self.redeem()
        text = "\n".join(message for message, _ in self.messages)
        self.assertIn("联系管理员", text)
        self.assertNotIn("已解封", text)

    async def test_successful_enable_reports_restored_and_keeps_admin_receipt(self):
        await self.redeem()
        self.emby.emby_change_policy.assert_awaited_once_with(emby_id="emby-test", disable=False)
        self.assertTrue(any("已解封" in text for text, _ in self.messages))
        self.assertTrue(any(options.get("send") for _, options in self.messages))

    async def test_banned_renewal_and_whitelist_report_admin_action_without_enabling(self):
        self.result = {"status": "blocked"}
        for code in ("DuSheng90Renew-test", "DuShengWhitelist-test"):
            await self.redeem(code)
        self.emby.emby_change_policy.assert_not_awaited()
        self.assertTrue(all("联系管理员" in text for text, _ in self.messages))


if __name__ == "__main__":
    unittest.main(verbosity=2)
