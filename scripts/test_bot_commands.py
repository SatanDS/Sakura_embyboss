#!/usr/bin/env python3
"""Offline command-menu regressions; never import Bot or connect to Telegram."""

import ast
import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


ROOT = Path(__file__).resolve().parents[1]


@dataclass
class BotCommand:
    command: str
    description: str


@dataclass
class BotCommandScopeAllPrivateChats:
    pass


@dataclass
class BotCommandScopeAllGroupChats:
    pass


@dataclass
class BotCommandScopeChat:
    chat_id: int


@dataclass
class BotCommandScopeChatMember:
    chat_id: int
    user_id: int


class RemoveApplicationImports(ast.NodeTransformer):
    def visit_ImportFrom(self, node):
        if node.module and node.module.split('.')[0] in {'bot', 'pyrogram'}:
            return None
        return node


def source(path):
    return ast.parse((ROOT / path).read_text(encoding='utf-8-sig'))


def load_function(path, name, namespace):
    node = next(node for node in source(path).body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    node.decorator_list = []
    node = RemoveApplicationImports().visit(node)
    exec(compile(ast.Module(body=[node], type_ignores=[]), path, 'exec'), namespace)
    return namespace[name]


def command_lists():
    # Execute only the three menu declarations, not application initialization.
    names = {'user_p', 'admin_p', 'owner_p'}
    nodes = []
    for node in source('bot/__init__.py').body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in names
                                               for target in node.targets):
            nodes.append(node)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in names:
                nodes.append(node)
        elif isinstance(node, ast.If) and any(isinstance(child, ast.AugAssign)
                                              and isinstance(child.target, ast.Name)
                                              and child.target.id == 'owner_p'
                                              for child in node.body):
            nodes.append(node)
    namespace = dict(BotCommand=BotCommand, sakura_b='币', extra_emby_libs=[])
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<command lists>', 'exec'), namespace)
    return {name: namespace[name] for name in names}


def load_menu(client, logger):
    namespace = dict(command_lists(), owner=10, admins=[20, 30], group=[-1001, -1002],
                     bot=client, LOGGER=logger,
                     BotCommandScopeAllPrivateChats=BotCommandScopeAllPrivateChats,
                     BotCommandScopeAllGroupChats=BotCommandScopeAllGroupChats,
                     BotCommandScopeChat=BotCommandScopeChat,
                     BotCommandScopeChatMember=BotCommandScopeChatMember)
    tree = RemoveApplicationImports().visit(source('bot/scheduler/bot_commands.py'))
    exec(compile(tree, 'bot/scheduler/bot_commands.py', 'exec'), namespace)
    return namespace


def scope_key(scope):
    return type(scope).__name__, getattr(scope, 'chat_id', None), getattr(scope, 'user_id', None)


def commands_from(call):
    return call.args[0] if call.args else call.kwargs['commands']


class CommandMenuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SimpleNamespace(set_bot_commands=AsyncMock(return_value=True),
                                      delete_bot_commands=AsyncMock())
        self.logger = Mock()
        self.namespace = load_menu(self.client, self.logger)
        self.menu = self.namespace['BotCommands']

    def expected_scopes(self):
        expected = {
            ('BotCommandScopeAllPrivateChats', None, None): self.namespace['user_p'],
            ('BotCommandScopeAllGroupChats', None, None): self.namespace['user_p'],
        }
        for uid in (*self.namespace['admins'], self.namespace['owner']):
            commands = self.namespace['owner_p' if uid == self.namespace['owner'] else 'admin_p']
            expected[('BotCommandScopeChat', uid, None)] = commands
            for group in self.namespace['group']:
                expected[('BotCommandScopeChatMember', group, uid)] = commands
        return expected

    def final_scopes(self):
        return {scope_key(call.kwargs['scope']): commands_from(call)
                for call in self.client.set_bot_commands.await_args_list}

    def test_real_menus_keep_import_and_owner_commands_in_the_right_roles(self):
        user = {command.command for command in self.namespace['user_p']}
        admin = {command.command for command in self.namespace['admin_p']}
        owner = {command.command for command in self.namespace['owner_p']}
        self.assertNotIn('uimport', user)
        self.assertIn('uimport', admin)
        self.assertIn('uimport', owner)
        self.assertNotIn('config', user | admin)
        self.assertIn('config', owner)
        self.assertTrue(user <= admin <= owner)

    async def test_initialization_updates_all_scopes_without_deleting_menus(self):
        self.assertIs(await self.menu.set_commands(self.client), True)
        self.assertEqual(self.final_scopes(), self.expected_scopes())
        self.client.delete_bot_commands.assert_not_awaited()
        self.logger.info.assert_called()
        self.logger.warning.assert_not_called()

    async def test_owner_keeps_owner_menu_when_also_listed_as_admin(self):
        self.namespace['admins'].append(10)
        await self.menu.set_commands(self.client)
        self.assertEqual(self.final_scopes(), self.expected_scopes())

    async def test_one_rpc_failure_continues_remaining_scopes_and_reports_partial_failure(self):
        calls = 0

        async def fail_first(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError('private credential must not be logged')
            return True

        self.client.set_bot_commands.side_effect = fail_first
        self.assertIs(await self.menu.set_commands(self.client), False)
        self.assertEqual(self.final_scopes(), self.expected_scopes())
        self.logger.warning.assert_called()
        self.logger.info.assert_not_called()
        self.assertNotIn('private credential', str(self.logger.mock_calls))

    async def test_all_rpc_failures_do_not_claim_success(self):
        self.client.set_bot_commands.side_effect = ConnectionError('offline')
        self.assertIs(await self.menu.set_commands(self.client), False)
        self.assertEqual(set(self.final_scopes()), set(self.expected_scopes()))
        self.logger.error.assert_called()
        self.logger.warning.assert_called()
        self.logger.info.assert_not_called()
        self.client.delete_bot_commands.assert_not_awaited()

    async def test_failed_rpc_logs_scope_identifiers_and_exception_type_only(self):
        self.client.set_bot_commands.side_effect = RuntimeError('secret token and URL')
        self.assertIs(await self.menu._set_commands(self.client, self.namespace['admin_p'],
                                                   BotCommandScopeChatMember(-1001, 20)), False)
        logged = str(self.logger.error.call_args_list)
        for detail in ('BotCommandScopeChatMember', '-1001', '20', 'RuntimeError'):
            self.assertIn(detail, logged)
        self.assertNotIn('secret token', logged)

    async def test_private_refresh_uses_current_user_admin_or_owner_role(self):
        for uid, menu in ((40, 'user_p'), (20, 'admin_p'), (10, 'owner_p')):
            with self.subTest(uid=uid):
                self.client.set_bot_commands.reset_mock()
                self.assertIs(await self.menu.refresh_private_commands(self.client, uid), True)
                self.assertEqual(self.final_scopes(), {
                    ('BotCommandScopeChat', uid, None): self.namespace[menu],
                })

    async def test_role_change_updates_private_and_group_menus_without_downgrading_owner(self):
        cases = [('pro_commands', 40, True, 'admin_p'), ('rev_commands', 20, False, 'user_p'),
                 ('pro_commands', 10, False, 'owner_p'), ('rev_commands', 10, False, 'owner_p')]
        for method, uid, is_admin, menu in cases:
            with self.subTest(method=method, uid=uid):
                self.namespace['admins'][:] = [30] + ([uid] if is_admin else [])
                self.client.set_bot_commands.reset_mock()
                await getattr(self.menu, method)(self.client, uid)
                expected = {('BotCommandScopeChat', uid, None): self.namespace[menu]}
                expected.update({('BotCommandScopeChatMember', group, uid): self.namespace[menu]
                                 for group in self.namespace['group']})
                self.assertEqual(self.final_scopes(), expected)


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, menu_error=False, idle_error=False, menu_result=True):
        events = []

        class Client:
            async def __aenter__(self):
                events.append('started')
                return self

            async def __aexit__(self, *exception):
                events.append('stopped')

        client = Client()

        async def set_commands(received):
            self.assertIs(received, client)
            self.assertEqual(events, ['started'])
            events.append('menu')
            if menu_error:
                raise RuntimeError('menu failure')
            return menu_result

        async def idle():
            self.assertEqual(events, ['started', 'menu'])
            events.append('idle')
            if idle_error:
                raise RuntimeError('idle failure')

        main = load_function('main.py', 'main', dict(bot=client, idle=idle,
                                                    BotCommands=SimpleNamespace(set_commands=set_commands)))
        if menu_error or idle_error:
            with self.assertRaises(RuntimeError):
                await main()
        else:
            await main()
        return events

    async def test_client_is_ready_before_registration_and_stops_after_idle(self):
        self.assertEqual(await self.exercise(), ['started', 'menu', 'idle', 'stopped'])

    async def test_partial_menu_failure_does_not_stop_bot_startup(self):
        self.assertEqual(await self.exercise(menu_result=False), ['started', 'menu', 'idle', 'stopped'])

    async def test_registration_exception_still_stops_client(self):
        self.assertEqual(await self.exercise(menu_error=True), ['started', 'menu', 'stopped'])

    async def test_idle_exception_still_stops_client(self):
        self.assertEqual(await self.exercise(idle_error=True), ['started', 'menu', 'idle', 'stopped'])

    def test_entry_point_runs_main_and_does_not_schedule_a_fixed_menu_delay(self):
        calls = [node for node in source('main.py').body if isinstance(node, ast.Expr)
                 and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                 and isinstance(node.value.func.value, ast.Name) and node.value.func.value.id == 'bot'
                 and node.value.func.attr == 'run']
        self.assertEqual(len(calls), 1)
        self.assertEqual(ast.unparse(calls[0]), 'bot.run(main())')
        self.assertFalse(any(isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                             and node.value.id == 'BotCommands' and node.attr == 'set_commands'
                             for node in ast.walk(source('bot/modules/panel/sched_panel.py'))))


class StartPanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SimpleNamespace(set_bot_commands=AsyncMock(return_value=True))
        self.logger = Mock()
        self.menu = load_menu(self.client, self.logger)['BotCommands']
        self.msg = SimpleNamespace(command=['start'], from_user=SimpleNamespace(id=20, first_name='Admin'),
                                   delete=AsyncMock())
        self.namespace = dict(asyncio=asyncio, BotCommands=self.menu, LOGGER=self.logger,
                              user_in_group_filter=AsyncMock(return_value=True),
                              deleteMessage=AsyncMock(), sendMessage=AsyncMock(), sendPhoto=AsyncMock(),
                              prepare_desktop_login=AsyncMock(), approve_browser_login=AsyncMock(return_value='approved'),
                              sql_get_emby=Mock(return_value=object()), sql_add_emby=Mock(),
                              members_info=AsyncMock(return_value=('俞', 'b', None, 0, EMBY_PLACEHOLDER, None)),
                              judge_admins=lambda uid: True, open_check=AsyncMock(return_value=('open', 100, 10, None)),
                              bot_photo='photo', sakura_b='币', judge_group_ikb='group',
                              judge_start_ikb=lambda *args: 'panel')
        self.start = load_function('bot/modules/commands/start.py', 'p_start', self.namespace)

    async def test_plain_start_refreshes_current_private_menu_and_preserves_panel(self):
        await self.start(self.client, self.msg)
        self.client.set_bot_commands.assert_awaited_once()
        self.assertEqual(scope_key(self.client.set_bot_commands.await_args.kwargs['scope']),
                         ('BotCommandScopeChat', 20, None))
        self.namespace['sendPhoto'].assert_awaited_once()

    async def test_menu_rpc_failure_does_not_block_existing_panel(self):
        self.client.set_bot_commands.side_effect = ConnectionError('offline')
        await self.start(self.client, self.msg)
        self.client.set_bot_commands.assert_awaited_once()
        self.namespace['sendPhoto'].assert_awaited_once()
        self.logger.error.assert_called()

    async def test_menu_is_refreshed_before_group_gate_without_bypassing_it(self):
        async def reject_group(*args):
            self.client.set_bot_commands.assert_awaited_once()
            return False

        self.namespace['user_in_group_filter'].side_effect = reject_group
        await self.start(self.client, self.msg)
        self.client.set_bot_commands.assert_awaited_once()
        self.namespace['sendPhoto'].assert_not_awaited()
        self.assertIn('加入我们的群组', self.namespace['sendMessage'].await_args.args[1])

    async def test_tv_login_deep_link_keeps_early_login_path(self):
        self.msg.command = ['start', 'tvlogin_example']
        await self.start(self.client, self.msg)
        self.namespace['prepare_desktop_login'].assert_awaited_once_with(self.msg, 'example')
        self.namespace['user_in_group_filter'].assert_not_awaited()
        self.client.set_bot_commands.assert_not_awaited()

    async def test_payment_deep_link_keeps_early_login_path(self):
        self.msg.command = ['start', 'paylogin_example']
        await self.start(self.client, self.msg)
        self.namespace['approve_browser_login'].assert_awaited_once_with(20, 'example')
        self.namespace['sendMessage'].assert_awaited_once_with(self.msg, 'approved', timer=120)
        self.namespace['user_in_group_filter'].assert_not_awaited()
        self.client.set_bot_commands.assert_not_awaited()


EMBY_PLACEHOLDER = '1234567890abcdef1234567890abcdef'


if __name__ == '__main__':
    unittest.main(verbosity=2)
