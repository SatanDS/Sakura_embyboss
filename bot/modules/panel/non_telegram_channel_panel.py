"""Owner/admin editor for the optional manual channel for non-Telegram users."""

from pyrogram import enums, filters
from pyromod.exceptions import ListenerTimeout
from pyromod.helpers import ikb

from bot import bot, config, save_config, LOGGER
from bot.func_helper.filters import admins_on_filter
from bot.func_helper.fix_bottons import back_config_p_ikb
from bot.func_helper.msg_utils import callAnswer, callListen, editMessage
from bot.func_helper.non_telegram_channel import (
    DEFAULT_NOTICE,
    channel_enabled,
    channel_notice,
    channel_url,
    validate_channel_notice,
    validate_channel_url,
)


_editors = set()


def _channel():
    # Pydantic creates this for current configs; the fallback keeps old test
    # fixtures and partially migrated configs safe until they are saved again.
    channel = getattr(config, "non_telegram_channel", None)
    if channel is None:
        from bot.schemas.schemas import NonTelegramChannel
        channel = NonTelegramChannel()
        config.non_telegram_channel = channel
    return channel


def _keyboard():
    channel = _channel()
    state = "✅ 已开启" if channel_enabled(config) else "❌ 已关闭"
    return ikb([
        [(f"{state} 非 TG 用户通道", "non_tg_toggle")],
        [("修改通道链接", "non_tg_edit_url"), ("修改通道说明", "non_tg_edit_notice")],
        [("恢复默认说明", "non_tg_reset_notice")],
        [("🔙 返回设置", "back_config")],
    ])


def _text():
    channel = _channel()
    status = "已开启" if channel_enabled(config) else ("已配置但未开启" if channel.url else "未配置")
    return (
        "🆘 非 TG 用户通道\n\n"
        f"状态：{status}\n"
        f"链接：{channel_url(config) or '未设置'}\n\n"
        f"说明：{channel_notice(config)}\n\n"
        "此通道只用于人工办理账号或续期。它不会把 Emby 密码转换成 Telegram 身份，"
        "也不会开放 Bot 群组、积分和管理员功能。"
    )


def _save(channel, actor_id):
    previous = config.non_telegram_channel
    config.non_telegram_channel = channel
    try:
        save_config()
    except Exception as exc:
        config.non_telegram_channel = previous
        LOGGER.error("非 TG 通道保存失败: actor={}, type={}", actor_id, type(exc).__name__)
        return False
    return True


async def _edit_value(call, field):
    actor_id = call.from_user.id
    if actor_id in _editors:
        return await editMessage(call, "已有编辑请求，请回复之前的提示，或发送 /cancel 取消。", buttons=_keyboard())
    _editors.add(actor_id)
    try:
        prompt = "请输入 HTTPS 通道链接（例如 https://t.me/xxx），输入 /cancel 取消。" if field == "url" else (
            "请输入给非 TG 用户看的通道说明，输入 /cancel 取消。")
        await editMessage(call, prompt, buttons=_keyboard())
        message = await callListen(call, 120, buttons=_keyboard())
        if message is False:
            return
        value = (message.text or "").strip()
        await message.delete()
        if value == "/cancel":
            return await editMessage(call, "已取消修改。", buttons=_keyboard())
        try:
            value = validate_channel_url(value) if field == "url" else validate_channel_notice(value)
        except ValueError as exc:
            return await editMessage(call, str(exc), buttons=_keyboard())
        channel = _channel().model_copy(deep=True)
        setattr(channel, field, value)
        if _save(channel, actor_id):
            return await editMessage(call, "✅ 已保存，修改立即生效。\n\n" + _text(), buttons=_keyboard())
        return await editMessage(call, "保存失败，当前配置未改变。", buttons=_keyboard())
    except ListenerTimeout:
        return await editMessage(call, "编辑已超时，当前配置未改变。", buttons=_keyboard())
    finally:
        _editors.discard(actor_id)


@bot.on_callback_query(filters.regex(r"^non_telegram_channel_panel$") & admins_on_filter)
async def non_telegram_channel_panel(_, call):
    await callAnswer(call, "非 TG 用户通道")
    return await editMessage(call, _text(), buttons=_keyboard(), parse_mode=enums.ParseMode.DISABLED)


@bot.on_callback_query(filters.regex(r"^non_tg_(toggle|edit_url|edit_notice|reset_notice)$") & admins_on_filter)
async def non_telegram_channel_action(_, call):
    action = call.data.removeprefix("non_tg_")
    await callAnswer(call, "非 TG 用户通道")
    if action == "edit_url":
        return await _edit_value(call, "url")
    if action == "edit_notice":
        return await _edit_value(call, "notice")
    channel = _channel().model_copy(deep=True)
    if action == "toggle":
        if not channel_enabled(config) and not channel_url(config):
            return await editMessage(call, "请先设置 HTTPS 通道链接，再开启此功能。", buttons=_keyboard())
        channel.enabled = not channel.enabled
    elif action == "reset_notice":
        channel.notice = DEFAULT_NOTICE
    if not _save(channel, call.from_user.id):
        return await editMessage(call, "保存失败，当前配置未改变。", buttons=_keyboard())
    return await editMessage(call, _text(), buttons=_keyboard(), parse_mode=enums.ParseMode.DISABLED)
