#!/usr/bin/env python3
"""Offline HTTP regressions; application configuration and services are stubbed."""
import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode

from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
API_KEY = "a" * 64
INTERNAL_KEY = "b" * 64
BOT_TOKEN = "12345:" + "z" * 35


class APISecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        originals = {name: value for name, value in sys.modules.items()
                     if name == "bot" or name.startswith("bot.")}

        def restore():
            for name in list(sys.modules):
                if name == "bot" or name.startswith("bot."):
                    del sys.modules[name]
            sys.modules.update(originals)

        self.addCleanup(restore)
        for name in originals:
            del sys.modules[name]

        def module(name, path=None, **values):
            result = types.ModuleType(name)
            if path is not None:
                result.__path__ = [str(ROOT / path)]
            result.__dict__.update(values)
            sys.modules[name] = result
            return result

        self.config = types.SimpleNamespace(api=types.SimpleNamespace(
            api_key=API_KEY, line_report_token=INTERNAL_KEY, allow_legacy_bot_token=False))
        self.bot = types.SimpleNamespace(send_message=AsyncMock(return_value=types.SimpleNamespace(forward=AsyncMock())))
        logger = types.SimpleNamespace(**{name: Mock() for name in ("error", "warning", "info", "debug")})
        module("bot", "bot", config=self.config, bot=self.bot, LOGGER=logger, bot_token=BOT_TOKEN, group=[1])
        module("bot.web", "bot/web")
        module("bot.func_helper", "bot/func_helper")
        self.emby = types.SimpleNamespace(emby_change_policy=AsyncMock(return_value=True),
                                          authority_account=AsyncMock(return_value=(True, "user-1")))
        module("bot.func_helper.emby", emby=self.emby)
        module("bot.sql_helper", "bot/sql_helper", Session=Mock())
        self.user = types.SimpleNamespace(tg=101, embyid="user-1", name="test-user", lv="b", iv=10, cr=None, ex=None)
        module("bot.sql_helper.sql_emby", Emby=type("Emby", (), {"embyid": "embyid", "tg": "tg"}),
               sql_get_emby=Mock(return_value=self.user),
               sql_get_emby_by_embyid=Mock(return_value=self.user), sql_update_emby=Mock(return_value=True))
        module("bot.sql_helper.sql_favorites", EmbyFavorites=Mock(), sql_add_favorites=Mock())

        self.api = importlib.import_module("bot.web.api")
        self.playlist = importlib.import_module("bot.web.api.ban_playlist")
        self.identity = importlib.import_module("bot.web.api.webhook.line_report")

        async def identify(token, auth_header=""):
            return ("user-1", "") if token == "valid-client-token" else ("", "invalid credential")

        self.identity._get_user_from_token = AsyncMock(side_effect=identify)
        self.identity._fetch_active_sessions_result = AsyncMock(return_value=(True, [], ""))
        self.app = FastAPI()
        self.app.include_router(self.api.emby_api_route)
        self.app.include_router(self.api.user_api_route)
        self.app.include_router(self.api.auth_api_route)

    async def request(self, path, *, headers=None, query=None, method="GET", body=None, peer="127.0.0.1"):
        headers = dict(headers or {})
        payload = b"" if body is None else json.dumps(body).encode()
        if body is not None:
            headers["Content-Type"] = "application/json"
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                 "query_string": urlencode(query or {}).encode(), "root_path": "",
                 "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
                 "client": (peer, 12345), "server": ("localhost", 8838)}
        messages = []

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message):
            messages.append(message)

        await self.app(scope, receive, send)
        status = next(item["status"] for item in messages if item["type"] == "http.response.start")
        self.response_headers = dict(next(item["headers"] for item in messages if item["type"] == "http.response.start"))
        response = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
        return status, json.loads(response)

    def playlist_headers(self, **changes):
        headers = {"X-DuSheng-Line-Token": INTERNAL_KEY, "X-Original-Method": "POST",
                   "X-Original-URI": "/emby/Playlists?userId=user-1", "X-Emby-Token": "valid-client-token"}
        headers.update(changes)
        return headers

    async def test_missing_shared_secret_blocks_before_identity_and_policy(self):
        headers = self.playlist_headers()
        del headers["X-DuSheng-Line-Token"]
        status, _ = await self.request("/emby/ban_playlist", headers=headers)
        self.assertEqual(status, 403)
        self.identity._get_user_from_token.assert_not_awaited()
        self.emby.emby_change_policy.assert_not_awaited()

    async def test_wrong_secret_and_nonloopback_cannot_ban(self):
        for headers, peer in [(self.playlist_headers(**{"X-DuSheng-Line-Token": "wrong"}), "127.0.0.1"),
                              (self.playlist_headers(), "198.51.100.1")]:
            status, _ = await self.request("/emby/ban_playlist", headers=headers, peer=peer)
            self.assertEqual(status, 403)
        self.emby.emby_change_policy.assert_not_awaited()

    async def test_missing_or_invalid_client_token_cannot_ban(self):
        for token in ("", "invalid-client-token"):
            status, body = await self.request("/emby/ban_playlist", headers=self.playlist_headers(**{"X-Emby-Token": token}))
            self.assertEqual(status, 403)
            self.assertFalse(body["is_baned"])
        self.emby.emby_change_policy.assert_not_awaited()

    async def test_stale_original_user_id_only_bans_the_authenticated_token_owner(self):
        status, body = await self.request("/emby/ban_playlist", headers=self.playlist_headers(
            **{"X-Original-URI": "/emby/Playlists?userId=victim"}))
        self.assertEqual(status, 403)
        self.assertTrue(body["is_baned"])
        self.assertEqual(body["embyid"], "user-1")
        self.emby.emby_change_policy.assert_awaited_once_with(emby_id="user-1", disable=True)

    async def test_forged_legacy_eid_cannot_change_the_authenticated_ban_target(self):
        status, _ = await self.request("/emby/ban_playlist", headers=self.playlist_headers(), query={"eid": "victim"})
        self.assertEqual(status, 403)
        self.emby.emby_change_policy.assert_awaited_once_with(emby_id="user-1", disable=True)

    async def test_conflicting_header_tokens_cannot_ban(self):
        status, _ = await self.request("/emby/ban_playlist", headers=self.playlist_headers(
            **{"X-Emby-Authorization": 'Emby Token="different-token", UserId="user-1"'}))
        self.assertEqual(status, 403)
        self.emby.emby_change_policy.assert_not_awaited()

    async def test_authenticated_identity_drives_policy_and_lookup(self):
        status, body = await self.request("/emby/ban_playlist", headers=self.playlist_headers())
        self.assertEqual(status, 403)
        self.assertTrue(body["is_baned"])
        self.assertEqual(body["embyid"], "user-1")
        self.emby.emby_change_policy.assert_awaited_once_with(emby_id="user-1", disable=True)
        self.playlist.sql_get_emby_by_embyid.assert_called_once_with("user-1")

    async def test_query_api_key_and_authorization_header_are_supported(self):
        for headers in [self.playlist_headers(**{"X-Emby-Token": "", "X-Original-URI":
                                                "/emby/Playlists?UserId=user-1&api_key=valid-client-token"}),
                        self.playlist_headers(**{"X-Emby-Token": "", "Authorization":
                                                'Emby Token="valid-client-token", UserId="user-1"'})]:
            status, body = await self.request("/emby/ban_playlist", headers=headers)
            self.assertEqual(status, 403)
            self.assertTrue(body["is_baned"])

    async def test_non_mutation_cannot_trigger_policy(self):
        status, _ = await self.request("/emby/ban_playlist", headers=self.playlist_headers(**{"X-Original-Method": "GET"}))
        self.assertEqual(status, 400)
        self.emby.emby_change_policy.assert_not_awaited()

    async def test_local_state_is_saved_before_notification_failure(self):
        events = []
        self.playlist.sql_update_emby.side_effect = lambda *args, **kwargs: events.append("saved") or True

        async def fail_send(*args, **kwargs):
            events.append("notification")
            raise RuntimeError("Telegram unavailable")

        self.bot.send_message.side_effect = fail_send
        status, body = await self.request("/emby/ban_playlist", headers=self.playlist_headers())
        self.assertEqual(status, 403)
        self.assertTrue(body["is_baned"])
        self.assertEqual(events, ["saved", "notification"])

    async def test_database_failure_is_reported(self):
        self.playlist.sql_update_emby.return_value = False
        status, body = await self.request("/emby/ban_playlist", headers=self.playlist_headers())
        self.assertEqual(status, 503)
        self.assertTrue(body["is_baned"])
        self.bot.send_message.assert_not_awaited()

    async def test_user_api_accepts_independent_header_key(self):
        status, _ = await self.request("/user/user_info", headers={"X-API-Key": API_KEY}, query={"tg": "101"})
        self.assertEqual(status, 200)

    async def test_missing_key_and_legacy_query_rejected_by_default(self):
        for query in ({"tg": "101"}, {"tg": "101", "token": BOT_TOKEN}):
            status, _ = await self.request("/user/user_info", query=query)
            self.assertEqual(status, 401)

    async def test_legacy_mode_requires_explicit_opt_in_and_no_wrong_header(self):
        self.config.api.allow_legacy_bot_token = True
        query = {"tg": "101", "token": BOT_TOKEN}
        status, _ = await self.request("/user/user_info", query=query)
        self.assertEqual(status, 200)
        status, _ = await self.request("/user/user_info", headers={"X-API-Key": "wrong"}, query=query)
        self.assertEqual(status, 403)

    async def test_short_missing_and_bot_token_api_keys_fail_closed(self):
        for key in (None, "short", BOT_TOKEN, INTERNAL_KEY):
            self.config.api.api_key = key
            status, _ = await self.request("/user/user_info", headers={"X-API-Key": API_KEY}, query={"tg": "101"})
            self.assertEqual(status, 503)

    async def test_login_invalid_password_returns_http_401(self):
        self.emby.authority_account.return_value = (False, 0)
        status, body = await self.request("/auth/login", method="POST", headers={"X-API-Key": API_KEY},
                                          body={"username": "test-user", "password": "wrong"})
        self.assertEqual(status, 401)
        self.assertEqual(body["code"], 401)

    async def test_invalid_login_shape_returns_http_400(self):
        status, _ = await self.request("/auth/login", method="POST", headers={"X-API-Key": API_KEY}, body=[])
        self.assertEqual(status, 400)

    async def test_invalid_user_mutation_shapes_return_http_400(self):
        for path in ("/user/ban", "/user/update_credit"):
            status, _ = await self.request(path, method="POST", headers={"X-API-Key": API_KEY}, body=[])
            self.assertEqual(status, 400)

    async def test_fractional_and_invalid_credit_are_not_silently_converted(self):
        for credit in (1.5, True, "invalid"):
            status, _ = await self.request("/user/update_credit", method="POST", headers={"X-API-Key": API_KEY},
                                           body={"tg": 101, "credit": credit})
            self.assertEqual(status, 400)
        self.playlist.sql_update_emby.assert_not_called()

    async def test_user_lookup_failure_returns_http_404(self):
        user_api = importlib.import_module("bot.web.api.user_info")
        user_api.sql_get_emby.return_value = None
        status, _ = await self.request("/user/user_info", headers={"X-API-Key": API_KEY}, query={"tg": "101"})
        self.assertEqual(status, 404)

    async def test_admin_ban_does_not_fail_when_notification_is_unavailable(self):
        self.bot.send_message.side_effect = RuntimeError("Telegram unavailable")
        status, body = await self.request("/user/ban", method="POST", headers={"X-API-Key": API_KEY}, body={"query": 101})
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["lv"], "c")
        self.playlist.sql_update_emby.assert_called_once()

    async def test_real_ip_requires_loopback_and_shared_secret(self):
        headers = {"X-Proxy-Peer-IP": "198.51.100.20", "X-Proxy-Forwarded-For": "192.0.2.1"}
        for secret, peer in (("", "127.0.0.1"), ("wrong", "127.0.0.1"), (INTERNAL_KEY, "198.51.100.20")):
            status, _ = await self.request("/emby/real_ip", peer=peer,
                                           headers={**headers, "X-DuSheng-Line-Token": secret})
            self.assertEqual(status, 403)
        self.config.api.line_report_token = ""
        status, _ = await self.request("/emby/real_ip", headers={**headers, "X-DuSheng-Line-Token": INTERNAL_KEY})
        self.assertEqual(status, 403)

    async def test_real_ip_ignores_public_forged_headers(self):
        headers = {"X-DuSheng-Line-Token": INTERNAL_KEY, "X-Proxy-Peer-IP": "198.51.100.20",
                   "X-Forwarded-For": "192.0.2.99", "X-Real-IP": "192.0.2.98",
                   "X-Verified-Client-IP": "192.0.2.97"}
        status, body = await self.request("/emby/real_ip", headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"client_ip": "198.51.100.20"})
        self.assertEqual(self.response_headers[b"x-verified-client-ip"], b"198.51.100.20")
        self.assertEqual(self.response_headers[b"cache-control"], b"no-store")
        self.identity._get_user_from_token.assert_not_awaited()
        self.emby.emby_change_policy.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_real_ip_runtime_configuration_add_remove_and_invalid(self):
        headers = {"X-DuSheng-Line-Token": INTERNAL_KEY, "X-Proxy-Peer-IP": "198.51.100.20",
                   "X-Proxy-Forwarded-For": "192.0.2.99, 203.0.113.10, 198.51.100.21"}
        for proxies, expected in (([], "198.51.100.20"), (["198.51.100.0/24"], "203.0.113.10"),
                                  ([], "198.51.100.20"), (["0.0.0.0/0"], "198.51.100.20"),
                                  (["198.51.100.20", "bad"], "198.51.100.20")):
            self.config.trusted_proxy_cidrs = proxies
            status, body = await self.request("/emby/real_ip", headers=headers)
            self.assertEqual(status, 200)
            self.assertEqual(body["client_ip"], expected)

    async def test_real_ip_ipv6_and_mapped_peer(self):
        self.config.trusted_proxy_cidrs = ["198.51.100.0/24", "2001:db8:1::/64"]
        for peer, chain, expected in (("::ffff:198.51.100.20", "::ffff:192.0.2.1", "192.0.2.1"),
                                      ("2001:db8:1::2", "2001:db8:2::3", "2001:db8:2::3")):
            status, body = await self.request("/emby/real_ip", headers={
                "X-DuSheng-Line-Token": INTERNAL_KEY, "X-Proxy-Peer-IP": peer, "X-Proxy-Forwarded-For": chain,
            })
            self.assertEqual(status, 200)
            self.assertEqual(body["client_ip"], expected)

    async def test_real_ip_missing_invalid_peer_and_invalid_chain(self):
        self.config.trusted_proxy_cidrs = ["198.51.100.20"]
        headers = {"X-DuSheng-Line-Token": INTERNAL_KEY}
        status, _ = await self.request("/emby/real_ip", headers=headers)
        self.assertEqual(status, 400)
        for peer in ("", "unknown", "198.51.100.20:80"):
            status, _ = await self.request("/emby/real_ip", headers={**headers, "X-Proxy-Peer-IP": peer})
            self.assertEqual(status, 400)
        status, body = await self.request("/emby/real_ip", headers={
            **headers, "X-Proxy-Peer-IP": "198.51.100.20", "X-Proxy-Forwarded-For": "192.0.2.1, invalid",
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["client_ip"], "198.51.100.20")


if __name__ == "__main__":
    unittest.main()
