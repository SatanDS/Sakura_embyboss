"""Desktop login decisions come only from authenticated Telegram updates."""

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from starlette.concurrency import run_in_threadpool

from bot import bot, prefixes
from bot.func_helper.msg_utils import sendMessage, callAnswer, editMessage
from bot.dushengtv.runtime import service
from bot.dushengtv.service import TVError


async def prepare_desktop_login(msg, link):
    try:
        result = await run_in_threadpool(service().prepare, link, msg.from_user.id,
                                         msg.from_user.first_name, msg.from_user.username)
    except TVError as error:
        return await sendMessage(msg, f"❌ {error.message}", timer=120)
    buttons = InlineKeyboardMarkup([[
        InlineKeyboardButton("确认登录", callback_data=f"tvlogin:yes:{result['id']}"),
        InlineKeyboardButton("拒绝", callback_data=f"tvlogin:no:{result['id']}")]])
    return await sendMessage(msg, "🔐 DuShengTV 登录确认\n\n"
                             f"确认码：`{result['displayCode']}`\n"
                             "请核对 TV 客户端显示的确认码。仅确认你本人发起的登录；他人转发的请求请拒绝。",
                             buttons=buttons, timer=300)


@bot.on_callback_query(filters.regex(r"^tvlogin:(yes|no):[a-f0-9]{32}$"))
async def desktop_decision(_, call):
    if not call.message or call.message.chat.type.value != "private":
        return await callAnswer(call, "请在 Bot 私聊中确认", True)
    action, challenge_id = call.data.split(":")[1:]
    try:
        status = await run_in_threadpool(service().decide, challenge_id, call.from_user.id, action == "yes")
    except TVError as error:
        return await callAnswer(call, error.message, True)
    messages = {"approved": "✅ 已确认 DuShengTV 登录，请返回客户端。",
                "denied": "❌ 已拒绝本次 DuShengTV 登录。",
                "unbound": "❌ 请先在 Bot 中绑定 Emby 账号，再返回 DuShengTV 重新登录。",
                "disabled": "❌ Emby 账号已停用或到期，请处理后重新登录。"}
    await callAnswer(call, "已处理")
    return await editMessage(call, messages[status])


@bot.on_message(filters.command("tvdevices", prefixes) & filters.private)
async def desktop_devices(_, msg):
    try:
        result = await run_in_threadpool(service().bot_devices, msg.from_user.id)
    except TVError as error:
        return await sendMessage(msg, f"❌ {error.message}", timer=120)
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton(f"撤销：{d['name'][:40]}", callback_data=f"tvrevoke:ask:{d['id']}")]
                                    for d in result["devices"]]) if result["devices"] else None
    return await sendMessage(msg, f"🖥 DuShengTV 登录设备：{len(result['devices'])} / {result['maxDevices']}\n"
                             "撤销后对应设备将退出登录。重新使用需要再次在 Telegram 确认。",
                             buttons=buttons, timer=300)


@bot.on_callback_query(filters.regex(r"^tvrevoke:(ask|yes|no):[a-f0-9]{32}$"))
async def desktop_revoke(_, call):
    if not call.message or call.message.chat.type.value != "private":
        return await callAnswer(call, "请在 Bot 私聊中操作", True)
    action, device_id = call.data.split(":")[1:]
    if action == "ask":
        await callAnswer(call, "请确认")
        return await editMessage(call, "确定撤销这台 DuShengTV 设备？正在使用的会话将失效。",
                                 buttons=InlineKeyboardMarkup([[
                                     InlineKeyboardButton("确认撤销", callback_data=f"tvrevoke:yes:{device_id}"),
                                     InlineKeyboardButton("取消", callback_data=f"tvrevoke:no:{device_id}")]]))
    if action == "no":
        await callAnswer(call, "已取消")
        return await editMessage(call, "已取消撤销。使用 /tvdevices 重新查看设备。")
    try:
        await run_in_threadpool(service().bot_revoke, call.from_user.id, device_id)
    except TVError as error:
        return await callAnswer(call, error.message, True)
    await callAnswer(call, "已撤销")
    return await editMessage(call, "✅ 已撤销该设备。使用 /tvdevices 重新查看设备。")
