"""Offline subscription regressions; no production API, secrets or messages."""
import asyncio
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@contextmanager
def fixture_server(respond):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def handle_request(self):
            request = {"method": self.command, "url": urlsplit(self.path), "headers": dict(self.headers),
                       "body": self.rfile.read(int(self.headers.get("Content-Length", "0")))}
            calls.append(request)
            status, result = respond(request)
            data = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        do_GET = do_POST = handle_request
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class RequestTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = {k: v for k, v in sys.modules.items() if k == "bot" or k.startswith("bot.")}
        for key in cls.original:
            del sys.modules[key]
        bot = types.ModuleType("bot")
        bot.__path__ = [str(ROOT / "bot")]
        sql = types.ModuleType("bot.sql_helper")
        sql.Base = declarative_base()
        sys.modules.update({"bot": bot, "bot.sql_helper": sql})
        cls.sql = sql
        cls.media = importlib.import_module("bot.dushengtv.request_media")
        cls.mod = importlib.import_module("bot.dushengtv.requests")
        cls.gateway_mod = importlib.import_module("bot.dushengtv.request_gateway")

    @classmethod
    def tearDownClass(cls):
        for key in list(sys.modules):
            if key == "bot" or key.startswith("bot."):
                del sys.modules[key]
        sys.modules.update(cls.original)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.temp.name) / "test.sqlite"), connect_args={"check_same_thread": False})
        self.sql.Base.metadata.create_all(self.engine)
        self.store = self.mod.RequestStore(sessionmaker(bind=self.engine), 20)
        self.movie = self.media.normalize({"tmdb_id": 123, "imdb_id": "tt123", "title": "片名", "year": 2026, "type": "电影"}, "tmdb", "movie")
        self.tv = self.media.normalize({"tmdb_id": 456, "title": "剧名", "year": 2026, "type": "电视剧", "seasons": [{"season_number": 0}, {"season_number": 1}, {"season_number": 2}]}, "tmdb", "tv")
        self.gateway = types.SimpleNamespace(detail=AsyncMock(return_value=self.movie), recommend=AsyncMock(return_value=[self.movie]), find_subscription=AsyncMock(return_value=None), subscribe=AsyncMock(return_value="77"))
        self.library = AsyncMock(return_value={"available": False, "items": []})
        self.service = self.mod.MediaRequests(self.gateway, self.store, self.library)
        self.identity = {"telegramId": "10", "embyUserId": "user-1"}
        self.recheck = AsyncMock()

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    async def test_direct_subscription_owner_and_shared_deduplication(self):
        first = await self.service.subscribe(self.identity, self.movie["key"], None, self.recheck)
        second = await self.service.subscribe({**self.identity, "telegramId": "20"}, self.movie["key"], None, self.recheck)
        self.assertEqual(first["state"], "subscribed")
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.gateway.subscribe.assert_awaited_once()
        self.assertEqual(len(self.store.mine(20, 1)["items"]), 1)
        self.assertEqual(self.store.mine(30, 1)["items"], [])

    async def test_same_time_calls_only_submit_once(self):
        async def accept(*_):
            await asyncio.sleep(.05)
            return "77"
        self.gateway.subscribe.side_effect = accept
        result = await asyncio.gather(*(self.service.subscribe(self.identity, self.movie["key"], None, self.recheck) for _ in range(3)))
        self.gateway.subscribe.assert_awaited_once()
        self.assertTrue(any(row["state"] == "subscribed" for row in result))

    async def test_existing_movie_only_plays(self):
        self.library.return_value = {"available": True, "items": [{"id": "emby123"}]}
        result = await self.service.subscribe(self.identity, self.movie["key"], None, self.recheck)
        self.assertEqual(result["state"], "available")
        self.gateway.subscribe.assert_not_awaited()
        self.assertEqual(self.store.mine(10, 1)["items"], [])

    async def test_existing_external_subscription_records_requester_without_post(self):
        self.gateway.find_subscription.return_value = "old-mp-id"
        result = await self.service.subscribe(self.identity, self.movie["key"], None, self.recheck)
        self.assertEqual(result["state"], "subscribed")
        self.gateway.subscribe.assert_not_awaited()

    async def test_timeout_is_uncertain_and_retry_only_reconciles(self):
        self.gateway.subscribe.side_effect = self.mod.TVError("MOVIEPILOT_UNAVAILABLE", "timeout", 502)
        with self.assertRaises(self.mod.TVError):
            await self.service.subscribe(self.identity, self.movie["key"], None, self.recheck)
        result = await self.service.subscribe(self.identity, self.movie["key"], None, self.recheck)
        self.assertEqual(result["state"], "pending")
        self.gateway.subscribe.assert_awaited_once()
        self.gateway.find_subscription.return_value = "accepted-before-timeout"
        result = await self.service.subscribe(self.identity, self.movie["key"], None, self.recheck)
        self.assertEqual(result["state"], "subscribed")

    async def test_definitive_reject_can_retry(self):
        self.gateway.subscribe.side_effect = self.mod.TVError("SUBSCRIPTION_REJECTED", "no", 409)
        with self.assertRaises(self.mod.TVError):
            await self.service.subscribe(self.identity, self.movie["key"], None, self.recheck)
        self.assertEqual(self.store.mine(10, 1)["items"][0]["state"], "failed")
        self.gateway.subscribe.side_effect = None
        result = await self.service.subscribe(self.identity, self.movie["key"], None, self.recheck)
        self.assertEqual(result["state"], "subscribed")

    async def test_revoked_between_lookups_and_post_cannot_subscribe(self):
        self.recheck.side_effect = [None, self.mod.TVError("TOKEN_EXPIRED", "revoked", 401)]
        with self.assertRaises(self.mod.TVError):
            await self.service.subscribe(self.identity, self.movie["key"], None, self.recheck)
        self.gateway.subscribe.assert_not_awaited()

    async def test_seasons_are_distinct_and_special_zero_is_not_one(self):
        self.gateway.detail.return_value = self.tv
        zero = await self.service.subscribe(self.identity, self.tv["key"], 0, self.recheck)
        one = await self.service.subscribe(self.identity, self.tv["key"], 1, self.recheck)
        self.assertNotEqual(zero["requestId"], one["requestId"])
        self.assertEqual(self.gateway.subscribe.await_args_list[0].args[1], 0)
        for invalid in (None, -1, 3, True, "1"):
            with self.assertRaises(self.mod.TVError):
                await self.service.subscribe(self.identity, self.tv["key"], invalid, self.recheck)
        self.assertEqual(self.gateway.subscribe.await_count, 2)

    async def test_catalog_tmdb_wins_and_one_failure_is_visible(self):
        alternative = self.media.normalize({"douban_id": 999, "title": "片名", "year": 2026, "type": "电影"}, "douban", "movie")
        self.gateway.recommend.side_effect = [[self.movie], [alternative]]
        result = await self.service.catalog("movie", 1)
        self.assertEqual([row["source"] for row in result["items"]], ["tmdb"])
        self.gateway.recommend.side_effect = [[self.movie], self.mod.TVError("FAILED", "no")]
        result = await self.service.catalog("movie", 2)
        self.assertEqual(result["warnings"][0]["source"], "douban")

    async def test_singleflight_details_and_no_identity_fallback(self):
        await asyncio.gather(*(self.service.media(self.movie["key"]) for _ in range(5)))
        self.gateway.detail.assert_awaited_once()
        for invalid in ("tmdb:movie:../../config", "tmdb:person:123", "https://secret.test", None):
            with self.assertRaises(self.mod.TVError):
                await self.service.media(invalid)

    async def test_daily_limit_only_new_ownership(self):
        self.store.daily_limit = 1
        await self.service.subscribe(self.identity, self.movie["key"], None, self.recheck)
        self.gateway.detail.return_value = self.tv
        with self.assertRaises(self.mod.TVError) as caught:
            await self.service.subscribe(self.identity, self.tv["key"], 1, self.recheck)
        self.assertEqual(caught.exception.code, "REQUEST_LIMIT_REACHED")

    async def test_transport_contract_and_nonretry_on_uncertain_mutation(self):
        gateway = self.gateway_mod.MoviePilotGateway("http://127.0.0.1:1", "Bearer fixture")
        gateway._request = AsyncMock(return_value={"success": True, "data": {"id": 50}})
        self.assertEqual(await gateway.subscribe(self.tv, 0), "50")
        method, endpoint = gateway._request.await_args.args
        body = gateway._request.await_args.kwargs["data"]
        self.assertEqual((method, endpoint), ("POST", "/subscribe/"))
        self.assertEqual((body["season"], body["media_id"], body["tmdbid"]), (0, "456", 456))
        self.assertNotIn("username", body)  # TG ownership is in our DB; do not spoof MP user.
        gateway._request.reset_mock()
        gateway._request.side_effect = self.mod.TVError("MOVIEPILOT_UNAVAILABLE", "timeout", 502)
        with self.assertRaises(self.mod.TVError):
            await gateway.subscribe(self.movie, None)
        gateway._request.assert_awaited_once()

    async def test_strict_provider_identity_conflicts_and_image_allowlist(self):
        same = {**self.movie, "providerIds": {"Tmdb": "123", "Imdb": "tt999"}}
        self.assertFalse(self.media.same_identity(self.movie, same))
        self.assertEqual(self.media.image_url("https://image.tmdb.org/t/p/w500/a.jpg"), "https://image.tmdb.org/t/p/w500/a.jpg")
        self.assertEqual(self.media.image_url("https://img1.doubanio.com/view/photo/a.jpg"), "https://img1.doubanio.com/view/photo/a.jpg")
        for invalid in ("http://image.tmdb.org/a", "https://image.tmdb.org.evil.test/a", "https://name:secret@image.tmdb.org/a", "https://127.0.0.1/private"):
            self.assertEqual(self.media.image_url(invalid), "")

    async def test_moviepilot_v2_detail_is_checked_by_requested_id(self):
        gateway = self.gateway_mod.MoviePilotGateway("http://127.0.0.1:1", "Bearer fixture")
        gateway.request = AsyncMock(return_value={"tmdb_id": 999, "title": "同名", "type": "电影"})
        with self.assertRaises(self.mod.TVError) as caught:
            await gateway.detail("tmdb:movie:123")
        self.assertEqual(caught.exception.code, "MEDIA_NOT_FOUND")
        self.assertEqual(gateway.request.await_args.args[1], "/media/tmdb:123")

    async def test_real_http_gateway_refreshes_auth_and_posts_only_exact_identity(self):
        def respond(request):
            if request["url"].path == "/api/v1/login/access-token":
                self.assertEqual(parse_qs(request["body"].decode()), {"username": ["fixture-user"], "password": ["fixture-password"]})
                return 200, {"access_token": "fixture-fresh", "token_type": "bearer"}
            if request["headers"].get("Authorization") != "Bearer fixture-fresh":
                return 401, {"detail": "expired"}
            if request["url"].path == "/api/v1/media/tmdb:123":
                return 200, {"tmdb_id": 123, "title": "片名", "type": "电影", "year": 2026}
            if request["url"].path == "/api/v1/subscribe/":
                if request["method"] == "GET":
                    return 200, []
                self.assertEqual(json.loads(request["body"])["tmdbid"], 123)
                return 200, {"success": True, "data": {"id": 987}}
            return 404, {}
        with fixture_server(respond) as (origin, calls):
            gateway = self.gateway_mod.MoviePilotGateway(origin, "Bearer fixture-old", "fixture-user", "fixture-password")
            item = await gateway.detail("tmdb:movie:123")
            self.assertEqual(item["title"], "片名")
            self.assertIsNone(await gateway.find_subscription(item, None))
            self.assertEqual(await gateway.subscribe(item, None), "987")
            self.assertEqual(sum(c["method"] == "POST" and c["url"].path == "/api/v1/subscribe/" for c in calls), 1)

    async def test_real_emby_lookup_is_user_scoped_and_rejects_loose_server_filter(self):
        def respond(request):
            self.assertEqual(request["url"].path, "/Users/user-1/Items")
            self.assertEqual(request["headers"].get("X-Emby-Token"), "fixture-emby-key")
            params = parse_qs(request["url"].query)
            self.assertIn("Tmdb.123", params["AnyProviderIdEquals"][0])
            return 200, {"Items": [{"Id": "wrong", "Name": "同名", "Type": "Movie", "ProviderIds": {"Tmdb": "999"}},
                                   {"Id": "conflict", "Type": "Movie", "ProviderIds": {"Tmdb": "123", "Imdb": "tt999"}},
                                   {"Id": "correct", "Name": "片名", "Type": "Movie", "ProviderIds": {"Tmdb": "123", "Imdb": "tt123"}}]}
        with fixture_server(respond) as (origin, _):
            result = await self.gateway_mod.library_lookup(origin, "fixture-emby-key", "user-1", self.movie)
            self.assertEqual([item["id"] for item in result["items"]], ["correct"])


if __name__ == "__main__":
    unittest.main()
