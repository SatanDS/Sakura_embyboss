"""Danmu adapter transport tests without Telegram, databases or network access."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import aiohttp

source = Path(__file__).resolve().parents[1] / "bot/dushengtv/danmaku.py"
spec = importlib.util.spec_from_file_location("tv_danmaku_test_module", source)
danmaku = importlib.util.module_from_spec(spec)
spec.loader.exec_module(danmaku)


class Response:
    def __init__(self, body, status=200):
        self.status = status
        self.raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def iter_chunked(self, size):
        for start in range(0, len(self.raw), size):
            yield self.raw[start:start + size]


class Client:
    def __init__(self, response):
        self.post = MagicMock(return_value=response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class DanmakuTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.secret = "private-fixture-token-0123456789abcdef"
        self.environment = patch.dict(os.environ, {"TGBOT_DANMU_API_URL": "http://127.0.0.1:9321", "TGBOT_DANMU_API_TOKEN": self.secret})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.item = {"title": "示例剧", "type": "Episode", "season": 2, "episode": 4, "providerIds": {"Tmdb": "42"}}
        self.comment = {"time": 1.5, "mode": 1, "color": "#FFFFFF", "text": "测试弹幕"}

    def test_endpoint_only_accepts_configured_origin_and_private_http(self):
        for origin in ("http://127.0.0.1:9321", "http://[::1]:9321", "http://192.168.5.4:9321", "http://10.5.0.2:9321", "http://172.20.0.2:9321", "http://[fd00::1]:9321", "http://danmu-api:9321", "https://danmu.example.test"):
            with self.subTest(origin=origin), patch.dict(os.environ, {"TGBOT_DANMU_API_URL": origin + "/"}):
                endpoint, secret = danmaku.endpoint_settings()
                self.assertEqual(endpoint, origin + danmaku.ADAPTER_PATH)
                self.assertEqual(secret, self.secret)
        for origin in ("http://8.8.8.8:9321", "http://public.example.test", "http://169.254.169.254", "https://x.test/path", "https://x.test?", "https://x.test#", "https://x.test/?token=secret", "https://user:secret@x.test", "file:///tmp/x", "https://x.test:99999", "https://x.test/%2e%2e"):
            with self.subTest(origin=origin), patch.dict(os.environ, {"TGBOT_DANMU_API_URL": origin}):
                with self.assertRaises(danmaku.DanmakuError) as caught:
                    danmaku.endpoint_settings()
                self.assertEqual(caught.exception.status, 503)
                self.assertNotIn(origin, str(caught.exception))
        for secret in ("", "short-token", "has a space", "line\nbreak", "x" * 257, "a" * 32 + "."):
            with patch.dict(os.environ, {"TGBOT_DANMU_API_TOKEN": secret}):
                with self.assertRaises(danmaku.DanmakuError):
                    danmaku.endpoint_settings()

    def test_metadata_preserves_special_numbers_and_rejects_missing_season(self):
        data = danmaku.request_metadata({**self.item, "season": 0, "episode": 0, "serverUrl": "http://attacker.test", "itemId": "1", "token": "client-secret"})
        self.assertEqual(data, {**self.item, "season": 0, "episode": 0})
        legacy = {"title": "剧名", "episode": 2}
        bad = [legacy, {**self.item, "title": "\n"}, {**self.item, "title": "bad\x7ftitle"}, {**self.item, "title": "https://client.test"},
               {**self.item, "title": "😀" * 129}, {**self.item, "title": "bad\ud800title"}, {**self.item, "episode": True}, {**self.item, "season": -1},
               {**self.item, "season": "2"}, {**self.item, "type": "Series"}, {**self.item, "type": []}, {**self.item, "year": 9999},
               {**self.item, "providerIds": {"Tmdb": {"url": "x"}}}]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(danmaku.DanmakuError) as caught:
                danmaku.request_metadata(data)
            self.assertEqual(caught.exception.status, 400)

    async def test_disabled_service_makes_no_request(self):
        with patch.dict(os.environ, {"TGBOT_DANMU_API_URL": "", "TGBOT_DANMU_API_TOKEN": ""}), patch.object(danmaku.aiohttp, "ClientSession") as constructor:
            result = await danmaku.fetch_danmaku(self.item)
            self.assertFalse(result["available"])
            constructor.assert_not_called()

    async def test_fixed_endpoint_uses_only_provider_secret_and_normalized_response(self):
        payload = {"available": True, "comments": [self.comment, {**self.comment, "time": -1}, {**self.comment, "text": "x" * 301}, {**self.comment, "mode": True}],
                   "token": "do-not-return", "url": "http://upstream/private", "match": {"animeTitle": "示例剧", "episodeId": 42, "url": "http://private"}}
        client = Client(Response(payload))
        with patch.object(danmaku.aiohttp, "ClientSession", return_value=client) as constructor:
            result = await danmaku.fetch_danmaku({**self.item, "serverUrl": "https://attacker.test", "authorization": "client-secret"})
        call = client.post.call_args
        self.assertEqual(call.args, ("http://127.0.0.1:9321" + danmaku.ADAPTER_PATH,))
        self.assertEqual(call.kwargs["json"], self.item)
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer " + self.secret)
        self.assertFalse(call.kwargs["allow_redirects"])
        self.assertFalse(constructor.call_args.kwargs["trust_env"])
        self.assertEqual(constructor.call_args.kwargs["timeout"].total, 70)
        self.assertEqual(result, {"available": True, "comments": [self.comment], "match": {"animeTitle": "示例剧", "episodeId": 42}})

    async def test_errors_do_not_leak_upstream_body_or_trigger_telegram_reauth(self):
        for status, expected in ((301, 502), (401, 502), (403, 502), (400, 502), (429, 429), (500, 502), (504, 504)):
            client = Client(Response({"message": "private-fixture-token http://private.example.test"}, status))
            with self.subTest(status=status), patch.object(danmaku.aiohttp, "ClientSession", return_value=client):
                with self.assertRaises(danmaku.DanmakuError) as caught:
                    await danmaku.fetch_danmaku(self.item)
                self.assertEqual(caught.exception.status, expected)
                self.assertNotIn("private", str(caught.exception))
        for error, status in ((asyncio.TimeoutError(), 504), (aiohttp.ClientConnectionError("secret-url"), 502)):
            with patch.object(danmaku.aiohttp, "ClientSession", side_effect=error):
                with self.assertRaises(danmaku.DanmakuError) as caught:
                    await danmaku.fetch_danmaku(self.item)
                self.assertEqual(caught.exception.status, status)
                self.assertNotIn("secret-url", str(caught.exception))

    async def test_invalid_and_chunked_oversized_responses_fail_closed(self):
        for body in (b"invalid-json", {"available": True}, {"available": "true", "comments": []}, b" " * 129):
            with self.subTest(body=body), patch.object(danmaku, "MAX_RESPONSE_BYTES", 128), patch.object(danmaku.aiohttp, "ClientSession", return_value=Client(Response(body))):
                with self.assertRaises(danmaku.DanmakuError) as caught:
                    await danmaku.fetch_danmaku(self.item)
                self.assertEqual(caught.exception.status, 502)

    async def test_no_match_and_special_episode_have_safe_visible_messages(self):
        response = Response({"available": False, "comments": [], "message": "secret internal details"})
        with patch.object(danmaku.aiohttp, "ClientSession", return_value=Client(response)):
            result = await danmaku.fetch_danmaku({**self.item, "season": 0})
        self.assertFalse(result["available"])
        self.assertIn("特殊集", result["message"])
        self.assertNotIn("secret", result["message"])
        response = Response({"available": False, "comments": [], "message": "特殊季或第 0 集暂不支持自动匹配，请在播放器导入本地弹幕"})
        with patch.object(danmaku.aiohttp, "ClientSession", return_value=Client(response)):
            result = await danmaku.fetch_danmaku({"title": "Show.S00E01.mkv", "type": "local"})
        self.assertFalse(result["available"])
        self.assertIn("特殊集", result["message"])

    def test_output_byte_limit_counts_utf8_and_discards_extra_fields(self):
        comments = [{**self.comment, "url": "secret"}] * 20
        with patch.object(danmaku, "MAX_OUTPUT_BYTES", 300):
            result = danmaku.response_comments({"available": True, "comments": comments})
        self.assertGreater(len(result["comments"]), 0)
        self.assertLess(len(result["comments"]), 20)
        encoded = json.dumps(result["comments"], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), 301)
        self.assertNotIn(b"secret", encoded)


if __name__ == "__main__":
    unittest.main()
