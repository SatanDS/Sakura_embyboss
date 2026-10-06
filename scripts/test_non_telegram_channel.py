"""Offline checks for the optional non-Telegram manual channel."""

import sys
import unittest
import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SPEC = importlib.util.spec_from_file_location(
    "non_telegram_channel_test_helper", ROOT / "bot/func_helper/non_telegram_channel.py"
)
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

(
    DEFAULT_NOTICE,
    channel_enabled,
    channel_notice,
    channel_url,
    validate_channel_notice,
    validate_channel_url,
) = (_MODULE.DEFAULT_NOTICE, _MODULE.channel_enabled, _MODULE.channel_notice,
     _MODULE.channel_url, _MODULE.validate_channel_notice, _MODULE.validate_channel_url)


class NonTelegramChannelTests(unittest.TestCase):
    def test_disabled_or_missing_config_fails_closed(self):
        self.assertFalse(channel_enabled(SimpleNamespace()))
        self.assertEqual(channel_url(SimpleNamespace()), "")
        self.assertEqual(channel_notice(SimpleNamespace()), DEFAULT_NOTICE)

    def test_enabled_channel_requires_https_url(self):
        config = SimpleNamespace(non_telegram_channel=SimpleNamespace(enabled=True, url="https://t.me/support", notice="联系服主"))
        self.assertTrue(channel_enabled(config))
        self.assertEqual(channel_url(config), "https://t.me/support")
        for value in ("", "http://example.com", "javascript:alert(1)", "https://user:pass@example.com"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_channel_url(value)

    def test_notice_is_bounded(self):
        self.assertEqual(validate_channel_notice("  联系服主  "), "联系服主")
        with self.assertRaises(ValueError):
            validate_channel_notice(" ")
        with self.assertRaises(ValueError):
            validate_channel_notice("a" * 4001)


if __name__ == "__main__":
    unittest.main(verbosity=2)
