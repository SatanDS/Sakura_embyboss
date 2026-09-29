"""
red_envelope - 

Author:susu
Date:2023/01/02
"""

import cn2an
import asyncio
import random
import math
from datetime import datetime, timedelta
from pyrogram import filters
from pyrogram.types import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func

from bot import bot, prefixes, sakura_b, bot_photo, red_envelope, _open, LOGGER
from bot.func_helper.filters import user_in_group_on_filter
from bot.func_helper.fix_bottons import users_iv_button
from bot.func_helper.msg_utils import sendPhoto, sendMessage, callAnswer, editMessage
from bot.func_helper.utils import pwd_create, judge_admins, get_users, cache
from bot.sql_helper import Session
from bot.sql_helper.sql_emby import sql_get_emby, sql_spend_emby_iv
from bot.sql_helper.sql_red_envelope import (
    sql_activate_red_envelope,
    sql_claim_red_envelope,
    sql_create_red_envelope,
    sql_get_red_envelope,
    sql_recover_pending_red_envelopes,
    sql_refund_pending_red_envelope,
)
from bot.ranks_helper.ranks_draw import RanksDraw
from bot.schemas import Yulv

# 红包余额和领取明细保存在数据库中，领取与积分入账在同一事务提交。

recovered_envelopes, failed_recoveries = sql_recover_pending_red_envelopes()
if recovered_envelopes or failed_recoveries:
    LOGGER.warning(
        f"Red envelope startup recovery: refunded={recovered_envelopes}, "
        f"pending={failed_recoveries}"
    )


class RedEnvelope:
    def __init__(self, money, members, sender_id, sender_name, envelope_type="random"):
        self.id = None
        self.money = money  # 总金额
        self.rest_money = money  # 剩余金额
        self.members = members  # 总份数
        self.rest_members = members  # 剩余份数
        self.sender_id = sender_id  # 发送者ID
        self.sender_name = sender_name  # 发送者名称
        self.type = envelope_type  # random/equal/private
        self.receivers = {}  # {user_id: {"amount": xx, "name": "xx"}}
        self.target_user = None  # 专享红包接收者ID
        self.message = None  # 红包消息（普通红包和专享红包共用）


def _build_completed_red_envelope(stored, claims):
    envelope = RedEnvelope(
        stored["total_amount"], stored["total_members"],
        stored["sender_id"], stored["sender_name"], stored["envelope_type"],
    )
    envelope.id = stored["id"]
    envelope.rest_money = stored["remaining_amount"]
    envelope.rest_members = stored["remaining_members"]
    envelope.target_user = stored["target_user"]
    envelope.message = stored["message"]
    envelope.receivers = {
        claim["tg"]: {"amount": claim["amount"], "name": claim["name"]}
        for claim in claims
    }
    return envelope


async def _show_completed_red_envelope(call, envelope):
    text = await generate_final_message(envelope)
    chunks = [text[i : i + 2048] for i in range(0, len(text), 2048)]
    for index, chunk in enumerate(chunks):
        if index == 0:
            await editMessage(call, chunk)
        else:
            await call.message.reply(chunk)


async def create_reds(
    money,
    members,
    first_name,
    sender_id,
    envelope_type="random",
    private=None,
    private_text=None,
):
    red_id = await pwd_create(5)
    envelope = RedEnvelope(
        money=money,
        members=members,
        sender_id=sender_id,
        sender_name=first_name,
        envelope_type=envelope_type,
    )

    if private:
        envelope.type = "private"
        envelope.target_user = private
    if private_text is None:
        # 专享红包：如果没有传入祝福语，则随机选择默认祝福语
        envelope.message = random.choice(Yulv.load_yulv().red_bag)
    else:
        envelope.message = private_text
    envelope.id = red_id
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="👆🏻 好運連連", callback_data=f"red_envelope-{red_id}"
                )
            ]
        ]
    )
    try:
        status = sql_create_red_envelope(
            envelope_id=red_id,
            sender_id=sender_id,
            sender_name=first_name,
            money=money,
            members=members,
            envelope_type=envelope.type,
            target_user=envelope.target_user,
            message=envelope.message,
        )
    except Exception as exc:
        LOGGER.error(f"Red envelope creation failed: {type(exc).__name__}")
        status = "error"
    if status != "ok":
        return None, red_id, status
    return keyboard, red_id, status


