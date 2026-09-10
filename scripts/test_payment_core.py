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

ROOT = Path(__file__).resolve().parents[1]


def load_payment_modules():
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
        self.assertTrue(token.startswith("Pay_"))
        with self.sessions() as session:
            code = session.query(self.models.Code).one()
            self.assertNotIn(token, code.ciphertext)
            self.assertEqual(code.token_hash, self.crypto.code_hash(token))

        self.settings.mode = "live"
        with self.assertRaises(self.service.PaymentError) as ctx:
            self.ps.reveal_code(order["id"], 42)
        self.assertEqual(ctx.exception.code, "order_mode_mismatch")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
