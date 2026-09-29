#!/usr/bin/env python3
"""Offline regression checks for the durable Emby device registry."""

import importlib.util
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


ROOT = Path(__file__).resolve().parents[1]


def load_registry():
    """Load sql_devices without importing production Bot configuration."""
    bot = types.ModuleType("bot")
    bot.LOGGER = logging.getLogger("device-registry-test")
    sql_helper = types.ModuleType("bot.sql_helper")
    sql_helper.Base = declarative_base()
    sql_helper.Session = None
    bot.sql_helper = sql_helper
    originals = {name: sys.modules.get(name) for name in ("bot", "bot.sql_helper")}
    sys.modules.update({"bot": bot, "bot.sql_helper": sql_helper})
    try:
        name = "device_registry_test_module"
        spec = importlib.util.spec_from_file_location(name, ROOT / "bot/sql_helper/sql_devices.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original in originals.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


class DeviceRegistryTests(unittest.TestCase):
    def setUp(self):
        self.module = load_registry()
        self.tempdir = tempfile.TemporaryDirectory(prefix=".device-registry-test-")
        self.addCleanup(self.tempdir.cleanup)
        self.engine = create_engine(
            "sqlite:///" + str(Path(self.tempdir.name) / "devices.db")
        )
        self.module.Session = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )
        self.module.Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)

    def observe(self, device_id, *, limit=2, enforce=True):
        return self.module.sql_observe_device(
            emby_user_id="emby-user",
            tg=7,
            device_id=device_id,
            device_name=device_id,
            client_name="Test Client",
            client_version="1",
            limit=limit,
            enforce=enforce,
        )

    def test_repeated_device_is_one_binding_and_new_device_is_blocked(self):
        first = self.observe("device-a")
        repeated = self.observe("device-a")
        second = self.observe("device-b")
        denied = self.observe("device-c")
        self.assertEqual(first["reason"], "new_device")
        self.assertTrue(repeated["allowed"])
        self.assertEqual(repeated["device_count"], 1)
        self.assertTrue(second["allowed"])
        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["reason"], "device_limit")

    def test_monthly_unbind_limit_and_rebind(self):
        first = self.observe("device-a")
        second = self.observe("device-b")
        unbound = self.module.sql_unbind_device(
            device_row_id=first["device"]["id"], tg=7, monthly_limit=1
        )
        denied = self.module.sql_unbind_device(
            device_row_id=second["device"]["id"], tg=7, monthly_limit=1
        )
        rebound = self.observe("device-a")
        self.assertTrue(unbound["ok"])
        self.assertEqual(denied["reason"], "monthly_limit")
        self.assertTrue(rebound["allowed"])
        self.assertEqual(rebound["reason"], "device_rebound")
        repeat_unbind = self.module.sql_unbind_device(
            device_row_id=first["device"]["id"], tg=7, monthly_limit=1
        )
        self.assertEqual(repeat_unbind["reason"], "monthly_limit")

    def test_missing_device_id_uses_low_confidence_observation(self):
        result = self.module.sql_observe_device(
            emby_user_id="emby-user",
            tg=7,
            device_id="",
            device_name="Browser",
            client_name="Web",
            client_version="1",
            limit=1,
            enforce=True,
        )
        self.assertTrue(result["allowed"])
        self.assertFalse(result["high_confidence"])


if __name__ == "__main__":
    unittest.main()