async def _publish_red_envelope(
    msg,
    reply,
    *,
    money,
    members,
    first_name,
    sender_id,
    envelope_type,
    photo_user,
    cover_name,
    private=None,
    private_text=None,
    success_text=None,
):
    try:
        keyboard, red_id, status = await create_reds(
            money=money,
            members=members,
            first_name=first_name,
            sender_id=sender_id,
            envelope_type=envelope_type,
            private=private,
            private_text=private_text,
        )
    except Exception as exc:
        LOGGER.error(f"Red envelope creation failed: {type(exc).__name__}")
        await reply.edit("红包创建失败；如果积分已扣除，请联系管理员核对并提供发送时间。")
        return False

    if status != "ok":
        refunded = sql_refund_pending_red_envelope(red_id)
        if status == "insufficient":
            text = "积分余额已变化或不足，未创建红包。"
        elif refunded:
            text = f"红包创建未完成，积分已退回。红包编号：{red_id}"
        else:
            current = sql_get_red_envelope(red_id)
            if current and current["state"] == "pending":
                detail = "退款暂未完成，系统会在重启恢复时重试。"
            elif current and current["state"] == "open":
                detail = "红包可能已开放，请联系管理员核对。"
            else:
                detail = "请联系管理员核对积分和红包状态。"
            text = f"红包记录创建未确认，{detail}红包编号：{red_id}"
        await reply.edit(text)
        return False

    try:
        user_pic = await get_user_photo(photo_user)
        cover = await RanksDraw.hb_test_draw(money, members, user_pic, cover_name)
        if await sendPhoto(msg, photo=cover, buttons=keyboard) is not True:
            raise RuntimeError("Telegram did not confirm photo delivery")
        if not sql_activate_red_envelope(red_id):
            raise RuntimeError("red envelope activation failed")
    except Exception as exc:
        refunded = sql_refund_pending_red_envelope(red_id)
        LOGGER.error(f"Red envelope publishing failed: {type(exc).__name__}")
        if refunded:
            result = "积分已退回。"
        else:
            current = sql_get_red_envelope(red_id)
            if current and current["state"] == "pending":
                result = "退款暂未完成，系统会在重启恢复时重试。"
            elif current and current["state"] == "open":
                result = "红包可能已开放，请联系管理员核对。"
            else:
                result = "请联系管理员核对积分和红包状态。"
        await reply.edit(f"红包发送失败。{result}")
        return False

    if success_text:
        await reply.edit(success_text)
    else:
        await reply.delete()
    return True


