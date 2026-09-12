"""Strict channel preferences shared by payment administration and Checkout."""

CHANNEL_KEYS = ("alipay", "wechat_pay", "card", "apple_pay", "google_pay")
DEFAULT_CHANNELS = {
    "alipay": True,
    "wechat_pay": True,
    "card": False,
    "apple_pay": False,
    "google_pay": False,
}
# Kept as a descriptive alias for callers handling pre-channel orders.
LEGACY_CHANNELS = DEFAULT_CHANNELS


class ChannelError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def normalize_channels(channels):
    if (not isinstance(channels, dict) or set(channels) != set(CHANNEL_KEYS)
            or any(type(channels[key]) is not bool for key in CHANNEL_KEYS)):
        raise ChannelError("invalid_channels")
    if not channels["card"] and (channels["apple_pay"] or channels["google_pay"]):
        raise ChannelError("wallet_requires_card")
    return {key: channels[key] for key in CHANNEL_KEYS}


def validate_channels(channels):
    """Validate and return a canonical five-key channel mapping."""
    return normalize_channels(channels)
