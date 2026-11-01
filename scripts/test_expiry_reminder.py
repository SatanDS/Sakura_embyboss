"""Expiry reminder behavior with real account models and SQLite; no live messages."""

import ast
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from pyrogram import enums
from pyrogram.errors import FloodWait
from pyromod.helpers import ikb
from sqlalchemy import column, create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker


ROOT = Path(__file__).resolve().parents[1]
REMINDER = ROOT / "bot/scheduler/expiry_reminder.py"


def load_source(path, namespace):
    class WithoutBotImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return None if node.module and node.module.startswith("bot") else node

    tree = WithoutBotImports().visit(ast.parse(path.read_text(encoding="utf-8")))
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace


class ExpiryReminderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertTrue(REMINDER.is_file(), "The expiry reminder is not implemented")
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.engine = create_engine("sqlite:///" + str(Path(folder.name) / "accounts.db"))
        self.addCleanup(self.engine.dispose)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.base = declarative_base()
        self.sql = load_source(ROOT / "bot/sql_helper/sql_emby.py", {
            "Base": self.base, "Session": self.sessions, "LOGGER": Mock(),
        })
        self.Emby = self.sql["Emby"]
        self.ledger = load_source(ROOT / "bot/payments/entitlements.py", {
            "Base": self.base, "__name__": "expiry_ledger_test",
        })
        self.base.metadata.create_all(self.engine)
        self.now = datetime(2026, 11, 1, 1, 30)
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=1)))
        self.locks = {}
        self.namespace = self.load_reminder()

    def load_reminder(self):
        return load_source(REMINDER, {
            "Session": self.sessions, "Emby": self.Emby, "bot": self.bot, "LOGGER": Mock(),
            "china_now": lambda: self.now, "resolve_entitlement": self.ledger["resolve_entitlement"],
            "get_user_lock": lambda tg: self.locks.setdefault(tg, asyncio.Lock()),
        })

    def account(self, tg=1, **changes):
        values = dict(tg=tg, embyid=f"emby-{tg}", name=f"user-{tg}", lv="b",
                      ex=self.now + timedelta(days=3), us=0, iv=0)
        values.update(changes)
        with self.sessions.begin() as session:
            session.add(self.Emby(**values))

    def read(self, tg=1):
        with self.sessions() as session:
            return session.get(self.Emby, tg)

    async def run_reminder(self):
        await self.namespace["remind_expiring_accounts"]()

    async def test_notifies_normal_and_whitelist_only_within_the_next_72_hours(self):
        for tg, values in (
            (1, {}), (2, {"lv": "a", "ex": self.now + timedelta(seconds=1)}),
            (3, {"ex": self.now + timedelta(days=3, seconds=1)}),
            (4, {"ex": self.now}), (5, {"ex": self.now - timedelta(seconds=1)}),
            (6, {"ex": None}), (7, {"lv": "c"}), (8, {"lv": "d"}),
            (9, {"embyid": None}), (10, {"embyid": ""}), (0, {}), (-1, {}),
        ):
            self.account(tg, **values)
        await self.run_reminder()
        recipients = [call.kwargs["chat_id"] for call in self.bot.send_message.await_args_list]
        self.assertCountEqual(recipients, [1, 2])
        self.assertEqual(self.read().expiry_reminded_for, datetime(2026, 11, 4, 1, 30))

    async def test_message_contains_expiry_and_contact_button_as_plain_text(self):
        self.account(name="<b>宏旺</b> **name**", pwd="secret-password")
        await self.run_reminder()
        message = self.bot.send_message.await_args.kwargs
        self.assertIn("2026-11-04 01:30:00", message["text"])
        self.assertIn("<b>宏旺</b> **name**", message["text"])
        self.assertNotIn("secret-password", message["text"])
        self.assertEqual(message["parse_mode"], enums.ParseMode.DISABLED)
        self.assertEqual(message["reply_markup"].inline_keyboard[0][0].url, "https://t.me/wugandu")

    async def test_marker_survives_module_reload_and_new_database_sessions(self):
        self.account()
        await self.run_reminder()
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.namespace = self.load_reminder()
        self.now += timedelta(days=1)
        await self.run_reminder()
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_renewed_expiry_gets_a_new_reminder_when_it_enters_the_window(self):
        self.account()
        await self.run_reminder()
        with self.sessions.begin() as session:
            session.get(self.Emby, 1).ex = datetime(2026, 12, 4, 1, 30)
        await self.run_reminder()
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.now = datetime(2026, 12, 1, 1, 30)
        await self.run_reminder()
        self.assertEqual(self.bot.send_message.await_count, 2)
        self.assertEqual(self.read().expiry_reminded_for, datetime(2026, 12, 4, 1, 30))

    async def test_failed_delivery_is_retryable_and_does_not_block_other_users(self):
        self.account()
        self.account(2)
        async def deliver(**message):
            if message["chat_id"] == 1:
                raise RuntimeError("synthetic blocked recipient")
            return SimpleNamespace(id=2)
        self.bot.send_message.side_effect = deliver
        await self.run_reminder()
        self.assertIsNone(self.read().expiry_reminded_for)
        self.assertEqual(self.read(2).expiry_reminded_for, datetime(2026, 11, 4, 1, 30))
        self.bot.send_message.side_effect = None
        await self.run_reminder()
        self.assertEqual(self.read().expiry_reminded_for, datetime(2026, 11, 4, 1, 30))
        self.assertEqual(self.bot.send_message.await_count, 3)

    async def test_concurrent_checks_do_not_send_two_messages_for_the_same_expiry(self):
        self.account()
        async def deliver(**message):
            await asyncio.sleep(0)
            return SimpleNamespace(id=1)
        self.bot.send_message.side_effect = deliver
        await asyncio.gather(self.run_reminder(), self.run_reminder())
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_telegram_delivery_does_not_hold_a_database_connection_or_block_renewal(self):
        self.account()
        connections = set()
        during_delivery = []
        event.listen(self.engine, "checkout", lambda connection, *_: connections.add(id(connection)))
        event.listen(self.engine, "checkin", lambda connection, *_: connections.discard(id(connection)))
        async def deliver(**message):
            during_delivery.append(len(connections))
            with self.sessions.begin() as session:
                session.get(self.Emby, 1).ex = self.now + timedelta(days=30)
            return SimpleNamespace(id=1)
        self.bot.send_message.side_effect = deliver
        await self.run_reminder()
        self.assertEqual(during_delivery, [0])
        self.assertEqual(self.read().ex, datetime(2026, 12, 1, 1, 30))
        self.assertEqual(self.read().expiry_reminded_for, datetime(2026, 11, 4, 1, 30))

    async def test_rechecks_expiry_after_waiting_for_a_concurrent_renewal(self):
        self.account()
        lock = self.locks.setdefault(1, asyncio.Lock())
        await lock.acquire()
        task = asyncio.create_task(self.run_reminder())
        await asyncio.sleep(0)
        with self.sessions.begin() as session:
            session.get(self.Emby, 1).ex = self.now + timedelta(days=30)
        lock.release()
        await task
        self.bot.send_message.assert_not_awaited()
        self.assertIsNone(self.read().expiry_reminded_for)

    async def test_managed_accounts_use_final_ledger_expiry_instead_of_stale_projection(self):
        for tg, projected, ending in (
            (1, self.now + timedelta(days=90), self.now + timedelta(days=3)),
            (2, self.now + timedelta(days=2), self.now + timedelta(days=32)),
        ):
            self.account(tg, ex=projected)
            with self.sessions.begin() as session:
                session.add(self.ledger["AccountEntitlement"](tg=tg, revision=1))
                session.add(self.ledger["AccountPeriod"](
                    tg=tg, starts_at=self.now - timedelta(days=1), ends_at=self.now + timedelta(days=2),
                    tier="vip", source_key=f"current:{tg}", kind="legacy"))
                session.add(self.ledger["AccountPeriod"](
                    tg=tg, starts_at=self.now + timedelta(days=2), ends_at=ending,
                    tier="normal", source_key=f"queued:{tg}", kind="legacy"))
        await self.run_reminder()
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual(self.bot.send_message.await_args.kwargs["chat_id"], 1)
        self.assertIn("2026-11-04 01:30:00", self.bot.send_message.await_args.kwargs["text"])
        self.assertEqual(self.read().expiry_reminded_for, datetime(2026, 11, 4, 1, 30))

    async def test_managed_admin_blocks_are_not_reminded_even_if_projection_is_active(self):
        self.account()
        with self.sessions.begin() as session:
            session.add(self.ledger["AccountEntitlement"](tg=1, revision=1, blocked_reason="admin"))
            session.add(self.ledger["AccountPeriod"](
                tg=1, starts_at=self.now - timedelta(days=1), ends_at=self.now + timedelta(days=3),
                tier="vip", source_key="blocked", kind="legacy"))
        await self.run_reminder()
        self.bot.send_message.assert_not_awaited()

    async def test_flood_wait_keeps_markers_retryable_without_delaying_expiry_enforcement(self):
        self.account()
        self.bot.send_message.side_effect = FloodWait(120)
        await asyncio.wait_for(self.run_reminder(), timeout=1)
        self.assertIsNone(self.read().expiry_reminded_for)

    async def test_database_outage_does_not_send_unverified_reminders(self):
        self.namespace["Session"] = Mock(side_effect=RuntimeError("synthetic database outage"))
        await self.run_reminder()
        self.bot.send_message.assert_not_awaited()


class ExpiryReminderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reminders_run_even_when_no_expired_accounts_exist(self):
        reminder = AsyncMock()
        namespace = {"remind_expiring_accounts": reminder, "reconcile_managed_accounts": AsyncMock(),
                     "get_all_emby": lambda condition: None, "LOGGER": Mock(),
                     "and_": lambda *conditions: None, "datetime": datetime,
                     "Emby": SimpleNamespace(ex=column("ex"), lv=column("lv"))}
        tree = ast.parse((ROOT / "bot/scheduler/check_ex.py").read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "check_expired")
        exec(compile(ast.Module(body=[function], type_ignores=[]), "check_expired", "exec"), namespace)
        await namespace["check_expired"]()
        reminder.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
