"""Exercise the diagnostic against a real isolated HTTP server and fake token."""
import importlib.util
import asyncio
import json
import os
from pathlib import Path
import sys
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
        self.movie_requests = []
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
            self.movie_requests.append(data)
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
        self.assertNotIn("reason", report["movie"])
        self.movie_body = {"available": False, "comments": [], "message": self.token}
        self.assertEqual((await self.probe())["result"], "NO_MATCH_OR_COMMENTS")

    async def test_unavailable_exact_messages_report_distinct_reasons(self):
        cases = [
            ("没有找到确定匹配的弹幕，可检查影片标题与季集资料，或导入本地弹幕", "NO_MATCH"),
            ("匹配结果的影片类型不一致，请检查影片资料或导入本地弹幕", "TYPE_MISMATCH"),
            ("已匹配影片，但目前没有可用弹幕", "EMPTY_COMMENTS"),
        ]
        for message, reason in cases:
            with self.subTest(reason=reason):
                self.movie_body = {"available": False, "comments": [], "message": message,
                    "match": {"animeTitle": "private-matched-title", "episodeTitle": self.token}}
                report = await self.probe()
                self.assertEqual(report["movie"]["http"], 200)
                self.assertFalse(report["movie"]["available"])
                self.assertEqual(report["movie"]["comments"], 0)
                self.assertEqual(report["movie"]["reason"], reason)
                self.assertEqual(report["result"], "NO_MATCH_OR_COMMENTS")
                serialized = json.dumps(report, ensure_ascii=False)
                self.assertNotIn(message, serialized)
                self.assertNotIn("private-matched-title", serialized)

    async def test_unknown_unavailable_messages_keep_fallback_without_leaking_content(self):
        messages = [
            "已匹配影片，但目前没有可用弹幕 " + self.token,
            "private upstream message " + self.token,
            {"secret": self.token},
            None,
        ]
        for message in messages:
            with self.subTest(message_type=type(message).__name__):
                self.movie_body = {"available": False, "comments": [], "message": message,
                    "url": "https://private.invalid/" + self.token,
                    "match": {"animeTitle": "private-matched-title"}}
                report = await self.probe()
                self.assertEqual(report["result"], "NO_MATCH_OR_COMMENTS")
                self.assertEqual(report["movie"]["reason"], "NO_MATCH_OR_COMMENTS")
                serialized = json.dumps(report, ensure_ascii=False)
                for private in ("private upstream message", "private.invalid", "private-matched-title", "已匹配影片"):
                    self.assertNotIn(private, serialized)

    async def test_ready_ignores_unavailable_message(self):
        self.movie_body["message"] = "已匹配影片，但目前没有可用弹幕"
        self.movie_body["reason"] = "EMPTY_COMMENTS"
        report = await self.probe()
        self.assertEqual(report["result"], "READY")
        self.assertEqual(report["movie"]["comments"], 1)
        self.assertNotIn("reason", report["movie"])

    async def test_safe_reason_codes_override_generic_or_legacy_messages(self):
        for reason in ("NO_MATCH", "TYPE_MISMATCH", "EMPTY_COMMENTS"):
            with self.subTest(reason=reason):
                self.movie_body = {"available": False, "comments": [], "reason": reason, "message": "無彈幕匹配"}
                report = await self.probe()
                self.assertEqual(report["movie"]["reason"], reason)
                self.assertEqual(report["result"], "NO_MATCH_OR_COMMENTS")
        self.movie_body["message"] = "已匹配影片，但目前没有可用弹幕"
        self.movie_body["reason"] = "TYPE_MISMATCH"
        self.assertEqual((await self.probe())["movie"]["reason"], "TYPE_MISMATCH")

    async def test_unknown_reason_with_generic_message_is_not_printed(self):
        for reason in ("NO_MATCH " + self.token, {"secret": self.token}, None):
            self.movie_body = {"available": False, "comments": [], "reason": reason, "message": "無彈幕匹配"}
            report = await self.probe()
            self.assertEqual(report["movie"]["reason"], "NO_MATCH_OR_COMMENTS")
            self.assertNotIn("無彈幕匹配", json.dumps(report, ensure_ascii=False))

    async def run_cli(self, *args):
        script = Path(__file__).resolve().parent / "diagnose_danmaku.py"
        process = await asyncio.create_subprocess_exec(sys.executable, str(script), "--title", "private-cli-title", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        output = (stdout + stderr).decode()
        self.assertNotIn(self.token, output)
        self.assertNotIn("private-cli-title", output)
        return process.returncode, stdout.decode(), stderr.decode()

    async def test_cli_sends_provider_ids_without_printing_them(self):
        code, stdout, stderr = await self.run_cli("--douban-id", "123456789", "--imdb-id", "tt987654321")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["result"], "READY")
        self.assertEqual(self.movie_requests[-1]["providerIds"], {"Douban": "123456789", "Imdb": "tt987654321"})
        for value in ("123456789", "tt987654321"):
            self.assertNotIn(value, stdout + stderr)

    async def test_cli_rejects_invalid_provider_ids_without_echoing_values(self):
        for flag, value in (("--douban-id", "0"), ("--douban-id", "-1"), ("--douban-id", "１２３"),
                ("--douban-id", "1" * 129), ("--imdb-id", "ttabc"), ("--imdb-id", "https://private.invalid/" + self.token)):
            with self.subTest(flag=flag):
                code, stdout, stderr = await self.run_cli(flag, value)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertNotIn(value, stderr)
        self.assertEqual(self.calls, 0)

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
