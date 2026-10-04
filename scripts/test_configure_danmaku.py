"""Offline configuration repair tests with fake credentials and loopback HTTP."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("configure_danmaku_fixture", ROOT / "scripts/configure_danmaku.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


class ConfigureDanmakuTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="danmaku-config-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "danmu.env"
        self.destination = self.directory / ".env"
        self.token = "fixture-only-token-" + "x" * 40
        self.original = b"MYSQL_PASSWORD=keep-this-fixture\r\n# unrelated setting\r\nOTHER_KEY=unchanged"
        self.source.write_text("TOKEN=" + self.token + "\n", encoding="utf-8")
        self.destination.write_bytes(self.original)
        self.requests = []
        self.status = 200
        self.response = {"available": False, "comments": []}
        self.before_reply = None
        owner = self

        class Adapter(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                owner.requests.append((self.path, dict(self.headers), json.loads(body)))
                authorized = self.headers.get("Authorization") == "Bearer " + owner.token
                status = owner.status if authorized else 401
                data = owner.response if authorized else {"message": owner.token}
                raw = data if isinstance(data, bytes) else json.dumps(data).encode()
                if owner.before_reply:
                    owner.before_reply()
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "/must-not-follow")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                owner.requests.append((self.path, {}, {}))
                self.send_response(500)
                self.end_headers()

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Adapter)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def apply(self):
        return configure.configure(self.source, self.destination, self.server.server_port)

    def assert_unchanged(self):
        self.assertEqual(self.destination.read_bytes(), self.original)
        self.assertFalse((self.directory / "db_backup").exists())
        self.assertEqual(list(self.directory.glob(".danmaku-env-*")), [])

    def test_accepted_token_updates_only_two_keys_and_preserves_exact_backup(self):
        self.original = (b"# unchanged\r\nMYSQL_PASSWORD=fixture#literal\r\n"
                         b"TGBOT_DANMU_API_TOKEN=old-one\r\n export TGBOT_DANMU_API_URL=http://old\r\n"
                         b"TGBOT_DANMU_API_TOKEN = old-two\r\n# TGBOT_DANMU_API_URL=keep-comment\r\n"
                         b"LAST_KEY=keep-no-final-newline")
        self.destination.write_bytes(self.original)
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1", "no_proxy": "", "NO_PROXY": ""}):
            backup = self.apply()
        expected = (b"# unchanged\r\nMYSQL_PASSWORD=fixture#literal\r\n# TGBOT_DANMU_API_URL=keep-comment\r\n"
                    b"LAST_KEY=keep-no-final-newline\n" +
                    f"TGBOT_DANMU_API_URL=http://127.0.0.1:{self.server.server_port}\nTGBOT_DANMU_API_TOKEN={self.token}\n".encode())
        self.assertEqual(self.destination.read_bytes(), expected)
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(backup.parent.parent, self.directory / "db_backup")
        self.assertTrue(backup.parent.name.startswith("danmaku-env-"))
        self.assertEqual(backup.name, ".env")
        self.assertEqual(list(self.directory.glob(".danmaku-env-*")), [])
        self.assertEqual(len(self.requests), 1)
        path, headers, body = self.requests[0]
        self.assertEqual(path, "/api/v1/dushengtv/danmaku")
        self.assertEqual(headers["Authorization"], "Bearer " + self.token)
        self.assertEqual((body["type"], body["season"], body["episode"]), ("Episode", 0, 0))
        if os.name != "nt":
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.destination.stat().st_mode & 0o777, 0o600)

    def test_rejected_token_does_not_modify_or_backup_settings_or_print_secret(self):
        wrong = "different-fixture-token-" + "z" * 40
        self.source.write_text("TOKEN=" + wrong + "\n", encoding="utf-8")
        arguments = ["configure_danmaku.py", "--danmu-env", str(self.source), "--bot-env", str(self.destination), "--port", str(self.server.server_port)]
        output = io.StringIO()
        with patch("sys.argv", arguments), contextlib.redirect_stdout(output):
            status = configure.main()
        self.assertEqual(status, 1)
        self.assertIn("rejected", output.getvalue())
        self.assertNotIn(self.token, output.getvalue())
        self.assertNotIn(wrong, output.getvalue())
        self.assertEqual(len(self.requests), 1)
        self.assert_unchanged()

    def test_redirect_and_invalid_probe_responses_never_modify_files(self):
        responses = [(302, {"message": self.token}), (502, {"message": self.token}),
                     (200, {"available": True, "comments": []}), (200, {"available": False, "comments": {}}),
                     (200, b"invalid json"), (200, b" " * 65537)]
        for status, body in responses:
            with self.subTest(status=status, body_type=type(body).__name__):
                self.status, self.response = status, body
                before = len(self.requests)
                with self.assertRaises(ValueError) as caught:
                    self.apply()
                self.assertNotIn(self.token, str(caught.exception))
                self.assertEqual(len(self.requests), before + 1)
                self.assert_unchanged()

    def test_source_token_handles_bom_quotes_export_and_trailing_comments(self):
        for value in (self.token, f"'{self.token}' # operator note", f'"{self.token}" # operator note'):
            with self.subTest(value_kind=value[:1]):
                self.source.write_text("\ufeff# source fixture\n export TOKEN = " + value + "\nOTHER_KEY=unused\n", encoding="utf-8")
                self.assertEqual(configure.read_token(self.source), self.token)
        self.assertEqual(self.requests, [])

    def test_missing_duplicate_or_invalid_source_token_is_rejected_before_http(self):
        for text in ("OTHER_KEY=value\n", f"TOKEN={self.token}\nTOKEN={self.token}\n", "TOKEN='unterminated\n", "TOKEN=short\n", f"TOKEN={self.token} extra\n"):
            with self.subTest(text_kind=text[:15]):
                self.source.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.apply()
                self.assert_unchanged()
        self.assertEqual(self.requests, [])

    def test_destination_symlink_is_rejected_before_http(self):
        target = self.directory / "untouched.env"
        self.destination.replace(target)
        try:
            self.destination.symlink_to(target)
        except OSError as error:
            self.skipTest("File symlinks are unavailable: " + str(error))
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.apply()
        self.assertEqual(target.read_bytes(), self.original)
        self.assertTrue(self.destination.is_symlink())
        self.assertEqual(self.requests, [])
        self.assertFalse((self.directory / "db_backup").exists())

    def test_backup_directory_symlink_is_rejected_without_changing_destination(self):
        target = self.directory / "other-backups"
        target.mkdir()
        try:
            (self.directory / "db_backup").symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest("Directory symlinks are unavailable: " + str(error))
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.apply()
        self.assertEqual(self.destination.read_bytes(), self.original)
        self.assertEqual(list(target.iterdir()), [])

    def test_concurrent_destination_change_is_preserved(self):
        concurrent = b"OTHER_KEY=edited-during-verification\n"
        self.before_reply = lambda: self.destination.write_bytes(concurrent)
        with self.assertRaisesRegex(ValueError, "changed during validation"):
            self.apply()
        self.assertEqual(self.destination.read_bytes(), concurrent)
        backups = list((self.directory / "db_backup").glob("danmaku-env-*/.env"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertEqual(list(self.directory.glob(".danmaku-env-*")), [])

    def test_atomic_replace_failure_preserves_original_and_cleans_temporary_file(self):
        with patch.object(configure.os, "replace", side_effect=OSError("fixture replace failure")):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(self.destination.read_bytes(), self.original)
        backups = list((self.directory / "db_backup").glob("danmaku-env-*/.env"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertEqual(list(self.directory.glob(".danmaku-env-*")), [])


if __name__ == "__main__":
    unittest.main()
