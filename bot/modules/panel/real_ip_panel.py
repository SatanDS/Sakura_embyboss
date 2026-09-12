"""Private administrator management of trusted CDN origin peers."""

import re
import secrets
from time import monotonic

from pyrogram import enums, filters
from pyrogram.types import ForceReply
from pyromod.exceptions import ListenerTimeout
from pyromod.helpers import ikb

from bot import bot, config, save_config, LOGGER
from bot.func_helper.filters import admins_on_filter
from bot.func_helper.msg_utils import callAnswer, editMessage, sendMessage
from bot.func_helper.proxy_ip import validate_proxy_cidrs


_proxy_editors = set()
_proxy_clear_requests = {}


def _can_manage_proxies(call):
    user = getattr(call, "from_user", None)
    chat = getattr(getattr(call, "message", None), "chat", None)
    return bool(user and chat and chat.id == user.id
                and (user.id == config.owner or user.id in config.admins))


def _proxy_keyboard():
    return ikb([
        [("查看节点", "cdn_ip_preview")],
        [("添加节点", "cdn_ip_add"), ("删除节点", "cdn_ip_remove")],
        [("清空节点", "cdn_ip_clear")],
        [("返回设置", "back_config")],
    ])


def _proxy_snapshot():
    return list(getattr(config, "trusted_proxy_cidrs", []))


def _save_proxies(values, actor_id):
    previous = _proxy_snapshot()
    config.trusted_proxy_cidrs = values
    try:
        save_config()
    except Exception as exc:
        config.trusted_proxy_cidrs = previous
        LOGGER.error("CDN 真实 IP 配置保存失败: actor={}, type={}", actor_id, type(exc).__name__)
        return False
    LOGGER.info("CDN 真实 IP 节点已更新: actor={}, count={}", actor_id, len(values))
    return True


