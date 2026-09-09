import asyncio
import importlib.util
import sys
import types
import unittest
import requests
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
config = types.SimpleNamespace(url="http://moviepilot.invalid", username="user", password="password", access_token="old")
bot_stub = types.ModuleType("bot")
bot_stub.moviepilot = config
bot_stub.LOGGER = Mock()
bot_stub.save_config = Mock()
spec = importlib.util.spec_from_file_location("moviepilot_login_under_test", ROOT / "bot/func_helper/moviepilot.py")
mp_module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"bot": bot_stub}):
    spec.loader.exec_module(mp_module)


class FakeResponse:
    status = 200

    def __init__(self, payload, entered, release):
        self.payload, self.entered, self.release = payload, entered, release

    async def __aenter__(self):
        self.entered.set()
        await self.release.wait()
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def post(self, *args, **kwargs):
        return self.response


class MoviePilotLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        mp_module.mp.access_token = "old"
        config.access_token = "old"
        bot_stub.save_config.reset_mock()
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def run_login(self, payload, status=200):
        response = FakeResponse(payload, self.entered, self.release)
        response.status = status
        self.release.set()
        with patch.object(mp_module.aiohttp, "ClientSession", return_value=FakeSession(response)), patch.object(requests, "post", side_effect=AssertionError("Synchronous HTTP is forbidden")):
            return await mp_module.login()

    async def test_login_yields_to_other_tasks_and_saves_token(self):
        response = FakeResponse({"token_type": "bearer", "access_token": "new"}, self.entered, self.release)
        with patch.object(mp_module.aiohttp, "ClientSession", return_value=FakeSession(response)), patch.object(requests, "post", side_effect=AssertionError("Synchronous HTTP is forbidden")):
            task = asyncio.create_task(mp_module.login())
            try:
                await asyncio.wait_for(self.entered.wait(), 0.5)
                self.assertFalse(task.done())
                self.release.set()
                self.assertTrue(await task)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(mp_module.mp.access_token, "bearer new")
        self.assertEqual(config.access_token, "bearer new")
        bot_stub.save_config.assert_called_once_with()

    async def test_rejected_login_does_not_replace_token(self):
        self.assertFalse(await self.run_login({"access_token": "new", "token_type": "bearer"}, 401))
        self.assertEqual(mp_module.mp.access_token, "old")
        bot_stub.save_config.assert_not_called()

    async def test_malformed_login_response_is_rejected(self):
        for payload in ([], {}, {"access_token": "new"}, {"access_token": "", "token_type": "bearer"}):
            with self.subTest(payload=payload):
                self.assertFalse(await self.run_login(payload))
                self.assertEqual(mp_module.mp.access_token, "old")
                bot_stub.save_config.assert_not_called()

    async def test_timeout_does_not_replace_token(self):
        self.assertFalse(await self.run_login(asyncio.TimeoutError()))
        self.assertEqual(mp_module.mp.access_token, "old")
        bot_stub.save_config.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
