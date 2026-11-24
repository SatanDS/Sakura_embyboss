"""Connect desktop authorization to the existing Bot account database."""

import asyncio
import json
import os

import aiohttp

from .models import EmbyOwner
from .service import DesktopAuth, TVError, canonical_url, valid


def account_lookup(db, tg):
    from bot.sql_helper.sql_emby import Emby
    from bot.payments.entitlements import china_now, resolve_entitlement

    # This row lock serializes device registration for one Telegram account.
    user = db.query(Emby).filter_by(tg=tg).with_for_update().one_or_none()
    if not user or not user.embyid:
        raise TVError("BOT_UNBOUND", "请先在 Bot 中绑定 Emby 账号")
    entitlement = resolve_entitlement(db, user)
    if entitlement is not None:
        allowed = entitlement.allowed
    else:
        allowed = user.lv in {"a", "b"} and not user.disabled_at and user.ex and user.ex > china_now()
    if not allowed:
        raise TVError("ACCOUNT_DISABLED", "Emby 账号已停用或到期，请先在 Bot 中处理")
    return {"embyUserId": str(user.embyid)}


def account_lookup_by_emby(db, emby_id):
    """Resolve both Telegram-bound and legacy Emby2 users for TV sessions."""
    from bot.sql_helper.sql_emby import Emby
    from bot.sql_helper.sql_emby2 import Emby2
    from bot.payments.entitlements import china_now

    tg_rows = db.query(Emby).filter_by(embyid=str(emby_id)).with_for_update().all()
    local_rows = db.query(Emby2).filter_by(embyid=str(emby_id)).with_for_update().all()
    if len(tg_rows) + len(local_rows) != 1:
        raise TVError("BOT_UNBOUND", "没有找到唯一的 Emby 账号绑定")
    if tg_rows:
        row = tg_rows[0]
        account = account_lookup(db, row.tg)
        return {**account, "tg": row.tg, "displayName": row.name or "Emby 用户"}
    row = local_rows[0]
    allowed = row.lv in {"a", "b"} and not row.expired and row.ex and row.ex > china_now()
    if not allowed:
        raise TVError("ACCOUNT_DISABLED", "Emby 账号已停用或到期")
    # The Emby2 row is locked above. Keep the mapping after sessions expire.
    owner = db.query(EmbyOwner).filter_by(emby_user_id=row.embyid).with_for_update().one_or_none()
    if owner is None:
        owner = EmbyOwner(emby_user_id=row.embyid)
        db.add(owner)
        db.flush()
    return {"embyUserId": str(row.embyid), "tg": -owner.id, "displayName": row.name or "Emby 用户"}


def settings():
    from bot import bot, config

    cfg = config.dushengtv
    if not cfg.enabled:
        raise TVError("NOT_CONFIGURED", "DuShengTV 登录服务尚未启用", 503)
    public_url = canonical_url(cfg.public_url, https_only=True)
    username = cfg.bot_username or getattr(getattr(bot, "me", None), "username", "")
    valid(username, r"[A-Za-z][A-Za-z0-9_]{4,31}", "请配置正确的 Telegram Bot 用户名")
    return cfg, public_url, username


def service():
    from bot.sql_helper import Session

    cfg, _, _ = settings()
    return DesktopAuth(Session, account_lookup, account_lookup_by_emby=account_lookup_by_emby,
                       max_devices=cfg.max_devices, consent_version=cfg.privacy_version)


async def authenticate_emby(username, password):
    """Only the configured origin receives the submitted credentials."""
    origin = emby_origin()
    unavailable = TVError("EMBY_UNAVAILABLE", "Emby 登录服务暂时不可用，请稍后再试", 503)
    headers = {"X-Emby-Authorization": 'MediaBrowser Client="DuShengTV", Device="TV Gateway", DeviceId="dushengtv-gateway", Version="1"'}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=False) as client:
            async with client.post(f"{origin}/Users/AuthenticateByName", headers=headers, allow_redirects=False,
                                   json={"Username": username, "Pw": password}) as response:
                if response.status in (401, 403):
                    raise TVError("INVALID_CREDENTIALS", "Emby 用户名或密码错误，或账号已停用", 401)
                if response.status == 429:
                    raise TVError("RATE_LIMITED", "Emby 登录尝试过于频繁，请稍后再试", 429)
                if response.status != 200:
                    raise unavailable
                payload = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    payload.extend(chunk)
                    if len(payload) > 65536:
                        raise unavailable
                data = json.loads(payload)
                user = data.get("User") if isinstance(data, dict) else None
                if not isinstance(user, dict) or not isinstance(user.get("Id"), str) or not user["Id"] or not data.get("AccessToken"):
                    raise unavailable
                if user.get("Policy", {}).get("IsDisabled"):
                    raise TVError("ACCOUNT_DISABLED", "Emby 账号已停用", 403)
                return user["Id"]
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        raise unavailable from None


def emby_origin():
    """Verify user tokens against the Bot's primary Emby, shared by its aliases."""
    from bot import config

    return canonical_url(config.emby_url)


def emby_auth_db_path():
    from bot import config
    from bot.func_helper.emby_identity import configured_auth_db_path

    return configured_auth_db_path(config)


def livekit():
    """Return public LiveKit settings without exposing signing credentials."""
    from bot import config

    cfg = config.dushengtv
    url = (getattr(cfg, "livekit_url", "") or os.getenv("TGBOT_LIVEKIT_URL", "")).strip()
    key = (getattr(cfg, "livekit_api_key", "") or os.getenv("TGBOT_LIVEKIT_API_KEY", "")).strip()
    secret = (getattr(cfg, "livekit_api_secret", "") or os.getenv("TGBOT_LIVEKIT_API_SECRET", "")).strip()
    if not url or not key or not secret:
        return None
    if url.startswith("https://"):
        url = "wss://" + url[8:]
    elif url.startswith("http://"):
        url = "ws://" + url[7:]
    if not url.startswith(("ws://", "wss://")):
        return None
    api_url = (getattr(cfg, "livekit_api_url", "") or os.getenv("TGBOT_LIVEKIT_API_URL", "")).strip()
    return {"url": url, "api_url": api_url or "http" + url[2:], "key": key, "secret": secret}
