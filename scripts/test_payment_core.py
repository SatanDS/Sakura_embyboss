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

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from loguru import logger
from unittest.mock import AsyncMock, patch

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

    def test_stale_mode_reconcile_task_is_completed_without_retry(self):
        order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        self.settings.mode = "live"
        with self.sessions.begin() as session:
            task = session.query(self.models.Task).filter_by(
                unique_key="checkout:" + order["id"]).one()
            task.unique_key = "reconcile-stale"
            task.task_type = "reconcile_order"
            task.next_run = self.service.utcnow()
        self.assertEqual(asyncio.run(self.ps.process_tasks(limit=1)), 1)
        with self.sessions() as session:
            task = session.query(self.models.Task).filter_by(unique_key="reconcile-stale").one()
            self.assertEqual((task.state, task.last_error), ("done", None))

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

    def test_order_lists_separate_modes_archives_and_buyers(self):
        test_order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        test_other = self.ps.create_order(1, "p1", 1, self.service.TERMS_VERSION, True)
        self.ps.set_orders_archived([test_order["id"]], 1, True)
        self.settings.mode = "live"
        live_order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        self.assertNotEqual(test_order["id"], live_order["id"])
        self.assertEqual([o["id"] for o in self.ps.list_orders()], [live_order["id"]])
        self.assertEqual([o["id"] for o in self.ps.list_orders(mode="test")], [test_other["id"]])
        self.assertEqual([o["id"] for o in self.ps.list_orders(42, mode="test", archived=True)], [test_order["id"]])
        self.assertEqual(len(self.ps.list_orders(mode="all", archived=None)), 3)
        self.assertEqual(len(self.ps.list_orders(42, mode="all", archived=None)), 2)
        self.assertIsNotNone(self.ps.get_order(test_order["id"], 42)["archived_at"])
        with self.assertRaises(self.service.PaymentError):
            self.ps.get_order(test_order["id"], 1)

    def test_archive_is_idempotent_and_audits_only_changed_rows(self):
        order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        self.assertEqual(self.ps.set_orders_archived([order["id"], order["id"]], 1, True), {"ok": True, "changed": 1})
        archived_at = self.ps.get_order(order["id"])["archived_at"]
        self.assertEqual(self.ps.set_orders_archived([order["id"]], 1, True)["changed"], 0)
        self.assertEqual(self.ps.get_order(order["id"])["archived_at"], archived_at)
        self.assertEqual(self.ps.set_orders_archived([order["id"]], 1, False)["changed"], 1)
        self.assertEqual(self.ps.set_orders_archived([order["id"]], 1, False)["changed"], 0)
        with self.sessions() as session:
            audits = session.query(self.models.Audit).order_by(self.models.Audit.created_at).all()
            self.assertEqual([a.action for a in audits], ["order_archived", "order_restored"])
            self.assertEqual({a.actor_tg for a in audits}, {1})

    def test_archive_missing_id_rolls_back_the_whole_batch(self):
        order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        with self.assertRaises(self.service.PaymentError) as ctx:
            self.ps.set_orders_archived([order["id"], "missing"], 1, True)
        self.assertEqual(ctx.exception.code, "not_found")
        self.assertIsNone(self.ps.get_order(order["id"])["archived_at"])
        with self.sessions() as session:
            self.assertEqual(session.query(self.models.Audit).count(), 0)

    def test_archive_rejects_invalid_batch_and_filter_arguments(self):
        for ids in ([], ["x"] * 101, [None], ["x" * 33], "order-id"):
            with self.subTest(ids=ids), self.assertRaises(self.service.PaymentError) as ctx:
                self.ps.set_orders_archived(ids, 1, True)
            self.assertEqual(ctx.exception.code, "invalid_archive_request")
        with self.assertRaises(self.service.PaymentError):
            self.ps.set_orders_archived(["x"], 1, "false")
        with self.assertRaises(self.service.PaymentError):
            self.ps.list_orders(mode="unknown")
        with self.assertRaises(self.service.PaymentError):
            self.ps.list_orders(archived="false")

    def test_archive_protects_paid_or_disputed_live_orders_atomically(self):
        test_order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        self.settings.mode = "live"
        live_order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        for change in ({"payment_state": "paid"}, {"refunded": True},
                       {"dispute_status": "won"}, {"review_required": True}):
            with self.sessions.begin() as session:
                row = session.get(self.models.Order, live_order["id"])
                row.payment_state, row.refunded, row.dispute_status, row.review_required = "pending", False, None, False
                for key, value in change.items():
                    setattr(row, key, value)
            with self.subTest(change=change), self.assertRaises(self.service.PaymentError) as ctx:
                self.ps.set_orders_archived([test_order["id"], live_order["id"]], 1, True)
            self.assertEqual(ctx.exception.code, "archive_not_allowed")
            self.assertIsNone(self.ps.get_order(test_order["id"])["archived_at"])
            self.assertIsNone(self.ps.get_order(live_order["id"])["archived_at"])

    def test_archive_preserves_issued_codes_tasks_reservations_and_redemption(self):
        with self.sessions.begin() as session:
            session.get(self.models.Product, "p1").kind = "register"
        # Production account inventory uses another MySQL connection; SQLite
        # in-memory pools reuse one connection during inspector queries.
        with patch.object(self.service, "actual_account_count", return_value=0):
            order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        with self.sessions.begin() as session:
            session.get(self.models.Order, order["id"]).payment_state = "paid"
        self.ps.fulfill_order(order["id"])
        token = self.ps.reveal_code(order["id"], 42)
        with self.sessions.begin() as session:
            code = session.query(self.models.Code).one()
            code.state, code.redeemer_tg, code.redeemed_at = "redeemed", 42, self.service.utcnow()
            code_id = code.id
        ledger = self.service.append_months.__globals__
        entitlement, period = ledger["AccountEntitlement"], ledger["AccountPeriod"]
        entitlement.__table__.create(self.engine, checkfirst=True)
        period.__table__.create(self.engine, checkfirst=True)
        with self.sessions.begin() as session:
            session.add(entitlement(tg=42, blocked_reason="admin", active_tier="vip", revision=3))
            session.add(period(tg=42, starts_at=datetime(2026, 9, 1), ends_at=datetime(2026, 10, 1),
                               tier="vip", kind="months", source_key="payment-code:" + code_id))
        def snapshot():
            with self.engine.connect() as conn:
                return {name: conn.execute(text("SELECT * FROM " + name)).all() for name in (
                    "payment_codes", "payment_tasks", "payment_registration_reservations", "payment_capacity",
                    "payment_account_entitlements", "payment_account_periods")}
        before = snapshot()
        self.ps.set_orders_archived([order["id"]], 1, True)
        self.assertEqual(snapshot(), before)
        self.assertTrue(self.ps.get_order(order["id"], 42)["code_state"] == "redeemed")
        self.assertEqual(self.ps.reveal_code(order["id"], 42), token)
        with self.assertRaises(self.service.PaymentError) as ctx:
            self.ps.claim_code(token, 42)
        self.assertEqual(ctx.exception.code, "code_used")
        with self.sessions() as session:
            self.assertTrue(session.get(self.models.Order, order["id"]).seat_reserved)

    def test_live_order_with_existing_code_cannot_be_archived_even_if_marked_unpaid(self):
        self.settings.mode = "live"
        order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        with self.sessions.begin() as session:
            session.get(self.models.Order, order["id"]).payment_state = "paid"
        self.ps.fulfill_order(order["id"])
        with self.sessions.begin() as session:
            session.get(self.models.Order, order["id"]).payment_state = "expired"
        with self.assertRaises(self.service.PaymentError) as ctx:
            self.ps.set_orders_archived([order["id"]], 1, True)
        self.assertEqual(ctx.exception.code, "archive_not_allowed")

    def test_archived_unpaid_order_is_reused_and_counts_against_sales_limit(self):
        with self.sessions.begin() as session:
            session.get(self.models.Product, "p1").sales_limit = 1
        order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        self.ps.set_orders_archived([order["id"]], 1, True)
        self.assertEqual(self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)["id"], order["id"])
        with self.assertRaises(self.service.PaymentError) as ctx:
            self.ps.create_order(1, "p1", 1, self.service.TERMS_VERSION, True)
        self.assertEqual(ctx.exception.code, "sold_out")
        self.settings.mode = "live"
        self.assertEqual(self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)["mode"], "live")

    def _provider_checkout(self, order, *, paid=False, refunded=False):
        return {"id": "cs_" + order["id"], "mode": "payment", "livemode": order["mode"] == "live",
                "currency": "cny", "amount_total": order["amount_fen"], "client_reference_id": order["id"],
                "metadata": {"order_id": order["id"]}, "payment_intent": "pi_" + order["id"] if paid else None,
                "payment_status": "paid" if paid else "unpaid", "charge_refunded": refunded}

    def test_archived_live_order_resurfaces_only_when_payment_arrives_and_fulfills_once(self):
        self.settings.mode = "live"
        order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        self.ps.set_orders_archived([order["id"]], 1, True)
        self.gateway.retrieve_checkout = AsyncMock(return_value=self._provider_checkout(order))
        asyncio.run(self.ps.reconcile_order(order["id"], session_hint="cs_" + order["id"]))
        self.assertIsNotNone(self.ps.get_order(order["id"])["archived_at"])
        self.assertEqual(asyncio.run(self.ps.reconcile_orders()), 1)
        self.gateway.retrieve_checkout.return_value = self._provider_checkout(order, paid=True)
        asyncio.run(self.ps.reconcile_order(order["id"]))
        self.assertIsNone(self.ps.get_order(order["id"])["archived_at"])
        self.assertEqual(self.ps.get_order(order["id"])["payment_state"], "paid")
        self.ps.fulfill_order(order["id"])
        self.ps.fulfill_order(order["id"])
        with self.sessions() as session:
            self.assertEqual(session.query(self.models.Code).count(), 1)
            self.assertEqual(session.query(self.models.Task).filter_by(unique_key="fulfill:" + order["id"]).count(), 1)

    def test_archived_test_order_can_fulfill_and_new_review_resurfaces_it(self):
        order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        with self.sessions.begin() as session:
            session.get(self.models.Order, order["id"]).payment_state = "paid"
        self.ps.set_orders_archived([order["id"]], 1, True)
        self.ps.fulfill_order(order["id"])
        self.assertIsNotNone(self.ps.get_order(order["id"])["archived_at"])
        self.gateway.retrieve_checkout = AsyncMock(return_value=self._provider_checkout(order, paid=True))
        asyncio.run(self.ps.reconcile_order(order["id"], session_hint="cs_" + order["id"]))
        self.assertIsNotNone(self.ps.get_order(order["id"])["archived_at"])
        self.gateway.retrieve_checkout.return_value = self._provider_checkout(order, paid=True, refunded=True)
        asyncio.run(self.ps.reconcile_order(order["id"]))
        self.assertIsNone(self.ps.get_order(order["id"])["archived_at"])
        self.assertTrue(self.ps.get_order(order["id"])["review_required"])
        self.assertEqual(self.ps.get_order(order["id"])["code_state"], "held")
        self.ps.set_orders_archived([order["id"]], 1, True)
        asyncio.run(self.ps.reconcile_order(order["id"]))
        self.assertIsNotNone(self.ps.get_order(order["id"])["archived_at"])

    def test_restore_keeps_financial_state_even_if_no_longer_archivable(self):
        order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        self.ps.set_orders_archived([order["id"]], 1, True)
        with self.sessions.begin() as session:
            row = session.get(self.models.Order, order["id"])
            row.mode, row.payment_state, row.review_required = "live", "paid", True
        self.assertEqual(self.ps.set_orders_archived([order["id"]], 1, False)["changed"], 1)
        restored = self.ps.get_order(order["id"])
        self.assertEqual((restored["mode"], restored["payment_state"], restored["review_required"]), ("live", "paid", True))

    def test_archive_migration_preserves_history_and_can_resume(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        spec = importlib.util.spec_from_file_location("payment_archive_migration", ROOT /
            "bot/sql_helper/alembic/versions/20260912_07_add_payment_order_archive.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        for existing_column in (False, True):
            engine = create_engine("sqlite:///:memory:")
            try:
                with engine.begin() as conn:
                    conn.execute(text("CREATE TABLE payment_orders (id VARCHAR(32) PRIMARY KEY, payment_state VARCHAR(24)" +
                                      (", archived_at DATETIME" if existing_column else "") + ")"))
                    conn.execute(text("INSERT INTO payment_orders (id, payment_state) VALUES ('legacy', 'paid')"))
                    with Operations.context(MigrationContext.configure(conn)):
                        migration.upgrade()
                        migration.upgrade()
                    self.assertEqual(conn.execute(text("SELECT id, payment_state, archived_at FROM payment_orders")).one(),
                                     ("legacy", "paid", None))
                    self.assertEqual([i["name"] for i in inspect(conn).get_indexes("payment_orders")],
                                     ["ix_payment_orders_archived_at"])
            finally:
                engine.dispose()
        # Fresh installs create the latest metadata in the initial payment migration.
        order = self.ps.create_order(42, "p1", 1, self.service.TERMS_VERSION, True)
        self.ps.set_orders_archived([order["id"]], 1, True)
        archived_at = self.ps.get_order(order["id"])["archived_at"]
        with self.engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            migration.upgrade()
        self.assertEqual(self.ps.get_order(order["id"])["archived_at"], archived_at)


if __name__ == "__main__":
    unittest.main(verbosity=2)
