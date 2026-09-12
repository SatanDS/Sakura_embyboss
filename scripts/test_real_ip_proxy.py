"""Opt-in real Caddy tests for dynamic client IPs and unchanged media forwarding."""

import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
CADDY_BIN = os.getenv('CADDY_BIN')
TOKEN = 'b' * 64
spec = importlib.util.spec_from_file_location('proxy_ip_fixture', ROOT / 'bot/func_helper/proxy_ip.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


@unittest.skipUnless(CADDY_BIN, 'Set CADDY_BIN for loopback Caddy real-IP integration')
class RealIPProxyTests(unittest.TestCase):
    def setUp(self):
        self.trusted = []
        self.auth_requests = []
        self.emby_requests = []
        self.resolver_down = False
        test = self

        class AuthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                headers = {key.lower(): value for key, value in self.headers.items()}
                test.auth_requests.append((self.path, headers))
                status, address = 403, None
                if headers.get('x-dusheng-line-token') == TOKEN:
                    if self.path == '/emby/real_ip':
                        if test.resolver_down:
                            status = 503
                        else:
                            status = 200
                            address = helper.resolve_client_ip(headers['x-proxy-peer-ip'],
                                        headers.get('x-proxy-forwarded-for', ''), test.trusted)
                    elif self.path.startswith('/emby/line_report?'):
                        status = 200 if headers.get('x-emby-token') == 'vip-token' else 403
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                if address:
                    self.send_header('X-Verified-Client-IP', address)
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *args):
                pass

        class EmbyHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.reply()

            def do_POST(self):
                self.reply()

            def reply(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                headers = {key.lower(): value for key, value in self.headers.items()}
                record = {'method': self.command, 'path': self.path, 'headers': headers,
                          'body': body.decode('utf-8')}
                test.emby_requests.append(record)
                self.send_response(206 if headers.get('range') else 200)
                self.send_header('Content-Type', 'application/json')
                if headers.get('range'):
                    self.send_header('Content-Range', 'bytes 0-3/8')
                self.end_headers()
                self.wfile.write(json.dumps(record).encode())

            def log_message(self, *args):
                pass

        def start_server(handler):
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            return server

        self.auth = start_server(AuthHandler)
        self.emby = start_server(EmbyHandler)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.directory = tempfile.TemporaryDirectory(prefix='tgbot-realip-')
        self.addCleanup(self.directory.cleanup)
        template = (ROOT / 'caddy/caddyfile').read_text(encoding='utf-8')
        template = template.replace('auto_https off', 'auto_https off\n\tadmin off')
        template = template.replace(
            'import emby_local_config localhost vipline 8080 127.0.0.1:8096 127.0.0.1:8838',
            f'import emby_local_config localhost vipline {self.port} 127.0.0.1:{self.emby.server_port} 127.0.0.1:{self.auth.server_port}')
        config = Path(self.directory.name) / 'Caddyfile'
        config.write_text(template, encoding='utf-8')
        self.process = subprocess.Popen([CADDY_BIN, 'run', '--config', str(config), '--adapter', 'caddyfile'],
                cwd=self.directory.name, env=dict(os.environ, DUSHENGCDN_ORIGIN_TOKEN=TOKEN),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_caddy)
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=0.1):
                    break
            except OSError:
                if self.process.poll() is not None or time.monotonic() >= deadline:
                    self.fail('Caddy real-IP fixture failed to start')
                time.sleep(0.05)

    def stop_caddy(self):
        self.process.terminate()
        try:
            self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate(timeout=5)

    def request(self, path='/System/Info/Public', *, headers=None, data=None):
        values = {'Host': 'localhost', 'X-DuSheng-Origin-Token': TOKEN, **(headers or {})}
        req = Request(f'http://127.0.0.1:{self.port}{path}', headers=values, data=data)
        with urlopen(req, timeout=5) as response:
            return response.status, json.load(response), response.headers

    def test_trust_updates_take_effect_without_gateway_restart(self):
        headers = {'X-Forwarded-For': '192.0.2.99, 198.51.100.42',
                   'X-Real-IP': '192.0.2.99', 'Forwarded': 'for=192.0.2.99',
                   'X-Verified-Client-IP': '192.0.2.99', 'X-Proxy-Peer-IP': '192.0.2.99',
                   'X-Proxy-Forwarded-For': '192.0.2.99'}
        for trusted, expected in (([], '127.0.0.1'), (['127.0.0.1/32'], '198.51.100.42'), ([], '127.0.0.1')):
            self.trusted = trusted
            status, record, _ = self.request(headers=headers)
            self.assertEqual(status, 200)
            self.assertEqual(record['headers']['x-forwarded-for'], expected)
            self.assertEqual(record['headers']['x-real-ip'], expected)
            for name in ('forwarded', 'x-proxy-peer-ip', 'x-proxy-forwarded-for',
                         'x-verified-client-ip', 'x-dusheng-origin-token', 'x-dusheng-line-token'):
                self.assertNotIn(name, record['headers'])
            self.assertEqual(self.auth_requests[-1][1]['x-proxy-peer-ip'], '127.0.0.1')
        self.trusted = ['127.0.0.1/32']
        _, record, _ = self.request(headers={'X-Forwarded-For': '192.0.2.99, 2001:db8::42'})
        self.assertEqual(record['headers']['x-real-ip'], '2001:db8::42')

    def test_post_body_query_token_and_media_range_are_preserved(self):
        self.trusted = ['127.0.0.1/32']
        headers = {'X-Forwarded-For': '198.51.100.42', 'X-Emby-Token': 'vip-token',
                   'Authorization': 'Emby token-fixture', 'Content-Type': 'application/json'}
        path = '/Users/AuthenticateByName?fixture=login'
        body = b'{"Username":"fixture","Pw":"fixture-password"}'
        _, record, _ = self.request(path, headers=headers, data=body)
        self.assertEqual((record['method'], record['path'], record['body']), ('POST', path, body.decode()))
        self.assertEqual(record['headers']['authorization'], headers['Authorization'])
        for path in ('/emby/Videos/1/stream.mp4', '/Videos/1/stream.mp4',
                     '/emby/Audio/1/stream.mp3', '/Audio/1/hls/segment.ts',
                     '/emby/Items/1/PlaybackInfo', '/Items/1/Download',
                     '/emby/Sessions/Playing/Progress', '/Sessions/Playing/Progress'):
            status, record, response_headers = self.request(path, headers={**headers, 'Range': 'bytes=0-3'})
            self.assertEqual(status, 206)
            self.assertEqual(response_headers['Content-Range'], 'bytes 0-3/8')
            self.assertEqual(record['headers']['range'], 'bytes=0-3')
            self.assertEqual(record['headers']['x-real-ip'], '198.51.100.42')
            self.assertEqual(record['headers']['x-emby-token'], 'vip-token')
            auth_path, auth_headers = self.auth_requests[-1]
            self.assertTrue(auth_path.startswith('/emby/line_report?'))
            self.assertNotIn('x-forwarded-for', auth_headers)
            self.assertNotIn('x-real-ip', auth_headers)
            self.assertEqual(auth_headers['x-dusheng-line-token'], TOKEN)

    def test_untrusted_headers_cannot_bypass_origin_or_vip_checks(self):
        for headers in ({'X-DuSheng-Origin-Token': 'wrong'},
                        {'X-Verified-Client-IP': '127.0.0.1', 'X-Emby-Token': 'not-vip'}):
            with self.assertRaises(HTTPError) as error:
                self.request('/Videos/1/stream.mp4', headers=headers)
            self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.emby_requests, [])

    def test_resolver_failure_cannot_forward_forged_ip_and_direct_upstream_is_unchanged(self):
        self.resolver_down = True
        with self.assertRaises(HTTPError) as error:
            self.request(headers={'X-Verified-Client-IP': '192.0.2.99'})
        self.assertEqual(error.exception.code, 503)
        self.assertEqual(self.emby_requests, [])
        count = len(self.auth_requests)
        with urlopen(f'http://127.0.0.1:{self.emby.server_port}/System/Info/Public', timeout=5) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.auth_requests), count)


if __name__ == '__main__':
    unittest.main(verbosity=2)
