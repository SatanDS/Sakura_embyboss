#!/usr/bin/env python3
"""Focused SQLite checks for the payment entitlement ledger."""
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


ROOT = Path(__file__).resolve().parents[1]


def load_module(base):
    source = (ROOT / "bot/payments/entitlements.py").read_text(encoding="utf-8")
    source = source.replace("from bot.sql_helper import Base", "")
    namespace = {"Base": base, "__name__": "payment_entitlements_test"}
    exec(compile(source, str(ROOT / "bot/payments/entitlements.py"), "exec"), namespace)
    return namespace


class EntitlementTests(unittest.TestCase):
    def setUp(self):
        self.base = declarative_base()
        self.e = create_engine("sqlite:///:memory:")
        self.s = sessionmaker(bind=self.e, expire_on_commit=False)
        self.m = load_module(self.base)
        from sqlalchemy import BigInteger, Column, DateTime, String
        class Emby(self.base):
            __tablename__ = "entitlement_test_emby"
            tg = Column(BigInteger, primary_key=True)
            embyid = Column(String(64))
            name = Column(String(64))
            lv = Column(String(1), default="b")
            cr = Column(DateTime)
            ex = Column(DateTime)
            disabled_at = Column(DateTime)
        self.m["Emby"] = Emby
        self.base.metadata.create_all(self.e)
        with self.s() as db:
            db.add(Emby(tg=7, embyid="emby-7", name="u7", lv="b"))
            db.commit()

    def test_month_end_and_queue(self):
        with self.s() as db:
            u = db.query(self.m["Emby"]).one()
            first = self.m["append_months"](db, u, "normal", 1, "order-1", datetime(2026, 1, 31))
            second = self.m["append_months"](db, u, "vip", 1, "order-2", datetime(2026, 1, 31))
            self.assertEqual(first.ends_at, datetime(2026, 2, 28))
            self.assertEqual(second.starts_at, first.ends_at)
            self.assertEqual(second.ends_at, datetime(2026, 3, 31))

    def test_replay_and_block(self):
        with self.s() as db:
            u = db.query(self.m["Emby"]).one()
            a = self.m["append_months"](db, u, "vip", 1, "same", datetime(2026, 2, 1))
            b = self.m["append_months"](db, u, "vip", 1, "same", datetime(2026, 2, 1))
            self.assertEqual(a.id, b.id)
            self.m["mark_block"](db, u, "admin", datetime(2026, 2, 1))
            with self.assertRaises(self.m["EntitlementError"]):
                self.m["append_months"](db, u, "vip", 1, "blocked", datetime(2026, 2, 1))

    def test_expiry_reason_is_recoverable_but_admin_block_is_not(self):
        with self.s() as db:
            u = db.query(self.m["Emby"]).one()
            self.m["append_months"](db, u, "vip", 1, "exp", datetime(2026, 1, 1))
            self.m["mark_block"](db, u, "expiry", datetime(2026, 2, 2))
            self.m["clear_block"](db, u, datetime(2026, 2, 2))
            self.m["mark_block"](db, u, "admin", datetime(2026, 2, 2))
            with self.assertRaises(self.m["EntitlementError"]):
                self.m["clear_block"](db, u, datetime(2026, 2, 2))

    def test_leap_year_anniversary(self):
        with self.s() as db:
            u = db.query(self.m["Emby"]).one()
            p = self.m["append_months"](db, u, "normal", 13, "leap", datetime(2028, 2, 29))
            self.assertEqual(p.ends_at, datetime(2029, 3, 29))


if __name__ == "__main__":
    unittest.main()