def _proxy_inputs(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请输入至少一个 CDN 回源节点 IP 或 CIDR。")
    # Preserve newline/tab as separators, but reject invisible control bytes.
    if any((ord(char) < 32 and char not in "\r\n\t") or ord(char) == 127 for char in text):
        raise ValueError("节点地址不能包含控制字符。")
    return validate_proxy_cidrs(re.split(r"[\s,，]+", text.strip()))


async def _show_proxy_list(call):
    values = _proxy_snapshot()
    if not values:
        return await sendMessage(call, "尚未添加 CDN 回源节点。使用连接来源 IP，不信任转发标头；不会阻止访问。")
    # The settings panel is a photo caption; long lists require text replies.
    for start in range(0, len(values), 40):
        lines = "\n".join(f"{index + 1}. {value}" for index, value in enumerate(values[start:start + 40], start))
        await call.message.reply(
            f"CDN 回源节点（共 {len(values)} 项）\n\n{lines}",
            quote=False, parse_mode=enums.ParseMode.DISABLED,
        )


async def _edit_proxy_list(call, action):
    actor_id = call.from_user.id
    if actor_id in _proxy_editors:
        return await sendMessage(call, "已有节点编辑请求，请回复之前的输入提示，或发送 /cancel 取消。")
    _proxy_editors.add(actor_id)
    previous = _proxy_snapshot()
    try:
        verb = "添加" if action == "add" else "删除"
        prompt = await call.message.reply(
            f"请在 120 秒内回复此消息，发送要{verb}的 CDN 回源节点 IP 或 CIDR。\n"
            "多个地址用换行、逗号或空格分隔，最多保留 128 项。只填写代理节点，不要填写用户 IP。\n"
            "不支持域名、端口或全部地址网段。删除时填写列表中的完整 IP 或 CIDR。\n"
            "发送 /cancel 取消。",
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
        if not _can_manage_proxies(call) or (message and getattr(message.from_user, "id", None) != actor_id):
            return await sendMessage(call, "管理权限已变更，节点列表未更新。")
        if not message or (message.text or "").strip() == "/cancel":
            return await editMessage(call, "已取消，节点列表未更新。", buttons=_proxy_keyboard())
        if _proxy_snapshot() != previous:
            return await editMessage(call, "节点列表已被其他操作修改，请查看最新列表后重新操作。", buttons=_proxy_keyboard())
        try:
            values = _proxy_inputs(message.text)
            current = validate_proxy_cidrs(previous)
            if action == "add":
                updated = validate_proxy_cidrs(list(dict.fromkeys(current + values)))
            else:
                if any(value not in current for value in values):
                    raise ValueError("部分地址不在当前列表中，请查看节点列表后填写完整 IP 或 CIDR。")
                updated = [value for value in current if value not in values]
        except ValueError:
            return await editMessage(
                call, "输入无效：请填写有效的节点 IP 或 CIDR，最多 128 项；不支持域名、端口或 /0 网段。\n"
                "删除时须填写当前列表中已存在的完整 IP 或 CIDR。节点列表未更新。",
                buttons=_proxy_keyboard(),
            )
        saved = _save_proxies(updated, actor_id)
        text = (f"已保存，共 {len(updated)} 个回源节点。新请求立即使用新列表，无需重启 Bot。"
                if saved else "保存失败，节点列表未更新。请检查配置文件写入权限后重试。")
        return await editMessage(call, text, buttons=_proxy_keyboard())
    except ListenerTimeout:
        return await editMessage(call, "输入已超时，节点列表未更新。", buttons=_proxy_keyboard())
    finally:
        _proxy_editors.discard(actor_id)


@bot.on_callback_query(filters.regex(r"^cdn_ip_(panel|preview|add|remove|clear|clear_confirm_[0-9a-f]{16})$") & admins_on_filter)
async def real_ip_settings(_, call):
    if not _can_manage_proxies(call):
        return await callAnswer(call, "请由所有者或管理员在 Bot 私聊中管理 CDN 回源节点。", True)
    await callAnswer(call, "CDN 真实 IP")
    if not _can_manage_proxies(call):
        return await sendMessage(call, "管理权限已变更，节点列表未更新。")
    actor_id = call.from_user.id
    action = call.data.removeprefix("cdn_ip_")
    if not action.startswith("clear_confirm_"):
        _proxy_clear_requests.pop(actor_id, None)
    if action in ("add", "remove"):
        return await _edit_proxy_list(call, action)
    if action == "preview":
        return await _show_proxy_list(call)
    if action == "clear":
        token = secrets.token_hex(8)
        _proxy_clear_requests[actor_id] = (token, _proxy_snapshot(), monotonic() + 120)
        return await editMessage(call, "确认清空所有 CDN 回源节点？\n清空后使用连接来源 IP，不信任转发标头；不会阻止普通或 VIP 用户访问。",
                                 buttons=ikb([[("确认清空", f"cdn_ip_clear_confirm_{token}"), ("取消", "cdn_ip_panel")]]))
    if action.startswith("clear_confirm_"):
        pending = _proxy_clear_requests.pop(actor_id, None)
        token = action.removeprefix("clear_confirm_")
        if not pending or pending[0] != token or monotonic() > pending[2]:
            return await editMessage(call, "清空确认已失效，请重新操作。", buttons=_proxy_keyboard())
        if _proxy_snapshot() != pending[1]:
            return await editMessage(call, "节点列表已被其他操作修改，请查看最新列表后重新确认。", buttons=_proxy_keyboard())
        saved = _save_proxies([], actor_id)
        text = "已清空节点列表，新请求使用连接来源 IP。" if saved else "保存失败，节点列表未更新。"
        return await editMessage(call, text, buttons=_proxy_keyboard())
    return await editMessage(
        call, f"CDN 真实 IP\n\n当前回源节点：{len(_proxy_snapshot())} 项\n"
        "添加实际向服务器回源的 CDN 或代理节点 IP，也可填写 CIDR 网段。不要填写普通用户 IP。\n"
        "完成网关接入后，列表变更对新请求立即生效。普通用户直连 8096 不受影响。\n"
        "空列表使用连接来源 IP，不信任转发标头，也不阻止访问。",
        buttons=_proxy_keyboard(), parse_mode=enums.ParseMode.DISABLED,
    )
