"""Telegram UI for MoviePilot's DoubanSync plugin."""

from pyrogram import filters

from bot import bot, LOGGER, moviepilot
from bot.func_helper.filters import user_in_group_on_filter
from bot.func_helper.fix_bottons import back_members_ikb, douban_watch_ikb
from bot.func_helper.moviepilot import (
    normalize_douban_user_id,
    remove_douban_sync_user,
    update_douban_sync_users,
)
from bot.func_helper.msg_utils import callAnswer, callListen, editMessage
from bot.func_helper.utils import judge_admins
from bot.sql_helper.sql_douban import (
    sql_count_moviepilot_douban,
    sql_delete_moviepilot_douban,
    sql_get_moviepilot_douban,
    sql_upsert_moviepilot_douban,
)
from bot.sql_helper.sql_emby import sql_get_emby


def _authorized_user(tg_id):
    """Return (Emby row, error message) using the MoviePilot user policy."""
    if not moviepilot.douban_status:
        return None, "❌ 管理员未开启豆瓣想看功能"

    user = sql_get_emby(tg=tg_id)
    if not user or not user.embyid:
        return None, "⚠️ 你还没有有效的 Emby 账号"
    if user.lv not in {"a", "b"}:
        return None, "❌ 当前账号状态无法使用豆瓣想看"
    if moviepilot.lv == "a" and not judge_admins(tg_id) and user.lv != "a":
        return None, "❌ 当前功能仅限白名单用户使用"
    return user, None


@bot.on_callback_query(filters.regex(r"^douban_watch$") & user_in_group_on_filter)
async def douban_watch(_, call):
    _, error = _authorized_user(call.from_user.id)
    if error:
        return await callAnswer(call, error, True)

    binding = sql_get_moviepilot_douban(call.from_user.id)
    current = binding.douban_id if binding else "未绑定"
    text = (
        "📚 **豆瓣想看**\n\n"
        "提交豆瓣用户 ID 后，Bot 会把它加入 MoviePilot v2 的 `DoubanSync` 用户列表。\n"
        "插件随后会按自己的定时任务同步该用户的「想看」。\n\n"
        f"当前绑定：`{current}`\n\n"
        "支持输入纯数字 ID，或 `https://www.douban.com/people/<ID>`。"
    )
    await editMessage(call, text, buttons=douban_watch_ikb(bool(binding)))


@bot.on_callback_query(filters.regex(r"^douban_watch_bind$") & user_in_group_on_filter)
async def douban_watch_bind(_, call):
    _, error = _authorized_user(call.from_user.id)
    if error:
        return await callAnswer(call, error, True)

    await callAnswer(call, "请输入豆瓣用户 ID")
    prompt = await editMessage(
        call,
        "🔗 请在 120 秒内发送豆瓣用户 ID 或个人主页链接。\n"
        "例如：`294556764` 或 `https://www.douban.com/people/294556764`\n"
        "取消请输入 `/cancel`。",
        buttons=back_members_ikb,
    )
    if prompt is False:
        return

    message = await callListen(call, 120, buttons=back_members_ikb)
    if message is False:
        return
    try:
        raw_value = message.text or ""
        if raw_value.strip().casefold() == "/cancel":
            await message.delete()
            return await editMessage(call, "✅ 已取消绑定。", buttons=back_members_ikb)
        douban_id = normalize_douban_user_id(raw_value)
        if not douban_id:
            await message.delete()
            return await editMessage(
                call,
                "❌ 豆瓣 ID 格式无效，请输入 4-20 位数字或豆瓣个人主页链接。",
                buttons=douban_watch_ikb(bool(sql_get_moviepilot_douban(call.from_user.id))),
            )
        await message.delete()

        previous_row = sql_get_moviepilot_douban(call.from_user.id)
        previous_id = previous_row.douban_id if previous_row else None
        # Do not remove an id that is still bound to another Telegram user.
        previous_for_api = None
        if previous_id and previous_id != douban_id:
            if sql_count_moviepilot_douban(previous_id) <= 1:
                previous_for_api = previous_id

        ok, result = await update_douban_sync_users(douban_id, previous_for_api)
        if not ok:
            return await editMessage(call, f"❌ 更新 MoviePilot 豆瓣想看失败：{result}", buttons=back_members_ikb)

        if not sql_upsert_moviepilot_douban(call.from_user.id, douban_id):
            LOGGER.error("MoviePilot 豆瓣列表已更新，但 TG 绑定记录保存失败: %s", call.from_user.id)
            return await editMessage(
                call,
                f"✅ 已加入 MoviePilot 用户列表：`{result}`\n⚠️ 本地绑定记录保存失败，请联系管理员。",
                buttons=back_members_ikb,
            )
        return await editMessage(
            call,
            f"✅ 已绑定豆瓣 ID：`{result}`\nMoviePilot 的 DoubanSync 将按插件计划自动同步。",
            buttons=back_members_ikb,
        )
    except Exception as exc:
        LOGGER.exception("处理豆瓣绑定失败: %s", exc)
        return await editMessage(call, "❌ 处理失败，请稍后重试或联系管理员。", buttons=back_members_ikb)


@bot.on_callback_query(filters.regex(r"^douban_watch_remove$") & user_in_group_on_filter)
async def douban_watch_remove(_, call):
    _, error = _authorized_user(call.from_user.id)
    if error:
        return await callAnswer(call, error, True)

    binding = sql_get_moviepilot_douban(call.from_user.id)
    if not binding:
        return await callAnswer(call, "你还没有绑定豆瓣 ID", True)

    # If another TG account uses the same id, keep it in the global plugin
    # list; otherwise remove this account's old id from DoubanSync as well.
    if sql_count_moviepilot_douban(binding.douban_id) <= 1:
        ok, result = await remove_douban_sync_user(binding.douban_id)
        if not ok:
            return await editMessage(call, f"❌ 从 MoviePilot 移除失败：{result}", buttons=back_members_ikb)

    if not sql_delete_moviepilot_douban(call.from_user.id):
        return await editMessage(call, "⚠️ MoviePilot 已更新，但本地解绑记录失败，请联系管理员。", buttons=back_members_ikb)
    return await editMessage(call, "✅ 已解绑豆瓣 ID。", buttons=back_members_ikb)
