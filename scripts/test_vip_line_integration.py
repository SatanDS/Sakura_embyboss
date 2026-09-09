"""Opt-in checks through the disposable Caddy -> Bot -> Emby gateway."""
import json
import os
from pathlib import Path
import unittest
from urllib.parse import urlencode

import requests


@unittest.skipUnless(os.environ.get('TGBOT_VIP_TEST') == '1', 'Set TGBOT_VIP_TEST=1 for the disposable gateway matrix')
class VIPLineIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(Path(os.environ['TGBOT_VIP_FIXTURES']).read_text())
        if cls.fixtures['gateway_base'] != 'http://127.0.0.1:18080':
            raise ValueError('This suite requires the disposable loopback gateway')
        cls.http = requests.Session()
        cls.http.trust_env = False
        cls.addClassCleanup(cls.http.close)

    def request_media(self, role='vip', line='vip', spelling='X-Emby-Token', path=None, extra=None):
        user = self.fixtures['users'][role]
        headers = {'Host': line + '.tgbot.test', 'X-DuSheng-Origin-Token': self.fixtures['origin_token']}
        query = {'static':'true'}
        if spelling in ('Authorization','X-Emby-Authorization'):
            headers[spelling] = 'Emby Token="' + user['token'] + '", UserId="' + user['id'] + '"'
        elif spelling in ('api_key','token','query-token'):
            query['X-Emby-Token' if spelling=='query-token' else spelling] = user['token']
        elif spelling in ('query-authorization','query-x-authorization'):
            query['Authorization' if spelling=='query-authorization' else 'X-Emby-Authorization'] = 'Emby Token="' + user['token'] + '"'
        else:
            headers['X-Emby-Token'] = user['token']
        if extra:
            query.update(extra)
        path = path or '/emby/Videos/' + self.fixtures['item_id'] + '/stream.mp4'
        return self.http.get(self.fixtures['gateway_base'] + path, headers=headers, params=query, timeout=20, allow_redirects=False)

    def test_valid_vip_supported_credentials_are_not_blocked(self):
        for spelling in ('X-Emby-Token','X-Emby-Authorization','Authorization','api_key','query-token'):
            with self.subTest(credential=spelling):
                response=self.request_media(spelling=spelling)
                self.assertEqual(response.status_code,200,'Valid VIP credential was blocked: '+spelling)
                self.assertIn(b'ftyp',response.content[:32])

    def test_hills_stale_user_id_does_not_block_valid_vip(self):
        for spelling in ('Authorization','X-Emby-Authorization','api_key'):
            with self.subTest(credential=spelling):
                response=self.request_media(spelling=spelling,extra={'UserId':self.fixtures['users']['normal']['id']})
                self.assertEqual(response.status_code,200,'A stale client UserId blocked the validated VIP token')
                self.assertIn(b'ftyp',response.content[:32])

    def test_hills_url_metadata_authenticates_without_trusting_stale_id(self):
        user=self.fixtures['users']['vip']
        for field in ('Authorization','X-Emby-Authorization'):
            query={field:'Emby Token="'+user['token']+'"','UserId':self.fixtures['users']['normal']['id']}
            response=self.http.get(self.fixtures['api_base']+'/emby/line_report',headers={
                'X-DuSheng-Line-Token':self.fixtures['origin_token'],
                'X-Original-URI':'/emby/Videos/'+self.fixtures['item_id']+'/stream.mp4?'+urlencode(query),
            },params={'line':'vip','host':'vip.tgbot.test:18080'},timeout=20)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['status'],'allowed')

    def test_invalid_token_does_not_use_forged_vip_id(self):
        response=self.http.get(self.fixtures['gateway_base']+'/emby/Videos/'+self.fixtures['item_id']+'/stream.mp4',
            headers={'Host':'vip.tgbot.test','X-DuSheng-Origin-Token':self.fixtures['origin_token'],'X-Emby-Token':'invalid-token'},
            params={'static':'true','userId':self.fixtures['users']['vip']['id']},timeout=20)
        self.assertIn(response.status_code,(401,403))

    def test_conflicting_credentials_are_rejected(self):
        response=self.request_media(spelling='X-Emby-Token',extra={'token':self.fixtures['users']['normal']['token']})
        self.assertIn(response.status_code,(401,403))

    def test_range_request_returns_only_authorized_media(self):
        path='/emby/Videos/'+self.fixtures['item_id']+'/stream.mp4'
        for role in ('vip','normal'):
            response=self.http.get(self.fixtures['gateway_base']+path,headers={
                'Host':'vip.tgbot.test','X-DuSheng-Origin-Token':self.fixtures['origin_token'],
                'X-Emby-Token':self.fixtures['users'][role]['token'],'Range':'bytes=0-31'},params={'static':'true'},timeout=20)
            if role=='vip':
                self.assertEqual(response.status_code,206)
                self.assertEqual(len(response.content),32)
                self.assertIn(b'ftyp',response.content)
            else:
                self.assertEqual(response.status_code,403)

    def test_client_user_agent_does_not_change_entitlement(self):
        path='/emby/Videos/'+self.fixtures['item_id']+'/stream.mp4'
        for agent in ('Emby/3.4 Android', 'Emby Theater', 'Infuse/8', 'SenPlayer', 'Hills', 'Jellyfin/10', 'VLC/3', 'Mozilla/5.0'):
            with self.subTest(user_agent=agent):
                response=self.http.get(self.fixtures['gateway_base']+path,headers={
                    'Host':'vip.tgbot.test','X-DuSheng-Origin-Token':self.fixtures['origin_token'],
                    'X-Emby-Token':self.fixtures['users']['vip']['token'],'User-Agent':agent},
                    params={'static':'true'},timeout=20)
                self.assertEqual(response.status_code,200)

    def test_hls_and_playback_info_normal_users_are_blocked(self):
        item=self.fixtures['item_id']
        for path in ('/emby/Videos/'+item+'/master.m3u8','/Videos/'+item+'/hls1/main/0.ts',
                     '/emby/Audio/'+item+'/universal','/emby/Items/'+item+'/PlaybackInfo',
                     '/LiveTv/LiveRecordings/'+item+'/hls/0.ts'):
            with self.subTest(path=path):
                self.assertEqual(self.request_media(role='normal',path=path).status_code,403)

    def test_normal_user_is_allowed_on_normal_line(self):
        response=self.request_media(role='normal',line='normal')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'ftyp',response.content[:32])

    def test_non_vip_expired_and_disabled_never_receive_vip_media(self):
        for role in ('normal','expired','no_expiry','db_disabled','remote_disabled'):
            with self.subTest(role=role):
                response=self.request_media(role=role)
                self.assertIn(response.status_code,(401,403))
                self.assertNotIn(b'ftyp',response.content[:32])

    def test_cooldown_requests_stay_blocked(self):
        for _ in range(3):
            self.assertEqual(self.request_media(role='normal').status_code,403)

    def test_forged_vip_id_does_not_grant_access(self):
        response=self.request_media(role='normal',extra={'userId':self.fixtures['users']['vip']['id']})
        self.assertEqual(response.status_code,403)

    def test_route_variants_cannot_bypass_vip_gate(self):
        item=self.fixtures['item_id']
        for path in ('/Videos/'+item+'/stream.mp4','/emby/videos/'+item+'/stream.mp4',
                     '/emby/Videos/'+item+'/stream','/emby/Items/'+item+'/Download',
                     '/emby/Videos/'+item+'/original.mp4'):
            with self.subTest(path=path):
                response=self.request_media(role='normal',path=path)
                self.assertIn(response.status_code,(401,403,404), 'Normal user reached a VIP media route')
                self.assertNotIn(b'ftyp',response.content[:32])

    def test_missing_origin_secret_cannot_reach_media(self):
        user=self.fixtures['users']['vip']
        response=self.http.get(self.fixtures['gateway_base']+'/emby/Videos/'+self.fixtures['item_id']+'/stream.mp4',
                               headers={'Host':'vip.tgbot.test','X-Emby-Token':user['token']},params={'static':'true'},timeout=20)
        self.assertEqual(response.status_code,403)


if __name__ == '__main__':
    unittest.main(verbosity=2)