@bot.on_message(
    filters.command("red", prefixes) & user_in_group_on_filter & filters.group
)
async def send_red_envelope(_, msg):
    if not red_envelope.status:
        return await asyncio.gather(
            msg.delete(), sendMessage(msg, "🚫 红包功能已关闭！")
        )

    if not red_envelope.allow_private and msg.reply_to_message:
        return await asyncio.gather(
            msg.delete(), sendMessage(msg, "🚫 专属红包功能已关闭！")
        )

    # 处理专享红包
    if msg.sender_chat:
        return await asyncio.gather(
            msg.delete(),
            sendMessage(msg, "红包必须由个人账号发起，暂不支持以群组身份发送。", timer=60),
        )

    if msg.reply_to_message and red_envelope.allow_private:
        target_from_user = msg.reply_to_message.from_user
        target_sender_chat = msg.reply_to_message.sender_chat

        # 不允许对机器人或频道发送专属红包
        if not target_from_user or target_from_user.is_bot or target_sender_chat:
            return await asyncio.gather(
                msg.delete(),
                sendMessage(msg, "🚫 专属红包不能发给机器人或频道!", timer=60),
            )

        try:
            money = int(msg.command[1])
            private_text = (
                msg.command[2]
                if len(msg.command) > 2
                else random.choice(Yulv.load_yulv().red_bag)
            )
        except (IndexError, ValueError):
            return await asyncio.gather(
                msg.delete(),
                sendMessage(
                    msg,
                    "**🧧 专享红包：\n\n请回复某人 [数额] [祝福语（可选）]**",
                    timer=60,
                ),
            )

        # 验证发送者资格
        if msg.reply_to_message and red_envelope.allow_private:
            try:
                money = int(msg.command[1])
                private_text = (
                    msg.command[2]
                    if len(msg.command) > 2
                    else random.choice(Yulv.load_yulv().red_bag)
                )
            except (IndexError, ValueError):
                return await asyncio.gather(
                    msg.delete(),
                    sendMessage(
                        msg,
                        "**🧧 专享红包：\n\n请回复某人 [数额] [祝福语（可选）]**",
                        timer=60,
                    ),
                )

            verified, first_name, error = await verify_red_envelope_sender(
                msg, money, is_private=True
            )
            if not verified:
                return

        # 创建并发送红包
        reply, _ = await asyncio.gather(
            msg.reply("正在准备专享红包，稍等"), msg.delete()
        )

        await _publish_red_envelope(
            msg,
            reply,
            money=money,
            members=1,
            first_name=first_name,
            sender_id=msg.from_user.id,
            envelope_type="private",
            photo_user=msg.reply_to_message.from_user,
            cover_name=f"{msg.reply_to_message.from_user.first_name} 专享",
            private=msg.reply_to_message.from_user.id,
            private_text=private_text,
            success_text=(
                f"🔥 [{msg.reply_to_message.from_user.first_name}]"
                f"(tg://user?id={msg.reply_to_message.from_user.id})\n"
                f"您收到一个来自 [{msg.from_user.first_name}]"
                f"(tg://user?id={msg.from_user.id}) 的专属红包"
            ),
        )
        return

    # 处理普通红包
    try:
        money = int(msg.command[1])
        members = int(msg.command[2])
    except (IndexError, ValueError):
        return await asyncio.gather(
            msg.delete(),
            sendMessage(
                msg,
                f"**🧧 发红包：\n\n/red [总{sakura_b}数] [份数] [mode] [祝福语（可选）]**\n\n"
                f"[mode]填 1 为拼手气, 其他值为均分\n[祝福语]不传则随机默认祝福语\n专享红包请回复 + {sakura_b}",
                timer=60,
            ),
        )

    if money < 5 or members < 1 or money < members:
        return await asyncio.gather(
            msg.delete(),
            sendMessage(msg, "红包金额至少为 5，份数必须大于 0 且不能多于金额。", timer=60),
        )

    # 验证发送者资格和红包参数
    verified, first_name, error = await verify_red_envelope_sender(msg, money)
    if not verified:
        return

    # 创建并发送红包
    mode_param = msg.command[3] if len(msg.command) > 3 else None
    envelope_type = "random" if (mode_param is None or str(mode_param) == "1") else "equal"
    private_text = msg.command[4] if len(msg.command) > 4 else None
    reply, _ = await asyncio.gather(msg.reply("正在准备红包，稍等"), msg.delete())

    await _publish_red_envelope(
        msg,
        reply,
        money=money,
        members=members,
        first_name=first_name,
        sender_id=msg.from_user.id,
        envelope_type=envelope_type,
        photo_user=msg.from_user,
        cover_name=first_name,
        private_text=private_text,
    )


@bot.on_callback_query(filters.regex("red_envelope") & user_in_group_on_filter)
async def grab_red_envelope(_, call):
    parts = call.data.split("-", 1)
    if len(parts) != 2 or not parts[1]:
        return await callAnswer(
            call, "/(ㄒoㄒ)/~~ \n\n来晚了，红包已经被抢光啦。", True
        )
    red_id = parts[1]
    envelope = sql_get_red_envelope(red_id)
    if not envelope:
        return await callAnswer(call, "红包记录暂时不可用，请稍后重试。", True)

    result = sql_claim_red_envelope(
        red_id, call.from_user.id, call.from_user.first_name or "Anonymous"
    )
    status = result["status"]
    if status == "already_claimed":
        await callAnswer(
            call, f"你已经领取过这个红包，获得 {result['amount']}{sakura_b}。", True
        )
        if result.get("completed") and result.get("envelope") and result.get("claims"):
            completed = _build_completed_red_envelope(
                result["envelope"], result["claims"]
            )
            await _show_completed_red_envelope(call, completed)
        return
    status_text = {
        "not_found": "红包不存在。",
        "closed": "红包已经被抢完或已退回。",
        "forbidden": "这是发给其他用户的专享红包。",
        "not_registered": "你还未私聊 bot，无法领取红包。",
        "balance_limit": "账户积分达到安全上限，暂时无法领取。",
        "invalid_state": "红包状态异常，未发放积分，请联系管理员。",
        "error": "领取暂时失败，积分未变动，请重试。",
    }
    if status != "ok":
        return await callAnswer(call, status_text.get(status, "领取暂时失败，请重试。"), True)

    amount = result["amount"]
    notice = f"🧧恭喜，你领取到了\n{envelope['sender_name']} の {amount}{sakura_b}"
    if envelope["envelope_type"] == "private":
        notice += f"\n\n{envelope['message']}"
    await callAnswer(call, notice, True)

    if result["completed"]:
        completed = _build_completed_red_envelope(
            result["envelope"], result["claims"]
        )
        await _show_completed_red_envelope(call, completed)


