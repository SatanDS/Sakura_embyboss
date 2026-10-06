"""Validation and presentation helpers for the optional non-Telegram path."""

from urllib.parse import urlsplit


NOTICE_MAX_UNITS = 4000
DEFAULT_NOTICE = "没有 Telegram？请通过下方人工通道联系服主办理账号或续期。"


def validate_channel_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("通道链接必须是 HTTPS 地址。")
    value = value.strip()
    if len(value) > 2048 or any(ch.isspace() for ch in value):
        raise ValueError("通道链接格式无效，且长度不能超过 2048 个字符。")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError("通道链接格式无效。") from None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("通道链接必须使用不带账号密码的 HTTPS 地址。")
    return value


def validate_channel_notice(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("通道说明不能为空。")
    value = value.strip()
    units = len(value.encode("utf-16-le")) // 2
    if units > NOTICE_MAX_UNITS:
        raise ValueError(f"通道说明过长，最多 {NOTICE_MAX_UNITS} 个字符。")
    return value


def channel_notice(config) -> str:
    channel = getattr(config, "non_telegram_channel", None)
    notice = getattr(channel, "notice", "") if channel else ""
    try:
        return validate_channel_notice(notice)
    except ValueError:
        return DEFAULT_NOTICE


def channel_url(config) -> str:
    channel = getattr(config, "non_telegram_channel", None)
    value = getattr(channel, "url", "") if channel else ""
    try:
        return validate_channel_url(value)
    except ValueError:
        return ""


def channel_enabled(config) -> bool:
    channel = getattr(config, "non_telegram_channel", None)
    return bool(getattr(channel, "enabled", False) and channel_url(config))
