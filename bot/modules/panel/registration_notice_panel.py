"""Private administrator editor for the post-registration notice."""

from pyrogram import enums, filters
from pyrogram.types import ForceReply
from pyromod.exceptions import ListenerTimeout
from pyromod.helpers import ikb

from bot import bot, config, save_config, LOGGER
from bot.func_helper.filters import admins_on_filter
from bot.func_helper.fix_bottons import registration_notice_ikb
from bot.func_helper.msg_utils import callAnswer, editMessage, sendMessage
from bot.func_helper.registration_notice import get_registration_notice, validate_registration_notice


_notice_editors = set()


def _can_edit_notice(call):
    user = getattr(call, "from_user", None)
    message = getattr(call, "message", None)
    chat = getattr(message, "chat", None)
    return bool(user and chat and chat.id == user.id
                and (user.id == config.owner or user.id in config.admins))


def _notice_keyboard():
    return ikb([
        [("查看当前内容", "registration_notice_preview"), ("修改内容", "registration_notice_edit")],
        [("恢复默认", "registration_notice_reset")],
        [("返回设置", "back_config")],
    ])


def _save_notice(value, actor_id):
    previous = config.registration_notice
    config.registration_notice = value
    try:
        save_config()
    except Exception as exc:
        config.registration_notice = previous
        LOGGER.error("注册须知保存失败: actor={}, type={}", actor_id, type(exc).__name__)
        return False
    LOGGER.info("注册须知已更新: actor={}, default={}", actor_id, value is None)
    return True


async def _edit_notice(call):
    actor_id = call.from_user.id
    if actor_id in _notice_editors:
        return await sendMessage(call, "已有编辑请求，请回复之前的输入提示，或发送 /cancel 取消。")
    _notice_editors.add(actor_id)
    previous = config.registration_notice
    try:
        prompt = await call.message.reply(
            "请在 120 秒内回复此消息，发送完整的新注册须知，发送后预览并自动保存。\n"
            "支持换行及 Bot 的 Markdown 格式，最长 4000 字符（部分表情按两个字符计）。\n"
            "取消请输入 /cancel。",
            quote=False, reply_markup=ForceReply(selective=True),
        )

        async def matches_reply(_, __, message):
            return bool(message.text and (
                message.text.strip() == "/cancel"
                or getattr(message, "reply_to_message_id", None) == prompt.id))

        message = await call.message.chat.listen(
            filters=filters.create(matches_reply), user_id=actor_id,
            timeout=120, unallowed_click_alert=False,
        )
        if not message or (message.text or "").strip() == "/cancel":
            return await editMessage(call, "已取消编辑，注册须知未更改。", buttons=_notice_keyboard())
        if not _can_edit_notice(call) or getattr(message.from_user, "id", None) != actor_id:
            return await sendMessage(call, "无权修改注册须知。")
        try:
            value = validate_registration_notice(message.text)
        except ValueError as exc:
            return await editMessage(call, str(exc), buttons=_notice_keyboard())

        # Preview as a text message: the configuration panel is a photo caption
        # and cannot display a full-length Telegram message.
        preview = await sendMessage(call, value, buttons=registration_notice_ikb)
        if preview is not True:
            return await editMessage(call, "预览发送失败，尚未保存。请检查文字格式后重试。",
                                     buttons=_notice_keyboard())
        if not _can_edit_notice(call):
            return await sendMessage(call, "管理权限已变更，尚未保存。")
        if config.registration_notice != previous:
            return await editMessage(call, "须知已被其他操作修改，请查看最新内容后重新编辑。",
                                     buttons=_notice_keyboard())
        saved = _save_notice(value, actor_id)
        text = ("已保存注册须知，下一个注册成功的用户会收到新内容，无需重启。"
                if saved else "保存失败，当前须知未更新。请检查配置文件写入权限后重试。")
        return await editMessage(call, text, buttons=_notice_keyboard())
    except ListenerTimeout:
        return await editMessage(call, "编辑已超时，注册须知未更改。", buttons=_notice_keyboard())
    finally:
        _notice_editors.discard(actor_id)


@bot.on_callback_query(filters.regex(r"^registration_notice_(panel|preview|edit|reset|reset_confirm)$") & admins_on_filter)
async def registration_notice_settings(_, call):
    if not _can_edit_notice(call):
        return await callAnswer(call, "请由管理员在 Bot 私聊中编辑注册须知。", True)
    await callAnswer(call, "注册须知")
    action = call.data.removeprefix("registration_notice_")
    if action == "edit":
        return await _edit_notice(call)
    if action == "preview":
        return await sendMessage(call, get_registration_notice(config), buttons=registration_notice_ikb)
    if action == "reset":
        return await editMessage(call, "确认将注册须知恢复为默认内容？", buttons=ikb([
            [("确认恢复", "registration_notice_reset_confirm"), ("取消", "registration_notice_panel")],
        ]))
    if action == "reset_confirm":
        saved = _save_notice(None, call.from_user.id)
        text = "已恢复默认注册须知，立即生效。" if saved else "保存失败，当前须知未更新。"
        return await editMessage(call, text, buttons=_notice_keyboard())
    current = "默认内容" if config.registration_notice is None else "自定义内容"
    return await editMessage(call, f"注册须知\n\n当前：{current}\n在 Emby 账号注册成功后发送给新用户。",
                             buttons=_notice_keyboard(), parse_mode=enums.ParseMode.DISABLED)