async def verify_red_envelope_sender(msg, money, is_private=False):
    """验证发红包者资格

    Args:
        msg: 消息对象
        money: 红包金额
        is_private: 是否为专享红包

    Returns:
        tuple: (验证是否通过, 发送者名称, 错误信息)
    """
    if not msg.sender_chat:
        e = sql_get_emby(tg=msg.from_user.id)
        conditions = [
            e,  # 用户存在
            e.iv >= money if e else False,  # 余额充足
            money >= 5,  # 红包金额不小于5
            e.iv >= 5 if e else False,  # 持有金额不小于5
        ]

        if is_private:
            # 专享红包额外检查 不能发给自己
            conditions.append(msg.reply_to_message.from_user.id != msg.from_user.id)
        else:
            # 普通红包额外检查
            conditions.append(money >= int(msg.command[2]))  # 金额不小于份数

        if not all(conditions):
            error_msg = (
                f"[{msg.from_user.first_name}](tg://user?id={msg.from_user.id}) "
                f"违反规则，禁言一分钟。\nⅰ 所持有{sakura_b}不得小于5\nⅱ 发出{sakura_b}不得小于5"
            )
            if is_private:
                error_msg += "\nⅲ 不许发自己"
            else:
                error_msg += "\nⅲ 未私聊过bot"

            await asyncio.gather(
                msg.delete(),
                msg.chat.restrict_member(
                    msg.from_user.id,
                    ChatPermissions(),
                    datetime.now() + timedelta(minutes=1),
                ),
                sendMessage(msg, error_msg, timer=60),
            )
            return False, None, error_msg

        # The sender balance is debited together with the durable pending record.
        return True, msg.from_user.first_name, None

    else:
        # 频道/群组发送
        first_name = msg.chat.title if msg.sender_chat.id == msg.chat.id else None
        if not first_name:
            return False, None, "无法获取发送者名称"
        return False, None, "红包必须由个人账号发起。"


async def get_user_photo(user):
    """获取用户头像"""
    if not user.photo:
        return None
    return await bot.download_media(
        user.photo.big_file_id,
        in_memory=True,
    )


async def generate_final_message(envelope):
    """生成红包领取完毕的消息"""
    if envelope.type == "private":
        receiver = envelope.receivers[envelope.target_user]
        return (
            f"🧧 {sakura_b}红包\n\n**{envelope.message}\n\n"
            f"🕶️{envelope.sender_name} **的专属红包已被 "
            f"[{receiver['name']}](tg://user?id={envelope.target_user}) 领取"
        )

    # 排序领取记录
    sorted_receivers = sorted(
        envelope.receivers.items(), key=lambda x: x[1]["amount"], reverse=True
    )
    envelope.message = envelope.message[:50] + "..." if len(envelope.message) > 53 else envelope.message

    text = (
        f"🧧 {sakura_b}红包\n\n**{envelope.message}\n\n"
        f"😎 {envelope.sender_name} **的红包已经被抢光啦~\n\n"
    )

    for i, (user_id, details) in enumerate(sorted_receivers):
        if i == 0:
            text += f"**🏆 手气最佳 [{details['name']}](tg://user?id={user_id}) **获得了 {details['amount']} {sakura_b}"
        else:
            text += f"\n**[{details['name']}](tg://user?id={user_id})** 获得了 {details['amount']} {sakura_b}"

    return text


