#!/usr/bin/env python3
"""Payment core regression tests; no MySQL, Stripe, or production config required."""

import asyncio
import base64
import importlib.util
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from loguru import logger
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]


def load_payment_modules():
    base = declarative_base()
    bot = types.ModuleType("bot")
    bot.LOGGER = logger
    sql = types.ModuleType("bot.sql_helper")
    sql.Base = base
    sys.modules["bot"] = bot
    sys.modules["bot.sql_helper"] = sql
    package = types.ModuleType("bot.payments")
    package.__path__ = [str(ROOT / "bot" / "payments")]
    sys.modules["bot.payments"] = package
    loaded = {}
    for name in ("models", "crypto", "service"):
        spec = importlib.util.spec_from_file_location(f"bot.payments.{name}", ROOT / "bot" / "payments" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        loaded[name] = module
    return base, loaded


class FakeGateway:
    def __init__(self):
        self.events = []

    def verify_event(self, raw, signature):
        return raw


class PaymentCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base, cls.mod = load_payment_modules()
        cls.models, cls.service, cls.crypto = cls.mod["models"], cls.mod["service"], cls.mod["crypto"]

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.key = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")
        self.settings = SimpleNamespace(enabled=True, public_url="https://pay.test", mode="test",
                                        stripe_secret_key="sk_test_x", stripe_webhook_secret="whsec_x",
                                        code_key=self.key, checkout_minutes=30, seat_limit=10,
                                        terms_version=self.service.TERMS_VERSION, test_buyer_ids=(1, 42))
        with self.sessions.begin() as session:
            self.product = self.models.Product(id="p1", title="VIP 1 个月", kind="renew", tier="vip",
                                               months=1, price_fen=100, version=1, active=True)
            session.add(self.product)
            self.service.seed_products(session)
        self.gateway = FakeGateway()
        self.ps = self.service.PaymentService(self.sessions, self.settings, self.gateway)

    def tearDown(self):
        self.engine.dispose()

    def test_terms_and_product_version_are_enforced(self):
        with self.assertRaises(self.service.PaymentError) as ctx:
            self.ps.create_order(99, "p1", 1, self.service.TERMS_VERSION, True)
        self.assertEqual(ctx.exception.code, "test_buyer_not_allowed")
        with self.assertRaises(self.service.PaymentError) as ctx:
            self.ps.create_order(1, "p1", 1, self.service.TERMS_VERSION, False)
        self.assertEqual(ctx.exception.code, "terms_required")
        with self.assertRaises(self.service.PaymentError) as ctx:
            self.ps.create_order(1, "p1", 2, self.service.TERMS_VERSION, True)
        self.assertEqual(ctx.exception.code, "product_changed")

    def test_pending_order_is_idempotent(self):
        first = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        second = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.ps.list_orders(42)), 1)

    def test_code_is_encrypted_and_recoverable(self):
        order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        with self.sessions.begin() as session:
            row = session.get(self.models.Order, order["id"])
            row.payment_state = "paid"
        self.ps.fulfill_order(order["id"])
        token = self.ps.reveal_code(order["id"], 42)
        self.assertTrue(token.startswith("DuSheng-Pay_"))
        with self.sessions() as session:
            code = session.query(self.models.Code).one()
            self.assertNotIn(token, code.ciphertext)
            self.assertEqual(code.token_hash, self.crypto.code_hash(token))

        self.settings.mode = "live"
        with self.assertRaises(self.service.PaymentError) as ctx:
            self.ps.reveal_code(order["id"], 42)
        self.assertEqual(ctx.exception.code, "order_mode_mismatch")

    def test_paid_code_uses_dusheng_payment_prefix_and_legacy_prefix_remains_valid(self):
        order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        with self.sessions.begin() as session:
            session.get(self.models.Order, order["id"]).payment_state = "paid"
        self.ps.fulfill_order(order["id"])
        token = self.ps.reveal_code(order["id"], 42)
        self.assertTrue(token.startswith("DuSheng-Pay_"))
        legacy, _, _ = self.crypto.CodeCipher(self.key).issue("legacy-order", prefix="Pay_")
        self.assertTrue(legacy.startswith("Pay_"))

    def test_reconcile_poll_does_not_create_audit_spam(self):
        order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        self.ps.schedule_reconcile(order["id"], 42, audit=False)
        self.ps.schedule_reconcile(order["id"], 42, audit=False)
        with self.sessions() as session:
            self.assertEqual(session.query(self.models.Task).filter(
                self.models.Task.task_type == "reconcile_order").count(), 1)
            self.assertEqual(session.query(self.models.Audit).count(), 0)

    def test_wrong_kind_claim_does_not_lock_a_transfer_code(self):
        order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        with self.sessions.begin() as session:
            session.get(self.models.Order, order["id"]).payment_state = "paid"
        self.ps.fulfill_order(order["id"])
        token = self.ps.reveal_code(order["id"], 42)
        with self.assertRaises(self.service.PaymentError) as ctx:
            self.ps.claim_code(token, 99, expected_kind="register")
        self.assertEqual(ctx.exception.code, "wrong_code_kind")
        with self.sessions() as session:
            code = session.query(self.models.Code).one()
            self.assertEqual((code.state, code.redeemer_tg), ("issued", None))

    def test_live_mode_never_creates_checkout_for_test_order(self):
        order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        self.settings.mode = "live"
        self.gateway.create_checkout = AsyncMock()
        with self.assertRaises(self.service.PaymentError) as ctx:
            asyncio.run(self.ps._create_checkout(order["id"]))
        self.assertEqual(ctx.exception.code, "order_mode_mismatch")
        self.gateway.create_checkout.assert_not_awaited()

    def test_reconcile_worker_ignores_orders_from_other_mode(self):
        order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        self.settings.mode = "live"
        self.assertEqual(asyncio.run(self.ps.reconcile_orders()), 0)
        with self.sessions() as session:
            self.assertEqual(session.query(self.models.Task).filter(
                self.models.Task.task_type == "reconcile_order").count(), 0)

    def test_webhook_event_is_idempotent(self):
        event = {"id": "evt_1", "type": "checkout.session.completed", "livemode": False,
                 "data": {"object": {"id": "cs_1", "metadata": {"order_id": "missing"}}}}
        self.assertEqual(self.ps.ingest_webhook(event, "sig"), "evt_1")
        self.assertEqual(self.ps.ingest_webhook(event, "sig"), "evt_1")
        with self.sessions() as session:
            self.assertEqual(session.query(self.models.Event).count(), 1)
            self.assertEqual(session.query(self.models.Task).count(), 1)

    def test_immediate_outbox_task_is_due_despite_second_precision(self):
        with self.sessions.begin() as session:
            task = self.service.enqueue(session, "immediate-test", "notify_code", {"order_id": "x"})
        self.assertLessEqual(task.next_run, self.service.utcnow())

    def test_checkout_worker_failure_is_retryable_and_logs_stripe_metadata(self):
        import stripe
        from datetime import timedelta
        order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        with self.sessions.begin() as session:
            task = session.query(self.models.Task).filter_by(unique_key="checkout:" + order["id"]).one()
            task.next_run = self.service.utcnow() - timedelta(seconds=5)
        self.gateway.create_checkout = AsyncMock(side_effect=stripe.InvalidRequestError(
            "private_response", param="expires_at", http_status=400,
            headers={"request-id": "req_worker123"}))
        messages = []
        sink = type(logger).add(logger, messages.append, format="{message}")
        try:
            self.assertEqual(asyncio.run(self.ps.process_tasks(limit=1)), 1)
        finally:
            logger.remove(sink)
        with self.sessions() as session:
            task = session.query(self.models.Task).filter_by(unique_key="checkout:" + order["id"]).one()
            self.assertEqual((task.state, task.attempts, task.last_error), ("pending", 1, "InvalidRequestError"))
            self.assertEqual(session.get(self.models.Order, order["id"]).payment_state, "pending")
            self.assertEqual(session.query(self.models.Code).count(), 0)
        self.assertIn("operation=task_create_checkout", str(messages))
        self.assertIn("param=expires_at", str(messages))
        self.assertIn("request_id=req_worker123", str(messages))
        self.assertNotIn("private_response", str(messages))


if __name__ == "__main__":
    unittest.main(verbosity=2)
