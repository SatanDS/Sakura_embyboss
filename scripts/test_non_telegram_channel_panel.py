"""Offline behavior checks for the manual channel's administrator editor."""

import ast
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pyrogram import enums, filters
from pyrogram.types import ForceReply
from pyromod.exceptions import ListenerTimeout
from pyromod.helpers import ikb


ROOT = Path(__file__).resolve().parents[1]


def load_file(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NonTelegramPanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.helper = load_file("manual_channel_helper_test", "bot/func_helper/non_telegram_channel.py")
        schemas = load_file("manual_channel_schema_test", "bot/schemas/schemas.py")
        self.config = SimpleNamespace(owner=10, admins=[11], non_telegram_channel=schemas.NonTelegramChannel(
            enabled=True, url="https://support.example.com/emby", notice="original"))
        self.input = SimpleNamespace(text="new contact instructions", from_user=SimpleNamespace(id=10),
                                     reply_to_message_id=100)
        self.chat = SimpleNamespace(id=10, listen=AsyncMock(side_effect=lambda **kwargs: self.input))
        self.reply = AsyncMock(return_value=SimpleNamespace(id=100))
        self.call = SimpleNamespace(from_user=SimpleNamespace(id=10), data="non_tg_edit_notice",
                                    message=SimpleNamespace(chat=self.chat, reply=self.reply))
        self.env = dict(config=self.config, save_config=Mock(), LOGGER=Mock(), enums=enums,
                        filters=filters, ForceReply=ForceReply, ListenerTimeout=ListenerTimeout,
                        ikb=ikb, _editors=set(), callAnswer=AsyncMock(), editMessage=AsyncMock(return_value=True))
        for name in ("DEFAULT_NOTICE", "channel_enabled", "channel_notice", "channel_url",
                     "validate_channel_notice", "validate_channel_url"):
            self.env[name] = getattr(self.helper, name)
        source = ast.parse((ROOT / "bot/modules/panel/non_telegram_channel_panel.py").read_text(encoding="utf-8"))
        definitions = []
        for node in source.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                node.decorator_list = []
                definitions.append(node)
        exec(compile(ast.Module(body=definitions, type_ignores=[]), "manual_channel_panel", "exec"), self.env)

    async def action(self, data=None):
        if data:
            self.call.data = data
        return await self.env["non_telegram_channel_action"](None, self.call)

    async def test_non_admin_and_group_callbacks_cannot_read_or_write_configuration(self):
        for user_id, chat_id in ((99, 99), (10, -1001), (11, -1001)):
            self.call.from_user.id, self.chat.id = user_id, chat_id
            await self.env["non_telegram_channel_panel"](None, self.call)
            for action in ("non_tg_edit_notice", "non_tg_toggle", "non_tg_preview"):
                await self.action(action)
        self.env["save_config"].assert_not_called()
        self.chat.listen.assert_not_awaited()
        self.reply.assert_not_awaited()
        self.env["editMessage"].assert_not_awaited()

    async def test_long_configuration_renders_within_telegram_caption_limit(self):
        prefix = "https://support.example.com/"
        self.config.non_telegram_channel.url = prefix + "a" * (2048 - len(prefix))
        self.config.non_telegram_channel.notice = "😀" * 2000
        await self.env["non_telegram_channel_panel"](None, self.call)
        summary = self.env["editMessage"].call_args.args[1]
        self.assertLessEqual(len(summary.encode("utf-16-le")) // 2, 1024)
        self.assertIn("…", summary)
        self.input.text = self.config.non_telegram_channel.notice
        await self.action()
        saved_summary = self.env["editMessage"].call_args.args[1]
        self.assertLessEqual(len(saved_summary.encode("utf-16-le")) // 2, 1024)

    async def test_full_preview_is_separate_plain_text(self):
        notice = "<b>contact</b> **owner**\n" + "😀" * 1900
        self.config.non_telegram_channel.notice = notice
        await self.action("non_tg_preview")
        self.assertEqual(self.reply.call_args.args[0], notice)
        self.assertEqual(self.reply.call_args.kwargs["parse_mode"], enums.ParseMode.DISABLED)
        self.env["editMessage"].assert_not_awaited()
        self.env["save_config"].assert_not_called()

    async def test_saves_only_actor_reply_to_prompt_and_scopes_listener(self):
        await self.action()
        self.assertEqual(self.config.non_telegram_channel.notice, self.input.text)
        self.env["save_config"].assert_called_once()
        self.assertIsInstance(self.reply.call_args.kwargs["reply_markup"], ForceReply)
        listener = self.chat.listen.call_args.kwargs
        self.assertEqual(listener["user_id"], 10)
        self.assertFalse(await listener["filters"](None, SimpleNamespace(text="unrelated", reply_to_message_id=101)))
        self.assertTrue(await listener["filters"](None, self.input))
        self.assertFalse(self.env["_editors"])

    async def test_revoked_admin_or_wrong_actor_input_does_not_save(self):
        self.call.from_user.id = self.chat.id = self.input.from_user.id = 11

        async def revoked_admin(**kwargs):
            self.config.admins = []
            return self.input

        self.chat.listen.side_effect = revoked_admin
        await self.action()
        self.env["save_config"].assert_not_called()
        self.assertEqual(self.config.non_telegram_channel.notice, "original")
        self.config.admins = [11]
        self.input.from_user.id = 99
        self.chat.listen.side_effect = lambda **kwargs: self.input
        await self.action()
        self.env["save_config"].assert_not_called()

    async def test_concurrent_changes_are_not_overwritten(self):
        async def changed_elsewhere(**kwargs):
            self.config.non_telegram_channel.notice = "changed elsewhere"
            return self.input

        self.chat.listen.side_effect = changed_elsewhere
        await self.action()
        self.assertEqual(self.config.non_telegram_channel.notice, "changed elsewhere")
        self.env["save_config"].assert_not_called()

    async def test_cancel_timeout_and_invalid_input_leave_saved_configuration_unchanged(self):
        for text in ("/cancel", " ", "😀" * 2001, "broken\ud800"):
            self.input.text = text
            await self.action()
            self.assertEqual(self.config.non_telegram_channel.notice, "original")
        self.chat.listen.side_effect = ListenerTimeout(120)
        await self.action()
        self.env["save_config"].assert_not_called()
        self.assertFalse(self.env["_editors"])

    async def test_save_failure_rolls_back_without_success_confirmation(self):
        original = self.config.non_telegram_channel
        self.env["save_config"].side_effect = OSError("fixture is read only")
        await self.action()
        self.assertIs(self.config.non_telegram_channel, original)
        self.assertEqual(original.notice, "original")
        self.assertNotIn("已保存", self.env["editMessage"].call_args.args[1])

    async def test_toggle_updates_public_data_immediately(self):
        await self.action("non_tg_toggle")
        self.assertEqual(self.helper.public_channel(self.config), {"enabled": False, "url": "", "notice": ""})
        await self.action("non_tg_toggle")
        self.assertEqual(self.helper.public_channel(self.config), {
            "enabled": True, "url": self.config.non_telegram_channel.url, "notice": "original",
        })
        self.assertEqual(self.env["save_config"].call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
