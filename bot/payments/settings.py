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
    test_buyer_ids: tuple = ()
    polygon_receive_address: str = ''
    polygon_rpc_url: str = ''
    binance_api_key: str = ''
    binance_api_secret: str = ''
    binance_network: str = 'POL'
    bsc_receive_address: str = ''
    bsc_rpc_url: str = ''
    ton_receive_address: str = ''
    ton_receive_memo: str = ''
    ton_api_url: str = 'https://toncenter.com/api/v3'
    ton_api_key: str = ''

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
            checkout_minutes=int(getattr(section, 'checkout_minutes', 30) or 30),
            seat_limit=int(getattr(section, 'seat_limit', 0) or 0),
            terms_version=str(getattr(section, 'terms_version', '2026-09-09-v1')),
            test_buyer_ids=tuple(int(value) for value in (getattr(section, 'test_buyer_ids', None) or ())),
            polygon_receive_address=str(getattr(section, 'polygon_receive_address', '') or ''),
            polygon_rpc_url=os.getenv('TGBOT_POLYGON_RPC_URL', ''),
            binance_api_key=os.getenv('TGBOT_BINANCE_API_KEY', ''),
            binance_api_secret=os.getenv('TGBOT_BINANCE_API_SECRET', ''),
            binance_network=str(getattr(section, 'binance_network', 'POL') or 'POL'),
            bsc_receive_address=str(getattr(section, 'bsc_receive_address', '') or ''),
            bsc_rpc_url=os.getenv('TGBOT_BSC_RPC_URL', ''),
            ton_receive_address=str(getattr(section, 'ton_receive_address', '') or ''),
            ton_receive_memo=str(getattr(section, 'ton_receive_memo', '') or ''),
            ton_api_url='https://toncenter.com/api/v3',
            ton_api_key=os.getenv('TGBOT_TONCENTER_API_KEY', ''),
        )

    def encryption_key_bytes(self):
        try:
            encoded = self.code_encryption_key.encode('ascii')
            # Accept the unpadded URL-safe key produced by the setup command.
            encoded += b'=' * (-len(encoded) % 4)
            key = base64.b64decode(encoded, altchars=b'-_', validate=True)
        except (ValueError, UnicodeError):
            raise ValueError('Payment encryption key must be a base64 encoded 32-byte key') from None
        if len(key) != 32:
            raise ValueError('Payment encryption key must be a base64 encoded 32-byte key')
        return key

    def validate_common(self):
        parsed = urlsplit(self.public_url)
        local = parsed.hostname in ('127.0.0.1', 'localhost', '::1')
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('Payment public_url must be a root HTTPS origin')
        if parsed.scheme != 'https' and not (not self.cookie_secure and local and parsed.scheme == 'http' and not self.live_mode):
            raise ValueError('Payment public_url must use HTTPS')
        if self.enabled and not self.live_mode and not self.test_buyer_ids:
            raise ValueError('Test payment mode requires an explicit test_buyer_ids allowlist')
        self.encryption_key_bytes()

    def validate_stripe(self):
        expected = 'sk_live_' if self.live_mode else 'sk_test_'
        if not self.stripe_secret_key.startswith(expected) or not self.stripe_webhook_secret.startswith('whsec_'):
            raise ValueError('Stripe credentials are missing or do not match payment mode')

    def validate(self):
        self.validate_common()
        self.validate_stripe()
