"""Exercise the expiry reminder migration with real Alembic and SQLite."""

from contextlib import ExitStack
from pathlib import Path
import runpy
import unittest

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import DateTime, create_engine, inspect


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "bot/sql_helper/alembic/versions/20261101_17_add_expiry_reminder.py"


class ExpiryReminderMigrationTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(MIGRATION.is_file(), "Expiry reminder migration is missing")
        self.migration = runpy.run_path(str(MIGRATION))
        self.engine = create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        self.connection = self.engine.connect()
        self.addCleanup(self.connection.close)
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(Operations.context(MigrationContext.configure(self.connection)))
        self.connection.exec_driver_sql(
            "CREATE TABLE emby (tg BIGINT PRIMARY KEY, name VARCHAR(255), ex DATETIME)"
        )
        self.original = (12345, "existing-account", "2026-11-04 12:30:00")
        self.connection.exec_driver_sql("INSERT INTO emby VALUES (?, ?, ?)", self.original)

    def test_upgrade_preserves_existing_account_and_starts_with_nullable_marker(self):
        self.migration["upgrade"]()

        columns = {column["name"]: column for column in inspect(self.connection).get_columns("emby")}
        self.assertTrue(columns["expiry_reminded_for"]["nullable"])
        self.assertIsInstance(columns["expiry_reminded_for"]["type"], DateTime)
        row = self.connection.exec_driver_sql(
            "SELECT tg, name, ex, expiry_reminded_for FROM emby"
        ).one()
        self.assertEqual(tuple(row), self.original + (None,))

    def test_repeated_upgrade_preserves_successfully_notified_expiry(self):
        self.migration["upgrade"]()
        self.connection.exec_driver_sql("UPDATE emby SET expiry_reminded_for = ex")

        self.migration["upgrade"]()

        row = self.connection.exec_driver_sql(
            "SELECT tg, name, ex, expiry_reminded_for FROM emby"
        ).one()
        self.assertEqual(tuple(row), self.original + (self.original[2],))

    def test_downgrade_is_repeatable_and_reupgrade_preserves_account(self):
        self.migration["upgrade"]()
        self.migration["downgrade"]()
        self.migration["downgrade"]()
        self.assertNotIn(
            "expiry_reminded_for",
            {column["name"] for column in inspect(self.connection).get_columns("emby")},
        )
        self.assertEqual(tuple(self.connection.exec_driver_sql("SELECT * FROM emby").one()), self.original)

        self.migration["upgrade"]()

        row = self.connection.exec_driver_sql(
            "SELECT tg, name, ex, expiry_reminded_for FROM emby"
        ).one()
        self.assertEqual(tuple(row), self.original + (None,))

    def test_revision_extends_the_single_existing_head(self):
        config = Config()
        config.set_main_option("script_location", str(ROOT / "bot/sql_helper/alembic"))
        scripts = ScriptDirectory.from_config(config)

        self.assertEqual(scripts.get_heads(), ["20261101_17"])
        self.assertEqual(self.migration["down_revision"], "20261006_16")


if __name__ == "__main__":
    unittest.main(verbosity=2)
