"""Opt-in MySQL regressions; never import the application configuration.

Set TGBOT_MYSQL_TEST=1, TGBOT_MYSQL_TEST_URL and TGBOT_MYSQL_TEST_FRESH_URL.
Both URLs must use mysql+pymysql and name distinct, pre-created empty
databases starting with tgbot_test. This script leaves test data in place;
database creation and cleanup belong to the caller.
"""

import ast
import asyncio
from contextlib import contextmanager
import importlib
import logging
import os
from pathlib import Path
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import ModuleType, SimpleNamespace
from typing import Dict, List, Optional, Tuple
import unittest
from unittest.mock import AsyncMock, patch

import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker


ROOT = Path(__file__).resolve().parents[1]
ENABLED = os.environ.get("TGBOT_MYSQL_TEST") == "1"
LOGGER = logging.getLogger("mysql-integration")
LOGGER.addHandler(logging.NullHandler())


def checked_test_url(value):
    try:
        url = make_url(value)
    except Exception:
        raise ValueError("A valid dedicated MySQL test URL is required") from None
    if url.drivername != "mysql+pymysql":
        raise ValueError("Integration tests require mysql+pymysql")
    if not re.fullmatch(r"tgbot_test[a-zA-Z0-9_]*", url.database or ""):
        raise ValueError("Database name must start with tgbot_test and contain only letters, digits or underscores")
    return url


def load_source(relative_path, names, namespace):
    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8-sig"))
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
            node.decorator_list = []
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in names for target in node.targets):
            selected.append(node)
    exec(compile(ast.Module(body=selected, type_ignores=[]), relative_path, "exec"), namespace)
    return namespace


def load_sql_runtime(engine):
    namespace = dict(
        Base=declarative_base(), Session=sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
        Column=sa.Column, BigInteger=sa.BigInteger, String=sa.String, DateTime=sa.DateTime,
        Integer=sa.Integer, datetime=datetime, timedelta=timedelta, and_=sa.and_, or_=sa.or_,
        Optional=Optional, List=List, Dict=Dict, Tuple=Tuple, LOGGER=LOGGER,
    )
    load_source("bot/sql_helper/sql_emby.py", {"Emby", "sql_update_emby"}, namespace)
    load_source("bot/sql_helper/sql_emby2.py", {"Emby2", "sql_get_emby2", "sql_update_emby2", "get_all_emby2"}, namespace)
    load_source("bot/sql_helper/sql_code.py",
                {"Code", "INVITE_DURATIONS", "MAX_INVITE_CODES", "sql_buy_invite_codes"}, namespace)
    load_source("bot/sql_helper/sql_partition.py",
                {"PartitionCode", "PartitionGrant", "sql_get_expired_grants", "sql_mark_grants_expired"}, namespace)
    return namespace


@contextmanager
def migration_imports(namespace):
    # env.py and the revision scripts remain real; only bot's import-time startup is replaced.
    modules = {"bot": ModuleType("bot"), "bot.sql_helper": ModuleType("bot.sql_helper")}
    modules["bot"].__path__ = [str(ROOT / "bot")]
    package = modules["bot.sql_helper"]
    package.__path__ = [str(ROOT / "bot/sql_helper")]
    package.Base, package.Session = namespace["Base"], namespace["Session"]
    for name in ("sql_code", "sql_emby", "sql_emby2", "sql_favorites", "sql_partition", "sql_request_record", "sql_douban"):
        module = ModuleType("bot.sql_helper." + name)
        modules[module.__name__] = module
        setattr(package, name, module)
    with patch.dict(sys.modules, modules):
        yield


def run_upgrade(url, namespace, revision="head"):
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(ROOT / "bot/sql_helper/alembic"))
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%"))
    with migration_imports(namespace):
        command.upgrade(config, revision)


def run_startup_migrations(url, namespace):
    startup = dict(namespace, os=os, importlib=importlib, Path=Path,
                   __file__=str(ROOT / "bot/sql_helper/__init__.py"),
                   DATABASE_URL=url.render_as_string(hide_password=False),
                   _MIGRATION_GUARD_ENV="SAKURA_RUNNING_MIGRATIONS")
    load_source("bot/sql_helper/__init__.py", {"run_migrations", "_legacy_create_all_tables"}, startup)
    previous_directory = Path.cwd()
    try:
        os.chdir(ROOT)
        with migration_imports(namespace), patch.dict(os.environ, {"SAKURA_RUNNING_MIGRATIONS": "0"}):
            startup["run_migrations"]()
    finally:
        os.chdir(previous_directory)


def revision_at(engine):
    with engine.connect() as connection:
        return connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()


