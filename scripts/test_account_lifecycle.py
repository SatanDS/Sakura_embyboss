#!/usr/bin/env python3
"""Offline lifecycle regression tests using the real SQL helpers and SQLite."""

import ast
import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import declarative_base, sessionmaker
from alembic.migration import MigrationContext
from alembic.operations import Operations


ROOT = Path(__file__).resolve().parents[1]


class FrozenDateTime(datetime):
    current = datetime(2026, 9, 9, 12)

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is None else cls.current.astimezone(tz)


class RemoveBotImports(ast.NodeTransformer):
    def visit_ImportFrom(self, node):
        return None if node.module and (node.module == 'bot' or node.module.startswith('bot.')) else node


def load_source(path, namespace):
    tree = RemoveBotImports().visit(ast.parse((ROOT / path).read_text(encoding='utf-8')))
    exec(compile(tree, str(ROOT / path), 'exec'), namespace)
    return namespace


def load_function(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    function.decorator_list = []
    function = RemoveBotImports().visit(function)
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(ROOT / path), 'exec'), namespace)
    return namespace[name]


class AccountLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FrozenDateTime.current = datetime(2026, 9, 9, 12)
        self.engine = create_engine('sqlite:///:memory:')
        self.base = declarative_base()
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.logger = Mock()
        self.sql = load_source('bot/sql_helper/sql_emby.py', {
            'Base': self.base, 'Session': self.session, 'LOGGER': self.logger,
        })
        self.sql['datetime'] = FrozenDateTime
        self.emby_model = self.sql['Emby']
        self.base.metadata.create_all(self.engine)
        self.row = self.emby_model(tg=1, embyid='emby-1', name='user-1', lv='b',
                                   cr=FrozenDateTime.now() - timedelta(minutes=1),
                                   ex=FrozenDateTime.now() + timedelta(days=30), us=0, iv=0)
        with self.session() as session:
            session.add(self.row)
            session.commit()
        self.emby = SimpleNamespace(
            users=AsyncMock(return_value=(True, [{'Id': 'emby-1', 'Name': 'user-1'}])),
            emby_change_policy=AsyncMock(return_value=True),
            emby_del=AsyncMock(return_value=True),
        )
        self.bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(forward=AsyncMock())))
        self.config = SimpleNamespace(activity_check_days=30, freeze_days=20)
        self.deleted = Mock()
        self.lock = asyncio.Lock()
        self.namespace = dict(self.sql, datetime=FrozenDateTime, timedelta=timedelta, timezone=timezone, re=re,
                              emby=self.emby, bot=self.bot, group=[1],
                              config=self.config, tem_deluser=self.deleted, get_user_lock=lambda tg: self.lock)
        self.check = load_function('bot/scheduler/userplays_rank.py', 'check_low_activity', self.namespace)

    def tearDown(self):
        self.engine.dispose()

    def update(self, **values):
        self.assertTrue(self.sql['sql_update_emby'](self.emby_model.tg == 1, **values))

    def get(self):
        return self.sql['sql_get_emby_by_embyid']('emby-1')

    async def test_new_user_without_activity_keeps_full_registration_grace(self):
        await self.check()
        self.emby.emby_change_policy.assert_not_awaited()
        self.emby.emby_del.assert_not_awaited()
        self.assertEqual(self.get().lv, 'b')

    async def test_inactive_user_freezes_from_disable_until_configured_deadline(self):
        self.update(cr=FrozenDateTime.now() - timedelta(days=31))
        await self.check()
        self.assertEqual(self.get().lv, 'c')
        self.assertEqual(self.get().disabled_at, FrozenDateTime.now())
        FrozenDateTime.current += timedelta(days=19)
        await self.check()
        self.emby.emby_del.assert_not_awaited()
        FrozenDateTime.current += timedelta(days=1)
        await self.check()
        self.emby.emby_del.assert_awaited_once_with(emby_id='emby-1')
        self.deleted.assert_called_once()
        self.assertEqual(self.sql['sql_get_emby'](1).lv, 'd')

    async def test_legacy_disabled_account_has_no_inferred_deletion_deadline(self):
        self.update(lv='c', disabled_at=None, cr=FrozenDateTime.now() - timedelta(days=300))
        await self.check()
        self.emby.emby_del.assert_not_awaited()
        self.assertIsNone(self.get().disabled_at)

    async def test_missing_or_invalid_dates_fail_without_disabling(self):
        self.update(cr=None)
        await self.check()
        self.emby.users.return_value = (True, [{'Id': 'emby-1', 'Name': 'user-1', 'LastActivityDate': 'bad-date'}])
        await self.check()
        self.emby.emby_change_policy.assert_not_awaited()

    async def test_recent_activity_overrides_old_registration(self):
        self.update(cr=FrozenDateTime.now() - timedelta(days=300))
        activity = FrozenDateTime.now().astimezone().isoformat()
        self.emby.users.return_value = (True, [{'Id': 'emby-1', 'Name': 'user-1', 'LastActivityDate': activity}])
        await self.check()
        self.emby.emby_change_policy.assert_not_awaited()

    async def test_disable_failure_does_not_start_freeze(self):
        self.update(cr=FrozenDateTime.now() - timedelta(days=31))
        self.emby.emby_change_policy.return_value = False
        await self.check()
        self.assertEqual(self.get().lv, 'b')
        self.assertIsNone(self.get().disabled_at)

    async def test_emby_seven_digit_fractional_timestamp_is_checked(self):
        self.emby.users.return_value = (True, [{'Id': 'emby-1', 'Name': 'user-1',
                                               'LastActivityDate': '2026-01-01T00:00:00.1234567Z'}])
        await self.check()
        self.emby.emby_change_policy.assert_awaited_once_with(emby_id='emby-1', disable=True)

    def test_all_level_transitions_track_and_clear_disable_time(self):
        self.update(lv='c')
        first_disabled = self.get().disabled_at
        FrozenDateTime.current += timedelta(days=2)
        self.update(lv='c')
        self.assertEqual(self.get().disabled_at, first_disabled)
        self.update(lv='b')
        self.assertIsNone(self.get().disabled_at)
        self.update(lv='c')
        self.assertEqual(self.get().disabled_at, FrozenDateTime.now())

    async def test_subscription_expiry_uses_actual_freeze_not_old_expiry(self):
        self.update(ex=FrozenDateTime.now() - timedelta(days=90))
        from sqlalchemy import and_
        namespace = dict(self.namespace, and_=and_, _open=SimpleNamespace(exchange=False),
                         FloodWait=type('FloodWait', (Exception,), {}), sleep=asyncio.sleep,
                         get_all_emby2=lambda condition: [], sql_update_emby2=Mock(),
                         _managed_or_none=lambda tg: None,
                         Emby2=self.emby_model)
        # The final non-Telegram query uses expired; its empty result is stubbed.
        self.emby_model.expired = 0
        check = load_function('bot/scheduler/check_ex.py', 'check_expired', namespace)
        await check()
        self.assertEqual(self.get().disabled_at, FrozenDateTime.now())
        self.emby.emby_del.assert_not_awaited()
        self.update(disabled_at=None)
        await check()
        self.emby.emby_del.assert_not_awaited()


class DisableTimeMigrationTests(unittest.TestCase):
    def test_upgrade_is_idempotent_and_keeps_legacy_dates_unknown(self):
        engine = create_engine('sqlite:///:memory:')
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql('CREATE TABLE emby (tg BIGINT PRIMARY KEY, lv VARCHAR(1))')
                connection.exec_driver_sql("INSERT INTO emby (tg, lv) VALUES (1, 'c')")
                namespace = {}
                source = ast.parse((ROOT / 'bot/sql_helper/alembic/versions/20260909_04_add_emby_disabled_at.py').read_text())
                exec(compile(source, '<migration>', 'exec'), namespace)
                namespace['op'] = Operations(MigrationContext.configure(connection))
                namespace['upgrade']()
                namespace['upgrade']()
                self.assertIn('disabled_at', {column['name'] for column in inspect(connection).get_columns('emby')})
                self.assertIsNone(connection.exec_driver_sql('SELECT disabled_at FROM emby').scalar())
                namespace['downgrade']()
                namespace['downgrade']()
                self.assertNotIn('disabled_at', {column['name'] for column in inspect(connection).get_columns('emby')})
        finally:
            engine.dispose()


if __name__ == '__main__':
    unittest.main(verbosity=2)
