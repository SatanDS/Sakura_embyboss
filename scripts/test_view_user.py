#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SAKURA_RUNNING_MIGRATIONS", "1")

from bot.modules.commands.view_user import create_normaluser_text


class NormalUserListTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_users_include_expiry_like_whitelist_users(self):
        user = SimpleNamespace(
            tg=866296028,
            name="rr1245",
            ex=datetime(2026, 10, 8, 4, 3, 44),
        )

        text = await create_normaluser_text([user], page=1)

        self.assertIn("TGID: `866296028`", text)
        self.assertIn("Emby用户名: [rr1245](tg://user?id=866296028)", text)
        self.assertIn("到期: `2026-10-08 04:03:44`", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
