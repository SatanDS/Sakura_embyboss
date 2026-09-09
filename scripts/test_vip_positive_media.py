"""Opt-in source/gateway media comparisons on the disposable VIP test stack.

Requires the prepared positive media fields in TGBOT_VIP_FIXTURES and the
test-only m3u8 dependency from requirements-test.txt. Never prints credentials.
"""
import hashlib
import json
import os
from pathlib import Path
import unittest
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import requests


ENABLED = os.environ.get('TGBOT_VIP_TEST') == '1'


@unittest.skipUnless(ENABLED, 'Set TGBOT_VIP_TEST=1 with the prepared positive media fixtures')
class VIPPositiveMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(Path(os.environ['TGBOT_VIP_FIXTURES']).read_text())
        if (cls.fixtures['gateway_base'] != 'http://127.0.0.1:18080'
                or cls.fixtures['emby_base'] != 'http://127.0.0.1:18096'
                or cls.fixtures['api_base'] != 'http://127.0.0.1:18838'):
            raise ValueError('Positive media tests require the disposable loopback stack')
        cls.http = requests.Session()
        cls.http.trust_env = False
        cls.addClassCleanup(cls.http.close)
        cls.video = cls.fixtures['positive_video']
        cls.audio = cls.fixtures['positive_audio']
        cls.source = cls.fixtures['positive_source']
        cls.subtitle = cls.fixtures['positive_subtitle']

    def setUp(self):
        self.account_before = self.account_state()

    def tearDown(self):
        self.assertEqual(self.account_state(), self.account_before,
                         'Positive media requests changed VIP entitlement or disabled the account')

    def account_state(self):
        user = self.fixtures['users']['vip']
        response = self.http.get(self.fixtures['api_base'] + '/user/user_info',
                                 headers={'X-API-Key': self.fixtures['management_key']},
                                 params={'tg': user['tg']}, timeout=20)
        self.assertEqual(response.status_code, 200, 'Cannot inspect test VIP entitlement')
        data = response.json()['data']
        response = self.http.get(self.fixtures['emby_base'] + '/emby/Users/' + user['id'],
                                 headers={'X-Emby-Token': user['token']}, timeout=20)
        self.assertEqual(response.status_code, 200, 'Cannot inspect test VIP account')
        disabled = response.json()['Policy']['IsDisabled']
        self.assertEqual(data['lv'], 'a')
        self.assertFalse(disabled)
        return data['lv'], data['ex'], disabled

    def fetch(self, path, *, gateway=False, prefix=True, params=None, headers=None, role='vip'):
        base = self.fixtures['gateway_base' if gateway else 'emby_base']
        actual_path = ('/emby' if prefix else '') + path
        request_headers = {'X-Emby-Token': self.fixtures['users'][role]['token']}
        if gateway:
            request_headers.update(Host='vip.tgbot.test', **{
                'X-DuSheng-Origin-Token': self.fixtures['origin_token']})
        request_headers.update(headers or {})
        return self.http.get(base + actual_path, params=params, headers=request_headers,
                             timeout=40, allow_redirects=False)

    def assert_bytes_match(self, path, content_type, marker, *, params=None, headers=None, status=200):
        baseline = self.fetch(path, params=params, headers=headers)
        self.assertEqual(baseline.status_code, status, 'Source failed for ' + path)
        self.assertIn(content_type, baseline.headers.get('Content-Type', ''))
        self.assertGreater(len(baseline.content), 0)
        self.assertIn(marker, baseline.content)
        digest = hashlib.sha256(baseline.content).digest()
        for gateway, prefix in ((False, False), (True, True), (True, False)):
            with self.subTest(path=path, gateway=gateway, prefix=prefix):
                response = self.fetch(path, gateway=gateway, prefix=prefix, params=params, headers=headers)
                self.assertEqual(response.status_code, status, 'Media comparison failed for ' + path)
                self.assertEqual(response.headers.get('Content-Type'), baseline.headers.get('Content-Type'))
                self.assertEqual(len(response.content), len(baseline.content))
                self.assertEqual(hashlib.sha256(response.content).digest(), digest)
                if status == 206:
                    self.assertEqual(response.headers.get('Content-Range'), baseline.headers.get('Content-Range'))

    def test_video_and_audio_match_source_with_both_prefixes(self):
        self.assert_bytes_match('/Videos/' + self.video + '/stream.mp4', 'video/mp4', b'ftyp', params={'Static':'true'})
        self.assert_bytes_match('/Audio/' + self.audio + '/stream.mp3', 'audio/mpeg', b'ID3', params={'Static':'true'})

    def test_range_start_middle_and_suffix_match_source(self):
        path = '/Videos/' + self.video + '/stream.mp4'
        for value in ('bytes=0-31', 'bytes=1024-2047', 'bytes=-128'):
            with self.subTest(byte_range=value):
                self.assert_bytes_match(path, 'video/mp4', b'', params={'Static':'true'},
                                        headers={'Range':value}, status=206)

    def test_cover_and_external_subtitles_match_source(self):
        self.assert_bytes_match('/Items/' + self.video + '/Images/Primary', 'image/jpeg', b'\xff\xd8')
        suffix = '/' + self.source + '/Subtitles/' + str(self.subtitle)
        self.assert_bytes_match('/Videos/' + self.video + suffix + '/Stream.vtt', 'text/vtt', b'WEBVTT')
        self.assert_bytes_match('/Items/' + self.video + suffix + '/Stream.srt', 'text/plain', b'Positive validation')

    def test_metadata_and_playback_info_match_source(self):
        paths = ['/Users/' + self.fixtures['users']['vip']['id'] + '/Items/' + self.video,
                 '/Items/' + self.video + '/PlaybackInfo']
        for path in paths:
            baseline = self.fetch(path)
            self.assertEqual(baseline.status_code, 200)
            expected = baseline.json()
            self.assertTrue(expected.get('MediaSources'))
            for gateway, prefix in ((False,False),(True,True),(True,False)):
                response = self.fetch(path, gateway=gateway, prefix=prefix)
                self.assertEqual(response.status_code, 200)
                self.assertEqual([item['Id'] for item in response.json()['MediaSources']],
                                 [item['Id'] for item in expected['MediaSources']])

    def walk_hls(self, *, gateway, prefix):
        import m3u8

        query = dict(self.fixtures['hls_query'], api_key=self.fixtures['users']['vip']['token'])
        base = self.fixtures['gateway_base' if gateway else 'emby_base']
        root = base + ('/emby' if prefix else '') + '/Videos/' + self.video + '/master.m3u8?' + urlencode(query)
        pending = [root]
        visited = set()
        segment_digests = []
        while pending:
            url = pending.pop(0)
            parsed = urlsplit(url)
            if parsed.hostname != '127.0.0.1' or parsed.port not in (18080,18096):
                self.fail('Fixture HLS referenced an unexpected origin')
            path = parsed.path.removeprefix('/emby')
            url = base + ('/emby' if prefix else '') + path + ('?' + parsed.query if parsed.query else '')
            if url in visited:
                continue
            visited.add(url)
            self.assertLessEqual(len(visited), 40, 'Fixture HLS recursion was unexpectedly large')
            # URL-token mode deliberately does not add an authorization header.
            headers = {'Host':'vip.tgbot.test','X-DuSheng-Origin-Token':self.fixtures['origin_token']} if gateway else {}
            response = self.http.get(url, headers=headers, timeout=40, allow_redirects=False)
            self.assertEqual(response.status_code, 200, 'HLS resource failed: ' + path)
            if '.m3u8' in path:
                self.assertTrue(response.content.startswith(b'#EXTM3U'))
                playlist = m3u8.loads(response.text, uri=url)
                pending.extend(item.absolute_uri for item in playlist.playlists)
                pending.extend(item.absolute_uri for item in playlist.media if item.uri)
                pending.extend(item.absolute_uri for item in playlist.segments)
                for segment in playlist.segments:
                    if segment.init_section:
                        pending.append(segment.init_section.absolute_uri)
                pending.extend(key.absolute_uri for key in playlist.keys if key and key.uri)
            else:
                self.assertGreater(len(response.content), 188, 'HLS segment was empty')
                self.assertIn(response.headers.get('Content-Type','').split(';')[0],
                              ('video/mp2t','video/MP2T','video/mp4','audio/mp4','application/octet-stream'))
                segment_digests.append(hashlib.sha256(response.content).hexdigest())
                if gateway:
                    denied_query = dict(parse_qsl(parsed.query))
                    denied_query['api_key'] = self.fixtures['users']['normal']['token']
                    denied = self.http.get(base + ('/emby' if prefix else '') + path,
                                           params=denied_query, headers=headers, timeout=20)
                    self.assertEqual(denied.status_code, 403, 'Non-VIP fetched an actual HLS segment')
        self.assertGreaterEqual(len(segment_digests), 1, 'HLS did not return media segments')
        return sorted(segment_digests)

    def test_complete_hls_chain_matches_source_with_both_prefixes(self):
        baseline = self.walk_hls(gateway=False, prefix=True)
        for gateway, prefix in ((False,False),(True,True),(True,False)):
            with self.subTest(gateway=gateway, prefix=prefix):
                self.assertEqual(self.walk_hls(gateway=gateway,prefix=prefix), baseline)

    def test_normal_line_hls_session_cannot_be_reused_on_vip_line(self):
        import m3u8

        user = self.fixtures['users']['normal']
        profile = {'DirectPlayProfiles': [], 'TranscodingProfiles': [{
            'Container':'ts','Type':'Video','VideoCodec':'h264','AudioCodec':'aac',
            'Protocol':'hls','Context':'Streaming','MinSegments':1,
        }]}
        headers = {'X-Emby-Token':user['token'],'X-Emby-Authorization':
                   'Emby Client="Validation", DeviceId="normal-positive-hls", Device="Test", Version="1"'}
        response = self.http.post(self.fixtures['emby_base'] + '/emby/Items/' + self.video + '/PlaybackInfo',
                                  headers=headers, params={'UserId':user['id'],'IsPlayback':'true'},
                                  json={'DeviceProfile':profile,'EnableDirectPlay':False,'EnableDirectStream':False},timeout=30)
        self.assertEqual(response.status_code,200)
        url = urljoin(self.fixtures['emby_base'] + '/emby/', response.json()['MediaSources'][0]['TranscodingUrl'])
        for _ in range(3):
            parsed = urlsplit(url)
            if '.m3u8' not in parsed.path:
                break
            proxy_url = self.fixtures['gateway_base'] + parsed.path + '?' + parsed.query
            response = self.http.get(proxy_url, headers={'Host':'normal.tgbot.test',
                'X-DuSheng-Origin-Token':self.fixtures['origin_token']}, timeout=40)
            self.assertEqual(response.status_code,200)
            playlist = m3u8.loads(response.text,uri=url)
            children = playlist.playlists or playlist.segments
            self.assertTrue(children)
            url = children[0].absolute_uri
        self.assertNotIn('.m3u8',urlsplit(url).path)
        baseline = self.http.get(url,timeout=40)
        self.assertEqual(baseline.status_code,200)
        parsed = urlsplit(url)
        denied = self.http.get(self.fixtures['gateway_base'] + parsed.path + '?' + parsed.query,
                               headers={'Host':'vip.tgbot.test','X-DuSheng-Origin-Token':self.fixtures['origin_token']},timeout=20)
        self.assertIn(denied.status_code,(401,403))

    def test_unauthorized_video_audio_and_subtitle_routes_stay_blocked(self):
        suffix = '/' + self.source + '/Subtitles/' + str(self.subtitle) + '/Stream.vtt'
        for prefix in (False,True):
            for path in ('/Videos/' + self.video + '/stream.mp4', '/Audio/' + self.audio + '/stream.mp3',
                         '/Videos/' + self.video + suffix):
                response = self.fetch(path, gateway=True, prefix=prefix, role='normal', params={'Static':'true'})
                self.assertEqual(response.status_code, 403)


if __name__ == '__main__':
    unittest.main(verbosity=2)
