#!/usr/bin/env python3
"""Isolated Emby authentication-database and identity regressions."""
import ast
import importlib.util
import os
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
USER_ONE = uuid.UUID("01234567-89ab-cdef-0123-456789abcdef")
USER_TWO = uuid.UUID("fedcba98-7654-3210-fedc-ba9876543210")
CLIENT_TOKEN = "synthetic-client-token"


class LookupColumn:
    def __init__(self, name):
        self.name = name

    def __eq__(self, value):
        return self.name, value


def load_identity_module(config, emby):
    logger = types.SimpleNamespace(**{name: Mock() for name in ("error", "warning", "info", "debug")})
    modules = {}
    for name, attributes in {
        "bot": {"LOGGER": logger, "bot": Mock(), "config": config},
        "bot.func_helper": {},
        "bot.func_helper.emby": {"emby": emby},
        "bot.sql_helper": {},
        "bot.sql_helper.sql_emby": {
            "Emby": type("Emby", (), {"tg": LookupColumn("tg")}),
            "sql_get_emby_by_embyid": Mock(return_value=None), "sql_update_emby": Mock(return_value=True)},
        "bot.sql_helper.sql_emby2": {
            "Emby2": type("Emby2", (), {"embyid": LookupColumn("embyid")}),
            "sql_get_emby2_by_embyid": Mock(return_value=None), "sql_update_emby2": Mock(return_value=True)},
    }.items():
        module = types.ModuleType(name)
        if name in ('bot', 'bot.func_helper'):
            module.__path__ = [str(ROOT / name.replace('.', '/'))]
        module.__dict__.update(attributes)
        modules[name] = module
    spec = importlib.util.spec_from_file_location("_vip_identity_test", ROOT / "bot/web/api/webhook/line_report.py")
    module = importlib.util.module_from_spec(spec)
    original_modules = {name: sys.modules.get(name) for name in modules}
    try:
        sys.modules.update(modules)
        spec.loader.exec_module(module)
    finally:
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
    return module