@bot.on_message(filters.command("srank", prefixes) & user_in_group_on_filter & filters.group)
async def s_rank(_, msg):
    await msg.delete()
    sender = None
    should_charge = False
    if not msg.sender_chat:
        e = sql_get_emby(tg=msg.from_user.id)
        if judge_admins(msg.from_user.id):
            sender = msg.from_user.id
        elif _open.srank_cost < 0:
            return await sendMessage(msg, "排行榜积分设置无效，未扣除积分。", timer=60)
        elif not e or e.iv < _open.srank_cost:
            await msg.delete()
            try:
                await msg.chat.restrict_member(
                    msg.from_user.id,
                    ChatPermissions(),
                    datetime.now() + timedelta(minutes=1),
                )
                await sendMessage(
                    msg,
                    f"[{msg.from_user.first_name}]({msg.from_user.id}) "
                    f"未私聊过bot或不足支付手续费{_open.srank_cost}{sakura_b}，禁言一分钟。",
                    timer=60,
                )
            except Exception as e:
                print(e)
            return
        else:
            should_charge = _open.srank_cost > 0
            sender = msg.from_user.id
    elif msg.sender_chat.id == msg.chat.id:
        sender = msg.chat.id
    reply = await msg.reply("正在读取排行榜，读取成功后再结算积分。")
    try:
        text, i = await users_iv_rank()
        if not text:
            await reply.delete()
            return await sendMessage(msg, "暂时无法读取排行榜，未扣除积分，请稍后重试。", timer=60)
        t = text[0]
        button = await users_iv_button(i, 1, sender or msg.chat.id)
    except Exception as exc:
        LOGGER.warning(f"Point ranking generation failed: {type(exc).__name__}")
        await reply.delete()
        return await sendMessage(msg, "暂时无法生成排行榜，未扣除积分，请稍后重试。", timer=60)

    if should_charge and not sql_spend_emby_iv(msg.from_user.id, _open.srank_cost):
        await reply.delete()
        return await sendMessage(msg, "积分余额已变化或扣款失败，未发送排行榜。", timer=60)
    await asyncio.gather(
        reply.delete(),
        sendPhoto(
            msg,
            photo=bot_photo,
            caption=f"**▎🏆 {sakura_b}风云录**\n\n{t}",
            buttons=button,
        ),
    )


@cache.memoize(ttl=120)
async def users_iv_rank():
    with Session() as session:
        # 查询 Emby 表的所有数据，且>0 的条数
        p = session.query(func.count()).filter(Emby.iv > 0).scalar()
        if p == 0:
            return None, 1
        # 创建一个空字典来存储用户的 first_name 和 id
        members_dict = await get_users()
        i = math.ceil(p / 10)
        a = []
        b = 1
        m = ["🥇", "🥈", "🥉", "🏅"]
        # 分析出页数，将检索出 分割p（总数目）的 间隔，将间隔分段，放进【】中返回
        while b <= i:
            d = (b - 1) * 10
            # 查询iv排序，分页查询
            result = (
                session.query(Emby)
                .filter(Emby.iv > 0)
                .order_by(Emby.iv.desc())
                .limit(10)
                .offset(d)
                .all()
            )
            e = 1 if d == 0 else d + 1
            text = ""
            for q in result:
                name = str(members_dict.get(q.tg, q.tg))[:12]
                medal = m[e - 1] if e < 4 else m[3]
                text += f"{medal}**第{cn2an.an2cn(e)}名** | [{name}](google.com?q={q.tg}) の **{q.iv} {sakura_b}**\n"
                e += 1
            a.append(text)
            b += 1
        # a 是内容物，i是页数
        return a, i


# 检索翻页
@bot.on_callback_query(filters.regex("users_iv") & user_in_group_on_filter)
async def users_iv_pikb(_, call):
    # print(call.data)
    j, tg = map(int, call.data.split(":")[1].split("_"))
    if call.from_user.id != tg:
        if not judge_admins(call.from_user.id):
            return await callAnswer(
                call, "❌ 这不是你召唤出的榜单，请使用自己的 /srank", True
            )

    await callAnswer(call, f"将为您翻到第 {j} 页")
    a, b = await users_iv_rank()
    button = await users_iv_button(b, j, tg)
    text = a[j - 1]
    await editMessage(call, f"**▎🏆 {sakura_b}风云录**\n\n{text}", buttons=button)
