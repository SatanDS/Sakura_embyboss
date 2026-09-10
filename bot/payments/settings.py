"""Payment configuration, with credentials read only from the environment."""
import base64
import os
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class PaymentSettings:
    enabled: bool = False
    public_url: str = ''
    stripe_secret_key: str = ''
    stripe_webhook_secret: str = ''
    code_encryption_key: str = ''
    live_mode: bool = False
    checkout_minutes: int = 30
    cookie_secure: bool = True
    code_key: str = ''
    mode: str = 'test'
    seat_limit: int = 0
    terms_version: str = '2026-09-09-v1'

    @classmethod
    def from_config(cls, config):
        section = getattr(config, 'payments', None)
        mode = 'live' if bool(getattr(section, 'live_mode', False)) else 'test'
        return cls(
            enabled=bool(getattr(section, 'enabled', False)),
            public_url=str(getattr(section, 'public_url', '') or '').rstrip('/'),
            stripe_secret_key=os.getenv('TGBOT_STRIPE_SECRET_KEY', ''),
            stripe_webhook_secret=os.getenv('TGBOT_STRIPE_WEBHOOK_SECRET', ''),
            code_encryption_key=os.getenv('TGBOT_PAYMENT_CODE_KEY', ''),
            live_mode=bool(getattr(section, 'live_mode', False)),
            code_key=os.getenv('TGBOT_PAYMENT_CODE_KEY', ''),
            mode=mode,
            seat_limit=int(getattr(section, 'seat_limit', 0) or 0),
            terms_version=str(getattr(section, 'terms_version', '2026-09-09-v1')),
        )

    def encryption_key_bytes(self):
        try:
            key = base64.urlsafe_b64decode(self.code_encryption_key.encode('ascii'))
        except (ValueError, UnicodeError):
            raise ValueError('Payment encryption key must be a base64 encoded 32-byte key') from None
        if len(key) != 32:
            raise ValueError('Payment encryption key must be a base64 encoded 32-byte key')
        return key

    def validate(self):
        parsed = urlsplit(self.public_url)
        local = parsed.hostname in ('127.0.0.1', 'localhost', '::1')
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('Payment public_url must be a root HTTPS origin')
        if parsed.scheme != 'https' and not (not self.cookie_secure and local and parsed.scheme == 'http' and not self.live_mode):
            raise ValueError('Payment public_url must use HTTPS')
        expected = 'sk_live_' if self.live_mode else 'sk_test_'
        if not self.stripe_secret_key.startswith(expected) or not self.stripe_webhook_secret.startswith('whsec_'):
            raise ValueError('Stripe credentials are missing or do not match payment mode')
        self.encryption_key_bytes()
