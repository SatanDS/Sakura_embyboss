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
            answer = respond(request)
            status, result = answer[:2]
            data = result if isinstance(result, bytes) else json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for key, value in (answer[2] if len(answer) > 2 else {}).items():
                self.send_header(key, value)
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
        cls.completion_mod = importlib.import_module("bot.dushengtv.request_completion")

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

    async def test_verified_cross_source_ids_share_one_subscription_and_detail_record(self):
        await self.service.subscribe(self.identity, self.movie["key"], None, self.recheck)
        douban = self.media.normalize({"douban_id": 888, "tmdb_id": 123, "title": "片名", "year": 2026, "type": "电影"}, "douban", "movie")
        self.gateway.detail.return_value = douban
        second = await self.service.subscribe(self.identity, douban["key"], None, self.recheck)
        self.assertTrue(second["duplicate"])
        self.gateway.subscribe.assert_awaited_once()
        self.assertEqual((await self.service.detail(self.identity, douban["key"]))["subscription"]["state"], "subscribed")

    async def test_mp_season_info_and_cast_are_normalized_without_untrusted_images(self):
        item = self.media.normalize({"tmdb_id": 456, "title": "剧名", "type": "电视剧", "seasons": {"1": [1, 2]},
            "season_info": [{"season_number": 0, "episode_count": 2}], "actors": [{"name": "演员", "character": "角色", "profile_path": "/actor.jpg"},
            {"name": "演员二", "profile_path": "https://attacker.test/avatar"}]}, "tmdb", "tv")
        self.assertEqual(item["seasons"][0]["number"], 0)
        self.assertEqual(item["cast"][0]["photo"], "https://image.tmdb.org/t/p/w185/actor.jpg")
        self.assertEqual(item["cast"][1]["photo"], "")

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

    async def test_search_catalog_prefers_tmdb_and_isolates_cache_dimensions(self):
        alternative = self.media.normalize({"douban_id": 999, "title": self.movie["title"], "year": 2026, "type": "movie"}, "douban", "movie")
        self.gateway.search = AsyncMock(return_value={"items": [alternative, self.movie, self.movie], "hasMore": True})
        result = await self.service.catalog("movie", 1, "  The   Movie  ")
        self.assertEqual([row["key"] for row in result["items"]], [self.movie["key"]])
        self.assertTrue(result["hasMore"])
        self.gateway.search.assert_awaited_once_with("movie", 1, "The Movie")
        self.assertEqual(await self.service.catalog("movie", 1, "The Movie"), result)
        self.gateway.search.assert_awaited_once()
        for kind, page, query in (("tv", 1, "The Movie"), ("movie", 2, "The Movie"), ("movie", 1, "Other Movie")):
            await self.service.catalog(kind, page, query)
        self.assertEqual(self.gateway.search.await_count, 4)
        self.gateway.recommend.assert_not_awaited()

    async def test_empty_search_uses_recommendations_and_invalid_query_does_no_io(self):
        self.gateway.search = AsyncMock()
        await self.service.catalog("movie", 1, "   ")
        self.gateway.search.assert_not_awaited()
        self.assertEqual(self.gateway.recommend.await_count, 2)
        self.gateway.recommend.reset_mock()
        with patch.object(self.store, "mine") as mine:
            for query in (None, [], 42, "x" * 129, "title\nmore", "title\x00more"):
                with self.subTest(query=query):
                    with self.assertRaises(self.mod.TVError) as caught:
                        await self.service.catalog("movie", 1, query)
                    self.assertEqual(caught.exception.code, "INVALID_REQUEST")
                    with self.assertRaises(self.mod.TVError):
                        await self.service.mine(self.identity, 1, query)
            mine.assert_not_called()
        self.gateway.search.assert_not_awaited()
        self.gateway.recommend.assert_not_awaited()

    async def test_metadata_search_get_filters_mixed_types_and_preserves_raw_pagination(self):
        rows = [
            {"tmdb_id": 100, "title": "Film", "type": "电影", "production_countries": 3, "production_companies": {}},
            {"douban_id": 200, "title": "Douban", "media_type": "movie", "tmdb_id": "invalid"},
            {"tmdb_id": 300, "title": "Series", "type": "电视剧"},
            {"tmdb_id": 400, "title": "Missing type"},
            {"tmdb_id": 500, "title": "Bad type", "type": ["movie"]},
            {"tmdb_id": 600, "title": "Person", "type": "person"},
            None,
        ]
        def respond(request):
            self.assertEqual(request["method"], "GET")
            self.assertEqual(request["url"].path, "/api/v1/media/search")
            self.assertEqual(request["body"], b"")
            self.assertEqual(parse_qs(request["url"].query), {"title": ["流浪地球"], "type": ["media"], "page": ["2"], "count": ["30"]})
            return 200, rows
        with fixture_server(respond) as (origin, calls):
            gateway = self.gateway_mod.MoviePilotGateway(origin, "Bearer fixture")
            movie = await gateway.search("movie", 2, "流浪地球")
            self.assertEqual([item["key"] for item in movie["items"]], ["tmdb:movie:100", "douban:movie:200"])
            self.assertEqual(movie["items"][0]["countries"], [])
            self.assertFalse(movie["hasMore"])
            tv = await gateway.search("tv", 2, "流浪地球")
            self.assertEqual([item["key"] for item in tv["items"]], ["tmdb:tv:300"])
            rows[:] = [{"tmdb_id": 1000 + index, "title": f"Series {index}", "type": "tv"} for index in range(30)]
            empty = await gateway.search("movie", 2, "流浪地球")
            self.assertEqual(empty, {"items": [], "hasMore": True})
            self.assertEqual(len(calls), 3)

    async def test_mine_search_is_literal_owner_scoped_and_paginates_stably(self):
        self.store.daily_limit = 100
        def item(number, title, original=""):
            return self.media.normalize({"tmdb_id": number, "title": title, "original_title": original, "type": "movie"}, "tmdb", "movie")
        for number, title, original in ((100, "百分之100%", "Original Title"), (101, "下划_线", "Other"), (102, "Normal", "Another")):
            self.store.claim(10, item(number, title, original), None)
        self.store.claim(20, item(103, "百分之100%", "Original Title"), None)
        self.assertEqual([row["item"]["id"] for row in self.store.mine(10, 1, "%")["items"]], ["100"])
        self.assertEqual([row["item"]["id"] for row in self.store.mine(10, 1, "_")["items"]], ["101"])
        self.assertEqual([row["item"]["id"] for row in self.store.mine(10, 1, "ORIGINAL title")["items"]], ["100"])
        self.assertEqual(self.store.mine(30, 1, "%")["items"], [])
        for number in range(200, 235):
            self.store.claim(10, item(number, f"分页 {number}"), None)
        first = self.store.mine(10, 1, "分页")
        second = self.store.mine(10, 2, "分页")
        self.assertEqual((len(first["items"]), len(second["items"])), (30, 5))
        self.assertTrue(first["hasMore"])
        self.assertFalse(second["hasMore"])
        self.assertEqual(first, self.store.mine(10, 1, "分页"))
        self.assertEqual(len({row["requestId"] for row in first["items"] + second["items"]}), 35)

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

    async def test_mine_refreshes_movie_completion_and_isolates_last_known_visibility(self):
        record = await self.service.subscribe(self.identity, self.movie["key"], None, self.recheck)
        completion = AsyncMock(return_value={record["requestId"]: True})
        self.service.completion = completion
        first = await self.service.mine(self.identity, 1)
        self.assertEqual(first["items"][0]["state"], "complete")
        self.assertEqual(self.store.mine(10, 1)["items"][0]["state"], "complete")
        await self.service.mine(self.identity, 1)
        completion.assert_awaited_once()
        # A delayed accepted-subscription response cannot undo delivery.
        self.assertEqual(self.store.update(record["requestId"], "subscribed", "late-mp-id")["state"], "complete")
        self.service.cache.clear()
        completion.return_value = {record["requestId"]: False}
        self.assertEqual((await self.service.mine(self.identity, 1))["items"][0]["state"], "subscribed")
        self.assertEqual(self.store.mine(10, 1)["items"][0]["state"], "complete")
        self.service.cache.clear()
        completion.side_effect = self.mod.TVError("LIBRARY_UNAVAILABLE", "temporary", 502)
        self.assertEqual((await self.service.mine(self.identity, 1))["items"][0]["state"], "subscribed")
        self.store.claim(20, self.movie, None)
        second_user = {"telegramId": "20", "embyUserId": "other-user"}
        self.assertEqual((await self.service.mine(second_user, 1))["items"][0]["state"], "subscribed")
        completion.side_effect = None
        self.service.cache.clear()
        completion.return_value = {record["requestId"]: True}
        self.assertEqual((await self.service.mine(self.identity, 1))["items"][0]["state"], "complete")
        self.service.cache.clear()
        completion.return_value = {}
        self.assertEqual((await self.service.mine(self.identity, 1))["items"][0]["state"], "complete")

    async def test_mine_checks_subscribed_seasons_independently_and_refreshes_details_too(self):
        self.gateway.detail.return_value = self.tv
        zero = await self.service.subscribe(self.identity, self.tv["key"], 0, self.recheck)
        one = await self.service.subscribe(self.identity, self.tv["key"], 1, self.recheck)
        self.service.completion = AsyncMock(return_value={zero["requestId"]: True, one["requestId"]: False})
        mine = await self.service.mine(self.identity, 1)
        self.assertEqual({row["season"]: row["state"] for row in mine["items"]}, {0: "complete", 1: "subscribed"})
        detail = await self.service.detail(self.identity, self.tv["key"])
        self.assertEqual({row["season"]: row["state"] for row in detail["subscriptions"]}, {0: "complete", 1: "subscribed"})
        self.service.completion.assert_awaited_once()

    async def test_real_completion_batches_identity_lookup_and_requires_playable_exact_season(self):
        tv = {**self.tv, "seasons": [{"number": 0, "episodeCount": 2}, {"number": 1, "episodeCount": 3}]}
        gateway = types.SimpleNamespace(detail=AsyncMock(return_value=tv), request=AsyncMock(side_effect=lambda method, path: [
            {"season_number": int(path.rsplit('/', 1)[1]), "episode_number": number, "show_id": 456}
            for number in ([2, 8] if path.endswith('/0') else [1, 2, 3])]))
        records = [{"requestId": "movie", "item": self.movie, "season": None},
                   {"requestId": "specials", "item": tv, "season": 0}, {"requestId": "season-one", "item": tv, "season": 1}]
        source = [{"Path": "/media/video.mkv"}]
        movie_missing, season_arrived = False, False
        def episode(number, season, **extra):
            return {"Type": "Episode", "Id": f"ep-{season}-{number}", "SeriesId": "series", "ParentIndexNumber": season,
                    "IndexNumber": number, "MediaSources": source, **extra}
        def respond(request):
            params = parse_qs(request["url"].query)
            self.assertEqual(request["headers"].get("X-Emby-Token"), "fixture")
            if request["url"].path == "/Users/user-1/Items":
                self.assertEqual(params["IncludeItemTypes"], ["Movie,Series"])
                self.assertIn("MediaSources", params["Fields"][0])
                return 200, {"Items": [
                    {"Id": "wrong-type", "Type": "Series", "ProviderIds": {"Tmdb": "123"}, "MediaSources": source},
                    {"Id": "conflict", "Type": "Movie", "ProviderIds": {"Tmdb": "123", "Imdb": "tt999"}, "MediaSources": source},
                    {"Id": "virtual", "Type": "Movie", "ProviderIds": {"Tmdb": "123"}, "LocationType": "Virtual", "MediaSources": source},
                    {"Id": "movie", "Type": "Movie", "ProviderIds": {"Tmdb": "123", "Imdb": "tt123"}, "MediaSources": [] if movie_missing else source},
                    {"Id": "series", "Type": "Series", "ProviderIds": {"Tmdb": "456"}}]}
            self.assertEqual(request["url"].path, "/Shows/series/Episodes")
            self.assertEqual(params["UserId"], ["user-1"])
            if params["Season"] == ["0"]:
                return 200, {"Items": [episode(2, 0), episode(8, 0)]}
            if season_arrived:
                return 200, {"Items": [episode(1, 1, IndexNumberEnd=3)]}
            return 200, {"Items": [episode(1, 1), episode(1, 1), episode(3, 1), episode(2, 2),
                episode(2, 1, IsMissing=True), episode(2, 1, SeriesId="other-series"), episode(2, 1, IsVirtualItem=True)]}
        with fixture_server(respond) as (origin, calls):
            checker = self.completion_mod.RequestCompletion(origin, "fixture", gateway)
            result = await checker(self.identity, records)
            self.assertEqual(result, {"movie": True, "specials": True, "season-one": False})
            self.assertEqual(sum(call["url"].path == "/Users/user-1/Items" for call in calls), 1)
            gateway.detail.assert_awaited_once()
            movie_missing, season_arrived = True, True
            self.assertEqual(await checker(self.identity, records), {"movie": False, "specials": True, "season-one": True})
        self.assertEqual(self.completion_mod.episode_numbers([episode(1, 1, IndexNumberEnd=3)], "series", 1), {1, 2, 3})
        self.assertFalse(self.completion_mod.playable({"Type": "Movie", "Path": "/folder", "MediaSources": []}))

    async def test_expected_season_does_not_guess_unknown_total_or_ignore_identity_and_duplicate_numbers(self):
        tv = {**self.tv, "seasons": [{"number": 0, "episodeCount": 2}]}
        gateway = types.SimpleNamespace(detail=AsyncMock(return_value=tv), request=AsyncMock(return_value=[
            {"season_number": 0, "episode_number": 2}, {"season_number": 0, "episode_number": 8}]))
        checker = self.completion_mod.RequestCompletion("http://127.0.0.1:1", "fixture", gateway)
        self.assertEqual(await checker.expected(tv, 0), {2, 8})
        for invalid in ([{"season_number": 0, "episode_number": 2}] * 2,
                        [{"season_number": 0, "episode_number": 2}, {"season_number": 1, "episode_number": 8}],
                        [{"season_number": 0, "episode_number": 2}, {"season_number": 0, "episode_number": 8, "show_id": 999}]):
            checker.metadata.clear(); gateway.request.return_value = invalid
            self.assertIsNone(await checker.expected(tv, 0))
        checker.metadata.clear(); gateway.request.return_value = [{"season_number": 0, "episode_number": 2}, {"season_number": 0, "episode_number": 8}]
        self.assertIsNone(await checker.expected({**tv, "seasons": [{"number": 0, "episodeCount": 10}]}, 0))
        checker.metadata.clear(); gateway.detail.return_value = {**tv, "seasons": []}
        self.assertIsNone(await checker.expected({**tv, "seasons": []}, 0))
        checker.metadata.clear(); gateway.detail.return_value = {**tv, "providerIds": {"Tmdb": "999"}}
        self.assertIsNone(await checker.expected(tv, 0))

    async def test_completion_total_deadline_keeps_fast_results_and_shared_metadata_finishes_cleanly(self):
        gate = asyncio.Event()
        async def slow_detail(_):
            await gate.wait()
            return self.tv
        gateway = types.SimpleNamespace(detail=AsyncMock(side_effect=slow_detail), request=AsyncMock())
        def respond(_):
            return 200, {"Items": [
                {"Id": "movie", "Type": "Movie", "ProviderIds": {"Tmdb": "123"}, "MediaSources": [{"Path": "/movie.mkv"}]},
                {"Id": "series", "Type": "Series", "ProviderIds": {"Tmdb": "456"}}]}
        with fixture_server(respond) as (origin, _):
            checker = self.completion_mod.RequestCompletion(origin, "fixture", gateway, budget_seconds=.08)
            started = asyncio.get_running_loop().time()
            result = await checker(self.identity, [{"requestId": "movie", "item": self.movie}, {"requestId": "series", "item": self.tv, "season": 1}])
            self.assertEqual(result, {"movie": True})
            self.assertLess(asyncio.get_running_loop().time() - started, .3)
            self.assertEqual(len(checker.pending), 1)
            gate.set()
            for _ in range(30):
                if not checker.pending:
                    break
                await asyncio.sleep(.01)
            self.assertEqual(checker.pending, {})
            self.assertIn(("media", "456"), checker.metadata)

    async def test_metadata_work_is_bounded_and_cancellation_consumes_failed_tasks(self):
        checker = self.completion_mod.RequestCompletion("http://127.0.0.1:1", "fixture", self.gateway)
        active, peak = 0, 0
        async def load():
            nonlocal active, peak
            active += 1; peak = max(peak, active)
            await asyncio.sleep(.01)
            active -= 1
            return []
        await asyncio.gather(*(checker._metadata(("test", number), load) for number in range(18)))
        self.assertEqual(peak, 3)
        self.assertEqual(checker.pending, {})
        gate = asyncio.Event()
        async def fail():
            await gate.wait()
            raise self.mod.TVError("UPSTREAM", "failure")
        waiting = asyncio.create_task(checker._metadata(("cancelled",), fail))
        await asyncio.sleep(.01); waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        gate.set(); await asyncio.sleep(.02)
        self.assertEqual(checker.pending, {})

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

    async def test_official_moviepilot_image_shapes_and_sizes(self):
        # MP MediaCard replaces original with w500; PersonCard supports
        # Douban avatar.normal objects and TMDB profile_path strings.
        item = self.media.normalize({"tmdb_id": 123, "title": "实际字段形态", "type": "电影",
            "poster_path": "https://tmdb-image.example/t/p/original/poster.jpg",
            "backdrop_path": "https://image.tmdb.org/t/p/original/background.jpg",
            "actors": [{"name": "演员", "avatar": {"normal": "https://img1.doubanio.com/view/celebrity/s_ratio_celebrity/public/p1.webp"}},
                       {"name": "演员2", "profile_path": "/actor.jpg"}]}, "tmdb", "movie")
        self.assertEqual(item["poster"], "https://image.tmdb.org/t/p/w500/poster.jpg")
        self.assertEqual(item["backdrop"], "https://image.tmdb.org/t/p/w1280/background.jpg")
        self.assertEqual(item["cast"][0]["photo"], "https://img1.doubanio.com/view/celebrity/s_ratio_celebrity/public/p1.webp")
        self.assertEqual(item["cast"][1]["photo"], "https://image.tmdb.org/t/p/w185/actor.jpg")
        for value in ("/../config", "//localhost/secret", "https://image.tmdb.org/api/private", "https://image.tmdb.org/t/p/w500/a.jpg?token=hidden"):
            self.assertEqual(self.media.image_url(value), "")

    async def test_real_image_proxy_cookie_refresh_binary_validation_and_coalescing(self):
        png = b"\x89PNG\r\n\x1a\n" + b"fixture-bytes"
        def respond(request):
            self.assertEqual(request["headers"].get("Authorization"), "Bearer fixture-api")
            if request["url"].path == "/api/v1/user/current":
                return 200, {"name": "fixture"}, {"Set-Cookie": "MoviePilot=fixture-resource; HttpOnly; Path=/"}
            self.assertEqual(request["url"].path, "/api/v1/system/img/1")
            self.assertEqual(parse_qs(request["url"].query), {"imgurl": ["https://image.tmdb.org/t/p/w500/a.jpg"], "cache": ["true"]})
            if request["headers"].get("Cookie") != "MoviePilot=fixture-resource":
                return 401, {"detail": "resource cookie required"}
            return 200, png, {"Content-Type": "image/png"}
        with fixture_server(respond) as (origin, calls):
            gateway = self.gateway_mod.MoviePilotGateway(origin, "Bearer fixture-api")
            results = await asyncio.gather(*(gateway.images.get("https://image.tmdb.org/t/p/w500/a.jpg") for _ in range(12)))
            self.assertTrue(all(row == (png, "image/png") for row in results))
            self.assertEqual(len(calls), 3)
            self.assertEqual(await gateway.images.get("https://image.tmdb.org/t/p/w500/a.jpg"), results[0])
            self.assertEqual(len(calls), 3)

    async def test_resource_cookie_received_on_catalog_survives_to_douban_proxy(self):
        def respond(request):
            if request["url"].path == "/api/v1/recommend/douban_movies":
                return 200, [], {"Set-Cookie": "MoviePilot=resource-from-api; HttpOnly; Path=/"}
            self.assertEqual(request["url"].path, "/api/v1/system/img/0")
            self.assertEqual(request["headers"].get("Cookie"), "MoviePilot=resource-from-api")
            return 200, b"\xff\xd8\xffvalid-jpeg-signature", {"Content-Type": "image/jpeg"}
        with fixture_server(respond) as (origin, calls):
            gateway = self.gateway_mod.MoviePilotGateway(origin, "Bearer fixture-api")
            await gateway.recommend("douban", "movie", 1)
            image = await gateway.images.get("https://img1.doubanio.com/view/photo/a.jpg")
            self.assertEqual(image[1], "image/jpeg")
            self.assertEqual(len(calls), 2)

    async def test_image_proxy_rejects_ssrf_html_and_redirects_without_requesting_target(self):
        gateway = self.gateway_mod.MoviePilotGateway("http://127.0.0.1:1", "Bearer fixture")
        for url in ("https://127.0.0.1/private", "https://mirror.invalid/t/p/w500/a.jpg", "https://image.tmdb.org/api/private"):
            with self.assertRaises(self.mod.TVError) as caught:
                await gateway.images.get(url)
            self.assertEqual(caught.exception.code, "INVALID_REQUEST_IMAGE")
        for status, body, headers in ((200, b"<html>login</html>", {}), (302, b"", {"Location": "http://127.0.0.1/private"})):
            with fixture_server(lambda _: (status, body, headers)) as (origin, calls):
                gateway = self.gateway_mod.MoviePilotGateway(origin, "Bearer fixture")
                with self.assertRaises(self.mod.TVError):
                    await gateway.images.get("https://image.tmdb.org/t/p/w500/a.jpg")
                self.assertEqual(len(calls), 1)

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
