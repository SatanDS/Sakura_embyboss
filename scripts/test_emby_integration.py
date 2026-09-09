"""Opt-in integration tests against a disposable local Emby server.

Required environment:
  TGBOT_EMBY_TEST=1
  TGBOT_EMBY_TEST_URL=http://127.0.0.1:8096
  TGBOT_EMBY_TEST_API_KEY=<test-instance API key>

Optional existing empty directories inside the Emby container:
  TGBOT_EMBY_TEST_MOVIES_PATH=/mnt/Movies
  TGBOT_EMBY_TEST_VIP_PATH=/mnt/VIP

Only loopback hosts and the Docker DNS name tgbot-emby-test are accepted.
The server must have completed initial setup. The tests create randomly
named users and libraries, removing only their own fixtures afterward.
No library media files are created or deleted. Without TGBOT_EMBY_TEST=1,
all network tests skip; application configuration is never imported.
"""

import ast
import logging
import os
from pathlib import Path, PurePosixPath
import secrets
import string
from types import SimpleNamespace
import unittest
from urllib.parse import urlsplit
import uuid


ROOT = Path(__file__).resolve().parents[1]
ENABLED = os.environ.get('TGBOT_EMBY_TEST') == '1'
SAFE_HOSTS = {'127.0.0.1', '::1', 'localhost', 'tgbot-emby-test'}


def checked_test_url(value):
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {'http', 'https'}
            and parsed.hostname in SAFE_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {'', '/'}
            and not parsed.query
            and not parsed.fragment
            and parsed.port != 0
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError('A loopback or tgbot-emby-test HTTP URL without credentials or a path is required')
    return value.rstrip('/')


def checked_library_path(value):
    if not value:
        return None
    path = PurePosixPath(value)
    if not path.is_absolute() or '..' in path.parts or value.startswith('//') or '\\' in value:
        raise ValueError('Test library paths must be absolute local paths inside the Emby container')
    return str(path)


def load_service(url, api_key):
    # Execute the complete real service module, replacing only bot imports.
    # No bot package, config file, SQL connection, or Telegram client is loaded.
    logger = logging.Logger('emby-integration-isolated')
    logger.addHandler(logging.NullHandler())
    logger.propagate = False

    async def password(length):
        return ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(length))

    class Cache:
        def memoize(self, **kwargs):
            return lambda function: function

    namespace = {
        '__name__': 'emby_integration_runtime',
        'emby_url': url, 'emby_api': api_key, 'emby_block': [], 'extra_emby_libs': [],
        'LOGGER': logger, 'Singleton': type, 'cache': Cache(), 'pwd_create': password,
        'convert_runtime': lambda value: value,
        'sql_update_emby': lambda *args, **kwargs: True,
        'Emby': SimpleNamespace(embyid='unused-test-field'),
    }
    path = ROOT / 'bot/func_helper/emby.py'
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.ImportFrom) and node.module and
        (node.module == 'bot' or node.module.startswith('bot.'))
    )]
    exec(compile(tree, str(path), 'exec'), namespace)
    return namespace['emby'], namespace['EmbyApiResult']


