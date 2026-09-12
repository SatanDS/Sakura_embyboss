"""Offline checks for trusted-proxy editing and durable configuration."""

import ast
import importlib.util
import json
import re
import secrets
import tempfile
import unittest
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pyrogram import enums, filters
from pyrogram.types import ForceReply
from pyromod.exceptions import ListenerTimeout
from pyromod.helpers import ikb


ROOT = Path(__file__).resolve().parents[1]


def load_file(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProxyEditorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        helper = load_file("proxy_panel_helper_test", "bot/func_helper/proxy_ip.py")
        self.original = ["192.0.2.1/32"]
        self.config = SimpleNamespace(owner=10, admins=[11], trusted_proxy_cidrs=self.original[:])
        self.message = SimpleNamespace(text="192.0.2.1, 198.51.100.4\n2001:db8::1",
                                       from_user=SimpleNamespace(id=10), reply_to_message_id=100)
        self.chat = SimpleNamespace(id=10, listen=AsyncMock(side_effect=lambda **kwargs: self.message))
        self.call = SimpleNamespace(from_user=SimpleNamespace(id=10), data="cdn_ip_add",
                                    message=SimpleNamespace(chat=self.chat,
                                    reply=AsyncMock(return_value=SimpleNamespace(id=100))))
        self.env = dict(config=self.config, save_config=Mock(), LOGGER=Mock(), re=re, secrets=secrets,
                        monotonic=monotonic, enums=enums, filters=filters, ForceReply=ForceReply,
                        ListenerTimeout=ListenerTimeout, ikb=ikb, _proxy_editors=set(),
                        _proxy_clear_requests={}, validate_proxy_cidrs=helper.validate_proxy_cidrs,
                        sendMessage=AsyncMock(return_value=True), editMessage=AsyncMock(return_value=True),
                        callAnswer=AsyncMock(return_value=True))
        source = ast.parse((ROOT / "bot/modules/panel/real_ip_panel.py").read_text(encoding="utf-8"))
        definitions = []
        for node in source.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                node.decorator_list = []
                definitions.append(node)
        exec(compile(ast.Module(body=definitions, type_ignores=[]), "real_ip_panel_test", "exec"), self.env)

    async def invoke(self, action=None):
        if action is not None:
            self.call.data = "cdn_ip_" + action
        await self.env["real_ip_settings"](None, self.call)

    async def test_add_remove_normalization_and_reply_scope(self):
        await self.invoke()
        self.assertEqual(self.config.trusted_proxy_cidrs, self.original + ["198.51.100.4/32", "2001:db8::1/128"])
        self.env["save_config"].assert_called_once_with()
        reply = self.env["sendMessage"].call_args
        self.assertIs(reply.args[0], self.message)
        self.assertIn("新增 2 个节点，1 个已存在", reply.args[1])
        self.assertIn("当前共 3 个", reply.args[1])
        self.assertIsNotNone(reply.kwargs['buttons'])
        listener = self.chat.listen.call_args.kwargs
        self.assertEqual(listener["user_id"], 10)
        self.assertIsInstance(self.call.message.reply.call_args.kwargs["reply_markup"], ForceReply)
        self.assertFalse(await listener["filters"](None, SimpleNamespace(text="unrelated", reply_to_message_id=101)))
        self.assertTrue(await listener["filters"](None, self.message))
        self.message.text = "198.51.100.4 2001:db8:0:0::1"
        await self.invoke("remove")
        self.assertEqual(self.config.trusted_proxy_cidrs, self.original)
        reply = self.env["sendMessage"].call_args
        self.assertIs(reply.args[0], self.message)
        self.assertIn("已删除 2 个节点", reply.args[1])
        self.assertIn("当前共 1 个", reply.args[1])
        self.assertFalse(self.env["_proxy_editors"])

    async def test_duplicate_add_replies_without_resaving(self):
        self.message.text = "192.0.2.1 192.0.2.1/32"
        await self.invoke()
        self.env["save_config"].assert_not_called()
        reply = self.env["sendMessage"].call_args
        self.assertIs(reply.args[0], self.message)
        self.assertIn("本次未新增", reply.args[1])
        self.assertIn("1 个节点均已存在", reply.args[1])
        self.assertEqual(self.config.trusted_proxy_cidrs, self.original)

    async def test_non_admin_or_group_callback_cannot_edit(self):
        for user_id, chat_id in ((99, 99), (10, -1001)):
            self.call.from_user.id, self.chat.id = user_id, chat_id
            await self.invoke()
        self.env["save_config"].assert_not_called()
        self.chat.listen.assert_not_awaited()
        self.call.message.reply.assert_not_awaited()

    async def test_permission_revoked_or_reply_author_changed_during_input(self):
        self.call.from_user.id = self.chat.id = self.message.from_user.id = 11
        async def revoked(**kwargs):
            self.config.admins = []
            return self.message
        self.chat.listen.side_effect = revoked
        await self.invoke()
        self.config.admins = [11]
        self.chat.listen.side_effect = lambda **kwargs: self.message
        self.message.from_user.id = 99
        await self.invoke()
        self.env["save_config"].assert_not_called()
        self.assertEqual(self.config.trusted_proxy_cidrs, self.original)

    async def test_cancel_timeout_invalid_and_oversized_inputs_do_not_save(self):
        invalid = ("/cancel", " \n ", "0.0.0.0/0", "::/0", "example.com", "198.51.100.2:443",
                   "198.51.100.2\x001", "\x07198.51.100.2", " ".join(f"198.51.100.{i}" for i in range(129)))
        for content in invalid:
            self.message.text = content
            await self.invoke()
            self.assertEqual(self.config.trusted_proxy_cidrs, self.original)
            reply = self.env["sendMessage"].call_args
            self.assertIs(reply.args[0], self.message)
            self.assertIn("节点列表未更新", reply.args[1])
            self.assertNotIn("添加成功", reply.args[1])
        self.chat.listen.side_effect = ListenerTimeout(120)
        await self.invoke()
        self.assertIn("输入已超时", self.env["sendMessage"].call_args.args[1])
        self.assertIs(self.env["sendMessage"].call_args.args[0], self.call.message.reply.return_value)
        self.env["save_config"].assert_not_called()
        self.assertFalse(self.env["_proxy_editors"])

    async def test_unknown_removal_does_not_partially_apply_and_save_failure_rolls_back(self):
        self.message.text = "192.0.2.1 198.51.100.10"
        await self.invoke("remove")
        self.env["save_config"].assert_not_called()
        self.assertEqual(self.config.trusted_proxy_cidrs, self.original)
        self.env["save_config"].side_effect = OSError("read only fixture")
        self.message.text = "198.51.100.10"
        await self.invoke("add")
        self.assertEqual(self.config.trusted_proxy_cidrs, self.original)
        self.assertNotIn("已保存", self.env["editMessage"].call_args.args[1])
        reply = self.env["sendMessage"].call_args
        self.assertIs(reply.args[0], self.message)
        self.assertIn("保存失败", reply.args[1])

    async def test_concurrent_edit_and_duplicate_editor_do_not_overwrite(self):
        self.env["_proxy_editors"].add(10)
        await self.invoke()
        self.chat.listen.assert_not_awaited()
        self.env["_proxy_editors"].clear()
        async def elsewhere(**kwargs):
            self.config.trusted_proxy_cidrs = ["203.0.113.4/32"]
            return self.message
        self.chat.listen.side_effect = elsewhere
        await self.invoke()
        self.env["save_config"].assert_not_called()
        self.assertEqual(self.config.trusted_proxy_cidrs, ["203.0.113.4/32"])

    async def test_clear_requires_fresh_confirmation_and_is_single_use(self):
        await self.invoke("clear_confirm_0123456789abcdef")
        self.env["save_config"].assert_not_called()
        await self.invoke("clear")
        self.env["save_config"].assert_not_called()
        token = self.env["_proxy_clear_requests"][10][0]
        await self.invoke("clear_confirm_" + token)
        self.assertEqual(self.config.trusted_proxy_cidrs, [])
        self.env["save_config"].assert_called_once()
        reply = self.env["sendMessage"].call_args
        self.assertIs(reply.args[0], self.call)
        self.assertIn("已清空 1 个节点", reply.args[1])
        await self.invoke("clear_confirm_" + token)
        self.env["save_config"].assert_called_once()

    async def test_feedback_failure_preserves_saved_nodes_and_updates_panel(self):
        self.env["sendMessage"].return_value = "message delivery failed"
        await self.invoke()
        self.assertEqual(self.config.trusted_proxy_cidrs, self.original + ["198.51.100.4/32", "2001:db8::1/128"])
        self.env["save_config"].assert_called_once()
        self.assertIn("添加成功", self.env["editMessage"].call_args.args[1])
        self.assertFalse(self.env["_proxy_editors"])

    async def test_clear_cancel_expiry_permission_change_and_race_preserve_nodes(self):
        await self.invoke("clear")
        token = self.env["_proxy_clear_requests"][10][0]
        await self.invoke("panel")
        await self.invoke("clear_confirm_" + token)
        await self.invoke("clear")
        token, snapshot, _ = self.env["_proxy_clear_requests"][10]
        self.env["_proxy_clear_requests"][10] = (token, snapshot, 0)
        await self.invoke("clear_confirm_" + token)
        self.assertEqual(self.config.trusted_proxy_cidrs, self.original)
        await self.invoke("clear")
        token = self.env["_proxy_clear_requests"][10][0]
        self.config.trusted_proxy_cidrs = ["203.0.113.4/32"]
        await self.invoke("clear_confirm_" + token)
        self.assertEqual(self.config.trusted_proxy_cidrs, ["203.0.113.4/32"])
        self.call.from_user.id = self.chat.id = 11
        await self.invoke("clear")
        token = self.env["_proxy_clear_requests"][11][0]
        self.config.admins = []
        await self.invoke("clear_confirm_" + token)
        self.env["save_config"].assert_not_called()

    async def test_large_list_preview_uses_bounded_text_replies(self):
        self.config.trusted_proxy_cidrs = [f"2001:db8:ffff:ffff:ffff:ffff:ffff:{i:x}/128" for i in range(128)]
        await self.invoke("preview")
        replies = self.call.message.reply.call_args_list
        self.assertEqual(len(replies), 4)
        self.assertTrue(all(len(item.args[0]) < 4096 for item in replies))
        self.assertTrue(all(item.kwargs["parse_mode"] == enums.ParseMode.DISABLED for item in replies))
        self.env["editMessage"].assert_not_awaited()
        self.env["save_config"].assert_not_called()


class ProxyPersistenceTests(unittest.TestCase):
    def test_defaults_are_independent_and_nodes_survive_save_reload(self):
        schemas = load_file("proxy_schema_test", "bot/schemas/schemas.py")
        data = json.loads((ROOT / "config_example.json").read_text(encoding="utf-8"))
        data.pop("trusted_proxy_cidrs", None)
        settings, other = schemas.Config(**data), schemas.Config(**data)
        self.assertEqual(settings.trusted_proxy_cidrs, [])
        settings.trusted_proxy_cidrs.append("192.0.2.1/32")
        self.assertEqual(other.trusted_proxy_cidrs, [])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            def fixture_open(*args, **kwargs):
                return path.open(*args[1:], **kwargs)
            with patch.dict(schemas.Config.save_config.__globals__, {"open": fixture_open}):
                settings.save_config()
                reloaded = schemas.Config.load_config()
        self.assertEqual(reloaded.trusted_proxy_cidrs, settings.trusted_proxy_cidrs)
        self.assertEqual(reloaded.payments, settings.payments)
        self.assertEqual(reloaded.emby_url, settings.emby_url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
