#!/usr/bin/env python3
"""Focused tests for durable browser login challenges."""
import unittest
import sys
from pathlib import Path
from datetime import datetime, timedelta
import importlib.util
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


class BrowserAuthTests(unittest.TestCase):
    def setUp(self):
        originals = {name: value for name, value in sys.modules.items()
                     if name == "bot" or name.startswith("bot.")}
        def restore_modules():
            for name in list(sys.modules):
                if name == "bot" or name.startswith("bot."):
                    del sys.modules[name]
            sys.modules.update(originals)
        self.addCleanup(restore_modules)
        for name in originals:
            del sys.modules[name]
        base = declarative_base()
        fake_bot = types.ModuleType("bot")
        fake_sql = types.ModuleType("bot.sql_helper")
        fake_sql.Base = base
        sys.modules["bot"] = fake_bot
        sys.modules["bot.sql_helper"] = fake_sql
        package = types.ModuleType("bot.payments")
        package.__path__ = [str(Path(__file__).resolve().parents[1] / "bot" / "payments")]
        sys.modules["bot.payments"] = package
        def load(name):
            spec = importlib.util.spec_from_file_location(f"bot.payments.{name}", Path(__file__).resolve().parents[1] / "bot" / "payments" / f"{name}.py")
            mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod); return mod
        models = load("models")
        auth_mod = load("browser_auth")
        BrowserChallenge, BrowserSession, BrowserAuth = models.BrowserChallenge, models.BrowserSession, auth_mod.BrowserAuth
        self.models = (BrowserChallenge, BrowserSession)
        # Models are registered on the application Base; use their metadata
        # directly to avoid importing the production engine in this test.
        engine = create_engine("sqlite:///:memory:")
        self.engine = engine
        base.metadata.create_all(engine, tables=[model.__table__ for model in self.models])
        self.sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self.now = datetime(2026, 1, 1, 12, 0, 0)
        self.auth = BrowserAuth(self.sessions, now=lambda: self.now)

    def tearDown(self):
        self.engine.dispose()

    def test_approval_poll_and_replay(self):
        challenge = self.auth.start()
        prepared = self.auth.prepare(challenge["token"], 42)
        self.auth.decide(prepared["id"], 42, approve=True)
        session_token = self.auth.poll(challenge["browser_token"])
        self.assertIsNotNone(session_token)
        self.assertEqual(self.auth.identity(session_token, challenge["browser_token"]), 42)
        with self.assertRaises(Exception):
            self.auth.poll(challenge["browser_token"])

    def test_wrong_approver_and_expiry_fail_closed(self):
        challenge = self.auth.start()
        self.auth.prepare(challenge["token"], 7)
        with self.assertRaises(Exception):
            self.auth.prepare(challenge["token"], 42)
        self.now += timedelta(minutes=6)
        with self.assertRaises(Exception):
            self.auth.prepare(challenge["token"], 42)


if __name__ == "__main__":
    unittest.main(verbosity=2)
