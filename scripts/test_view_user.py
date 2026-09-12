#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SAKURA_RUNNING_MIGRATIONS", "1")

from bot.modules.commands import view_user


class NormalUserListTests(unittest.IsolatedAsyncioTestCase):
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

        self.assertIn("TGID: `866296028`", text)
        self.assertIn("TG用户名: `viewer`", text)
        get_users.assert_awaited_once_with(866296028)
        self.assertIn("Emby用户名: [rr1245](tg://user?id=866296028)", text)
        self.assertIn("到期: `2026-10-08 04:03:44`", text)
    async def test_telegram_username_is_cached_and_missing_username_is_safe(self):
        first = SimpleNamespace(tg=1001, name="one", ex=None)
        second = SimpleNamespace(tg=1001, name="one", ex=None)
        view_user._tg_username_cache.clear()
        with patch.object(view_user.bot, "get_users", new=AsyncMock(
                return_value=SimpleNamespace(username=None))) as get_users:
            text = await view_user.create_normaluser_text([first, second], page=1)

        self.assertEqual(text.count("TG用户名: `未设置`"), 2)
        get_users.assert_awaited_once_with(1001)

    async def test_telegram_lookup_failure_does_not_break_whitelist_list(self):
        user = SimpleNamespace(tg=1002, name="two", ex=None)
        view_user._tg_username_cache.clear()
        with patch.object(view_user.bot, "get_users", new=AsyncMock(
                side_effect=RuntimeError("Telegram unavailable"))):
            text = await view_user.create_whitelist_text([user], page=1)

        self.assertIn("TGID: `1002`", text)
        self.assertIn("TG用户名: `未设置`", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
