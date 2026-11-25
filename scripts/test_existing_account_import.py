#!/usr/bin/env python3
"""Offline coverage for importing an existing account through Telegram."""

import ast
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker


ROOT = Path(__file__).resolve().parents[1]
EMBY_ID = '1234567890abcdef1234567890abcdef'
OTHER_ID = 'abcdef1234567890abcdef1234567890'
OLD_ID = '12345678-90AB-CDEF-1234-567890ABCDEF'


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 25, 23, 30, 1, 123456)


class RemoveBotImports(ast.NodeTransformer):
    def visit_ImportFrom(self, node):
        return None if node.module and (node.module == 'bot' or node.module.startswith('bot.')) else node


def load_source(path, namespace):
    tree = RemoveBotImports().visit(ast.parse((ROOT / path).read_text(encoding='utf-8')))
    exec(compile(tree, str(ROOT / path), 'exec'), namespace)
    return namespace


def handler_source():
    tree = ast.parse((ROOT / 'bot/modules/extra/create.py').read_text(encoding='utf-8'))
    return next(node for node in tree.body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == 'import_existing_account')


class ImportDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        self.base = declarative_base()
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.emby = load_source('bot/sql_helper/sql_emby.py', {
            'Base': self.base, 'Session': self.session, 'LOGGER': Mock(),
        })['Emby']
        self.namespace = load_source('bot/sql_helper/sql_emby2.py', {
            'Base': self.base, 'Session': self.session, 'Emby': self.emby,
        })
        self.namespace['datetime'] = FrozenDateTime
        self.emby2 = self.namespace['Emby2']
        self.import_account = self.namespace['sql_import_existing_emby2']
        self.base.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def rows(self):
        with self.session() as session:
            return session.query(self.emby2).all()

    def seed(self, table, embyid, name):
        model = self.emby if table == 'emby' else self.emby2
        values = dict(embyid=embyid, name=name, pwd='existing-password', lv='a',
                      ex=datetime(2028, 1, 1))
        if table == 'emby':
            values['tg'] = 1
        with self.session() as session:
            session.add(model(**values))
            session.commit()

    def test_registers_original_id_and_365_days_without_creating_a_password(self):
        expires = self.import_account(EMBY_ID, '俞', 365)
        self.assertEqual(expires, datetime(2027, 11, 25, 23, 30, 1))
        row, = self.rows()
        self.assertEqual((row.embyid, row.name, row.lv, row.expired), (EMBY_ID, '俞', 'b', 0))
        self.assertEqual(row.cr, datetime(2026, 11, 25, 23, 30, 1))
        self.assertEqual(row.ex, expires)
        self.assertIsNone(row.pwd)
        self.assertIsNone(row.pwd2)
        with self.session() as session:
            self.assertEqual(session.query(self.emby).count(), 0)

    def test_duplicate_id_or_name_in_either_table_preserves_existing_record(self):
        for table in ('emby', 'emby2'):
            for embyid, name in ((OTHER_ID, '俞'), (EMBY_ID, '旧名称'), (OLD_ID, '旧名称')):
                with self.subTest(table=table, embyid=embyid, name=name):
                    self.seed(table, embyid, name)
                    self.assertIsNone(self.import_account(EMBY_ID, '俞', 365))
                    with self.session() as session:
                        model = self.emby if table == 'emby' else self.emby2
                        row, = session.query(model).all()
                        self.assertEqual((row.embyid, row.name, row.lv, row.pwd, row.ex),
                                         (embyid, name, 'a', 'existing-password', datetime(2028, 1, 1)))
                        self.assertEqual(session.query(self.emby2).count(), int(table == 'emby2'))
                        session.query(model).delete()
                        session.commit()

    def test_repeated_import_does_not_extend_subscription(self):
        expires = self.import_account(EMBY_ID, '俞', 365)
        self.assertIsNone(self.import_account(EMBY_ID, '俞', 730))
        row, = self.rows()
        self.assertEqual(row.ex, expires)

    def test_database_error_after_insert_rolls_back(self):
        def fail_after_insert(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith('INSERT INTO EMBY2'):
                raise RuntimeError('Injected failure after the real INSERT')

        event.listen(self.engine, 'after_cursor_execute', fail_after_insert)
        try:
            with self.assertRaises(RuntimeError):
                self.import_account(EMBY_ID, '俞', 365)
        finally:
            event.remove(self.engine, 'after_cursor_execute', fail_after_insert)
        self.assertEqual(self.rows(), [])
        self.assertIsNotNone(self.import_account(EMBY_ID, '俞', 365))

    def test_invalid_subscription_or_identity_never_creates_a_record(self):
        cases = [(EMBY_ID, '俞', days) for days in (0, -1, 3651, True, 1.5, '365', None)]
        cases += [(embyid, '俞', 365) for embyid in (None, '', 'bad-id', OLD_ID, EMBY_ID.upper())]
        cases += [(EMBY_ID, name, 365) for name in (None, '', ' ', 'a' * 129)]
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.import_account(*values)
        self.assertEqual(self.rows(), [])


class ImportHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = SimpleNamespace(users=AsyncMock(), emby_create=AsyncMock(),
                                   emby_del=AsyncMock(), emby_change_policy=AsyncMock())
        self.user = {'Id': EMBY_ID, 'Name': '俞', 'Policy': {'IsDisabled': False}}
        self.api.users.return_value = (True, [self.user])
        self.import_account = Mock(return_value=datetime(2027, 11, 25, 23, 30, 1))
        self.send_message = AsyncMock()
        self.reply = SimpleNamespace(edit=AsyncMock())
        self.msg = SimpleNamespace(command=['uimport', '俞', '365'], reply=AsyncMock(return_value=self.reply))
        self.logger = Mock()
        namespace = dict(emby=self.api, sql_import_existing_emby2=self.import_account,
                         sendMessage=self.send_message, LOGGER=self.logger,
                         enums=SimpleNamespace(ParseMode=SimpleNamespace(DISABLED='disabled')))
        function = handler_source()
        function.decorator_list = []
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<uimport>', 'exec'), namespace)
        self.handle = namespace['import_existing_account']

    def tearDown(self):
        self.api.emby_create.assert_not_awaited()
        self.api.emby_del.assert_not_awaited()
        self.api.emby_change_policy.assert_not_awaited()

    async def invoke(self):
        await self.handle(None, self.msg)
        return self.reply.edit.await_args.args[0] if self.reply.edit.await_args else ''

    async def test_exact_existing_chinese_name_imports_and_shows_vip_command(self):
        self.api.users.return_value = (True, [dict(self.user, Name='俞2', Id=OTHER_ID), self.user])
        text = await self.invoke()
        self.api.users.assert_awaited_once_with()
        self.import_account.assert_called_once_with(EMBY_ID, '俞', 365)
        self.assertIn('已导入现有账号：俞', text)
        self.assertIn('/prouser 俞', text)
        self.assertIn('到期后', text)
        self.assertEqual(self.reply.edit.await_args.kwargs['parse_mode'], 'disabled')

    async def test_invalid_command_does_not_query_emby(self):
        for command in (['uimport'], ['uimport', '俞'], ['uimport', '俞', 'oops'],
                        ['uimport', '俞', '0'], ['uimport', '俞', '3651'],
                        ['uimport', '俞', '365', 'extra']):
            with self.subTest(command=command):
                self.msg.command = command
                await self.handle(None, self.msg)
        self.api.users.assert_not_awaited()
        self.import_account.assert_not_called()
        self.assertEqual(self.send_message.await_count, 6)

    async def test_no_exact_match_or_duplicate_server_names_are_rejected(self):
        for users in ([], [dict(self.user, Name='俞2')], [self.user, dict(self.user, Id=OTHER_ID)]):
            with self.subTest(users=users):
                self.api.users.return_value = (True, users)
                self.assertIn('没有找到唯一', await self.invoke())
        self.import_account.assert_not_called()

    async def test_disabled_missing_or_unknown_policy_is_rejected(self):
        for policy in (None, {}, {'IsDisabled': True}, {'IsDisabled': 0}, 'false'):
            with self.subTest(policy=policy):
                self.api.users.return_value = (True, [dict(self.user, Policy=policy)])
                self.assertIn('未导入', await self.invoke())
        self.import_account.assert_not_called()

    async def test_server_failure_or_malformed_payload_does_not_import(self):
        for response in ((False, {'error': 'failed'}), (True, {}), (True, None)):
            with self.subTest(response=response):
                self.api.users.return_value = response
                self.assertIn('未导入', await self.invoke())
        self.api.users.side_effect = RuntimeError('secret API response')
        text = await self.invoke()
        self.assertIn('未完成', text)
        self.assertNotIn('secret', text)
        self.assertNotIn('secret', str(self.logger.mock_calls))
        self.import_account.assert_not_called()

    async def test_existing_bot_record_is_never_overwritten(self):
        self.import_account.return_value = None
        text = await self.invoke()
        self.assertIn('未覆盖原记录', text)
        self.assertNotIn('已导入现有账号', text)

    async def test_database_failure_is_not_reported_as_success(self):
        self.import_account.side_effect = RuntimeError('secret database credentials')
        text = await self.invoke()
        self.assertIn('未完成', text)
        self.assertNotIn('secret', text)
        self.assertNotIn('secret', str(self.logger.mock_calls))
        self.assertNotIn('已导入现有账号', text)

    async def test_invalid_server_id_is_rejected_by_real_validation(self):
        # Load the real helper with a Session mock so validation cannot touch a database.
        base = declarative_base()
        session = Mock()
        sql = load_source('bot/sql_helper/sql_emby2.py', {'Base': base, 'Session': session, 'Emby': Mock()})
        self.handle.__globals__['sql_import_existing_emby2'] = sql['sql_import_existing_emby2']
        for embyid in (None, '', 'bad-id', OLD_ID, EMBY_ID.upper()):
            with self.subTest(embyid=embyid):
                self.api.users.return_value = (True, [dict(self.user, Id=embyid)])
                self.assertIn('未完成', await self.invoke())
        session.assert_not_called()

    def test_telegram_registration_requires_admin_and_private_chat(self):
        decorators = [ast.unparse(node) for node in handler_source().decorator_list]
        self.assertEqual(len(decorators), 1)
        self.assertIn("filters.command('uimport', prefixes)", decorators[0])
        self.assertIn('admins_on_filter', decorators[0])
        self.assertIn('filters.private', decorators[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
