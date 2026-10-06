"""Offline checks for notice editing, authorization and durable configuration."""

import ast
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pyrogram import enums, filters
from pyrogram.types import ForceReply, InlineKeyboardMarkup
from pyromod.exceptions import ListenerTimeout
from pyromod.helpers import ikb


ROOT = Path(__file__).resolve().parents[1]


def load_helper():
    spec = importlib.util.spec_from_file_location("notice_helper_test", ROOT / "bot/func_helper/registration_notice.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_notice_buttons(config):
    spec = importlib.util.spec_from_file_location(
        "manual_channel_notice_test", ROOT / "bot/func_helper/non_telegram_channel.py"
    )
    channel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(channel)
    source = ast.parse((ROOT / "bot/func_helper/fix_bottons.py").read_text(encoding="utf-8"))
    functions = [node for node in source.body
                 if isinstance(node, ast.FunctionDef)
                 and node.name in {"_telegram_url", "registration_notice_buttons"}]
    env = dict(config=config, main_group="https://t.me/+GroupInvite", ikb=ikb,
               InlineKeyboardMarkup=InlineKeyboardMarkup,
               channel_enabled=channel.channel_enabled, channel_url=channel.channel_url)
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(ROOT / "notice_buttons"), "exec"), env)
    return env["registration_notice_buttons"]


class NoticeEditorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.helper = load_helper()
        self.config = SimpleNamespace(
            owner=10, admins=[11], registration_notice="original",
            non_telegram_channel=SimpleNamespace(enabled=False, url="https://support.example.org/manual"),
        )
        self.buttons = Mock(wraps=load_notice_buttons(self.config))
        self.message = SimpleNamespace(text="**new rules**\nKeep your account private.",
                                       from_user=SimpleNamespace(id=10), reply_to_message_id=100)
        self.chat = SimpleNamespace(id=10, listen=AsyncMock(side_effect=lambda **kwargs: self.message))
        self.call = SimpleNamespace(from_user=SimpleNamespace(id=10), data="registration_notice_edit",
                                    message=SimpleNamespace(chat=self.chat,
                                    reply=AsyncMock(return_value=SimpleNamespace(id=100))))
        self.env = dict(config=self.config, save_config=Mock(), LOGGER=Mock(), bot=Mock(),
                        enums=enums, filters=filters, ForceReply=ForceReply, ListenerTimeout=ListenerTimeout,
                        ikb=ikb, registration_notice_buttons=self.buttons, _notice_editors=set(),
                        get_registration_notice=self.helper.get_registration_notice,
                        validate_registration_notice=self.helper.validate_registration_notice,
                        sendMessage=AsyncMock(return_value=True), editMessage=AsyncMock(return_value=True),
                        callAnswer=AsyncMock(return_value=True))
        source = ast.parse((ROOT / "bot/modules/panel/registration_notice_panel.py").read_text(encoding="utf-8"))
        definitions = []
        for node in source.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                node.decorator_list = []
                definitions.append(node)
        exec(compile(ast.Module(body=definitions, type_ignores=[]), str(ROOT / "notice_panel"), "exec"), self.env)

    async def test_saves_valid_message_and_preserves_join_button_in_preview(self):
        await self.env["registration_notice_settings"](None, self.call)
        self.assertEqual(self.config.registration_notice, self.message.text)
        self.env["save_config"].assert_called_once_with()
        self.env["sendMessage"].assert_awaited_once()
        preview = self.env["sendMessage"].call_args
        self.assertEqual(preview.args, (self.call, self.message.text))
        keyboard = preview.kwargs["buttons"].inline_keyboard
        self.assertEqual(len(keyboard), 1)
        self.assertEqual(keyboard[0][0].url, "https://t.me/+GroupInvite")
        self.assertFalse(self.env["_notice_editors"])
        listener = self.chat.listen.call_args.kwargs
        self.assertEqual(listener["user_id"], 10)
        self.assertFalse(await listener["filters"](None, SimpleNamespace(text="unrelated", reply_to_message_id=101)))
        self.assertTrue(await listener["filters"](None, self.message))

    async def test_previews_rebuild_buttons_after_manual_channel_is_disabled(self):
        for action in ("registration_notice_preview", "registration_notice_edit"):
            self.call.data = action
            for enabled in (True, False):
                self.config.non_telegram_channel.enabled = enabled
                await self.env["registration_notice_settings"](None, self.call)
                keyboard = self.env["sendMessage"].call_args.kwargs["buttons"].inline_keyboard
                urls = [button.url for row in keyboard for button in row]
                expected = ["https://t.me/+GroupInvite"]
                if enabled:
                    expected.append(self.config.non_telegram_channel.url)
                self.assertEqual(urls, expected)
        self.assertEqual(self.buttons.call_count, 4)

    async def test_non_admin_and_group_callbacks_cannot_edit(self):
        for user_id, chat_id in ((99, 99), (10, -1001)):
            self.call.from_user.id, self.chat.id = user_id, chat_id
            await self.env["registration_notice_settings"](None, self.call)
        self.env["save_config"].assert_not_called()
        self.chat.listen.assert_not_awaited()
        self.env["sendMessage"].assert_not_awaited()

    async def test_cancel_timeout_and_invalid_text_leave_saved_notice_unchanged(self):
        for content in ("/cancel", " \n ", "a" * 4001):
            self.message.text = content
            await self.env["registration_notice_settings"](None, self.call)
            self.assertEqual(self.config.registration_notice, "original")
        self.chat.listen.side_effect = ListenerTimeout(120)
        await self.env["registration_notice_settings"](None, self.call)
        self.env["save_config"].assert_not_called()
        self.assertFalse(self.env["_notice_editors"])

    async def test_preview_failure_does_not_save(self):
        self.env["sendMessage"].return_value = "ENTITY_INVALID"
        await self.env["registration_notice_settings"](None, self.call)
        self.env["save_config"].assert_not_called()
        self.assertEqual(self.config.registration_notice, "original")

    async def test_save_failure_restores_runtime_notice(self):
        self.env["save_config"].side_effect = OSError("read only fixture")
        await self.env["registration_notice_settings"](None, self.call)
        self.assertEqual(self.config.registration_notice, "original")
        self.assertNotIn("已保存", self.env["editMessage"].call_args.args[1])

    async def test_other_admin_edit_is_not_overwritten(self):
        async def preview(*args, **kwargs):
            self.config.registration_notice = "changed elsewhere"
            return True
        self.env["sendMessage"].side_effect = preview
        await self.env["registration_notice_settings"](None, self.call)
        self.env["save_config"].assert_not_called()
        self.assertEqual(self.config.registration_notice, "changed elsewhere")

    async def test_permissions_are_checked_again_after_input(self):
        self.call.from_user.id = self.chat.id = self.message.from_user.id = 11
        async def listen(**kwargs):
            self.config.admins = []
            return self.message
        self.chat.listen.side_effect = listen
        await self.env["registration_notice_settings"](None, self.call)
        self.env["save_config"].assert_not_called()
        self.assertEqual(self.config.registration_notice, "original")

    async def test_reset_requires_confirmation_and_preview_does_not_mutate(self):
        self.call.data = "registration_notice_preview"
        await self.env["registration_notice_settings"](None, self.call)
        self.call.data = "registration_notice_reset"
        await self.env["registration_notice_settings"](None, self.call)
        self.env["save_config"].assert_not_called()
        self.call.data = "registration_notice_reset_confirm"
        await self.env["registration_notice_settings"](None, self.call)
        self.env["save_config"].assert_called_once()
        self.assertIsNone(self.config.registration_notice)
        self.assertEqual(self.helper.get_registration_notice(self.config), self.helper.DEFAULT_REGISTRATION_NOTICE)


class NoticePersistenceTests(unittest.TestCase):
    def test_notice_survives_save_and_reload_with_existing_config(self):
        import json
        spec = importlib.util.spec_from_file_location("notice_schema_test", ROOT / "bot/schemas/schemas.py")
        schemas = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(schemas)
        data = json.loads((ROOT / "config_example.json").read_text(encoding="utf-8"))
        data.pop("registration_notice", None)
        settings = schemas.Config(**data)
        self.assertIsNone(settings.registration_notice)
        settings.registration_notice = "**新用户须知**\n第二行，保留换行。"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            def fixture_open(*args, **kwargs):
                return path.open(*args[1:], **kwargs)
            with patch.dict(schemas.Config.save_config.__globals__, {"open": fixture_open}):
                settings.save_config()
                reloaded = schemas.Config.load_config()
        self.assertEqual(reloaded.registration_notice, settings.registration_notice)
        self.assertEqual(reloaded.payments, settings.payments)
        self.assertEqual(reloaded.emby_url, settings.emby_url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
