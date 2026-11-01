"""Send one private renewal reminder per expiry in the next three days."""

from datetime import timedelta

from pyrogram import enums
from pyrogram.errors import FloodWait
from pyromod.helpers import ikb

from bot import bot, LOGGER
from bot.func_helper.concurrency import get_user_lock
from bot.payments.entitlements import china_now, resolve_entitlement
from bot.sql_helper import Session
from bot.sql_helper.sql_emby import Emby


async def remind_expiring_accounts():
    try:
        with Session() as session:
            # Managed accounts may have a stale ex projection; resolve their ledger below.
            users = session.query(Emby.tg).filter(
                Emby.tg > 0, Emby.embyid.isnot(None), Emby.embyid != "",
                Emby.lv.in_(("a", "b")),
            ).all()
    except Exception as exc:
        LOGGER.error("Expiry reminder lookup failed: type={}", type(exc).__name__)
        return

    for (tg,) in users:
        try:
            async with get_user_lock(tg):
                with Session() as session:
                    user = session.get(Emby, tg)
                    if not user or not user.embyid or user.lv not in {"a", "b"}:
                        continue
                    now = china_now()
                    entitlement = resolve_entitlement(session, user, now)
                    if entitlement is not None and not entitlement.allowed:
                        continue
                    expiry = entitlement.final_expiry if entitlement is not None else user.ex
                    if (expiry is None or not now < expiry <= now + timedelta(days=3)
                            or user.expiry_reminded_for == expiry):
                        continue
                    text = (
                        "⏰ 账户到期提醒\n\n"
                        f"Emby 用户名：{user.name or user.embyid}\n"
                        "您的账户将在 3 天内到期，请及时续费。\n"
                        f"到期时间：{expiry:%Y-%m-%d %H:%M:%S}（北京时间）\n\n"
                        "如需续期，请点击下方「注册续期」联系服主。"
                    )
                    emby_id = user.embyid
                await bot.send_message(
                    chat_id=tg, text=text, parse_mode=enums.ParseMode.DISABLED,
                    reply_markup=ikb([[('注册续期', 'https://t.me/wugandu', 'url')]]),
                )
                # shortcut: crashes/multiple workers can repeat delivery; add delivery review if strict dedup is needed.
                with Session.begin() as session:
                    session.query(Emby).filter(Emby.tg == tg, Emby.embyid == emby_id).update(
                        {Emby.expiry_reminded_for: expiry}, synchronize_session=False,
                    )
        except FloodWait as exc:
            LOGGER.warning("Expiry reminders rate limited; retry next check: seconds={}", exc.value)
            return
        except Exception as exc:
            LOGGER.error("Expiry reminder failed: tg={}, type={}", tg, type(exc).__name__)
