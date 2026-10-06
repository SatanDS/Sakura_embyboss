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

    def test_invalid_unicode_is_rejected_and_config_has_safe_fallback(self):
        with self.assertRaises(ValueError):
            validate_channel_notice("broken\ud800")
        config = SimpleNamespace(non_telegram_channel=SimpleNamespace(
            enabled=True, url="https://support.example.com", notice="broken\ud800"))
        self.assertEqual(channel_notice(config), DEFAULT_NOTICE)
        with self.assertRaises(ValueError):
            validate_channel_url("https://support.example.com/\ud800")

    def test_url_rejects_browser_ambiguity_and_invalid_ports(self):
        for value in ("https://support.example.com\n", "https://support.example.com/\x00",
                      "https://support.example.com/\\path", "https://@support.example.com",
                      "https://support.example.com:0", "https://support.example.com:",
                      "https://support.example.com:invalid", "https://support.example.com:65536"):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                validate_channel_url(value)
        self.assertEqual(validate_channel_url("https://support.example.com:443/emby"),
                         "https://support.example.com:443/emby")

    def test_public_data_tracks_runtime_configuration_and_hides_disabled_details(self):
        channel = SimpleNamespace(enabled=False, url="https://support.example.com/emby", notice="联系服主")
        config = SimpleNamespace(non_telegram_channel=channel, api_key="private-key")
        self.assertEqual(_MODULE.public_channel(config), {"enabled": False, "url": "", "notice": ""})
        channel.enabled = True
        self.assertEqual(_MODULE.public_channel(config), {
            "enabled": True, "url": channel.url, "notice": channel.notice,
        })
        channel.url = "http://support.example.com"
        self.assertEqual(_MODULE.public_channel(config), {"enabled": False, "url": "", "notice": ""})


if __name__ == "__main__":
    unittest.main(verbosity=2)