def load_sqlite_emby2_lookup():
    """Run the real account model and helpers without loading Bot services."""
    from sqlalchemy import Column, DateTime, Integer, String, create_engine, or_
    from sqlalchemy.orm import declarative_base, sessionmaker

    base = declarative_base()
    engine = create_engine("sqlite://")
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    source = (ROOT / "bot/sql_helper/sql_emby2.py").read_text(encoding="utf-8")
    names = {"Emby2", "sql_get_emby2", "sql_get_emby2_by_embyid"}
    nodes = [node for node in ast.parse(source).body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    namespace = {"Base": base, "Session": session_factory, "Column": Column,
                 "DateTime": DateTime, "Integer": Integer, "String": String, "or_": or_}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<sqlite-emby2-test>", "exec"), namespace)
    base.metadata.create_all(engine)
    return types.SimpleNamespace(engine=engine, Session=session_factory,
                                 Emby2=namespace["Emby2"], namespace=namespace,
                                 lookup=namespace["sql_get_emby2_by_embyid"],
                                 broad_lookup=namespace["sql_get_emby2"])


class VIPIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".vip-identity-test-", dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.auth_db = self.directory / "authentication.db"
        self.config = types.SimpleNamespace(emby_auth_db_path=str(self.auth_db))
        self.emby = types.SimpleNamespace(_request=AsyncMock(side_effect=AssertionError("Unexpected remote lookup")))
        self.identity = load_identity_module(self.config, self.emby)
        self.environment = patch.dict(os.environ, {"EMBY_AUTH_DB_PATH": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def tokens(self, rows, table="Tokens", *, db_path=None):
        path = db_path or self.auth_db
        with sqlite3.connect(path) as connection:
            connection.execute(f"CREATE TABLE {table} (AccessToken TEXT, UserId, IsActive INTEGER)")
            connection.executemany(f"INSERT INTO {table} VALUES (?, ?, ?)", rows)

    def users(self, rows, table="LocalUsersv2", *, db_path=None):
        path = db_path or self.directory / "users.db"
        with sqlite3.connect(path) as connection:
            connection.execute(f"CREATE TABLE {table} (Id INTEGER, guid)")
            connection.executemany(f"INSERT INTO {table} VALUES (?, ?)", rows)

    def lookup(self, token=CLIENT_TOKEN):
        return self.identity._lookup_user_from_auth_db_sync(token, str(self.auth_db))

    def test_tokens_table_resolves_public_guid(self):
        self.tokens([(CLIENT_TOKEN, str(USER_ONE).upper(), 1)])
        self.assertEqual(self.lookup(), (USER_ONE.hex, "emby.authentication_db"))

    def test_tokens_2_resolves_little_endian_numeric_user_mapping(self):
        self.tokens([(CLIENT_TOKEN, 7, 1)], "Tokens_2")
        self.users([(7, USER_ONE.bytes_le)])
        self.assertEqual(self.lookup()[0], USER_ONE.hex)
        self.assertNotEqual(USER_ONE.hex, uuid.UUID(bytes=USER_ONE.bytes_le).hex)

    def test_numeric_text_id_resolves_text_guid(self):
        self.tokens([(CLIENT_TOKEN, "7", 1)], "Tokens_2")
        self.users([(7, str(USER_ONE))])
        self.assertEqual(self.lookup()[0], USER_ONE.hex)

    def test_legacy_users_table_mapping_is_supported(self):
        self.tokens([(CLIENT_TOKEN, 7, 1)])
        self.users([(7, USER_ONE.bytes_le)], "Users")
        self.assertEqual(self.lookup()[0], USER_ONE.hex)

    def test_users_database_in_parent_directory_is_supported(self):
        nested = self.directory / "data"
        nested.mkdir()
        self.auth_db = nested / "authentication.db"
        self.tokens([(CLIENT_TOKEN, 7, 1)])
        self.users([(7, USER_ONE.bytes_le)])
        self.assertEqual(self.lookup()[0], USER_ONE.hex)

    def test_auth_database_path_with_uri_reserved_characters_is_supported(self):
        self.auth_db = self.directory / "authentication #1?.db"
        if os.name == "nt":
            self.auth_db = self.directory / "authentication #1.db"
        self.tokens([(CLIENT_TOKEN, USER_ONE.hex, 1)])
        self.assertEqual(self.lookup()[0], USER_ONE.hex)

    def test_inactive_and_unknown_tokens_cannot_authenticate(self):
        self.tokens([(CLIENT_TOKEN, USER_ONE.hex, 0), ("other-token", USER_TWO.hex, 1)])
        self.assertEqual(self.lookup()[0], "")
        self.assertEqual(self.lookup("missing-token")[0], "")

    def test_token_lookup_uses_bound_sql_parameters(self):
        self.tokens([(CLIENT_TOKEN, USER_ONE.hex, 1)])
        self.assertEqual(self.lookup("' OR 1=1 --")[0], "")
        self.assertEqual(self.lookup()[0], USER_ONE.hex)

    def test_tokens_for_two_different_users_are_rejected(self):
        self.tokens([(CLIENT_TOKEN, USER_ONE.hex, 1), (CLIENT_TOKEN, USER_TWO.hex, 1)])
        self.assertEqual(self.lookup()[0], "")

    def test_conflicting_token_tables_are_rejected(self):
        self.tokens([(CLIENT_TOKEN, USER_ONE.hex, 1)])
        self.tokens([(CLIENT_TOKEN, USER_TWO.hex, 1)], "Tokens_2")
        self.assertEqual(self.lookup()[0], "")

    def test_duplicate_rows_for_the_same_user_are_not_ambiguous(self):
        self.tokens([(CLIENT_TOKEN, USER_ONE.hex, 1)] * 3)
        self.assertEqual(self.lookup()[0], USER_ONE.hex)

    def test_unknown_schema_and_missing_required_columns_fail_closed(self):
        for sql in ("CREATE TABLE OtherTokens (AccessToken TEXT, UserId TEXT, IsActive INTEGER)",
                    "CREATE TABLE Tokens (AccessToken TEXT, UserId TEXT)"):
            with self.subTest(schema=sql):
                path = self.directory / ("schema" + str(len(sql)) + ".db")
                with sqlite3.connect(path) as connection:
                    connection.execute(sql)
                self.assertEqual(self.identity._lookup_user_from_auth_db_sync(CLIENT_TOKEN, str(path))[0], "")

    def test_missing_or_corrupt_database_does_not_create_or_authorize(self):
        self.assertEqual(self.lookup()[0], "")
        self.assertFalse(self.auth_db.exists())
        self.auth_db.write_bytes(b"not a sqlite database")
        self.assertEqual(self.lookup()[0], "")

    def test_missing_or_unknown_numeric_user_mapping_fails_closed(self):
        self.tokens([(CLIENT_TOKEN, 7, 1)])
        self.assertEqual(self.lookup()[0], "")
        self.users([(8, USER_ONE.bytes_le)])
        self.assertEqual(self.lookup()[0], "")

    def test_invalid_guid_mapping_fails_closed(self):
        self.tokens([(CLIENT_TOKEN, 7, 1)])
        self.users([(7, b"short")])
        self.assertEqual(self.lookup()[0], "")

    def test_opened_auth_and_user_connections_are_read_only(self):
        self.tokens([(CLIENT_TOKEN, 7, 1)])
        self.users([(7, USER_ONE.bytes_le)])
        connect = sqlite3.connect
        verified = []

        def guarded_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("CREATE TABLE unauthorized_write (value INTEGER)")
                verified.append(args[0])
            except BaseException:
                connection.close()
                raise
            return connection

        with patch.object(sys.modules["bot.func_helper.emby_identity"].sqlite3, "connect", side_effect=guarded_connect):
            self.assertEqual(self.lookup()[0], USER_ONE.hex)
        self.assertEqual(len(verified), 2)

    def test_committed_wal_token_is_visible_and_revocation_is_immediate(self):
        writer = sqlite3.connect(self.auth_db)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE Tokens (AccessToken TEXT, UserId TEXT, IsActive INTEGER)")
        writer.execute("INSERT INTO Tokens VALUES (?, ?, 1)", (CLIENT_TOKEN, USER_ONE.hex))
        writer.commit()
        self.assertTrue(Path(str(self.auth_db) + "-wal").exists())
        self.assertEqual(self.lookup()[0], USER_ONE.hex)
        writer.execute("UPDATE Tokens SET IsActive=0 WHERE AccessToken=?", (CLIENT_TOKEN,))
        writer.commit()
        self.assertEqual(self.lookup()[0], "")

    async def test_configured_database_does_not_call_users_me(self):
        self.tokens([(CLIENT_TOKEN, USER_ONE.hex, 1)])
        self.assertEqual((await self.identity._get_user_from_token(CLIENT_TOKEN))[0], USER_ONE.hex)
        self.emby._request.assert_not_awaited()

    async def test_unknown_token_does_not_fall_back_to_users_me(self):
        self.tokens([("other-token", USER_ONE.hex, 1)])
        self.assertEqual((await self.identity._get_user_from_token(CLIENT_TOKEN))[0], "")
        self.emby._request.assert_not_awaited()

    async def test_revoked_auth_db_token_never_uses_stale_session_fallback(self):
        self.tokens([(CLIENT_TOKEN, USER_ONE.hex, 0)])
        self.identity._fetch_active_sessions_result = AsyncMock(return_value=(True, [{
            'AccessToken': CLIENT_TOKEN, 'UserId': USER_ONE.hex, 'Id': 'stale-session',
        }], ''))
        result = await self.identity.resolve_user_context(token=CLIENT_TOKEN)
        self.assertEqual(result[0], '')
        self.identity._fetch_active_sessions_result.assert_not_awaited()

    def test_duplicate_rows_cannot_hide_another_token_owner(self):
        self.tokens([(CLIENT_TOKEN, USER_ONE.hex, 1)] * 3 + [(CLIENT_TOKEN, USER_TWO.hex, 1)])
        self.assertEqual(self.lookup()[0], '')

    def test_partially_mapped_token_owners_cannot_authorize(self):
        self.tokens([(CLIENT_TOKEN, USER_ONE.hex, 1), (CLIENT_TOKEN, 777, 1)])
        self.assertEqual(self.lookup()[0], '')

    async def test_valid_token_ignores_stale_user_id_and_foreign_session(self):
        self.tokens([(CLIENT_TOKEN, USER_ONE.hex, 1)])
        self.identity._fetch_active_sessions_result = AsyncMock(return_value=(True, [{
            "Id": "victim-session", "UserId": USER_TWO.hex, "DeviceId": "victim-device"}], ""))
        user_id, session, _ = await self.identity.resolve_user_context(
            token=CLIENT_TOKEN, user_id=USER_TWO.hex, device_id="victim-device", session_id="victim-session",
            auth_header=f'Emby UserId="{USER_TWO.hex}"',
            original_request_uri=f"/emby/Videos/item/stream?UserId={USER_TWO.hex}")
        self.assertEqual(user_id, USER_ONE.hex)
        self.assertIsNone(session)

    async def test_invalid_token_never_uses_claimed_user_or_device(self):
        self.tokens([("other-token", USER_ONE.hex, 1)])
        self.identity._fetch_active_sessions_result = AsyncMock(return_value=(True, [{
            "Id": "victim-session", "UserId": USER_TWO.hex, "DeviceId": "victim-device"}], ""))
        user_id, session, _ = await self.identity.resolve_user_context(
            token=CLIENT_TOKEN, user_id=USER_TWO.hex, device_id="victim-device", session_id="victim-session")
        self.assertEqual(user_id, "")
        self.assertIsNone(session)

    async def test_conflicting_tokens_reject_before_db_lookup(self):
        lookup = AsyncMock()
        self.identity._get_user_from_token = lookup
        result = await self.identity.resolve_user_context(token=CLIENT_TOKEN, auth_header='Emby Token="different-token"')
        self.assertEqual(result[0], "")
        lookup.assert_not_awaited()

    async def test_auth_db_lookup_runs_off_the_event_loop(self):
        self.tokens([(CLIENT_TOKEN, USER_ONE.hex, 1)])
        loop_thread = threading.get_ident()
        threads = []
        lookup = self.identity._lookup_user_from_auth_db_sync

        def record_thread(*args):
            threads.append(threading.get_ident())
            return lookup(*args)

        with patch.object(self.identity, "_lookup_user_from_auth_db_sync", side_effect=record_thread):
            self.assertEqual((await self.identity._get_user_from_auth_db(CLIENT_TOKEN))[0], USER_ONE.hex)
        self.assertNotEqual(threads, [loop_thread])

    async def report_line(self):
        return await self.identity.line_report(
            line="vip", host="vip.example.test", token=CLIENT_TOKEN,
            x_emby_authorization=None, authorization=None, x_emby_token=None, x_original_uri=None)

    def setup_line_entitlement_check(self):
        self.config.line_filter_block_user = True
        self.config.line_filter_terminate_session = True
        self.identity.classify_line_request = Mock(return_value="vip")
        self.identity.resolve_user_context = AsyncMock(return_value=(USER_ONE.hex, {"Id": "client-session"}, "test-token"))
        self.identity.handle_line_violation = AsyncMock()
        self.identity.update_cooldown = Mock()

    async def test_entitlement_database_outage_is_503_without_enforcement_or_cooldown(self):
        self.setup_line_entitlement_check()
        self.identity.sql_get_emby_by_embyid.side_effect = sqlite3.OperationalError("database unavailable")
        result = await self.report_line()
        self.assertEqual(result.status_code, 503)
        self.identity.sql_get_emby_by_embyid.assert_called_once_with(USER_ONE.hex, raise_on_error=True)
        self.identity.sql_get_emby2_by_embyid.assert_not_called()
        self.identity.handle_line_violation.assert_not_awaited()
        self.identity.update_cooldown.assert_not_called()

    async def test_vip_recovers_immediately_after_entitlement_database_outage(self):
        self.setup_line_entitlement_check()
        vip = types.SimpleNamespace(lv="a", ex=datetime.now() + timedelta(days=1))
        self.identity.sql_get_emby_by_embyid.side_effect = [sqlite3.OperationalError("offline"), vip]
        self.assertEqual((await self.report_line()).status_code, 503)
        self.assertEqual((await self.report_line())["status"], "allowed")
        self.identity.handle_line_violation.assert_not_awaited()
        self.identity.update_cooldown.assert_not_called()


class NonTelegramLineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = types.SimpleNamespace(
            emby_line="normal.example.test", emby_whitelist_line="vip.example.test",
            line_filter_terminate_session=True, line_filter_block_user=False)
        self.emby = types.SimpleNamespace(
            _request=AsyncMock(side_effect=AssertionError("Unexpected remote lookup")),
            terminate_session=AsyncMock(return_value=True),
            emby_change_policy=AsyncMock(return_value=True))
        self.identity = load_identity_module(self.config, self.emby)
        self.session = {"Id": "client-session", "UserName": "宏旺", "Client": "Infuse-Direct"}
        self.identity.resolve_user_context = AsyncMock(return_value=(USER_ONE.hex, self.session, "test-token"))
        self.enforce_line_violation = self.identity.handle_line_violation
        self.identity.handle_line_violation = AsyncMock(return_value={})
        self.identity.update_cooldown = Mock()

    def account(self, *, lv="a", ex=None):
        account = self.identity.Emby2()
        account.embyid = USER_ONE.hex
        account.name = "宏旺"
        account.lv = lv
        account.ex = ex
        return account

    async def report_line(self):
        return await self.identity.line_report(
            line="vip", host="vip.example.test", token=CLIENT_TOKEN,
            x_emby_authorization=None, authorization=None, x_emby_token=None, x_original_uri=None)

    async def test_active_non_telegram_whitelist_uses_emby2_entitlement(self):
        account = self.account(ex=datetime.now() + timedelta(days=1000))
        self.identity.sql_get_emby2_by_embyid.return_value = account
        result = await self.report_line()
        self.assertEqual(result["status"], "allowed")
        self.identity.sql_get_emby_by_embyid.assert_called_once_with(USER_ONE.hex, raise_on_error=True)
        self.identity.sql_get_emby2_by_embyid.assert_called_once_with(USER_ONE.hex, raise_on_error=True)
        self.identity.handle_line_violation.assert_not_awaited()
        self.identity.update_cooldown.assert_not_called()
        self.emby.terminate_session.assert_not_awaited()
        self.emby.emby_change_policy.assert_not_awaited()

    async def test_non_telegram_normal_expired_and_banned_accounts_are_denied(self):
        for lv, expiry in (("b", datetime.now() + timedelta(days=1)),
                           ("a", datetime.now() - timedelta(days=1)),
                           ("c", datetime.now() + timedelta(days=1)), ("a", None)):
            with self.subTest(level=lv, expiry=expiry):
                account = self.account(lv=lv, ex=expiry)
                self.identity.sql_get_emby2_by_embyid.return_value = account
                result = await self.report_line()
                self.assertEqual(result.status_code, 403)
                self.identity.handle_line_violation.assert_awaited_with(
                    emby_id=USER_ONE.hex, user_name="宏旺", session_id="client-session",
                    client_name="Infuse-Direct", user_details=account)

    async def test_telegram_entitlement_cannot_be_upgraded_by_duplicate_emby2_record(self):
        self.identity.sql_get_emby2_by_embyid.return_value = self.account(
            ex=datetime.now() + timedelta(days=1000))
        for lv, expiry in (("b", datetime.now() + timedelta(days=1)),
                           ("c", datetime.now() + timedelta(days=1)),
                           ("a", datetime.now() - timedelta(days=1))):
            with self.subTest(level=lv):
                telegram_account = types.SimpleNamespace(
                    tg=101, embyid=USER_ONE.hex, name="宏旺", lv=lv, ex=expiry)
                self.identity.sql_get_emby_by_embyid.return_value = telegram_account
                result = await self.report_line()
                self.assertEqual(result.status_code, 403)
                self.identity.sql_get_emby2_by_embyid.assert_not_called()
                self.assertIs(self.identity.handle_line_violation.await_args.kwargs["user_details"],
                              telegram_account)

    async def test_emby2_database_outage_is_503_without_enforcement_or_cooldown(self):
        self.identity.sql_get_emby2_by_embyid.side_effect = sqlite3.OperationalError("database unavailable")
        result = await self.report_line()
        self.assertEqual(result.status_code, 503)
        self.identity.handle_line_violation.assert_not_awaited()
        self.identity.update_cooldown.assert_not_called()
        self.emby.terminate_session.assert_not_awaited()
        self.emby.emby_change_policy.assert_not_awaited()

    async def test_non_telegram_whitelist_recovers_immediately_after_database_outage(self):
        account = self.account(ex=datetime.now() + timedelta(days=1))
        self.identity.sql_get_emby2_by_embyid.side_effect = [sqlite3.OperationalError("offline"), account]
        self.assertEqual((await self.report_line()).status_code, 503)
        self.assertEqual((await self.report_line())["status"], "allowed")
        self.identity.handle_line_violation.assert_not_awaited()
        self.identity.update_cooldown.assert_not_called()

    def test_payment_configuration_does_not_apply_telegram_ledger_to_emby2(self):
        self.config.payments = types.SimpleNamespace()
        account = self.account(ex=datetime.now() + timedelta(days=1))
        sql_module = types.ModuleType("bot.sql_helper")
        sql_module.Session = Mock(side_effect=AssertionError("Unexpected Telegram ledger lookup"))
        ledger_module = types.ModuleType("bot.payments.entitlements")
        ledger_module.resolve_entitlement = Mock(side_effect=AssertionError("Unexpected Telegram entitlement"))
        ledger_module.AccountEntitlement = Mock()
        with patch.dict(sys.modules, {"bot.sql_helper": sql_module,
                                      "bot.payments.entitlements": ledger_module}):
            self.assertIs(self.identity.effective_line_entitlement(account), account)
        sql_module.Session.assert_not_called()
        ledger_module.resolve_entitlement.assert_not_called()

    async def test_non_telegram_username_collision_does_not_grant_vip_access(self):
        database = load_sqlite_emby2_lookup()
        self.addCleanup(database.engine.dispose)
        with database.Session() as session:
            session.add(database.Emby2(embyid=USER_TWO.hex, name=USER_ONE.hex, lv="a",
                                       ex=datetime.now() + timedelta(days=1)))
            session.commit()
        self.assertEqual(database.broad_lookup(USER_ONE.hex).embyid, USER_TWO.hex)
        self.identity.sql_get_emby2_by_embyid = database.lookup
        result = await self.report_line()
        self.assertEqual(result.status_code, 403)
        self.assertIsNone(self.identity.handle_line_violation.await_args.kwargs["user_details"])

    async def test_non_telegram_enforcement_writes_emby2_and_logs_level_without_tg(self):
        account = self.account(lv="b", ex=datetime.now() + timedelta(days=1))
        self.config.line_filter_block_user = True
        self.config.group = [999]
        notification = types.SimpleNamespace(forward=AsyncMock())
        self.identity.bot = types.SimpleNamespace(send_message=AsyncMock(return_value=notification))
        result = await self.enforce_line_violation(
            emby_id=USER_ONE.hex, user_name=account.name, session_id="client-session",
            client_name="Infuse-Direct", user_details=account)
        self.assertTrue(result["terminate_success"])
        self.assertTrue(result["block_success"])
        self.emby.emby_change_policy.assert_awaited_once_with(emby_id=USER_ONE.hex, disable=True)
        self.identity.sql_update_emby2.assert_called_once_with(("embyid", USER_ONE.hex), lv="c")
        self.identity.sql_update_emby.assert_not_called()
        self.identity.bot.send_message.assert_awaited_once()
        text = self.identity.bot.send_message.await_args.kwargs["text"]
        self.assertIn("📱 TG ID: Unknown", text)
        self.assertIn("🏷️ 用户等级: 普通用户", text)
        notification.forward.assert_not_awaited()


class StrictNonTelegramEntitlementLookupTests(unittest.TestCase):
    def setUp(self):
        self.database = load_sqlite_emby2_lookup()
        self.addCleanup(self.database.engine.dispose)

    def test_exact_lookup_finds_only_the_canonical_emby_id(self):
        with self.database.Session() as session:
            session.add_all([
                self.database.Emby2(embyid=USER_TWO.hex, name=USER_ONE.hex, lv="a"),
                self.database.Emby2(embyid=USER_ONE.hex, name="宏旺", lv="b"),
            ])
            session.commit()
        account = self.database.lookup(USER_ONE.hex, raise_on_error=True)
        self.assertEqual(account.embyid, USER_ONE.hex)
        self.assertEqual(account.name, "宏旺")
        self.assertEqual(account.lv, "b")

    def test_name_matching_without_an_emby_id_match_is_absent(self):
        with self.database.Session() as session:
            session.add(self.database.Emby2(embyid=USER_TWO.hex, name=USER_ONE.hex, lv="a"))
            session.commit()
        self.assertIsNone(self.database.lookup(USER_ONE.hex, raise_on_error=True))
        self.assertIsNone(self.database.lookup(None, raise_on_error=True))

    def test_strict_lookup_preserves_sqlite_errors_and_default_remains_compatible(self):
        from sqlalchemy.exc import OperationalError

        self.database.Emby2.__table__.drop(self.database.engine)
        self.assertIsNone(self.database.lookup(USER_ONE.hex))
        with self.assertRaises(OperationalError):
            self.database.lookup(USER_ONE.hex, raise_on_error=True)


class StrictEntitlementLookupTests(unittest.TestCase):
    def setUp(self):
        source = (ROOT / "bot/sql_helper/sql_emby.py").read_text(encoding="utf-8")
        node = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == "sql_get_emby_by_embyid")
        session = Mock()
        session.query.side_effect = sqlite3.OperationalError("database unavailable")
        self.session = session

        class SessionContext:
            def __enter__(self):
                return session

            def __exit__(self, *args):
                pass

        namespace = {"Session": SessionContext, "Emby": type("Emby", (), {"embyid": "embyid"})}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<strict-entitlement-test>", "exec"), namespace)
        self.lookup = namespace["sql_get_emby_by_embyid"]

    def test_default_lookup_remains_compatible(self):
        self.assertIsNone(self.lookup(USER_ONE.hex))

    def test_strict_lookup_preserves_database_errors(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.lookup(USER_ONE.hex, raise_on_error=True)

    def test_strict_lookup_distinguishes_absent_account(self):
        self.session.query.side_effect = None
        self.session.query.return_value.filter.return_value.first.return_value = None
        self.assertIsNone(self.lookup(USER_ONE.hex, raise_on_error=True))


if __name__ == "__main__":
    unittest.main()
