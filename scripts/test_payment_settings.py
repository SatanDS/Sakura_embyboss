import base64
import os
import importlib.util
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

path = Path(__file__).resolve().parents[1] / "bot/payments/settings.py"
spec = importlib.util.spec_from_file_location("payment_settings_under_test", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
PaymentSettings = module.PaymentSettings


class PaymentSettingsTests(unittest.TestCase):
    def config(self, **values):
        defaults = dict(enabled=True, public_url="https://pay.example.test", live_mode=False,
                        checkout_minutes=30, seat_limit=100, terms_version="terms-v1",
                        test_buyer_ids=[1001])
        defaults.update(values)
        return SimpleNamespace(payments=SimpleNamespace(**defaults), open=SimpleNamespace(all_user=100))

    def env(self, **values):
        defaults = dict(TGBOT_STRIPE_SECRET_KEY="sk_test_example", TGBOT_STRIPE_WEBHOOK_SECRET="whsec_example",
                        TGBOT_PAYMENT_CODE_KEY=base64.urlsafe_b64encode(b"k" * 32).decode())
        defaults.update(values)
        return patch.dict(os.environ, defaults, clear=False)

    def test_valid_test_mode_settings(self):
        with self.env():
            settings = PaymentSettings.from_config(self.config())
            settings.validate()
            self.assertEqual(settings.mode, "test")
            self.assertEqual(settings.seat_limit, 100)

    def test_checkout_minutes_is_loaded_from_config(self):
        with self.env():
            settings = PaymentSettings.from_config(self.config(checkout_minutes=45))
            self.assertEqual(settings.checkout_minutes, 45)

    def test_disabled_payment_can_start_without_stripe_credentials(self):
        with self.env(TGBOT_STRIPE_SECRET_KEY="", TGBOT_STRIPE_WEBHOOK_SECRET="", TGBOT_PAYMENT_CODE_KEY=""):
            settings = PaymentSettings.from_config(self.config(enabled=False))
            self.assertFalse(settings.enabled)

    def test_live_mode_rejects_test_key(self):
        with self.env():
            with self.assertRaises(ValueError):
                PaymentSettings.from_config(self.config(live_mode=True)).validate()

    def test_enabled_test_mode_requires_allowlist(self):
        with self.env():
            with self.assertRaises(ValueError):
                PaymentSettings.from_config(self.config(test_buyer_ids=[])).validate()

    def test_setup_command_unpadded_key_preserves_cipher_key(self):
        raw = bytes(range(224, 256))
        padded = base64.urlsafe_b64encode(raw).decode("ascii")
        crypto_spec = importlib.util.spec_from_file_location("payment_crypto_key_test", path.with_name("crypto.py"))
        crypto = importlib.util.module_from_spec(crypto_spec)
        crypto_spec.loader.exec_module(crypto)
        token, _, ciphertext = crypto.CodeCipher(padded).issue("order-key-compatibility")
        for encoded in (padded, padded.rstrip("=")):
            with self.subTest(padded=encoded.endswith("=")), self.env(TGBOT_PAYMENT_CODE_KEY=encoded):
                settings = PaymentSettings.from_config(self.config())
                settings.validate()
                self.assertEqual(settings.encryption_key_bytes(), raw)
                self.assertEqual(crypto.CodeCipher(settings.code_key).reveal(
                    ciphertext, "order-key-compatibility"), token)

    def test_invalid_encryption_keys_are_rejected(self):
        valid = base64.urlsafe_b64encode(b"k" * 32).decode("ascii")
        for encoded in ("", "abc", "!" + valid, valid[:8] + "\n" + valid[8:], "\u4e2d" + valid,
                        base64.urlsafe_b64encode(b"k" * 31).decode("ascii")):
            with self.env(TGBOT_PAYMENT_CODE_KEY=encoded), self.assertRaises(ValueError):
                PaymentSettings.from_config(self.config()).validate()

    def test_http_payment_origin_is_only_allowed_for_local_test(self):
        with self.env():
            with self.assertRaises(ValueError):
                PaymentSettings.from_config(self.config(public_url="http://pay.example.test")).validate()
            local = PaymentSettings.from_config(self.config(public_url="http://127.0.0.1:8080"))
            local = PaymentSettings(**{**local.__dict__, "cookie_secure": False})
            local.validate()


if __name__ == "__main__":
    unittest.main(verbosity=2)
