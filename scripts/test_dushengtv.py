"""Offline TV authorization regressions. No Telegram or production config is read."""

import base64
import hashlib
import importlib.util
import json
import os
import secrets
import sqlite3
import sys
import tempfile
import types
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, Column, DateTime, String, create_engine, inspect
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[1]


class LoginPage(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.links, self.scripts, self.command = [], [], ""
        self.in_command = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.links.append(attrs.get("href", ""))
        elif tag == "script":
            self.scripts.append(attrs)
        elif tag == "textarea" and attrs.get("id") == "login-command":
            self.in_command = True

    def handle_endtag(self, tag):
        if tag == "textarea":
            self.in_command = False

    def handle_data(self, data):
        if self.in_command:
            self.command += data


def load_modules():
    base = declarative_base()
    bot = types.ModuleType("bot")
    bot.__path__ = [str(ROOT / "bot")]
    bot.config = SimpleNamespace(emby_url="http://emby-origin.test:8096")
    sql = types.ModuleType("bot.sql_helper")
    sql.Base = base
    sys.modules.update({"bot": bot, "bot.sql_helper": sql})

    class User(base):
        __tablename__ = "emby"
        tg = Column(BigInteger, primary_key=True)
        embyid = Column(String(255))
        lv = Column(String(1))
        ex = Column(DateTime)
        disabled_at = Column(DateTime)

    sql_emby = types.ModuleType("bot.sql_helper.sql_emby")
    sql_emby.Emby = User
    sys.modules[sql_emby.__name__] = sql_emby
    import bot.payments.entitlements as entitlements
    import bot.dushengtv.models as models
    import bot.dushengtv.service as service
    import bot.dushengtv.runtime as runtime
    import bot.dushengtv.api as api
    import bot.dushengtv.server as server
    return SimpleNamespace(base=base, User=User, models=models, service=service, runtime=runtime,
                           api=api, server=server, entitlements=entitlements)


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = {k: v for k, v in sys.modules.items() if k == "bot" or k.startswith("bot.")}
        for k in cls.original:
            del sys.modules[k]
        cls.m = load_modules()

    @classmethod
    def tearDownClass(cls):
        for k in list(sys.modules):
            if k == "bot" or k.startswith("bot."):
                del sys.modules[k]
        sys.modules.update(cls.original)

    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        self.m.base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.now = datetime(2026, 10, 4)
        with self.sessions.begin() as db:
            db.add_all([self.m.User(tg=tg, embyid=f"emby-{tg}", lv="b", ex=datetime(2099, 1, 1)) for tg in (42, 43)])
        self.auth = self.m.service.DesktopAuth(self.sessions, self.m.runtime.account_lookup, now=lambda: self.now)
        self.installation = "test-installation-1234"
        self.private = Ed25519PrivateKey.generate()
        self.hardware = {"installationId": self.installation, "fingerprint": "a" * 64,
                         "deviceName": "测试电脑", "os": {"platform": "win32"}, "gpus": []}
        self.settings = SimpleNamespace(server_urls=["https://emby.test"], privacy_version="2026-10-03")
        self.m.api.limits.buckets.clear()
        self.patches = [patch.object(self.m.api.runtime, "service", return_value=self.auth),
                        patch.object(self.m.api.runtime, "settings", return_value=(self.settings, "https://tv-api.test", "example_bot"))]
        for p in self.patches:
            p.start()
        self.client = TestClient(self.m.server.create_app())

    def tearDown(self):
        self.client.close()
        for p in reversed(self.patches):
            p.stop()
        self.engine.dispose()

    def start(self, installation=None):
        verifier = secrets.token_urlsafe(48)
        data = {"state": secrets.token_urlsafe(32), "codeChallengeMethod": "S256", "client": "DuShengTV",
                "codeChallenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="),
                "installationId": installation or self.installation}
        result = self.auth.start(data, "https://tv-api.test", "example_bot")
        result["codeVerifier"] = verifier
        result["link"] = parse_qs(urlsplit(result["authorizationUrl"]).query)["request"][0]
        return result

    def approve(self, challenge, tg=42):
        self.auth.prepare(challenge["link"], tg, "Viewer", "viewer")
        return self.auth.decide(challenge["challengeId"], tg, True)

    def login(self, tg=42, installation=None, register=True):
        challenge = self.start(installation)
        self.approve(challenge, tg)
        tokens = self.auth.poll(challenge)
        if register:
            hardware = {**self.hardware, "installationId": installation or self.installation}
            self.auth.register(tokens["accessToken"], self.registration(tokens["accessToken"], hardware))
        return tokens

    def proof(self, token, hardware=None, private=None):
        hardware, private = hardware or self.hardware, private or self.private
        nonce = self.auth.challenge(token, hardware["installationId"])["nonce"]
        payload = json.dumps({"nonce": nonce, "fingerprint": hardware["fingerprint"], "installationId": hardware["installationId"]}, separators=(",", ":"))
        return {"payload": payload, "publicKey": private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
                "signature": base64.b64encode(private.sign(payload.encode())).decode()}

    def registration(self, token, hardware=None):
        hardware = hardware or self.hardware
        return {"device": hardware, "proof": self.proof(token, hardware), "consentVersion": "2026-10-03", "appVersion": "0.0.1"}

    def error(self, code, function, *args):
        with self.assertRaises(self.m.service.TVError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def test_telegram_confirmation_pkce_one_time_and_pending_device(self):
        challenge = self.start()
        self.assertEqual(self.auth.poll(challenge), {"status": "pending"})
        self.approve(challenge)
        self.error("STATE_MISMATCH", self.auth.poll, {**challenge, "state": "z" * 43})
        self.error("STATE_MISMATCH", self.auth.poll, {**challenge, "codeVerifier": "z" * 64})
        result = self.auth.poll(challenge)
        self.assertFalse(self.auth.session(result["accessToken"])["device"]["allowed"])
        self.error("DEVICE_REQUIRED", self.auth.devices, result["accessToken"])
        self.error("CHALLENGE_EXPIRED", self.auth.poll, challenge)
        self.auth.register(result["accessToken"], self.registration(result["accessToken"]))
        self.assertTrue(self.auth.session(result["accessToken"])["device"]["allowed"])
        with self.sessions() as db:
            saved = db.query(self.m.models.DesktopSession).one()
            self.assertNotEqual(saved.access_hash, result["accessToken"])
            self.assertEqual(db.query(self.m.models.RefreshToken).one().token_hash, self.m.service.digest(result["refreshToken"]))

    def test_cloud_settings_are_durable_and_shared_only_by_the_same_account(self):
        route = self.m.service.PREFIX + "/settings/cloud"
        one = self.login()["accessToken"]
        two = self.login(installation="second-desktop-12345")["accessToken"]
        other = self.login(tg=43, installation="other-desktop-12345")["accessToken"]
        header = lambda value: {"Authorization": "Bearer " + value}
        self.assertFalse(self.client.get(route, headers=header(one)).json()["available"])
        settings = {"volume": 0, "theme": "dark", "danmakuRows": 8, "subtitleSize": 35}
        response = self.client.post(route, headers=header(one), json={"schema": 1, "settings": settings})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["revision"], 1)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get(route, headers=header(two)).json()["settings"], settings)
        self.assertFalse(self.client.get(route + "?tg=42", headers=header(other)).json()["available"])
        restarted = self.m.service.DesktopAuth(self.sessions, self.m.runtime.account_lookup, now=lambda: self.now)
        self.assertEqual(restarted.cloud_settings(two)["settings"], settings)
        restarted.cloud_settings(two, {"schema": 1, "settings": {"volume": 36}})
        self.assertEqual(self.auth.cloud_settings(one)["revision"], 2)
        with self.sessions() as db:
            self.assertEqual(db.query(self.m.models.CloudSettings).count(), 1)

    def test_cloud_settings_require_a_bound_account_and_live_registered_device(self):
        route = self.m.service.PREFIX + "/settings/cloud"
        self.assertEqual(self.client.get(route).status_code, 401)
        pending = self.login(register=False)["accessToken"]
        headers = {"Authorization": "Bearer " + pending}
        self.assertEqual(self.client.get(route, headers=headers).json()["code"], "DEVICE_REQUIRED")
        self.assertEqual(self.client.post(route, headers=headers, json={"schema": 1, "settings": {"volume": 30}}).status_code, 403)
        token = self.login(installation="cloud-desktop-12345")["accessToken"]
        headers = {"Authorization": "Bearer " + token}
        with self.sessions.begin() as db:
            db.query(self.m.models.Device).filter_by(installation_id="cloud-desktop-12345").one().revoked_at = self.now
        self.assertEqual(self.client.get(route, headers=headers).json()["code"], "DEVICE_REVOKED")
        token = self.login(installation="cloud-desktop-67890")["accessToken"]
        with self.sessions.begin() as db:
            db.get(self.m.User, 42).embyid = None
        self.assertEqual(self.client.get(route, headers={"Authorization": "Bearer " + token}).json()["code"], "BOT_UNBOUND")

    def test_cloud_settings_reject_secrets_unknown_keys_bad_values_and_partial_writes(self):
        token = self.login()["accessToken"]
        self.auth.cloud_settings(token, {"schema": 1, "settings": {"volume": 20}})
        invalid = [{"schema": True, "settings": {"volume": 1}}, {"schema": 2, "settings": {"volume": 1}},
                   {"schema": 1, "settings": {"volume": 30}, "tg": 43}]
        for settings in [{}, {"volume": True}, {"volume": 131}, {"volume": -1}, {"volume": float("nan")},
                         {"danmakuRows": 1.5}, {"theme": "unknown"}, {"volume": 30, "token": "secret"},
                         {"proxyAddress": "http://private"}, {"gpu": "another-device"}, {"__proto__": {}},
                         {"subtitleFont": "x\nunsafe"}, {"danmakuBlockedWords": "x" * 2049}]:
            invalid.append({"schema": 1, "settings": settings})
        for data in invalid:
            self.error("INVALID_SETTINGS", self.auth.cloud_settings, token, data)
            self.assertEqual(self.auth.cloud_settings(token)["settings"], {"volume": 20})

    def test_cloud_settings_http_bounds_and_rate_limit(self):
        route = self.m.service.PREFIX + "/settings/cloud"
        token = self.login()["accessToken"]
        headers = {"Authorization": "Bearer " + token}
        self.assertEqual(self.client.post(route, headers=headers, content="{}").status_code, 415)
        self.assertEqual(self.client.post(route, headers={**headers, "Content-Type": "application/json"}, content=" " * 32769).status_code, 413)
        for _ in range(10):
            self.assertEqual(self.client.post(route, headers=headers, json={"schema": 1, "settings": {"volume": 30}}).status_code, 200)
        self.assertEqual(self.client.post(route, headers=headers, json={"schema": 1, "settings": {"volume": 90}}).status_code, 429)
        self.assertEqual(self.auth.cloud_settings(token)["settings"], {"volume": 30})

    def test_cloud_settings_migration_is_repeatable_and_preserves_backups(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        spec = importlib.util.spec_from_file_location("cloud_migration", ROOT / "bot/sql_helper/alembic/versions/20261005_15_add_tv_cloud_settings.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        token = self.login()["accessToken"]
        self.auth.cloud_settings(token, {"schema": 1, "settings": {"volume": 0}})
        with self.engine.begin() as connection:
            with patch.object(migration, "op", Operations(MigrationContext.configure(connection))):
                migration.upgrade()
                migration.upgrade()
        self.assertEqual(self.auth.cloud_settings(token)["settings"], {"volume": 0})
        self.assertEqual(migration.down_revision, "20261004_14")

    def test_claim_cannot_be_taken_by_another_telegram_user(self):
        challenge = self.start()
        self.auth.prepare(challenge["link"], 42, "Viewer", "viewer")
        self.error("CHALLENGE_CLAIMED", self.auth.prepare, challenge["link"], 43, "Other", "other")
        self.error("CHALLENGE_CLAIMED", self.auth.decide, challenge["challengeId"], 43, True)
        self.assertEqual(self.auth.poll(challenge)["status"], "pending")

    def test_avatar_is_private_and_rechecks_revocation_after_fetch(self):
        route = self.m.service.PREFIX + "/profile/avatar"
        self.assertEqual(self.client.get(route).status_code, 401)
        token = self.login()["accessToken"]
        headers = {"Authorization": "Bearer " + token}
        image = (b"\x89PNG\r\n\x1a\nfixture", "image/png")
        with patch.object(self.m.api.avatar_cache, "get", new=AsyncMock(return_value=image)) as load:
            response = self.client.get(route + "?telegramId=43", headers=headers)
            load.assert_awaited_once_with("42")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, image[0])
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotIn(token, str(response.headers))
        with patch.object(self.m.api.avatar_cache, "get", new=AsyncMock(return_value=None)):
            self.assertEqual(self.client.get(route, headers=headers).status_code, 204)
        async def revoked(_):
            self.auth.logout(token)
            return image
        with patch.object(self.m.api.avatar_cache, "get", new=revoked):
            response = self.client.get(route, headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(b"fixture", response.content)


    def test_danmaku_requires_live_registered_identity_and_rechecks_after_fetch(self):
        route = self.m.service.PREFIX + "/providers/danmaku"
        data = {"title": "示例电影", "type": "Movie", "year": 2026}
        with patch.object(self.m.api, "fetch_danmaku", new_callable=AsyncMock) as fetch:
            self.assertEqual(self.client.post(route, json=data).status_code, 401)
            pending = self.login(register=False)["accessToken"]
            self.assertEqual(self.client.post(route, json=data, headers={"Authorization": "Bearer " + pending}).status_code, 403)
            fetch.assert_not_called()
        token = self.login()["accessToken"]
        headers = {"Authorization": "Bearer " + token}
        result = {"available": True, "comments": [{"time": 1, "mode": 1, "color": "#ffffff", "text": "private fixture"}]}
        with patch.object(self.m.api, "fetch_danmaku", new=AsyncMock(return_value=result)) as fetch:
            response = self.client.post(route, json=data, headers=headers)
            self.assertEqual(response.json(), result)
            self.assertEqual(response.headers["cache-control"], "no-store")
            fetch.assert_awaited_once_with(data)
        async def revoke(_):
            self.auth.logout(token)
            return result
        with patch.object(self.m.api, "fetch_danmaku", new=revoke):
            response = self.client.post(route, json=data, headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("private fixture", response.text)

    def test_danmaku_provider_failure_still_rechecks_authorization(self):
        token = self.login()["accessToken"]
        async def revoke(_):
            self.auth.logout(token)
            raise self.m.api.DanmakuError("DANMAKU_UNAVAILABLE", "provider failure", 502)
        with patch.object(self.m.api, "fetch_danmaku", new=revoke):
            response = self.client.post(self.m.service.PREFIX + "/providers/danmaku", json={"title": "Movie"}, headers={"Authorization": "Bearer " + token})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("provider failure", response.text)

    def test_danmaku_limit_is_per_telegram_account(self):
        route = self.m.service.PREFIX + "/providers/danmaku"
        token = self.login()["accessToken"]
        with patch.object(self.m.api, "fetch_danmaku", new=AsyncMock(return_value={"available": False, "comments": []})) as fetch:
            for _ in range(12):
                self.assertEqual(self.client.post(route, json={"title": "Movie"}, headers={"Authorization": "Bearer " + token}).status_code, 200)
            self.assertEqual(self.client.post(route, json={"title": "Movie"}, headers={"Authorization": "Bearer " + token}).status_code, 429)
            self.assertEqual(fetch.await_count, 12)
            other = self.login(tg=43, installation="other-installation-1234")["accessToken"]
            self.assertEqual(self.client.post(route, json={"title": "Movie"}, headers={"Authorization": "Bearer " + other}).status_code, 200)

    def test_denial_cancel_and_expiry(self):
        challenge = self.start()
        self.auth.prepare(challenge["link"], 42, "Viewer", "viewer")
        self.auth.decide(challenge["challengeId"], 42, False)
        self.assertEqual(self.auth.poll(challenge)["status"], "denied")
        challenge = self.start()
        self.auth.poll(challenge, True)
        self.error("CHALLENGE_EXPIRED", self.auth.prepare, challenge["link"], 42, "Viewer", "viewer")
        challenge = self.start()
        self.now += timedelta(seconds=301)
        self.error("CHALLENGE_EXPIRED", self.auth.poll, challenge)

    def test_unbound_disabled_and_expired_users_never_get_tokens(self):
        for changes, expected in [({"embyid": None}, "unbound"), ({"lv": "c"}, "disabled"),
                                  ({"ex": datetime(2000, 1, 1)}, "disabled")]:
            with self.subTest(changes=changes):
                with self.sessions.begin() as db:
                    user = db.get(self.m.User, 42)
                    user.embyid, user.lv, user.ex = "emby-42", "b", datetime(2099, 1, 1)
                    for k, v in changes.items():
                        setattr(user, k, v)
                challenge = self.start()
                self.assertEqual(self.approve(challenge), expected)
                if expected == "unbound":
                    self.assertEqual(self.auth.poll(challenge)["status"], expected)
                else:
                    self.error("ACCOUNT_DISABLED", self.auth.poll, challenge)
        with self.sessions() as db:
            self.assertEqual(db.query(self.m.models.DesktopSession).count(), 0)

    def test_managed_entitlement_overrides_stale_legacy_expiry(self):
        ent = self.m.entitlements
        now = ent.china_now()
        with self.sessions.begin() as db:
            db.add(ent.AccountEntitlement(tg=42))
            db.add(ent.AccountPeriod(tg=42, starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=1),
                                     tier="normal", source_key="test-paid", kind="paid"))
            db.get(self.m.User, 42).ex = datetime(2000, 1, 1)
        tokens = self.login()
        with self.sessions.begin() as db:
            db.get(ent.AccountEntitlement, 42).blocked_reason = "admin"
        self.error("ACCOUNT_DISABLED", self.auth.session, tokens["accessToken"])

    def test_bound_user_change_invalidates_existing_session(self):
        token = self.login()["accessToken"]
        with self.sessions.begin() as db:
            db.get(self.m.User, 42).embyid = "replacement"
        self.error("BOT_UNBOUND", self.auth.session, token)

    def test_device_proof_tamper_replay_nonce_expiry_and_key_substitution(self):
        token = self.login()["accessToken"]
        proof = self.proof(token)
        data = {**self.hardware, "proof": proof}
        self.auth.heartbeat(token, data)
        self.error("INVALID_PROOF", self.auth.heartbeat, token, data)
        data["proof"] = self.proof(token, private=Ed25519PrivateKey.generate())
        self.error("INVALID_PROOF", self.auth.heartbeat, token, data)
        data["proof"] = self.proof(token)
        data["proof"]["payload"] += " "
        self.error("INVALID_PROOF", self.auth.heartbeat, token, data)
        data["proof"] = self.proof(token)
        self.now += timedelta(seconds=61)
        self.error("INVALID_PROOF", self.auth.heartbeat, token, data)

    def test_device_limit_recovery_and_revocation_owner(self):
        self.auth.max_devices = 1
        first = self.login()
        self.login()  # An identical installation does not consume another slot.
        second = self.login(installation="second-installation-1234", register=False)
        hardware = {**self.hardware, "installationId": "second-installation-1234"}
        self.error("DEVICE_LIMIT", self.auth.register, second["accessToken"], self.registration(second["accessToken"], hardware))
        device = self.auth.bot_devices(42)["devices"][0]
        self.error("DEVICE_NOT_FOUND", self.auth.bot_revoke, 43, device["id"])
        self.auth.bot_revoke(42, device["id"])
        self.error("TOKEN_EXPIRED", self.auth.session, first["accessToken"])
        self.auth.register(second["accessToken"], self.registration(second["accessToken"], hardware))
        self.assertEqual(len(self.auth.devices(second["accessToken"])["devices"]), 1)

    def test_omitted_or_zero_limit_allows_more_than_three_devices(self):
        spec = importlib.util.spec_from_file_location("tv_settings_test", ROOT / "bot/schemas/schemas.py")
        schemas = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(schemas)
        for tg, settings in ((42, {}), (43, {"max_devices": 0})):
            with self.subTest(settings=settings):
                cfg = schemas.DuShengTV(**settings)
                self.assertEqual(cfg.max_devices, 0)
                self.auth = self.m.service.DesktopAuth(self.sessions, self.m.runtime.account_lookup,
                                                       max_devices=cfg.max_devices, now=lambda: self.now)
                for i in range(4):
                    tokens = self.login(tg=tg, installation=f"unlimited-installation-{i}")
                result = self.auth.devices(tokens["accessToken"])
                self.assertEqual(result["maxDevices"], 0)
                self.assertEqual(len(result["devices"]), 4)
                self.assertEqual(self.auth.session(tokens["accessToken"])["maxDevices"], 0)
                self.assertEqual(self.auth.bot_devices(tg)["maxDevices"], 0)
        self.assertEqual(schemas.DuShengTV(max_devices=3).max_devices, 3)
        with self.assertRaises(ValueError):
            schemas.DuShengTV(max_devices=-1)

    def test_refresh_rotation_replay_revokes_family_and_checks_installation(self):
        first = self.login()
        request = {"refreshToken": first["refreshToken"], "installationId": self.installation}
        self.error("INVALID_DEVICE", self.auth.refresh, {**request, "installationId": "someone-else"})
        second = self.auth.refresh(request)
        self.error("TOKEN_EXPIRED", self.auth.session, first["accessToken"])
        self.assertTrue(self.auth.session(second["accessToken"])["allowed"])
        self.error("TOKEN_REPLAYED", self.auth.refresh, request)
        self.error("TOKEN_EXPIRED", self.auth.session, second["accessToken"])
        self.error("TOKEN_EXPIRED", self.auth.refresh, {**request, "refreshToken": second["refreshToken"]})

    def test_access_expiry_refresh_and_logout(self):
        tokens = self.login()
        self.now += timedelta(seconds=901)
        self.error("TOKEN_EXPIRED", self.auth.session, tokens["accessToken"])
        tokens = self.auth.refresh({"refreshToken": tokens["refreshToken"], "installationId": self.installation})
        self.auth.logout(tokens["accessToken"])
        self.error("TOKEN_EXPIRED", self.auth.session, tokens["accessToken"])

    def test_durable_sessions_survive_service_restart(self):
        tokens = self.login()
        restarted = self.m.service.DesktopAuth(self.sessions, self.m.runtime.account_lookup, now=lambda: self.now)
        self.assertTrue(restarted.session(tokens["accessToken"])["allowed"])
        self.assertIn("accessToken", restarted.refresh({"refreshToken": tokens["refreshToken"], "installationId": self.installation}))

    def test_isolated_routes_and_bounded_json(self):
        self.assertNotIn("bot.web", sys.modules)
        for path in ("/payments/shop", "/payments/products", "/emby/auth", "/user", "/auth", "/docs", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        prefix = self.m.service.PREFIX
        result = self.client.get(prefix + "/session")
        self.assertEqual(result.status_code, 401)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(self.client.post(prefix + "/auth/telegram/start", content="x").status_code, 415)
        self.assertEqual(self.client.post(prefix + "/auth/telegram/start", content="x", headers={"Content-Type": "application/json"}).status_code, 400)
        self.assertEqual(self.client.post(prefix + "/auth/telegram/start", json={"large": "x" * 32769}).status_code, 413)

    def test_browser_link_is_dedicated_no_credentials_or_payment_routes(self):
        challenge = self.start()
        response = self.client.get(self.m.service.PREFIX + "/auth/telegram/authorize", params={"request": challenge["link"]})
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"https://t.me/example_bot?start=tvlogin_{challenge['link']}", response.text)
        self.assertIn(challenge["displayCode"], response.text)
        self.assertNotIn("payments", response.text)
        self.assertNotIn(challenge["codeVerifier"], response.text)
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    def test_relogin_app_link_web_link_and_manual_command_use_the_new_request(self):
        previous_token, previous_link = None, None
        for _ in range(3):
            challenge = self.start()
            response = self.client.get(challenge["authorizationUrl"])
            self.assertEqual(response.status_code, 200, response.text)
            page = LoginPage(response.text)
            direct, web = map(urlsplit, page.links)
            self.assertEqual((direct.scheme, direct.netloc), ("tg", "resolve"))
            self.assertEqual(parse_qs(direct.query)["domain"], ["example_bot"])
            self.assertEqual((web.scheme, web.netloc, web.path), ("https", "t.me", "/example_bot"))
            payload = parse_qs(direct.query)["start"][0]
            self.assertEqual(parse_qs(web.query)["start"], [payload])
            self.assertLessEqual(len(payload), 64)  # Telegram's deep-link limit.
            self.assertEqual(page.command, "/start " + payload)
            self.assertNotEqual(challenge["link"], previous_link)
            if previous_link:
                self.assertEqual(self.client.get(self.m.service.PREFIX + "/auth/telegram/authorize", params={"request": previous_link}).status_code, 410)
                self.error("TOKEN_EXPIRED", self.auth.session, previous_token)
            # Sending the page's fallback command reaches the exact same
            # pending login as either Telegram link, including after logout.
            command, argument = page.command.split()
            self.assertEqual(command, "/start")
            result = self.auth.prepare(argument.removeprefix("tvlogin_"), 42, "Viewer", "viewer")
            self.assertEqual(result["displayCode"], challenge["displayCode"])
            self.auth.decide(result["id"], 42, True)
            token = self.auth.poll(challenge)["accessToken"]
            self.auth.register(token, self.registration(token))
            self.assertTrue(self.auth.session(token)["device"]["allowed"])
            self.assertEqual(len(self.auth.devices(token)["devices"]), 1)
            self.auth.logout(token)
            previous_token, previous_link = token, challenge["link"]

    def test_login_page_only_allows_its_nonce_protected_copy_script(self):
        challenge = self.start()
        first = self.client.get(challenge["authorizationUrl"])
        second = self.client.get(challenge["authorizationUrl"])
        first_page, second_page = LoginPage(first.text), LoginPage(second.text)
        self.assertEqual(len(first_page.scripts), 1)
        nonce = first_page.scripts[0]["nonce"]
        self.assertNotEqual(nonce, second_page.scripts[0]["nonce"])
        self.assertIn(f"script-src 'nonce-{nonce}'", first.headers["content-security-policy"])
        self.assertNotIn("script-src 'unsafe-inline'", first.headers["content-security-policy"])
        self.assertIn("default-src 'none'", first.headers["content-security-policy"])
        self.assertEqual(first.headers["cache-control"], "no-store")
        self.assertNotIn("script-src", self.client.get(self.m.service.PREFIX + "/config").headers["content-security-policy"])

    def test_server_whitelist_token_mapping_and_midflight_revocation(self):
        tokens = self.login()
        headers = {"Authorization": "Bearer " + tokens["accessToken"]}
        path = self.m.service.PREFIX + "/servers/authorize"
        data = {"serverUrl": "https://emby.test", "embyUserId": "emby-42", "embyAccessToken": "user-token"}
        with patch.object(self.m.api, "emby_identity", new_callable=AsyncMock) as verify:
            self.assertEqual(self.client.post(path, headers=headers, json={**data, "serverUrl": "http://127.0.0.1"}).status_code, 403)
            self.assertEqual(self.client.post(path, headers=headers, json={**data, "embyUserId": "emby-43"}).status_code, 403)
            verify.assert_not_called()
            verify.return_value = "emby-43"
            self.assertEqual(self.client.post(path, headers=headers, json=data).status_code, 403)
            verify.return_value = "emby-42"
            result = self.client.post(path, headers=headers, json=data)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["telegramId"], "42")
            verify.assert_awaited_with("http://emby-origin.test:8096", "user-token")
            async def revoke(*_):
                self.auth.logout(tokens["accessToken"])
                return "emby-42"
            verify.side_effect = revoke
            self.assertEqual(self.client.post(path, headers=headers, json=data).status_code, 401)

    @contextmanager
    def emby49_origin(self):
        owner = uuid.UUID("01234567-89ab-cdef-0123-456789abcdef")
        other = uuid.UUID("fedcba98-7654-3210-fedc-ba9876543210")
        with self.sessions.begin() as db:
            db.get(self.m.User, 42).embyid = owner.hex
        tokens = self.login()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        auth_db = directory / "authentication.db"
        with sqlite3.connect(auth_db) as db:
            db.execute("CREATE TABLE Tokens_2 (AccessToken TEXT, UserId INTEGER, IsActive INTEGER)")
            db.executemany("INSERT INTO Tokens_2 VALUES (?, ?, 1)", [("user-token", 7), ("other-user-token", 8)])
        with sqlite3.connect(directory / "users.db") as db:
            db.execute("CREATE TABLE LocalUsersv2 (Id INTEGER, guid BLOB)")
            db.executemany("INSERT INTO LocalUsersv2 VALUES (?, ?)", [(7, owner.bytes_le), (8, other.bytes_le)])
        state = SimpleNamespace(requests=[], status=200, owner=owner.hex, other=other.hex, auth_db=auth_db,
                                users={uid.hex: {"Id": uid.hex, "Policy": {"IsDisabled": False}} for uid in (owner, other)})

        class Origin(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                state.requests.append((self.path, self.headers.get("X-Emby-Token")))
                user_id = self.path.removeprefix("/emby/Users/")
                # Emby 4.9 rejects /Users/Me, while a valid token can fetch
                # either user's record. The token DB must establish ownership.
                status = state.status if user_id in state.users else 400
                body = json.dumps(state.users.get(user_id, {"error": "Unrecognized Guid format"})).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                if status == 302:
                    self.send_header("Location", "/must-not-follow")
                self.end_headers()
                self.wfile.write(body)

        origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        worker = Thread(target=origin.serve_forever, daemon=True)
        worker.start()
        config = SimpleNamespace(emby_url=f"http://127.0.0.1:{origin.server_port}/emby/",
                                 emby_api="admin-key-must-not-be-sent", emby_auth_db_path=str(auth_db))
        state.config = config
        data = {"serverUrl": "https://emby.test", "embyUserId": owner.hex, "embyAccessToken": "user-token"}
        headers = {"Authorization": "Bearer " + tokens["accessToken"]}
        path = self.m.service.PREFIX + "/servers/authorize"
        state.authorize = lambda **changes: self.client.post(path, headers=headers, json={**data, **changes})
        try:
            with patch.object(sys.modules["bot"], "config", config):
                yield state
        finally:
            origin.shutdown()
            origin.server_close()
            worker.join(timeout=5)

    def test_public_alias_verifies_emby49_token_owner_at_bot_origin(self):
        with self.emby49_origin() as state:
            response = state.authorize()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["serverUrl"], "https://emby.test")
            self.assertEqual(response.json()["embyUserId"], state.owner)
            self.assertNotIn("user-token", response.text)
            self.assertNotIn("127.0.0.1", response.text)
            self.assertEqual(state.requests, [(f"/emby/Users/{state.owner}", "user-token")])
            state.users[state.owner]["Policy"]["IsDisabled"] = True
            self.assertEqual(state.authorize().status_code, 403)

    def test_emby49_foreign_token_cannot_authorize_claimed_bound_user(self):
        with self.emby49_origin() as state:
            response = state.authorize(embyAccessToken="other-user-token")
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()["code"], "SERVER_NOT_BOUND")
            self.assertEqual(state.requests, [(f"/emby/Users/{state.other}", "other-user-token")])

    def test_emby49_revoked_unknown_and_ambiguous_tokens_fail_before_http(self):
        with self.emby49_origin() as state:
            cases = {
                "revoked": [("user-token", 7, 0)],
                "unknown": [("different-token", 7, 1)],
                "ambiguous": [("user-token", 7, 1), ("user-token", 8, 1)],
                "unmapped": [("user-token", 99, 1)],
            }
            for name, rows in cases.items():
                with self.subTest(case=name):
                    with sqlite3.connect(state.auth_db) as db:
                        db.execute("DELETE FROM Tokens_2")
                        db.executemany("INSERT INTO Tokens_2 VALUES (?, ?, ?)", rows)
                    response = state.authorize()
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(response.json()["code"], "SERVER_NOT_BOUND")
            self.assertEqual(state.requests, [])

    def test_emby49_database_setup_failures_are_not_reported_as_bad_credentials(self):
        with self.emby49_origin() as state, patch.dict(os.environ, {"EMBY_AUTH_DB_PATH": ""}):
            state.config.emby_auth_db_path = ""
            response = state.authorize()
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.json()["code"], "EMBY_AUTH_NOT_CONFIGURED")
            # The same environment configuration as the existing line verifier works.
            with patch.dict(os.environ, {"EMBY_AUTH_DB_PATH": str(state.auth_db)}):
                self.assertEqual(state.authorize().status_code, 200)
            state.requests.clear()
            state.config.emby_auth_db_path = str(state.auth_db.with_name("missing.db"))
            response = state.authorize()
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.json()["code"], "EMBY_AUTH_UNAVAILABLE")
            self.assertNotIn(state.config.emby_auth_db_path, response.text)
            self.assertFalse(Path(state.config.emby_auth_db_path).exists())
            state.config.emby_auth_db_path = str(state.auth_db)
            with sqlite3.connect(state.auth_db) as db:
                db.execute("DROP TABLE Tokens_2")
            response = state.authorize()
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.json()["code"], "EMBY_AUTH_UNAVAILABLE")
            self.assertEqual(state.requests, [])

    def test_emby49_live_origin_must_accept_token_and_return_canonical_account(self):
        with self.emby49_origin() as state:
            for status, expected in ((401, 403), (403, 403), (404, 403), (500, 502), (302, 502)):
                with self.subTest(upstream_status=status):
                    state.status = status
                    response = state.authorize()
                    self.assertEqual(response.status_code, expected, response.text)
            state.status = 200
            for user in ({"Id": state.other, "Policy": {}}, {"Id": state.owner, "Policy": None}, []):
                with self.subTest(user=user):
                    state.users[state.owner] = user
                    self.assertEqual(state.authorize().status_code, 502)
            self.assertEqual(state.requests, [(f"/emby/Users/{state.owner}", "user-token")] * 8)

    def test_migration_matches_models_and_is_idempotent(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        spec = importlib.util.spec_from_file_location("tv_migration", ROOT / "bot/sql_helper/alembic/versions/20261004_14_add_dushengtv_auth.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        other = create_engine("sqlite://")
        with other.begin() as connection:
            with patch.object(migration, "op", Operations(MigrationContext.configure(connection))):
                migration.upgrade()
                migration.upgrade()
            cloud_spec = importlib.util.spec_from_file_location("cloud_migration", ROOT / "bot/sql_helper/alembic/versions/20261005_15_add_tv_cloud_settings.py")
            cloud_migration = importlib.util.module_from_spec(cloud_spec)
            cloud_spec.loader.exec_module(cloud_migration)
            with patch.object(cloud_migration, "op", Operations(MigrationContext.configure(connection))):
                cloud_migration.upgrade()
                cloud_migration.upgrade()
            request_spec = importlib.util.spec_from_file_location("request_migration", ROOT / "bot/sql_helper/alembic/versions/20261006_16_add_tv_media_requests.py")
            request_migration = importlib.util.module_from_spec(request_spec)
            request_spec.loader.exec_module(request_migration)
            with patch.object(request_migration, "op", Operations(MigrationContext.configure(connection))):
                request_migration.upgrade()
                request_migration.upgrade()
            inspector = inspect(connection)
            for table in self.m.base.metadata.tables.values():
                if table.name.startswith("tv_"):
                    self.assertEqual({c["name"] for c in inspector.get_columns(table.name)}, set(table.columns.keys()))
        other.dispose()

    def test_requests_require_bound_device_and_recheck_revocation_after_discovery(self):
        from bot.dushengtv import requests_api
        prefix = self.m.service.PREFIX + "/requests"
        fake = SimpleNamespace(catalog=AsyncMock(return_value={"items": [], "page": 1}),
                               detail=AsyncMock(), subscribe=AsyncMock())
        with patch.object(requests_api, "permission"), patch.object(requests_api, "service", return_value=fake):
            self.assertEqual(self.client.get(prefix + "/catalog").status_code, 401)
            not_registered = self.login(register=False)
            header = {"Authorization": "Bearer " + not_registered["accessToken"]}
            self.assertEqual(self.client.get(prefix + "/catalog", headers=header).status_code, 403)
            fake.catalog.assert_not_awaited()
            self.auth.register(not_registered["accessToken"], self.registration(not_registered["accessToken"]))
            self.assertEqual(self.client.get(prefix + "/catalog", headers=header).status_code, 200)
            async def revoke_while_fetching(*_):
                self.auth.logout(not_registered["accessToken"])
                return {"items": []}
            fake.catalog.side_effect = revoke_while_fetching
            self.assertEqual(self.client.get(prefix + "/catalog", headers=header).status_code, 401)
            self.assertEqual(self.client.post(prefix + "/subscribe", headers=header, json={"key": "tmdb:movie:123"}).status_code, 401)
            fake.subscribe.assert_not_awaited()

    def test_requests_preserve_moviepilot_account_policy(self):
        from bot.dushengtv import requests_api
        import bot
        import bot.sql_helper as sql
        identity = {"telegramId": "42", "embyUserId": "emby-42"}
        config = SimpleNamespace(moviepilot=SimpleNamespace(status=False, douban_status=True, url="http://mp", lv="a"),
                                 dushengtv=SimpleNamespace(requests_enabled=True), admins=[], owner=99)
        with patch.object(bot, "config", config), patch.object(sql, "Session", self.sessions, create=True):
            self.error("REQUESTS_FORBIDDEN", requests_api.permission, identity)
            config.moviepilot.lv = "b"
            requests_api.permission(identity)
            config.dushengtv.requests_enabled = False
            self.error("REQUESTS_NOT_CONFIGURED", requests_api.permission, identity)

    def test_translation_cloud_settings_exclude_keys_and_unsafe_endpoints(self):
        from bot.dushengtv.cloud_settings import validate_cloud_settings
        settings = {"subtitleTranslationProvider": "openai", "subtitleTranslationEndpoint": "https://api.example.com/v1",
                    "subtitleTranslationModel": "gpt-4o-mini", "subtitleTranslationLanguage": "zh-CN",
                    "subtitleTranslationMode": "translation", "subtitleTranslationOriginalScale": .8}
        self.assertEqual(validate_cloud_settings({"schema": 1, "settings": settings}), settings)
        for extra in ({"subtitleTranslationApiKey": "private"}, {"hiddenLibraries": {}},
                      {"subtitleTranslationEndpoint": "https://api.example.com/v1?key=private"},
                      {"subtitleTranslationEndpoint": "https://user:secret@api.example.com/v1"},
                      {"subtitleTranslationEndpoint": "http://other-host/v1"},
                      {"subtitleTranslationOriginalScale": 0}, {"subtitleTranslationModel": " "}):
            with self.assertRaises(ValueError):
                validate_cloud_settings({"schema": 1, "settings": {**settings, **extra}})
        for endpoint in ("http://127.0.0.1:8080/v1", "http://localhost/v1", "http://[::1]:8080/v1"):
            validate_cloud_settings({"schema": 1, "settings": {**settings, "subtitleTranslationEndpoint": endpoint}})


if __name__ == "__main__":
    unittest.main()
