"""HLS continuity authorizes only previously verified VIP manifests."""
import importlib.util
from pathlib import Path
import sys
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock

from scripts.test_vip_identity import CLIENT_TOKEN, USER_ONE, USER_TWO, load_identity_module


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('_hls_access_test', ROOT / 'bot/func_helper/hls_access.py')
hls = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hls
spec.loader.exec_module(hls)
HOST = 'vip.example.test'
SESSION = '1' * 32
MANIFEST = '/emby/Videos/42/master.m3u8?PlaySessionId=' + SESSION + '&api_key=' + CLIENT_TOKEN
SEGMENT = '/Videos/42/hls1/main/0.ts?PlaySessionId=' + SESSION


class HLSAccessTests(unittest.TestCase):
    def setUp(self):
        self.access = hls.HLSAccess()

    def test_unknown_sessions_and_non_segment_paths_are_rejected(self):
        self.assertIsNone(self.access.lookup(HOST, SEGMENT))
        self.access.register(HOST, MANIFEST, USER_ONE.hex, CLIENT_TOKEN)
        for uri in (MANIFEST, SEGMENT.replace('/hls1/main/0.ts','/stream.mp4'),
                    SEGMENT.replace('/42/','/43/'), SEGMENT.replace(SESSION,'2'*32),
                    SEGMENT + '&playsessionid=' + SESSION):
            self.assertIsNone(self.access.lookup(HOST, uri))
        self.assertIsNone(self.access.lookup('other.example.test', SEGMENT))

    def test_both_prefixes_share_only_the_verified_media_binding(self):
        self.access.register(HOST, MANIFEST, USER_ONE.hex, CLIENT_TOKEN)
        for uri in (SEGMENT, '/emby' + SEGMENT, SEGMENT.replace('Videos','videos')):
            binding = self.access.lookup(HOST, uri)
            self.assertEqual(binding.user_id, USER_ONE.hex)
            self.assertEqual(binding.token, CLIENT_TOKEN)
            self.assertNotIn(CLIENT_TOKEN, repr(binding))

    def test_sessions_cannot_be_rebound_to_another_user(self):
        self.assertTrue(self.access.register(HOST, MANIFEST, USER_ONE.hex, CLIENT_TOKEN))
        self.assertFalse(self.access.register(HOST, MANIFEST, USER_TWO.hex, 'other-token'))
        self.assertEqual(self.access.lookup(HOST, SEGMENT).user_id, USER_ONE.hex)

    def test_short_session_ids_do_not_create_bindings(self):
        self.access.register(HOST, MANIFEST.replace(SESSION,'guessable'), USER_ONE.hex, CLIENT_TOKEN)
        self.assertIsNone(self.access.lookup(HOST, SEGMENT.replace(SESSION,'guessable')))

    def test_explicit_bad_credentials_cannot_use_session_fallback(self):
        for token, header, uri in (('bad','',SEGMENT), ('','Emby Token="bad"',SEGMENT),
                                  ('','Bearer bad',SEGMENT),
                                  ('','',SEGMENT+'&api_key='), ('','',SEGMENT+'&token=bad')):
            self.assertTrue(hls.has_explicit_credential(token, header, uri))
        self.assertFalse(hls.has_explicit_credential('', 'Emby Device="Player"', SEGMENT))

    def test_inactive_bindings_expire_and_active_bindings_refresh(self):
        clock = [0]
        self.access.cache.configure(timer=lambda: clock[0])
        self.access.register(HOST, MANIFEST, USER_ONE.hex, CLIENT_TOKEN)
        clock[0] = 3500
        binding = self.access.lookup(HOST, SEGMENT)
        self.access.refresh(HOST, SEGMENT, binding)
        clock[0] = 3601
        self.assertIsNotNone(self.access.lookup(HOST, SEGMENT))
        clock[0] = 7200
        self.assertIsNone(self.access.lookup(HOST, SEGMENT))


class HLSLineAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='.hls-access-test-', dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.auth_db = Path(temporary.name) / 'authentication.db'
        with sqlite3.connect(self.auth_db) as connection:
            connection.execute('CREATE TABLE Tokens (AccessToken TEXT, UserId TEXT, IsActive INTEGER)')
            connection.execute('INSERT INTO Tokens VALUES (?, ?, 1)', (CLIENT_TOKEN, USER_ONE.hex))
        config = SimpleNamespace(emby_auth_db_path=str(self.auth_db))
        self.identity = load_identity_module(config, SimpleNamespace(_request=AsyncMock()))
        self.identity.classify_line_request = Mock(return_value='vip')
        self.identity._fetch_active_sessions_result = AsyncMock(return_value=(True, [], ''))
        self.identity.sql_get_emby_by_embyid = Mock(return_value=type('User', (), {
            'tg': 1, 'lv':'a', 'ex':datetime.now()+timedelta(days=1)})())
        self.identity.handle_line_violation = AsyncMock()

    async def report(self, uri, token=''):
        return await self.identity.line_report(line='vip',host=HOST,token=token,
            x_emby_authorization=None,authorization=None,x_emby_token=None,x_original_uri=uri)

    async def test_valid_manifest_allows_tokenless_segments_after_revalidation(self):
        self.assertEqual((await self.report(MANIFEST))['status'], 'allowed')
        self.assertEqual((await self.report(SEGMENT))['status'], 'allowed')
        self.assertEqual(self.identity.sql_get_emby_by_embyid.call_count, 2)
        self.identity.handle_line_violation.assert_not_awaited()

    async def test_revoked_token_or_expired_vip_is_not_allowed_or_punished(self):
        await self.report(MANIFEST)
        with sqlite3.connect(self.auth_db) as connection:
            connection.execute('UPDATE Tokens SET IsActive=0')
        result = await self.report(SEGMENT)
        self.assertEqual(result.status_code,403)
        self.identity.handle_line_violation.assert_not_awaited()
        with sqlite3.connect(self.auth_db) as connection:
            connection.execute('UPDATE Tokens SET IsActive=1')
        await self.report(MANIFEST)
        self.identity.sql_get_emby_by_embyid.return_value.ex = datetime.now()-timedelta(seconds=1)
        self.assertEqual((await self.report(SEGMENT)).status_code,403)
        self.identity.handle_line_violation.assert_not_awaited()

    async def test_database_outage_does_not_punish_or_destroy_recoverable_binding(self):
        await self.report(MANIFEST)
        self.identity.sql_get_emby_by_embyid.side_effect = RuntimeError('database unavailable')
        self.assertEqual((await self.report(SEGMENT)).status_code,503)
        self.identity.handle_line_violation.assert_not_awaited()
        self.identity.sql_get_emby_by_embyid.side_effect = None
        self.assertEqual((await self.report(SEGMENT))['status'],'allowed')

    async def test_invalid_explicit_token_does_not_reuse_valid_manifest(self):
        await self.report(MANIFEST)
        self.assertEqual((await self.report(SEGMENT, token='bad-token')).status_code,403)
        self.identity.handle_line_violation.assert_not_awaited()

    def test_play_session_is_redacted_from_logs(self):
        self.assertNotIn(SESSION,self.identity.redact_request_uri(SEGMENT))


if __name__ == '__main__':
    unittest.main(verbosity=2)
