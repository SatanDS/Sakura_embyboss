"""Telegram entry point for the external Stripe shop."""

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot import bot, prefixes, config
from bot.func_helper.msg_utils import sendMessage, deleteMessage, callAnswer, editMessage


async def approve_browser_login(user_id, token):
    """Approve a browser payment login from the existing /start handler."""
    from bot.payments.browser_auth import LoginError
    from bot.web.api.payment import _auth
    try:
        challenge = _auth().prepare(token, int(user_id))
    except LoginError:
        return "❌ 登录请求已过期，请返回购买页面重新发起。"
    return (
        f"🔐 浏览器登录确认\n\n请确认这次登录请求属于你本人。\n确认码：`{challenge['display_code']}`",
        InlineKeyboardMarkup([[InlineKeyboardButton("确认登录", callback_data=f"paylogin:yes:{challenge['id']}"),
                               InlineKeyboardButton("拒绝", callback_data=f"paylogin:no:{challenge['id']}")]])
    )


@bot.on_callback_query(filters.regex(r"^paylogin:(yes|no):[a-f0-9]{32}$"))
async def payment_login_decision(_, call):
    from bot.payments.browser_auth import LoginError
    from bot.web.api.payment import _auth
    action, challenge_id = call.data.split(":", 2)[1:]
    try:
        _auth().decide(challenge_id, int(call.from_user.id), approve=action == "yes")
    except LoginError:
        return await callAnswer(call, "登录请求已过期或无效", True)
    await callAnswer(call, "已确认" if action == "yes" else "已拒绝")
    await editMessage(call, "✅ 浏览器登录已确认。请返回购买页面，页面会自动完成登录。" if action == "yes" else "❌ 已拒绝本次浏览器登录请求。")


@bot.on_message(filters.command("pay", prefixes) & filters.private)
async def payment_shop_command(_, msg):
    await deleteMessage(msg)
    payments = getattr(config, "payments", None)
    if not payments or not payments.enabled or not payments.public_url:
        return await sendMessage(msg, "❌ 当前未开放在线购买。", timer=60)
    return await sendMessage(msg, f"🛒 套餐购买：{payments.public_url.rstrip('/')}/payments/shop", timer=120)
