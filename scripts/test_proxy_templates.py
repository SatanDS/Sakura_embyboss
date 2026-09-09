#!/usr/bin/env python3
"""Optional Caddy template integration checks using only loopback test servers."""
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CADDY_BIN = os.environ.get("CADDY_BIN")
NGINX_BIN = os.environ.get("NGINX_BIN")


@unittest.skipUnless(CADDY_BIN, "Set CADDY_BIN to run the Caddy template integration test")
class CaddyTemplateTests(unittest.TestCase):
    def test_playlist_mutations_use_authenticated_internal_requests(self):
        received = []

        class BotFixture(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append({"path": self.path, "headers": dict(self.headers), "method": self.command})
                self.send_response(403 if self.path.startswith('/emby/') else 200)
                self.end_headers()
                self.wfile.write(b'{"is_baned":true}')

            def log_message(self, *args):
                pass

        backend = ThreadingHTTPServer(("127.0.0.1", 0), BotFixture)
        thread = threading.Thread(target=backend.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(backend.server_close)
        self.addCleanup(backend.shutdown)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            gateway_port = sock.getsockname()[1]
        template = (ROOT / "caddy/caddyfile").read_text(encoding="utf-8")
        template = template.replace("auto_https off", "auto_https off\n\tadmin off")
        template = template.replace(
            "import emby_local_config localhost vipline 8080 127.0.0.1:8096 127.0.0.1:8838",
            f"import emby_local_config localhost vipline {gateway_port} 127.0.0.1:{backend.server_port} 127.0.0.1:{backend.server_port}")
        environment = dict(os.environ, DUSHENGCDN_ORIGIN_TOKEN="b" * 64)
        with tempfile.TemporaryDirectory(prefix="tgbot-proxy-test-") as directory:
            config = Path(directory) / "Caddyfile"
            config.write_text(template, encoding="utf-8")
            process = subprocess.Popen([CADDY_BIN, "run", "--config", str(config), "--adapter", "caddyfile"],
                                       cwd=directory, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        with socket.create_connection(("127.0.0.1", gateway_port), timeout=0.1):
                            break
                    except OSError:
                        if process.poll() is not None or time.monotonic() >= deadline:
                            self.fail("Caddy test gateway did not start")
                        time.sleep(0.05)

                paths = [("POST", "/emby/Playlists?userId=user-1&api_key=client-token"),
                         ("POST", "/emby/Playlists/list1/Items?userId=user-1"),
                         ("DELETE", "/emby/Playlists/list1/Items/item1?userId=user-1"),
                         ("POST", "/emby/Playlists/list1/Items/item1/Move/item2?userId=user-1")]
                for method, path in paths:
                    request = Request(f"http://localhost:{gateway_port}{path}", method=method, headers={
                        "X-DuSheng-Origin-Token": "b" * 64, "X-DuSheng-Line-Token": "client-forgery",
                        "X-Original-URI": "/forged", "X-Original-Method": "GET", "X-Emby-Token": "client-token"})
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(request, timeout=5)
                    self.assertEqual(raised.exception.code, 403)
                    self.assertIsNone(raised.exception.headers.get("Location"))
                    observed = received[-1]
                    self.assertEqual(observed["path"], "/emby/ban_playlist")
                    self.assertEqual(observed["method"], "GET")
                    headers = {key.lower(): value for key, value in observed["headers"].items()}
                    self.assertEqual(headers["x-original-uri"], path)
                    self.assertEqual(headers["x-original-method"], method)
                    self.assertEqual(headers["x-dusheng-line-token"], "b" * 64)
                    self.assertEqual(headers["x-emby-token"], "client-token")
                    self.assertNotIn("x-dusheng-origin-token", headers)
                self.assertEqual(len(received), len(paths))

                media_paths = [
                    '/emby/Videos/123/stream.mp4', '/Videos/123/stream.mp4',
                    '/emby/videos/123/arbitrary.mp4', '/Audio/123/hls1/list/0.ts',
                    '/Items/123/Download', '/emby/Items/123/PlaybackInfo',
                    '/LiveTv/LiveRecordings/123/hls/0.ts', '/sessions/playing/Progress',
                ]
                for path in media_paths:
                    with self.subTest(media_path=path):
                        request = Request(f'http://localhost:{gateway_port}{path}', headers={
                            'X-DuSheng-Origin-Token': 'b' * 64,
                            'X-DuSheng-Line-Token': 'client-forgery',
                        })
                        with self.assertRaises(HTTPError) as raised:
                            urlopen(request, timeout=5)
                        self.assertEqual(raised.exception.code, 403)
                        observed = received[-1]
                        self.assertTrue(observed['path'].startswith('/emby/line_report?'))
                        headers = {key.lower(): value for key, value in observed['headers'].items()}
                        self.assertEqual(headers['x-dusheng-line-token'], 'b' * 64)
                        self.assertEqual(headers['x-original-uri'], path)
                self.assertEqual(len(received), len(paths) + len(media_paths))

                with self.assertRaises(HTTPError) as raised:
                    urlopen(Request(f"http://localhost:{gateway_port}/emby/Playlists", method="POST"), timeout=5)
                self.assertEqual(raised.exception.code, 403)
                self.assertEqual(len(received), len(paths) + len(media_paths))
            finally:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=5)


@unittest.skipUnless(NGINX_BIN, "Set NGINX_BIN to validate the Nginx template")
class NginxTemplateTests(unittest.TestCase):
    def test_template_is_valid_in_http_context(self):
        template = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix=".nginx-test-", dir=ROOT) as directory:
            root = Path(directory)
            (root / "logs").mkdir()
            (root / "temp").mkdir()
            template = template.replace("/var/log/nginx/", root.as_posix() + "/logs/")
            config = root / "nginx.conf"
            config.write_text("events {}\nhttp {\n" + template + "\n}\n", encoding="utf-8")
            result = subprocess.run([NGINX_BIN, "-t", "-p", root.as_posix() + "/", "-c", config.as_posix()],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
