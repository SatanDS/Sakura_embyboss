"""Connect desktop authorization to the existing Bot account database."""

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
    return DesktopAuth(Session, account_lookup, max_devices=cfg.max_devices, consent_version=cfg.privacy_version)


def emby_origin():
    """Verify user tokens against the Bot's primary Emby, shared by its aliases."""
    from bot import config

    return canonical_url(config.emby_url)