def expiry_query(namespace):
    """Execute the actual Emby2 expiry selection from the scheduler."""
    tree = ast.parse((ROOT / "bot/scheduler/check_ex.py").read_text(encoding="utf-8-sig"))
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "check_expired")
    statement = next(node for node in function.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == "rseired" for target in node.targets))
    exec(compile(ast.Module(body=[statement], type_ignores=[]), "check_ex.py", "exec"), namespace)
    return namespace["rseired"]


class MySQLConfigurationTests(unittest.TestCase):
    def test_rejects_non_test_databases_before_connecting(self):
        for url in ["mysql+pymysql://root:secret@localhost/production", "sqlite:///tgbot_test",
                    "mysql+pymysql://root:secret@localhost/", "mysql+pymysql://root:secret@localhost/tgbot_test-other"]:
            with self.subTest(url=url.replace("secret", "REDACTED")):
                with self.assertRaises(ValueError):
                    checked_test_url(url)

    def test_accepts_dedicated_test_database(self):
        self.assertEqual(checked_test_url("mysql+pymysql://tester@localhost/tgbot_test_upgrade").database,
                         "tgbot_test_upgrade")


@unittest.skipUnless(ENABLED, "Set TGBOT_MYSQL_TEST=1 and both dedicated test database URLs")
class MySQLIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upgrade_url = checked_test_url(os.environ.get("TGBOT_MYSQL_TEST_URL", ""))
        cls.fresh_url = checked_test_url(os.environ.get("TGBOT_MYSQL_TEST_FRESH_URL", ""))
        if cls.upgrade_url.database == cls.fresh_url.database:
            raise ValueError("Upgrade and fresh-start tests require distinct database names")
        cls.engine = sa.create_engine(cls.upgrade_url, pool_pre_ping=True, hide_parameters=True)
        cls.fresh_engine = sa.create_engine(cls.fresh_url, pool_pre_ping=True, hide_parameters=True)
        cls.addClassCleanup(cls.engine.dispose)
        cls.addClassCleanup(cls.fresh_engine.dispose)
        for engine in (cls.engine, cls.fresh_engine):
            if sa.inspect(engine).get_table_names():
                raise RuntimeError("Test databases must be empty; recreate them before rerunning")
        cls.sql = load_sql_runtime(cls.engine)
        cls.fresh_sql = load_sql_runtime(cls.fresh_engine)

        run_upgrade(cls.upgrade_url, cls.sql, "20260315_04")
        cls.old_revision = revision_at(cls.engine)
        cls.old_columns = {column["name"] for column in sa.inspect(cls.engine).get_columns("emby")}
        with cls.engine.begin() as connection:
            connection.execute(sa.text(
                "INSERT INTO emby (tg,embyid,name,lv,ex,us,iv) VALUES (:tg,:embyid,:name,:lv,:ex,:us,:iv)"
            ), [dict(tg=11001, embyid="legacy-disabled", name="legacy disabled", lv="c",
                     ex=datetime(2020, 1, 1), us=17, iv=123),
                dict(tg=11002, embyid="legacy-active", name="legacy active", lv="b",
                     ex=datetime(2030, 1, 1), us=18, iv=124)])
        run_upgrade(cls.upgrade_url, cls.sql)
        cls.first_revision = revision_at(cls.engine)
        run_upgrade(cls.upgrade_url, cls.sql)
        cls.repeated_revision = revision_at(cls.engine)

        run_startup_migrations(cls.fresh_url, cls.fresh_sql)
        cls.first_fresh_revision = revision_at(cls.fresh_engine)
        run_startup_migrations(cls.fresh_url, cls.fresh_sql)
        cls.repeated_fresh_revision = revision_at(cls.fresh_engine)

    def test_upgrade_retains_old_rows_and_null_freeze_start(self):
        self.assertEqual(self.old_revision, "20260315_03")
        self.assertNotIn("disabled_at", self.old_columns)
        self.assertEqual(self.first_revision, "20260909_05")
        self.assertEqual(self.repeated_revision, self.first_revision)
        with self.sql["Session"]() as session:
            disabled = session.get(self.sql["Emby"], 11001)
            active = session.get(self.sql["Emby"], 11002)
            self.assertEqual((disabled.name, disabled.lv, disabled.ex, disabled.us, disabled.iv),
                             ("legacy disabled", "c", datetime(2020, 1, 1), 17, 123))
            self.assertEqual((active.name, active.lv, active.ex, active.us, active.iv),
                             ("legacy active", "b", datetime(2030, 1, 1), 18, 124))
            self.assertIsNone(disabled.disabled_at)
            self.assertIsNone(active.disabled_at)

    def test_fresh_startup_and_repeat_upgrade_are_usable(self):
        self.assertEqual(self.first_fresh_revision, "20260909_05")
        self.assertEqual(self.repeated_fresh_revision, self.first_fresh_revision)
        with self.fresh_sql["Session"]() as session:
            row = self.fresh_sql["Emby"](tg=12001, embyid="fresh-user", lv="c", disabled_at=datetime(2026, 9, 9))
            session.add(row)
            session.commit()
        with self.fresh_sql["Session"]() as session:
            self.assertEqual(session.get(self.fresh_sql["Emby"], 12001).disabled_at, datetime(2026, 9, 9))

    def test_real_concurrent_invites_cannot_double_spend(self):
        with self.sql["Session"]() as session:
            session.add(self.sql["Emby"](tg=13001, lv="b", iv=100))
            session.commit()
        barrier = threading.Barrier(2)

        def buy(index):
            barrier.wait(timeout=10)
            return self.sql["sql_buy_invite_codes"](13001, ["mysql-concurrent-" + str(index)], 30, 60, lambda row: True)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(buy, [1, 2]))
        self.assertEqual(sorted(result["status"] for result in results), ["insufficient", "ok"])
        with self.sql["Session"]() as session:
            self.assertEqual(session.get(self.sql["Emby"], 13001).iv, 40)
            self.assertEqual(session.query(self.sql["Code"]).filter(self.sql["Code"].tg == 13001).count(), 1)

    def test_real_code_insert_failure_rolls_back_charge(self):
        with self.sql["Session"]() as session:
            session.add(self.sql["Emby"](tg=13002, lv="b", iv=100))
            session.add(self.sql["Code"](code="mysql-duplicate", tg=13002, us=30))
            session.commit()
        result = self.sql["sql_buy_invite_codes"](13002, ["mysql-duplicate"], 30, 60, lambda row: True)
        self.assertEqual(result["status"], "error")
        with self.sql["Session"]() as session:
            self.assertEqual(session.get(self.sql["Emby"], 13002).iv, 100)
            self.assertEqual(session.query(self.sql["Code"]).filter(self.sql["Code"].tg == 13002).count(), 1)

    def test_non_tg_renewal_reenters_actual_expiry_query(self):
        with self.sql["Session"]() as session:
            session.add(self.sql["Emby2"](embyid="mysql-renew", name="mysql renew", lv="c", expired=1,
                                           ex=datetime(2020, 1, 1)))
            session.commit()
        row = self.sql["sql_get_emby2"]("mysql-renew")
        reply = SimpleNamespace(edit=AsyncMock(return_value=SimpleNamespace(forward=AsyncMock())))
        namespace = dict(self.sql, get_user_input=AsyncMock(return_value=(30, row, 1, "test admin")),
                         sendMessage=AsyncMock(), emby=SimpleNamespace(emby_change_policy=AsyncMock(return_value=True)))
        load_source("bot/modules/commands/renew.py", {"renew_user"}, namespace)
        asyncio.run(namespace["renew_user"](None, SimpleNamespace(reply=AsyncMock(return_value=reply))))
        renewed = self.sql["sql_get_emby2"]("mysql-renew")
        self.assertEqual((renewed.lv, renewed.expired), ("b", 0))
        self.assertGreater(renewed.ex, datetime.now() + timedelta(days=29))
        self.assertEqual(expiry_query(dict(self.sql)), [])

        class AfterExpiry(datetime):
            @classmethod
            def now(cls):
                return renewed.ex + timedelta(seconds=1)

        expired = expiry_query(dict(self.sql, datetime=AfterExpiry))
        self.assertEqual([user.embyid for user in expired], ["mysql-renew"])

    def test_partition_update_preserves_a_renewed_grant(self):
        now = datetime.now().replace(microsecond=0)
        Grant = self.sql["PartitionGrant"]
        with self.sql["Session"]() as session:
            rows = [Grant(tg=14001, partition="expired", expires_at=now - timedelta(days=1), status="active"),
                    Grant(tg=14001, partition="renewed", expires_at=now - timedelta(days=1), status="active"),
                    Grant(tg=14002, partition="other-user", expires_at=now - timedelta(days=1), status="active")]
            session.add_all(rows)
            session.commit()
            expired_id, renewed_id, other_id = [row.id for row in rows]
        observed = self.sql["sql_get_expired_grants"](now, tg=14001)
        self.assertEqual({row.id for row in observed}, {expired_id, renewed_id})
        with self.sql["Session"]() as session:
            session.get(Grant, renewed_id).expires_at = now + timedelta(days=30)
            session.commit()
        self.assertTrue(self.sql["sql_mark_grants_expired"]([row.id for row in observed], now=now))
        with self.sql["Session"]() as session:
            self.assertEqual(session.get(Grant, expired_id).status, "expired")
            self.assertEqual(session.get(Grant, renewed_id).status, "active")
            self.assertEqual(session.get(Grant, other_id).status, "active")
        self.assertEqual(self.sql["sql_get_expired_grants"](now, tg=14001), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
