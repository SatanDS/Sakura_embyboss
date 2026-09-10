"""Browser payment login regressions; no Telegram, MySQL, or production config."""

import ast
import importlib.util
import json
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from http.cookies import SimpleCookie
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from loguru import logger

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
        sys.modules["bot"].LOGGER = logger
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
        self.load_bot_login_handlers()

    def load_bot_login_handlers(self):
        # Load the real command handlers, stubbing only Telegram transport and
        # unrelated startup imports. Authentication and persistence stay real.
        bot_module = sys.modules["bot"]
        bot_module.prefixes = ["/"]
        bot_module.bot.on_message = lambda *_args, **_kwargs: lambda handler: handler
        bot_module.bot.on_callback_query = lambda *_args, **_kwargs: lambda handler: handler
        for name in ("bot.modules", "bot.modules.commands", "bot.func_helper"):
            package = types.ModuleType(name)
            package.__path__ = []
            sys.modules[name] = package
        transport = types.ModuleType("bot.func_helper.msg_utils")
        for name in ("sendMessage", "deleteMessage", "callAnswer", "editMessage"):
            setattr(transport, name, AsyncMock())
        self.transport = transport
        sys.modules[transport.__name__] = transport
        sys.modules["bot.web.api.payment"] = self.api
        spec = importlib.util.spec_from_file_location(
            "bot.modules.commands.payment", ROOT / "bot/modules/commands/payment.py")
        self.payment_commands = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.payment_commands
        spec.loader.exec_module(self.payment_commands)
        source = ROOT / "bot/modules/commands/start.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        handler = next(node for node in tree.body
                       if isinstance(node, ast.AsyncFunctionDef) and node.name == "p_start")
        handler.decorator_list = []
        self.group_check = AsyncMock(return_value=False)
        namespace = {"deleteMessage": transport.deleteMessage, "sendMessage": transport.sendMessage,
                     "user_in_group_filter": self.group_check}
        exec(compile(ast.Module(body=[handler], type_ignores=[]), str(source), "exec"), namespace)
        self.start_handler = namespace["p_start"]

    async def begin_bot_login(self, user=1001):
        status, challenge, headers = await self.request("/payments/auth/start", method="POST",
            headers={"origin": "https://pay.test", "X-Payment-Request": "1"})
        self.assertEqual(status, 200)
        self.assertTrue(any(b"HttpOnly" in value and b"Secure" in value
                            for key, value in headers if key == b"set-cookie"))
        payload = parse_qs(urlsplit(challenge["login_url"]).query)["start"][0]
        self.assertLessEqual(len(payload), 64)
        message = types.SimpleNamespace(command=["start", payload], from_user=types.SimpleNamespace(id=user))
        await self.start_handler(None, message)
        self.group_check.assert_not_awaited()
        self.transport.deleteMessage.assert_awaited_with(message)
        prompt = self.transport.sendMessage.await_args
        self.assertIn(challenge["display_code"], prompt.args[1])
        buttons = prompt.kwargs["buttons"].inline_keyboard[0]
        self.assertEqual([button.callback_data for button in buttons], [
            f"paylogin:yes:{challenge['challenge_id']}", f"paylogin:no:{challenge['challenge_id']}"])
        return challenge

    async def decide_bot_login(self, challenge, user=1001, approve=True):
        action = "yes" if approve else "no"
        callback = types.SimpleNamespace(data=f"paylogin:{action}:{challenge['challenge_id']}",
                                        from_user=types.SimpleNamespace(id=user))
        await self.payment_commands.payment_login_decision(None, callback)

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
        challenge = await self.begin_bot_login(user)
        status, pending, _ = await self.request("/payments/auth/poll", headers={"X-Payment-Request": "1"})
        self.assertEqual((status, pending["authenticated"]), (200, False))
        await self.decide_bot_login(challenge, user)
        status, body, headers = await self.request("/payments/auth/poll", headers={"X-Payment-Request": "1"})
        self.assertEqual((status, body["authenticated"]), (200, True))
        for key, value in headers:
            if key == b"set-cookie":
                self.assertIn(b"Max-Age=86400", value)
        status, identity, _ = await self.request("/payments/me")
        self.assertEqual(status, 200)
        return identity["csrf_token"]

    async def test_bot_preserves_urlsafe_token_characters(self):
        for edge in ("_", "-"):
            with self.subTest(edge=edge):
                self.cookies.clear()
                link_token = edge + "A" * 41 + edge
                with patch.object(self.auth_module.secrets, "token_urlsafe",
                                  side_effect=["B" * 43, link_token]):
                    challenge = await self.begin_bot_login()
                await self.decide_bot_login(challenge)
                status, body, _ = await self.request("/payments/auth/poll", headers={"X-Payment-Request": "1"})
                self.assertEqual((status, body["authenticated"]), (200, True))

    async def test_wrong_bot_approver_and_denial_do_not_issue_session(self):
        challenge = await self.begin_bot_login()
        await self.decide_bot_login(challenge, user=1002)
        status, body, _ = await self.request("/payments/auth/poll", headers={"X-Payment-Request": "1"})
        self.assertEqual((status, body["authenticated"]), (200, False))
        await self.decide_bot_login(challenge, approve=False)
        status, body, _ = await self.request("/payments/auth/poll", headers={"X-Payment-Request": "1"})
        self.assertEqual((status, body["detail"]), (410, "challenge_denied"))
        await self.decide_bot_login(challenge)
        self.assertEqual((await self.request("/payments/me"))[0], 401)

    async def test_bot_confirmation_cannot_bypass_expiry(self):
        challenge = await self.begin_bot_login()
        self.authenticator.now = lambda: datetime.utcnow() + timedelta(seconds=self.auth_module.LOGIN_SECONDS + 1)
        await self.decide_bot_login(challenge)
        status, body, _ = await self.request("/payments/auth/poll", headers={"X-Payment-Request": "1"})
        self.assertEqual((status, body["detail"]), (410, "challenge_expired"))
        self.assertEqual((await self.request("/payments/me"))[0], 401)

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

    async def test_checkout_provider_error_is_logged_and_response_stays_private(self):
        import stripe
        csrf = await self.login()
        secret = "sk_test_private_fixture"
        self.service.create_checkout.side_effect = stripe.InvalidRequestError(
            "bad request " + secret, param="payment_method_types[1]", http_status=400,
            headers={"request-id": "req_test123"}, http_body=secret,
        )
        messages = []
        sink = type(logger).add(logger, messages.append, format="{message}")
        try:
            status, body, _ = await self.request("/payments/checkout", method="POST",
                headers={"origin": "https://pay.test", "X-CSRF-Token": csrf},
                body={"product_id": "p1", "product_version": 1, "terms_version": "v1", "accepted": True})
        finally:
            logger.remove(sink)
        self.assertEqual((status, body), (400, {"detail": "service_unavailable"}))
        self.assertEqual(len(messages), 1)
        self.assertIn("request_id=req_test123", str(messages[0]))
        self.assertIn("param=payment_method_types[1]", str(messages[0]))
        self.assertNotIn(secret, str(messages) + str(body))

    async def test_checkout_reports_only_confirmed_provider_categories(self):
        import stripe
        csrf = await self.login()
        for error, expected in (
            (stripe.InvalidRequestError("private", param="amount", code="amount_too_small"), "stripe_amount_too_small"),
            (stripe.AuthenticationError("private"), "stripe_credentials_invalid"),
            (stripe.PermissionError("private"), "stripe_permission_denied"),
            (stripe.InvalidRequestError("private", param="payment_method_types"), "service_unavailable"),
        ):
            with self.subTest(error=type(error).__name__):
                self.service.create_checkout.side_effect = error
                status, body, _ = await self.request("/payments/checkout", method="POST",
                    headers={"origin": "https://pay.test", "X-CSRF-Token": csrf},
                    body={"product_id": "p1", "product_version": 1, "terms_version": "v1", "accepted": True})
                self.assertEqual((status, body), (400, {"detail": expected}))

    async def test_webhook_signature_failure_does_not_log_payload(self):
        import stripe
        self.settings.stripe_webhook_secret = "whsec_fixture"
        error = stripe.SignatureVerificationError("private webhook body", "secret_signature")
        self.service.ingest_webhook = lambda *_args: (_ for _ in ()).throw(error)
        messages = []
        sink = type(logger).add(logger, messages.append, format="{message}")
        try:
            status, body, _ = await self.request("/payments/stripe/webhook", method="POST",
                headers={"Stripe-Signature": "secret_signature"}, body={"private": "secret-body"})
        finally:
            logger.remove(sink)
        self.assertEqual((status, body), (400, {"detail": "stripe_signature_invalid"}))
        self.assertIn("type=SignatureVerificationError", str(messages))
        for secret in ("private webhook body", "secret_signature", "secret-body"):
            self.assertNotIn(secret, str(messages) + str(body))

    async def test_browser_cookie_required_and_cross_origin_reads_rejected(self):
        await self.login()
        session_name, browser_name = self.api._cookie_names()
        copied = {session_name: self.cookies[session_name]}
        self.assertEqual((await self.request("/payments/me", cookies=copied))[0], 401)
        self.assertEqual((await self.request("/payments/me", headers={"origin": "https://evil.test"}))[0], 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