class EmbyIntegrationConfigurationTests(unittest.TestCase):
    def test_remote_or_credentialed_urls_are_rejected(self):
        for value in ('http://example.com:8096', 'http://192.168.1.10:8096',
                      'http://user:secret@localhost:8096', 'http://localhost:8096/emby',
                      'http://localhost:8096?token=secret', 'file:///tmp/emby',
                      'http://localhost:bad', 'http://localhost:0'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    checked_test_url(value)

    def test_local_urls_and_fixture_paths_are_accepted(self):
        for value in ('http://127.0.0.1:8096', 'http://[::1]:8096/', 'http://tgbot-emby-test:8096'):
            self.assertEqual(checked_test_url(value), value.rstrip('/'))
        self.assertEqual(checked_library_path('/mnt/Movies'), '/mnt/Movies')
        self.assertIsNone(checked_library_path(''))
        for value in ('relative/path', '//remote/share', '/mnt/../data', 'C:\\Movies'):
            with self.assertRaises(ValueError):
                checked_library_path(value)


@unittest.skipUnless(ENABLED, 'Set TGBOT_EMBY_TEST=1 with a disposable local Emby URL and API key')
class EmbyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.url = checked_test_url(os.environ.get('TGBOT_EMBY_TEST_URL', ''))
        api_key = os.environ.get('TGBOT_EMBY_TEST_API_KEY', '').strip()
        if not api_key:
            raise ValueError('TGBOT_EMBY_TEST_API_KEY must contain a test-instance API key')
        self.paths = [checked_library_path(os.environ.get(variable, '')) for variable in (
            'TGBOT_EMBY_TEST_MOVIES_PATH', 'TGBOT_EMBY_TEST_VIP_PATH',
        )]
        self.prefix = 'tgbot_test_' + uuid.uuid4().hex
        self.user_names = set()
        self.library_names = set()
        self.service, self.result_type = load_service(self.url, api_key)
        raw_request = self.service._request

        async def request(method, endpoint, **kwargs):
            if not endpoint.startswith('/emby/'):
                raise AssertionError('Integration requests must use an Emby-relative endpoint')
            kwargs['allow_redirects'] = False
            return await raw_request(method, endpoint, **kwargs)

        self.real_request = request
        self.service._request = request
        self.addAsyncCleanup(self.cleanup_fixtures)
        info = await self.real_request('GET', '/emby/System/Info')
        self.assertTrue(info.success, 'Cannot authenticate to the disposable Emby test instance')
        self.assertIsInstance(info.data, dict)
        self.assertTrue(info.data.get('Version'), 'Emby did not return its server version')
        self.user_id, self.password, self.username = await self.create_user('user')

    async def cleanup_fixtures(self):
        self.service._request = self.real_request
        failures = []
        try:
            success, users = await self.service.users()
            if not success:
                failures.append('cannot list test users for cleanup')
            else:
                for user in users:
                    if user.get('Name') in self.user_names:
                        if not await self.service.emby_del(user['Id']):
                            failures.append('could not remove test user ' + user['Name'])
            if self.library_names:
                libraries = await self.real_request('GET', '/emby/Library/VirtualFolders')
                if not libraries.success or not isinstance(libraries.data, list):
                    failures.append('cannot list test libraries for cleanup')
                else:
                    for library in libraries.data:
                        name = library.get('Name')
                        if name in self.library_names:
                            result = await self.real_request('POST', '/emby/Library/VirtualFolders/Delete',
                                                             json={'Id': library.get('Id') or library.get('ItemId'),
                                                                   'Name': name, 'RefreshLibrary': False})
                            if not result.success:
                                failures.append('could not remove test library ' + name)
        finally:
            await self.service.close()
        if failures:
            self.fail('; '.join(failures))

    async def create_user(self, suffix):
        name = self.prefix + '_' + suffix
        success, users = await self.service.users()
        self.assertTrue(success, 'Cannot inspect existing users before fixture creation')
        self.assertNotIn(name, {user['Name'] for user in users})
        # Keep the exact generated name even if a later creation stage fails.
        self.user_names.add(name)
        result = await self.service.emby_create(name, days=1)
        self.assertIsInstance(result, tuple, 'Real Embyservice.emby_create failed')
        user_id, password, expiry = result
        self.assertTrue(user_id)
        self.assertTrue(password)
        self.assertIsNotNone(expiry)
        return user_id, password, name

    async def create_libraries(self):
        initial = await self.service.get_emby_libs()
        self.assertIsInstance(initial, dict, 'Cannot inspect existing libraries')
        names = [self.prefix + '_Movies', self.prefix + '_VIP']
        for name, path in zip(names, self.paths):
            self.assertNotIn(name, initial.values())
            self.library_names.add(name)
            options = {'EnableRealtimeMonitor': False, 'EnableInternetProviders': False}
            if path:
                options['PathInfos'] = [{'Path': path}]
            result = await self.real_request(
                'POST', '/emby/Library/VirtualFolders',
                params={'name': name, 'collectionType': 'movies', 'refreshLibrary': 'false'},
                json={'LibraryOptions': options},
            )
            self.assertTrue(result.success, 'Cannot create an isolated test library; configure the optional container paths')
        libraries = await self.service.get_emby_libs()
        self.assertIsInstance(libraries, dict)
        ids = {name: guid for guid, name in libraries.items() if name in names}
        self.assertEqual(set(ids), set(names), 'Both randomly named test libraries must be discoverable')
        return names, ids

    async def read_policy(self):
        success, user = await self.service.user(self.user_id)
        self.assertTrue(success, 'Cannot read test user policy')
        self.assertEqual(user.get('Name'), self.username)
        self.assertIsInstance(user.get('Policy'), dict)
        return user['Policy']

    async def test_created_user_authenticates_and_disable_enable_takes_effect(self):
        authenticated, user_id = await self.service.authority_account(1, self.username, self.password)
        self.assertTrue(authenticated, 'The created password did not authenticate')
        self.assertEqual(user_id, self.user_id)
        rejected, _ = await self.service.authority_account(1, self.username, self.password + '_incorrect')
        self.assertFalse(rejected, 'An incorrect password was accepted')
        self.assertTrue(await self.service.emby_change_policy(self.user_id, disable=True))
        self.assertTrue((await self.read_policy())['IsDisabled'])
        authenticated, _ = await self.service.authority_account(1, self.username, self.password)
        self.assertFalse(authenticated, 'A disabled user still authenticated')
        self.assertTrue(await self.service.emby_change_policy(self.user_id, disable=False))
        self.assertFalse((await self.read_policy())['IsDisabled'])
        authenticated, user_id = await self.service.authority_account(1, self.username, self.password)
        self.assertTrue(authenticated, 'The re-enabled user did not authenticate')
        self.assertEqual(user_id, self.user_id)

    async def test_hide_show_and_reenable_preserve_unrelated_library(self):
        names, ids = await self.create_libraries()
        movies, vip = names
        self.assertTrue(await self.service.update_user_enabled_folder(
            self.user_id, enabled_folder_ids=list(ids.values()), blocked_media_folders=[], enable_all_folders=False,
        ))
        self.assertTrue(await self.service.hide_folders_by_names(self.user_id, [vip]))
        policy = await self.read_policy()
        self.assertFalse(policy['EnableAllFolders'])
        self.assertEqual(set(policy['EnabledFolders']), {ids[movies]})
        if 'BlockedMediaFolders' in policy:
            self.assertIn(vip, policy['BlockedMediaFolders'])
        self.assertTrue(await self.service.emby_change_policy(self.user_id, disable=True))
        self.assertTrue(await self.service.emby_change_policy(self.user_id, disable=False))
        policy = await self.read_policy()
        self.assertEqual(set(policy['EnabledFolders']), {ids[movies]})
        if 'BlockedMediaFolders' in policy:
            self.assertIn(vip, policy['BlockedMediaFolders'])
        self.assertTrue(await self.service.show_folders_by_names(self.user_id, [vip]))
        policy = await self.read_policy()
        self.assertEqual(set(policy['EnabledFolders']), set(ids.values()))
        self.assertNotIn(vip, policy.get('BlockedMediaFolders', []))

    async def test_failed_reads_never_write_policy_or_clear_unrelated_libraries(self):
        names, ids = await self.create_libraries()
        self.assertTrue(await self.service.update_user_enabled_folder(
            self.user_id, enabled_folder_ids=list(ids.values()), blocked_media_folders=[], enable_all_folders=False,
        ))
        for read_failure in ('user', 'libraries'):
            for operation in ('hide_folders_by_names', 'show_folders_by_names'):
                with self.subTest(read_failure=read_failure, operation=operation):
                    before = await self.read_policy()
                    writes = []

                    async def failed_read(method, endpoint, **kwargs):
                        if method == 'GET' and (
                            (read_failure == 'user' and endpoint.startswith('/emby/Users/' + self.user_id))
                            or (read_failure == 'libraries' and endpoint.startswith('/emby/Library/VirtualFolders'))
                        ):
                            return self.result_type(False, error='injected read failure')
                        if method == 'POST' and endpoint.endswith('/Policy'):
                            writes.append(endpoint)
                        return await self.real_request(method, endpoint, **kwargs)

                    self.service._request = failed_read
                    try:
                        self.assertFalse(await getattr(self.service, operation)(self.user_id, [names[1]]))
                    finally:
                        self.service._request = self.real_request
                    self.assertEqual(writes, [], 'A failed read still caused a policy write')
                    self.assertEqual(await self.read_policy(), before, 'A failed read changed real Emby policy')

    async def test_failed_password_setup_removes_only_its_partially_created_user(self):
        failed_name = self.prefix + '_failed_password'
        self.user_names.add(failed_name)

        async def fail_password(method, endpoint, **kwargs):
            if method == 'POST' and endpoint.endswith('/Password'):
                return self.result_type(False, error='injected password setup failure')
            return await self.real_request(method, endpoint, **kwargs)

        self.service._request = fail_password
        try:
            self.assertFalse(await self.service.emby_create(failed_name, days=1))
        finally:
            self.service._request = self.real_request
        success, users = await self.service.users()
        self.assertTrue(success)
        names = {user['Name'] for user in users}
        self.assertNotIn(failed_name, names, 'Failed creation left a partially configured account')
        self.assertIn(self.username, names, 'Creation rollback removed the other test account')


if __name__ == '__main__':
    unittest.main(verbosity=2)
