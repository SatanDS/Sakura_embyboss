"""Offline TV authorization regressions. No Telegram or production config is read."""

import base64
import hashlib
import importlib.util
import json
import secrets
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
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


def load_modules():
    base = declarative_base()
    bot = types.ModuleType("bot")
    bot.__path__ = [str(ROOT / "bot")]
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

    def test_claim_cannot_be_taken_by_another_telegram_user(self):
        challenge = self.start()
        self.auth.prepare(challenge["link"], 42, "Viewer", "viewer")
        self.error("CHALLENGE_CLAIMED", self.auth.prepare, challenge["link"], 43, "Other", "other")
        self.error("CHALLENGE_CLAIMED", self.auth.decide, challenge["challengeId"], 43, True)
        self.assertEqual(self.auth.poll(challenge)["status"], "pending")

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
            async def revoke(*_):
                self.auth.logout(tokens["accessToken"])
                return "emby-42"
            verify.side_effect = revoke
            self.assertEqual(self.client.post(path, headers=headers, json=data).status_code, 401)

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
            inspector = inspect(connection)
            for table in self.m.base.metadata.tables.values():
                if table.name.startswith("tv_"):
                    self.assertEqual({c["name"] for c in inspector.get_columns(table.name)}, set(table.columns.keys()))
        other.dispose()


if __name__ == "__main__":
    unittest.main()
