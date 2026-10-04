"""Exercise the diagnostic against a real isolated HTTP server and fake token."""
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from aiohttp import web


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


diagnostic = load("diagnostic_fixture", "scripts/diagnose_danmaku.py")
adapter = load("adapter_fixture", "bot/dushengtv/danmaku.py")


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.token = "secret-fixture-".ljust(64, "x")
        self.movie_status = 200
        self.movie_body = {"available": True, "comments": [{"time": 1, "mode": 1, "color": "#ffffff", "text": "test"}]}
        self.calls = 0
        app = web.Application()
        async def health(request):
            return web.json_response({"status": "ok"})
        app.router.add_get("/healthz", health)

        async def post(request):
            self.calls += 1
            if request.headers.get("Authorization") != "Bearer " + self.token:
                return web.json_response({"message": "弹幕服务验证失败"}, status=401)
            data = await request.json()
            if data.get("season") == 0:
                return web.json_response({"available": False, "comments": []})
            return web.json_response(self.movie_body, status=self.movie_status)

        app.router.add_post(adapter.ADAPTER_PATH, post)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.environment = patch.dict(os.environ, {"TGBOT_DANMU_API_URL": f"http://127.0.0.1:{port}", "TGBOT_DANMU_API_TOKEN": self.token})
        self.environment.start()

    async def asyncTearDown(self):
        self.environment.stop()
        await self.runner.cleanup()

    async def probe(self):
        report = await diagnostic.diagnose(adapter, {"title": "老枪", "type": "Movie", "year": 2024})
        self.assertNotIn(self.token, json.dumps(report))
        return report

    async def test_ready_and_no_match(self):
        report = await self.probe()
        self.assertEqual(report["result"], "READY")
        self.assertEqual(report["movie"]["comments"], 1)
        self.movie_body = {"available": False, "comments": [], "message": self.token}
        self.assertEqual((await self.probe())["result"], "NO_MATCH_OR_COMMENTS")

    async def test_wrong_bot_token_is_identified_before_external_provider_request(self):
        with patch.dict(os.environ, {"TGBOT_DANMU_API_TOKEN": "wrong".ljust(64, "x")}):
            report = await self.probe()
        self.assertEqual(report["authentication"]["http"], 401)
        self.assertEqual(report["authentication"]["error"], "TOKEN_REJECTED")
        self.assertEqual(report["result"], "AUTH_OR_ADAPTER_FAILED")
        self.assertEqual(self.calls, 1)

    async def test_source_failure_preserves_http_status_without_response_secrets(self):
        self.movie_status = 502
        self.movie_body = {"message": self.token, "url": "https://private.invalid/" + self.token}
        report = await self.probe()
        self.assertEqual(report["result"], "LIVE_PROVIDER_FAILED")
        self.assertEqual(report["movie"]["http"], 502)
        self.assertNotIn("private.invalid", json.dumps(report))
        self.movie_body = {"message": "弹幕源返回的内容格式无效"}
        self.assertEqual((await self.probe())["movie"]["error"], "PROVIDER_FORMAT_ERROR")

    async def test_disabled_environment_and_bad_contract(self):
        with patch.dict(os.environ, {"TGBOT_DANMU_API_URL": "", "TGBOT_DANMU_API_TOKEN": ""}):
            self.assertEqual((await self.probe())["result"], "BOT_ENV_NOT_LOADED")
        self.movie_body = {"available": True, "comments": {"secret": self.token}}
        self.assertEqual((await self.probe())["result"], "BOT_RESPONSE_CONTRACT_FAILED")


if __name__ == "__main__":
    unittest.main()
