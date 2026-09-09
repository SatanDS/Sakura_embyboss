#!/usr/bin/env python3
"""Offline partition tests with real SQL functions and controlled Emby awaits."""

import ast
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


ROOT = Path(__file__).resolve().parents[1]


def load_source(path, namespace):
    source = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    source.body = [node for node in source.body if not (
        isinstance(node, ast.ImportFrom) and node.module and
        (node.module == 'bot' or node.module.startswith('bot.'))
    )]
    exec(compile(source, str(ROOT / path), 'exec'), namespace)
    return namespace


class PartitionAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        self.base = declarative_base()
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.sql = load_source('bot/sql_helper/sql_partition.py', {'Base': self.base, 'Session': self.session})
        self.base.metadata.create_all(self.engine)
        self.grant_model = self.sql['PartitionGrant']
        self.code_model = self.sql['PartitionCode']
        self.locks = load_source('bot/func_helper/concurrency.py', {})
        self.logger = Mock()
        self.emby = SimpleNamespace(hide_folders_by_names=AsyncMock(return_value=True),
                                    show_folders_by_names=AsyncMock(return_value=True))
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.mapping = {'paid': ['VIP'], 'other': ['VIP', 'Shared']}
        shared = dict(self.sql, get_user_lock=self.locks['get_user_lock'], partition_libs=self.mapping,
                      bot=self.bot, emby=self.emby, LOGGER=self.logger,
                      sql_get_emby=lambda tg: SimpleNamespace(tg=tg, embyid='emby-1', name='user-1'))
        self.scheduler = load_source('bot/scheduler/partition_access.py', dict(shared))
        self.command = load_source('bot/modules/commands/partition_code.py', dict(shared))

    def tearDown(self):
        self.engine.dispose()

    def add_grant(self, *, tg=1, partition='paid', days=-1, status='active', code='spent'):
        with self.session() as session:
            row = self.grant_model(tg=tg, embyid='emby-1', partition=partition,
                                   expires_at=datetime.now() + timedelta(days=days), status=status, code=code)
            session.add(row)
            session.commit()
            return row.id

    def add_code(self, code='new-code', days=30):
        self.assertTrue(self.sql['sql_add_partition_codes']([
            {'code': code, 'partition': 'paid', 'duration_days': days},
        ]))

    def grant(self, grant_id):
        with self.session() as session:
            return session.get(self.grant_model, grant_id)

    async def test_revoke_failure_stays_pending_and_retries(self):
        grant_id = self.add_grant()
        self.emby.hide_folders_by_names.return_value = False
        await self.scheduler['check_partition_access']()
        self.assertEqual(self.grant(grant_id).status, 'active')
        self.bot.send_message.assert_not_awaited()
        self.emby.hide_folders_by_names.return_value = True
        await self.scheduler['check_partition_access']()
        self.assertEqual(self.grant(grant_id).status, 'expired')
        self.assertEqual(self.emby.hide_folders_by_names.await_count, 2)

    async def test_shared_library_is_preserved(self):
        expired_id = self.add_grant()
        self.add_grant(partition='other', days=30)
        await self.scheduler['check_partition_access']()
        self.emby.hide_folders_by_names.assert_not_awaited()
        self.assertEqual(self.grant(expired_id).status, 'expired')

    async def test_unknown_library_mapping_is_not_marked_processed(self):
        grant_id = self.add_grant(partition='removed-mapping')
        await self.scheduler['check_partition_access']()
        self.emby.hide_folders_by_names.assert_not_awaited()
        self.assertEqual(self.grant(grant_id).status, 'active')

    async def test_failed_status_commit_keeps_retryable_work(self):
        grant_id = self.add_grant()
        self.scheduler['sql_mark_grants_expired'] = Mock(return_value=False)
        await self.scheduler['check_partition_access']()
        self.assertEqual(self.grant(grant_id).status, 'active')
        self.bot.send_message.assert_not_awaited()

    async def test_redemption_waits_for_revocation_and_finishes_active(self):
        grant_id = self.add_grant()
        self.add_code()
        entered, resume = asyncio.Event(), asyncio.Event()

        async def revoke(*args):
            entered.set()
            await resume.wait()
            return True

        self.emby.hide_folders_by_names.side_effect = revoke
        checking = asyncio.create_task(self.scheduler['check_partition_access']())
        await entered.wait()
        redeeming = asyncio.create_task(self.command['_redeem_partition_code']('new-code', 1))
        await asyncio.sleep(0)
        self.assertFalse(redeeming.done())
        self.assertIsNotNone(self.sql['sql_get_partition_code']('new-code'))
        resume.set()
        await checking
        ok, _ = await redeeming
        self.assertTrue(ok)
        self.assertEqual(self.grant(grant_id).status, 'active')
        self.assertGreater(self.grant(grant_id).expires_at, datetime.now())
        self.emby.show_folders_by_names.assert_awaited_once()

    async def test_scheduler_refreshes_expired_snapshot_after_waiting_for_lock(self):
        grant_id = self.add_grant()
        self.add_code()
        lock = self.locks['get_user_lock'](1)
        async with lock:
            checking = asyncio.create_task(self.scheduler['check_partition_access']())
            await asyncio.sleep(0)
            ok, _, _ = self.sql['sql_redeem_partition_code_atomic'](
                'new-code', 1, 'emby-1', datetime.now(), 'user-1',
            )
            self.assertTrue(ok)
        await checking
        self.emby.hide_folders_by_names.assert_not_awaited()
        self.assertEqual(self.grant(grant_id).status, 'active')

    def test_conditional_expiry_does_not_overwrite_new_deadline(self):
        grant_id = self.add_grant()
        cutoff = datetime.now()
        self.add_code()
        self.assertTrue(self.sql['sql_redeem_partition_code_atomic'](
            'new-code', 1, 'emby-1', datetime.now(), 'user-1',
        )[0])
        self.assertTrue(self.sql['sql_mark_grants_expired']([grant_id], cutoff))
        self.assertEqual(self.grant(grant_id).status, 'active')

    def test_cleanup_does_not_erase_pending_revocations(self):
        pending = self.add_grant(code='pending')
        done = self.add_grant(status='expired', code='done')
        self.assertEqual(self.sql['sql_delete_partition_code_or_grant_by_code']('pending'), (0, 0))
        self.assertEqual(self.sql['sql_clear_used_partition_grants'](), 1)
        self.assertIsNotNone(self.grant(pending))
        self.assertIsNone(self.grant(done))

    async def test_failed_activation_can_retry_same_code_without_extending_again(self):
        self.add_code()
        self.emby.show_folders_by_names.return_value = False
        ok, _ = await self.command['_redeem_partition_code']('new-code', 1)
        self.assertFalse(ok)
        stored = self.sql['sql_get_redeemed_partition_grant']('new-code', 1, datetime.now())
        self.assertIsNotNone(stored)
        original_expiry = stored.expires_at
        self.emby.show_folders_by_names.return_value = True
        ok, _ = await self.command['_redeem_partition_code']('new-code', 1)
        self.assertTrue(ok)
        self.assertEqual(self.grant(stored.id).expires_at, original_expiry)
        ok, _ = await self.command['_redeem_partition_code']('new-code', 2)
        self.assertFalse(ok)

    async def test_notification_failure_does_not_undo_completed_revocation(self):
        grant_id = self.add_grant()
        self.bot.send_message.side_effect = RuntimeError('Telegram unavailable')
        await self.scheduler['check_partition_access']()
        self.assertEqual(self.grant(grant_id).status, 'expired')


class UserLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_released_lock_with_waiter_survives_registry_churn(self):
        namespace = load_source('bot/func_helper/concurrency.py', {})
        get_lock = namespace['get_user_lock']
        first = get_lock(1)
        await first.acquire()
        acquired = asyncio.Event()

        async def waiting():
            async with get_lock(1):
                acquired.set()

        waiter = asyncio.create_task(waiting())
        await asyncio.sleep(0)
        first.release()
        retained = [get_lock(user_id) for user_id in range(2, 1100)]
        self.assertIs(get_lock(1), first)
        await waiter
        self.assertTrue(acquired.is_set())
        self.assertEqual(len(retained), 1098)


if __name__ == '__main__':
    unittest.main(verbosity=2)
