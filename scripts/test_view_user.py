#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import re
import sys
import unittest
from datetime import datetime
from html import escape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pyrogram import enums
from pyrogram.errors import MessageNotModified

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SAKURA_RUNNING_MIGRATIONS", "1")

from bot.modules.commands import view_user


class NormalUserListTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        view_user._tg_username_cache.clear()

    def users(self, count=17, name=None):
        return [SimpleNamespace(
            tg=1000000000 + index,
            name=name if name is not None else f"viewer{index}",
            ex=datetime(2026, 10, 8, 4, 3, 44),
        ) for index in range(count)]

    def callback(self, data="normaluser", media=enums.MessageMediaType.PHOTO):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=123),
            message=SimpleNamespace(
                media=media,
                reply_text=AsyncMock(),
                edit_text=AsyncMock(),
                edit_caption=AsyncMock(),
            ),
        )

    async def test_empty_normal_user_list_is_renderable(self):
        text = await view_user.create_normaluser_text([], page=1)

        self.assertIn("暂无普通用户。", text)
        self.assertIn("共 0 人", text)

    async def test_database_failure_is_reported_without_editing_panel(self):
        call = object()
        with patch.object(view_user, "get_all_emby", return_value=None), \
                patch.object(view_user, "callAnswer", new=AsyncMock()) as answer:
            result = await view_user._load_users(call, object(), "普通用户列表")

        self.assertIsNone(result)
        answer.assert_awaited_once_with(call, "⚠️ 普通用户列表加载失败，请检查数据库连接", True)

    async def test_normal_users_include_expiry_like_whitelist_users(self):
        user = SimpleNamespace(
            tg=866296028,
            name="rr1245",
            ex=datetime(2026, 10, 8, 4, 3, 44),
        )

        view_user._tg_username_cache.clear()
        with patch.object(view_user.bot, "get_users", new=AsyncMock(
                return_value=SimpleNamespace(username="viewer"))) as get_users:
            text = await view_user.create_normaluser_text([user], page=1)

        self.assertIn("TGID: <code>866296028</code>", text)
        self.assertIn("TG用户名: <code>viewer</code>", text)
        get_users.assert_awaited_once_with(866296028)
        self.assertIn('<a href="tg://user?id=866296028">rr1245</a>', text)
        self.assertIn("到期: <code>2026-10-08 04:03:44</code>", text)
    async def test_telegram_username_is_cached_and_missing_username_is_safe(self):
        first = SimpleNamespace(tg=1001, name="one", ex=None)
        second = SimpleNamespace(tg=1001, name="one", ex=None)
        view_user._tg_username_cache.clear()
        with patch.object(view_user.bot, "get_users", new=AsyncMock(
                return_value=SimpleNamespace(username=None))) as get_users:
            text = await view_user.create_normaluser_text([first, second], page=1)

        self.assertEqual(text.count("TG用户名: <code>未设置</code>"), 2)
        get_users.assert_awaited_once_with(1001)

    async def test_telegram_lookup_failure_does_not_break_whitelist_list(self):
        user = SimpleNamespace(tg=1002, name="two", ex=None)
        view_user._tg_username_cache.clear()
        with patch.object(view_user.bot, "get_users", new=AsyncMock(
                side_effect=RuntimeError("Telegram unavailable"))):
            text = await view_user.create_whitelist_text([user], page=1)

        self.assertIn("TGID: <code>1002</code>", text)
        self.assertIn("TG用户名: <code>未设置</code>", text)

    async def test_populated_lists_open_as_text_from_photo_admin_panel(self):
        users = self.users()
        for callback_name, handler in (
            ("normaluser", view_user.list_normaluser),
            ("whitelist", view_user.list_whitelist),
        ):
            with self.subTest(callback=callback_name):
                call = self.callback(callback_name)
                with patch.object(view_user, "get_all_emby", return_value=users), \
                        patch.object(view_user.bot, "get_users", new=AsyncMock(
                            return_value=SimpleNamespace(username="test_viewer"))), \
                        patch.object(view_user, "callAnswer", new=AsyncMock()) as answer:
                    await handler(None, call)

                call.message.reply_text.assert_awaited_once()
                call.message.edit_caption.assert_not_awaited()
                call.message.edit_text.assert_not_awaited()
                options = call.message.reply_text.await_args.kwargs
                text = options["text"]
                self.assertGreater(len(text), 1024)
                self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
                self.assertEqual(len(re.findall(r'tg://user\?id=\d+', text)), 17)
                self.assertEqual(options["parse_mode"], enums.ParseMode.HTML)
                self.assertFalse(options["quote"])
                self.assertTrue(options["disable_notification"])
                self.assertTrue(options["disable_web_page_preview"])
                self.assertIsNotNone(options["reply_markup"])
                answer.assert_awaited_once()

    async def test_pagination_edits_existing_text_message(self):
        users = self.users(25)
        for callback_name, handler in (
            ("normaluser:2", view_user.normaluser_page),
            ("whitelist:2", view_user.whitelist_page),
        ):
            with self.subTest(callback=callback_name):
                call = self.callback(callback_name, media=None)
                with patch.object(view_user, "get_all_emby", return_value=users), \
                        patch.object(view_user.bot, "get_users", new=AsyncMock(
                            return_value=SimpleNamespace(username="test_viewer"))), \
                        patch.object(view_user, "callAnswer", new=AsyncMock()) as answer:
                    await handler(None, call)

                call.message.edit_text.assert_awaited_once()
                call.message.reply_text.assert_not_awaited()
                call.message.edit_caption.assert_not_awaited()
                options = call.message.edit_text.await_args.kwargs
                self.assertIn("第 2 页", options["text"])
                self.assertEqual(options["parse_mode"], enums.ParseMode.HTML)
                self.assertTrue(options["disable_web_page_preview"])
                self.assertIsNotNone(options["reply_markup"])
                answer.assert_awaited_once()

    async def test_long_names_paginate_without_losing_or_duplicating_accounts(self):
        for name in ("长" * 255, "😀" * 255, "<&>\"'" * 51):
            users = self.users(43, name=name)
            pages = view_user._user_pages(users)
            self.assertGreater(len(pages), 3)
            self.assertTrue(all(1 <= len(page) <= 20 for page in pages))
            self.assertEqual([user.tg for page in pages for user in page],
                             [user.tg for user in users])
            for build in (view_user.create_normaluser_text, view_user.create_whitelist_text):
                with self.subTest(name=name[:5], build=build.__name__):
                    found = []
                    with patch.object(view_user.bot, "get_users", new=AsyncMock(
                            return_value=SimpleNamespace(username="u" * 32))):
                        for page_number in range(1, len(pages) + 1):
                            text = await build(users, page_number)
                            self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
                            found.extend(map(int, re.findall(r'tg://user\?id=(\d+)', text)))
                    self.assertEqual(found, [user.tg for user in users])

    async def test_names_and_usernames_are_html_escaped(self):
        name = '<b>name & "test"</b>'
        username = "viewer<&>"
        user = self.users(1, name=name)[0]
        with patch.object(view_user.bot, "get_users", new=AsyncMock(
                return_value=SimpleNamespace(username=username))):
            for build in (view_user.create_normaluser_text, view_user.create_whitelist_text):
                text = await build([user], 1)
                self.assertIn(escape(name), text)
                self.assertIn(escape(username), text)
                self.assertNotIn(name, text)
                self.assertNotIn(username, text)

    async def test_empty_list_callback_still_opens_text_panel(self):
        call = self.callback()
        with patch.object(view_user, "get_all_emby", return_value=[]), \
                patch.object(view_user, "callAnswer", new=AsyncMock()) as answer:
            await view_user.list_normaluser(None, call)
        self.assertIn("暂无普通用户。", call.message.reply_text.await_args.kwargs["text"])
        self.assertEqual(view_user._user_pages([]), [[]])
        answer.assert_awaited_once()

    async def test_database_failures_have_one_visible_alert_without_success_notice(self):
        for failure in (None, RuntimeError("database unavailable")):
            with self.subTest(failure=failure):
                call = self.callback()
                query = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch.object(view_user, "get_all_emby", **query), \
                        patch.object(view_user, "callAnswer", new=AsyncMock()) as answer:
                    await view_user.list_normaluser(None, call)
                call.message.reply_text.assert_not_awaited()
                call.message.edit_text.assert_not_awaited()
                answer.assert_awaited_once()
                self.assertIn("加载失败", answer.await_args.args[1])
                self.assertTrue(answer.await_args.args[2])

    async def test_telegram_send_failure_is_reported_once_as_visible_alert(self):
        call = self.callback()
        call.message.reply_text.side_effect = RuntimeError("Telegram unavailable")
        with patch.object(view_user, "get_all_emby", return_value=[]), \
                patch.object(view_user, "callAnswer", new=AsyncMock()) as answer:
            await view_user.list_normaluser(None, call)
        answer.assert_awaited_once()
        self.assertIn("显示失败", answer.await_args.args[1])
        self.assertTrue(answer.await_args.args[2])

    async def test_message_not_modified_does_not_report_false_failure(self):
        call = self.callback("normaluser:1", media=None)
        call.message.edit_text.side_effect = MessageNotModified()
        with patch.object(view_user, "get_all_emby", return_value=[]), \
                patch.object(view_user, "callAnswer", new=AsyncMock()) as answer:
            await view_user.normaluser_page(None, call)
        call.message.edit_text.assert_awaited_once()
        call.message.reply_text.assert_not_awaited()
        answer.assert_awaited_once()
        self.assertNotIn("失败", answer.await_args.args[1])
        self.assertFalse(answer.await_args.kwargs.get("show_alert", False))
        self.assertFalse(len(answer.await_args.args) > 2 and answer.await_args.args[2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
