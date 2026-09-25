import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parents[1]


def load_server_handler(namespace):
    source = ROOT / "bot/modules/panel/server_panel.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    handler = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "server"
    )
    handler.decorator_list = []
    module = ast.Module(body=[handler], type_ignores=[])
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["server"]


class ServerPanelTests(unittest.IsolatedAsyncioTestCase):
    async def render(self, level):
        self.row = SimpleNamespace(
            pwd="password",
            lv=level,
            ex=datetime.now() + timedelta(days=30),
        )
        self.config = SimpleNamespace(
            emby_line="new-normal.example",
            emby_whitelist_line="new-vip.example",
        )
        edited = []

        async def edit_message(call, text, buttons=None):
            edited.append(text)
            return True

        namespace = {
            "datetime": datetime,
            "timezone": timezone,
            "timedelta": timedelta,
            "sql_get_emby": lambda tg: self.row,
            "callAnswer": AsyncMock(),
            "editMessage": edit_message,
            "cr_page_server": AsyncMock(return_value=("keyboard", [])),
            "is_subscription_active": lambda expiry: True,
            "config": self.config,
            "emby_line": "stale-normal.example",
            "emby_whitelist_line": "stale-vip.example",
            "emby": SimpleNamespace(get_current_playing_count=AsyncMock(return_value=0)),
        }
        handler = load_server_handler(namespace)
        await handler(None, SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            data="server",
        ))
        return edited[-1]

    async def test_normal_account_server_panel_reads_current_line_config(self):
        text = await self.render("b")
        self.assertIn("new-normal.example", text)
        self.assertNotIn("stale-normal.example", text)

    async def test_whitelist_account_server_panel_reads_current_line_config(self):
        text = await self.render("a")
        self.assertIn("new-normal.example", text)
        self.assertIn("new-vip.example", text)
        self.assertNotIn("stale-normal.example", text)
        self.assertNotIn("stale-vip.example", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
