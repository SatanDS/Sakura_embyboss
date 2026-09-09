#!/usr/bin/env python3
import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = ROOT / "bot"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# This test imports Emby with lightweight module stubs.  Keep those stubs
# local to this module so unittest discovery can load the other test modules
# against the real application package.
_ORIGINAL_MODULES = {
    name: module
    for name, module in sys.modules.items()
    if name == "aiohttp" or name == "bot" or name.startswith("bot.")
}


def install_test_stubs():
    aiohttp_stub = types.ModuleType("aiohttp")

    class ClientTimeout:
        def __init__(self, total=None, connect=None):
            self.total = total
            self.connect = connect

    class ClientSession:
        def __init__(self, *args, **kwargs):
            self.closed = False

        async def close(self):
            self.closed = True

    aiohttp_stub.ClientTimeout = ClientTimeout
    aiohttp_stub.ClientSession = ClientSession
    sys.modules["aiohttp"] = aiohttp_stub

    bot_stub = types.ModuleType("bot")
    bot_stub.__path__ = [str(BOT_DIR)]
    bot_stub.emby_url = "http://emby.local"
    bot_stub.emby_api = "token"
    bot_stub.emby_block = ["播放列表"]
    bot_stub.extra_emby_libs = ["额外库"]
    bot_stub.LOGGER = types.SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )
    sys.modules["bot"] = bot_stub

    sql_emby_stub = types.ModuleType("bot.sql_helper.sql_emby")
    sql_emby_stub.sql_update_emby = lambda *args, **kwargs: True
    sql_emby_stub.Emby = types.SimpleNamespace(embyid="embyid")
    sys.modules["bot.sql_helper.sql_emby"] = sql_emby_stub

    utils_stub = types.ModuleType("bot.func_helper.utils")

    async def pwd_create(length):
        return "password"

    class CacheStub:
        def memoize(self, ttl=None):
            def decorator(func):
                return func

            return decorator

    class Singleton(type):
        _instances = {}

        def __call__(cls, *args, **kwargs):
            if cls not in cls._instances:
                cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
            return cls._instances[cls]

    utils_stub.pwd_create = pwd_create
    utils_stub.convert_runtime = lambda value: value
    utils_stub.cache = CacheStub()
    utils_stub.Singleton = Singleton
    sys.modules["bot.func_helper.utils"] = utils_stub


install_test_stubs()
from bot.func_helper.emby import EmbyApiResult, Embyservice

# Restore the module table immediately after importing the classes under test.
# The imported classes retain their references to the stubs, while subsequent
# test modules get a clean import of the real bot package.
for _name in list(sys.modules):
    if _name != "aiohttp" and _name != "bot" and not _name.startswith("bot."):
        continue
    if _name in _ORIGINAL_MODULES:
        sys.modules[_name] = _ORIGINAL_MODULES[_name]
    else:
        del sys.modules[_name]


class EmbyPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = object.__new__(Embyservice)
        self.service.__init__("http://emby.local", "token")

    async def asyncTearDown(self):
        await self.service.close()

    async def test_change_policy_preserves_existing_folder_restrictions_when_enabling_user(self):
        service = self.service
        posted_policies = []
        existing_policy = {
            "IsAdministrator": False,
            "IsDisabled": True,
            "EnableAllFolders": False,
            "EnabledFolders": ["movies-guid", "shows-guid"],
            "BlockedMediaFolders": ["播放列表", "额外库"],
        }

        async def fake_request(method, endpoint, **kwargs):
            if method == "GET" and endpoint.startswith("/emby/Users/user-1"):
                return EmbyApiResult(True, {"Policy": existing_policy})
            if method == "POST" and endpoint == "/emby/Users/user-1/Policy":
                posted_policies.append(kwargs["json"])
                return EmbyApiResult(True, b"")
            return EmbyApiResult(False, error=f"unexpected request: {method} {endpoint}")

        service._request = fake_request

        result = await service.emby_change_policy(emby_id="user-1", disable=False)

        self.assertTrue(result)
        self.assertEqual(len(posted_policies), 1)
        posted_policy = posted_policies[0]
        self.assertFalse(posted_policy["IsDisabled"])
        self.assertFalse(posted_policy["EnableAllFolders"])
        self.assertEqual(posted_policy["EnabledFolders"], ["movies-guid", "shows-guid"])
        self.assertEqual(posted_policy["BlockedMediaFolders"], ["播放列表", "额外库"])

    async def test_failed_policy_read_never_writes_folder_permissions(self):
        for operation in ("hide_folders_by_names", "show_folders_by_names"):
            with self.subTest(operation=operation):
                self.service.user = AsyncMock(return_value=(False, {}))
                self.service.get_folder_ids_by_names = AsyncMock(return_value=["vip"])
                self.service.update_user_enabled_folder = AsyncMock(return_value=True)
                result = await getattr(self.service, operation)("user-1", ["VIP"])
                self.assertFalse(result)
                self.service.update_user_enabled_folder.assert_not_awaited()

    async def test_failed_library_read_does_not_clear_all_enabled_folders(self):
        self.service.user = AsyncMock(return_value=(True, {"Policy": {
            "EnableAllFolders": True, "BlockedMediaFolders": [],
        }}))
        self.service.get_emby_libs = AsyncMock(return_value=None)
        self.service.get_folder_ids_by_names = AsyncMock(return_value=["vip"])
        self.service.update_user_enabled_folder = AsyncMock(return_value=True)
        self.assertFalse(await self.service.hide_folders_by_names("user-1", ["VIP"]))
        self.service.update_user_enabled_folder.assert_not_awaited()

    async def test_failed_target_lookup_is_not_reported_as_success(self):
        self.service.user = AsyncMock(return_value=(True, {"Policy": {
            "EnableAllFolders": False, "EnabledFolders": ["movies", "vip"],
            "BlockedMediaFolders": [],
        }}))
        self.service._request = AsyncMock(return_value=EmbyApiResult(False, error="offline"))
        self.service.update_user_enabled_folder = AsyncMock(return_value=True)
        self.assertFalse(await self.service.hide_folders_by_names("user-1", ["VIP"]))
        self.service.update_user_enabled_folder.assert_not_awaited()

    async def test_hiding_one_library_preserves_the_others(self):
        self.service.user = AsyncMock(return_value=(True, {"Policy": {
            "EnableAllFolders": False, "EnabledFolders": ["movies", "vip"],
            "BlockedMediaFolders": ["Private"],
        }}))
        self.service.get_folder_ids_by_names = AsyncMock(return_value=["vip"])
        self.service.update_user_enabled_folder = AsyncMock(return_value=True)
        self.assertTrue(await self.service.hide_folders_by_names("user-1", ["VIP"]))
        update = self.service.update_user_enabled_folder.await_args.kwargs
        self.assertEqual(update["enabled_folder_ids"], ["movies"])
        self.assertEqual(set(update["blocked_media_folders"]), {"Private", "VIP"})
        self.assertFalse(update["enable_all_folders"])

    async def test_showing_one_library_keeps_unrelated_blocks(self):
        self.service.user = AsyncMock(return_value=(True, {"Policy": {
            "EnableAllFolders": True, "BlockedMediaFolders": ["VIP", "Private"],
        }}))
        self.service.get_emby_libs = AsyncMock(return_value={"movies": "Movies", "vip": "VIP"})
        self.service.update_user_enabled_folder = AsyncMock(return_value=True)
        self.assertTrue(await self.service.show_folders_by_names("user-1", ["VIP"]))
        update = self.service.update_user_enabled_folder.await_args.kwargs
        self.assertEqual(update["blocked_media_folders"], ["Private"])

    async def test_malformed_policy_does_not_write_permissions(self):
        self.service._request = AsyncMock(return_value=EmbyApiResult(True, {}))
        self.assertFalse(await self.service.update_user_enabled_folder("user-1", []))
        self.assertEqual(self.service._request.await_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
