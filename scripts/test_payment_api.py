"""Browser payment login regressions; no Telegram, MySQL, or production config."""

import importlib.util
import json
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from http.cookies import SimpleCookie
from unittest.mock import AsyncMock

from fastapi import FastAPI

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

ROOT = Path(__file__).resolve().parents[1]


def load_auth():
    base = declarative_base()
    bot = types.ModuleType("bot")
    sql = types.ModuleType("bot.sql_helper")
    sql.Base = base
    sys.modules["bot"] = bot
    sys.modules["bot.sql_helper"] = sql
    package = types.ModuleType("bot.payments")
    package.__path__ = [str(ROOT / "bot" / "payments")]
    sys.modules["bot.payments"] = package
    loaded = {}
    for name in ("models", "browser_auth"):
        spec = importlib.util.spec_from_file_location(f"bot.payments.{name}", ROOT / "bot" / "payments" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        loaded[name] = module
    return base, loaded["browser_auth"], loaded["models"]


class BrowserAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._original_modules = {name: value for name, value in sys.modules.items()
                                 if name == "bot" or name.startswith("bot.")}
        cls.base, cls.auth, cls.models = load_auth()

    @classmethod
    def tearDownClass(cls):
        for name in list(sys.modules):
            if name == "bot" or name.startswith("bot."):
                del sys.modules[name]
        sys.modules.update(cls._original_modules)

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.now = datetime(2026, 9, 10, 12, 0, 0)
        self.authenticator = self.auth.BrowserAuth(self.sessions, now=lambda: self.now)

    def tearDown(self):
        self.engine.dispose()

    def test_challenge_requires_same_browser_and_is_one_time(self):
        first = self.authenticator.start()
        prepared = self.authenticator.prepare(first["token"], 1001)
        self.assertEqual(prepared["display_code"], first["display_code"])
        with self.assertRaises(self.auth.LoginError):
            self.authenticator.prepare(first["token"], 1002)
        self.authenticator.decide(first["id"], 1001, approve=True)
        session = self.authenticator.poll(first["browser_token"])
        self.assertIsNotNone(session)
        self.assertEqual(self.authenticator.identity(session, first["browser_token"]), 1001)
        with self.assertRaises(self.auth.LoginError):
            self.authenticator.poll(first["browser_token"])
        with self.assertRaises(self.auth.LoginError):
            self.authenticator.identity(session, self.authenticator.start()["browser_token"])

    def test_expired_and_denied_challenges_cannot_create_session(self):
        denied = self.authenticator.start()
        self.authenticator.prepare(denied["token"], 2002)
        self.authenticator.decide(denied["id"], 2002, approve=False)
        with self.assertRaises(self.auth.LoginError) as error:
            self.authenticator.poll(denied["browser_token"])
        self.assertEqual(error.exception.code, "challenge_denied")

        expired = self.authenticator.start()
        self.now += timedelta(seconds=self.auth.LOGIN_SECONDS + 1)
        with self.assertRaises(self.auth.LoginError) as error:
            self.authenticator.prepare(expired["token"], 2002)
        self.assertEqual(error.exception.code, "challenge_expired")

    def test_logout_revokes_session_and_browser_challenges(self):
        challenge = self.authenticator.start()
        self.authenticator.prepare(challenge["token"], 3003)
        self.authenticator.decide(challenge["id"], 3003, approve=True)
        token = self.authenticator.poll(challenge["browser_token"])
        self.authenticator.logout(token, challenge["browser_token"])
        with self.assertRaises(self.auth.LoginError):
            self.authenticator.identity(token, challenge["browser_token"])


class PaymentHTTPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_modules = {name: value for name, value in sys.modules.items()
                                 if name == "bot" or name.startswith("bot.")}
        self.base, self.auth_module, self.models = load_auth()
        self.engine = create_engine("sqlite:///:memory:")
        self.base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.authenticator = self.auth_module.BrowserAuth(self.sessions)
        sys.modules["bot"].bot = types.SimpleNamespace(send_message=AsyncMock())
        sys.modules["bot"].config = types.SimpleNamespace()
        sys.modules["bot"].owner = 1001
        sys.modules["bot"].admins = [1002]
        sys.modules["bot"].bot_name = "test_login_bot"
        sys.modules["bot.sql_helper"].Session = self.sessions
        for name, path in (("bot.web", "bot/web"), ("bot.web.api", "bot/web/api")):
            package = types.ModuleType(name)
            package.__path__ = [str(ROOT / path)]
            sys.modules[name] = package
        spec = importlib.util.spec_from_file_location("payment_http_under_test", ROOT / "bot/web/api/payment.py")
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        self.settings = types.SimpleNamespace(cookie_secure=True, public_url="https://pay.test",
                                              enabled=True, terms_version="v1", validate=lambda: None)
        self.api._settings = lambda: self.settings
        self.api._auth = lambda: self.authenticator
        self.service = types.SimpleNamespace(list_orders=lambda buyer: [{"id": "o1", "buyer_tg": buyer}],
                                            create_checkout=AsyncMock(return_value={"id": "o1", "checkout_url": "https://checkout.stripe.com/c/pay/test"}))
        self.api._service = lambda: self.service
        self.app = FastAPI()
        self.app.include_router(self.api.router)
        self.cookies = {}

    def tearDown(self):
        self.engine.dispose()
        for name in list(sys.modules):
            if name == "bot" or name.startswith("bot."):
                del sys.modules[name]
        sys.modules.update(self.original_modules)

    async def request(self, path, *, method="GET", body=None, headers=None, cookies=None):
        request_headers = {"host": "pay.test", **(headers or {})}
        jar = self.cookies if cookies is None else cookies
        if jar:
            request_headers["cookie"] = "; ".join(f"{key}={value}" for key, value in jar.items())
        payload = b"" if body is None else json.dumps(body).encode()
        if body is not None:
            request_headers["content-type"] = "application/json"
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                 "method": method, "scheme": "https", "path": path, "raw_path": path.encode(),
                 "query_string": b"", "root_path": "", "headers": [(key.lower().encode(), value.encode()) for key, value in request_headers.items()],
                 "client": ("127.0.0.1", 12345), "server": ("pay.test", 443)}
        sent = []

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message):
            sent.append(message)

        await self.app(scope, receive, send)
        start = next(message for message in sent if message["type"] == "http.response.start")
        for key, value in start["headers"]:
            if key == b"set-cookie" and cookies is None:
                parsed = SimpleCookie(value.decode())
                for name, morsel in parsed.items():
                    if morsel.value:
                        self.cookies[name] = morsel.value
                    else:
                        self.cookies.pop(name, None)
        payload = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
        return start["status"], json.loads(payload), start["headers"]

    async def login(self, user=1001):
        status, challenge, headers = await self.request("/payments/auth/start", method="POST",
                                                        headers={"origin": "https://pay.test", "X-Payment-Request": "1"})
        self.assertEqual(status, 200)
        self.assertTrue(any(b"HttpOnly" in value and b"Secure" in value for key, value in headers if key == b"set-cookie"))
        token = challenge["login_url"].split("paylogin_", 1)[1]
        prepared = self.authenticator.prepare(token, user)
        self.authenticator.decide(prepared["id"], user, approve=True)
        status, body, headers = await self.request("/payments/auth/poll", headers={"X-Payment-Request": "1"})
        self.assertEqual((status, body["authenticated"]), (200, True))
        for key, value in headers:
            if key == b"set-cookie":
                self.assertIn(b"Max-Age=86400", value)
        status, identity, _ = await self.request("/payments/me")
        self.assertEqual(status, 200)
        return identity["csrf_token"]

    async def test_actual_routes_login_csrf_origin_and_logout(self):
        self.assertEqual((await self.request("/payments/auth/start", method="POST"))[0], 403)
        csrf = await self.login()
        body = {"product_id": "p1", "product_version": 1, "terms_version": "v1", "accepted": True}
        self.assertEqual((await self.request("/payments/checkout", method="POST", body=body))[0], 403)
        self.assertEqual((await self.request("/payments/checkout", method="POST", body=body,
                                            headers={"origin": "https://evil.test", "X-CSRF-Token": csrf}))[0], 403)
        status, _, _ = await self.request("/payments/checkout", method="POST", body=body,
                                         headers={"origin": "https://pay.test", "X-CSRF-Token": csrf})
        self.assertEqual(status, 200)
        self.service.create_checkout.assert_awaited_once()
        copied = dict(self.cookies)
        status, _, _ = await self.request("/payments/auth/logout", method="POST",
                                         headers={"origin": "https://pay.test", "X-CSRF-Token": csrf})
        self.assertEqual(status, 200)
        self.assertEqual((await self.request("/payments/me", cookies=copied))[0], 401)

    async def test_strict_consent_and_disabled_sales_keep_orders_readable(self):
        csrf = await self.login()
        headers = {"origin": "https://pay.test", "X-CSRF-Token": csrf}
        for accepted in ("true", 1, "yes"):
            status, body, _ = await self.request("/payments/checkout", method="POST", headers=headers,
                body={"product_id": "p1", "product_version": 1, "terms_version": "v1", "accepted": accepted})
            self.assertEqual((status, body), (422, {"detail": "invalid_request"}))
        self.settings.enabled = False
        self.assertEqual((await self.request("/payments/checkout", method="POST", headers=headers,
            body={"product_id": "p1", "product_version": 1, "terms_version": "v1", "accepted": True}))[0], 503)
        status, body, response_headers = await self.request("/payments/orders")
        self.assertEqual((status, body["orders"][0]["buyer_tg"]), (200, 1001))
        self.assertIn((b"cache-control", b"no-store"), response_headers)

    async def test_browser_cookie_required_and_cross_origin_reads_rejected(self):
        await self.login()
        session_name, browser_name = self.api._cookie_names()
        copied = {session_name: self.cookies[session_name]}
        self.assertEqual((await self.request("/payments/me", cookies=copied))[0], 401)
        self.assertEqual((await self.request("/payments/me", headers={"origin": "https://evil.test"}))[0], 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
