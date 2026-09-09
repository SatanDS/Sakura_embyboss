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


def load_identity_module(config, emby):
    logger = types.SimpleNamespace(**{name: Mock() for name in ("error", "warning", "info", "debug")})
    modules = {}
    for name, attributes in {
        "bot": {"LOGGER": logger, "bot": Mock(), "config": config},
        "bot.func_helper": {},
        "bot.func_helper.emby": {"emby": emby},
        "bot.sql_helper": {},
        "bot.sql_helper.sql_emby": {
            "Emby": type("Emby", (), {}), "sql_get_emby_by_embyid": Mock(), "sql_update_emby": Mock()},
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

        with patch.object(self.identity.sqlite3, "connect", side_effect=guarded_connect):
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
