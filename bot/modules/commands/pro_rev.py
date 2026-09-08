"""
对用户的等级调整
使得其能够成为管理员
或者白名单；白名单权限跟随订阅到期时间.
"""
import random
import asyncio
from pyrogram import filters
from pyrogram.errors import BadRequest

from bot import bot, prefixes, owner, admins, save_config, LOGGER
from bot.func_helper.filters import admins_on_filter
from bot.func_helper.utils import is_subscription_active
from bot.func_helper.msg_utils import sendMessage, deleteMessage
from bot.schemas import Yulv
from bot.scheduler.bot_commands import BotCommands
from bot.sql_helper.sql_emby import sql_update_emby, Emby, sql_get_emby
from bot.sql_helper.sql_emby2 import sql_get_emby2, sql_update_emby2, Emby2


# 新增管理名单
@bot.on_message(filters.command('proadmin', prefixes=prefixes) & filters.user(owner))
async def pro_admin(_, msg):
    if msg.reply_to_message is None:
        try:
            uid = int(msg.text.split()[1])
            first = await bot.get_chat(uid)
        except (IndexError, KeyError, BadRequest):
            await deleteMessage(msg)
            return await sendMessage(msg,
                                     '**请先给我一个正确的id！**\n输入格式为：/proadmin [tgid]或**命令回复想要授权的人**',
                                     timer=60)
    else:
        uid = msg.reply_to_message.from_user.id
        first = await bot.get_chat(uid)
    if uid not in admins:
        admins.append(uid)
        save_config()

    await asyncio.gather(deleteMessage(msg), BotCommands.pro_commands(_, uid),
                         sendMessage(msg,
                                     f'**{random.choice(Yulv.load_yulv().wh_msg)}**\n\n'
                                     f'👮🏻 新更新管理员 #[{first.first_name}](tg://user?id={uid}) | `{uid}`\n**当前admins**\n{admins}',
                                     timer=60))

    LOGGER.info(f"【admin】：{msg.from_user.id} 新更新 管理 {first.first_name}-{uid}")


# 增加白名单
@bot.on_message(filters.command('prouser', prefixes=prefixes) & admins_on_filter)
async def pro_user(_, msg):
    if msg.reply_to_message is None:
        try:
            param = msg.text.split()[1]
            # 尝试解析为整数（tgid）
            try:
                uid = int(param)
                first = await bot.get_chat(uid)
                query_by_username = False
            except ValueError:
                # 如果不是整数，则视为用户名
                uid = None
                username = param
                query_by_username = True
        except (IndexError, KeyError, BadRequest):
            await deleteMessage(msg)
            return await sendMessage(msg,
                                     '**请先给我一个正确的id或用户名！**\n输入格式为：/prouser [tgid/username]或**命令回复想要授权的人**',
                                     timer=60)
    else:
        uid = msg.reply_to_message.from_user.id
        first = await bot.get_chat(uid)
        query_by_username = False
    
    if query_by_username:
        # 通过用户名查询emby表
        e = sql_get_emby(tg=username)
        # 同时查询emby2表
        e2 = sql_get_emby2(name=username)
        
        if e is None and e2 is None:
            return await sendMessage(msg, f'用户名 `{username}` 在数据库中不存在！')
        
        result_msg = f"**{random.choice(Yulv.load_yulv().wh_msg)}**\n\n"
        sign_name = f'{msg.sender_chat.title}' if msg.sender_chat else f'[{msg.from_user.first_name}](tg://user?id={msg.from_user.id})'
        
        # 更新emby表
        if e is not None and e.embyid is not None:
            if not is_subscription_active(e.ex):
                result_msg += "⚠️ Emby账户没有有效订阅，无法授予限时白名单；请先续期。\n"
            elif sql_update_emby(Emby.name == username, lv='a'):
                user_display = f'[{e.name}](tg://user?id={e.tg})' if e.tg else e.name
                result_msg += f"🎉 恭喜：{user_display} 获得 {sign_name} 签出的白名单.\n"
            else:
                result_msg += "⚠️ 错误：数据库执行错误\n"
        
        # 更新emby2表
        if e2 is not None:
            if not is_subscription_active(e2.ex):
                result_msg += "⚠️ Emby账户没有有效订阅，无法授予限时白名单；请先续期。\n"
            elif sql_update_emby2(Emby2.name == username, lv='a'):
                result_msg += f"🎉 恭喜 {e2.name} 获得 {sign_name} 签出的白名单.\n"
            else:
                result_msg += "⚠️ 错误：数据库执行错误\n"
        
        await asyncio.gather(deleteMessage(msg), sendMessage(msg, result_msg))
        LOGGER.info(f"【admin】：{msg.from_user.id} 新增 白名单（用户名） {username}")
    else:
        # 通过tgid查询
        e = sql_get_emby(tg=uid)
        if e is None or e.embyid is None:
            return await sendMessage(msg, f'[ta](tg://user?id={uid}) 还没有emby账户无法操作！请先注册')
        if not is_subscription_active(e.ex):
            return await sendMessage(msg, '⏳ 该账户没有有效订阅，无法授予限时白名单；请先续期。')
        if sql_update_emby(Emby.tg == uid, lv='a'):
            sign_name = f'{msg.sender_chat.title}' if msg.sender_chat else f'[{msg.from_user.first_name}](tg://user?id={msg.from_user.id})'
            await asyncio.gather(deleteMessage(msg), sendMessage(msg,
                                                                 f"**{random.choice(Yulv.load_yulv().wh_msg)}**\n\n"
                                                                 f"🎉 恭喜 [{first.first_name}](tg://user?id={uid}) 获得 {sign_name} 签出的白名单."))
        else:
            return await sendMessage(msg, '⚠️ 数据库执行错误')
        LOGGER.info(f"【admin】：{msg.from_user.id} 新增 白名单 {first.first_name}-{uid}")


# 减少管理
@bot.on_message(filters.command('revadmin', prefixes=prefixes) & filters.user(owner))
async def del_admin(_, msg):
    if msg.reply_to_message is None:
        try:
            uid = int(msg.text.split()[1])
            first = await bot.get_chat(uid)
        except (IndexError, KeyError, BadRequest):
            await deleteMessage(msg)
            return await sendMessage(msg,
                                     '**请先给我一个正确的id！**\n输入格式为：/revadmin [tgid]或**命令回复想要取消授权的人**',
                                     timer=60)

    else:
        uid = msg.reply_to_message.from_user.id
        first = await bot.get_chat(uid)
    if uid in admins:
        admins.remove(uid)
        save_config()
    await asyncio.gather(deleteMessage(msg), BotCommands.rev_commands(_, uid),
                         sendMessage(msg,
                                     f'👮🏻 已减少管理员 #[{first.first_name}](tg://user?id={uid}) | `{uid}`\n**当前admins**\n{admins}'))
    LOGGER.info(f"【admin】：{msg.from_user.id} 新减少 管理 {first.first_name}-{uid}")


# 减少白名单
@bot.on_message(filters.command('revuser', prefixes=prefixes) & admins_on_filter)
async def rev_user(_, msg):
    if msg.reply_to_message is None:
        try:
            param = msg.text.split()[1]
            # 尝试解析为整数（tgid）
            try:
                uid = int(param)
                first = await bot.get_chat(uid)
                query_by_username = False
            except ValueError:
                # 如果不是整数，则视为用户名
                uid = None
                username = param
                query_by_username = True
        except (IndexError, KeyError, BadRequest):
            await deleteMessage(msg)
            return await msg.reply(
                '**请先给我一个正确的id或用户名！**\n输入格式为：/revuser [tgid/username]或**命令回复想要取消授权的人**')

    else:
        uid = msg.reply_to_message.from_user.id
        first = await bot.get_chat(uid)
        query_by_username = False
    
    if query_by_username:
        # 通过用户名查询emby表
        e = sql_get_emby(tg=username)
        # 同时查询emby2表
        e2 = sql_get_emby2(name=username)
        
        if e is None and e2 is None:
            return await sendMessage(msg, f'用户名 `{username}` 在数据库中不存在！')
        
        result_msg = ""
        sign_name = f'{msg.sender_chat.title}' if msg.sender_chat else f'[{msg.from_user.first_name}](tg://user?id={msg.from_user.id})'
        
        # 更新emby表
        if e is not None:
            if sql_update_emby(Emby.name == username, lv='b'):
                user_display = f'[{e.name}](tg://user?id={e.tg})' if e.tg else e.name
                result_msg += f"🤖 很遗憾 {user_display} 被 {sign_name} 移出白名单.\n"
            else:
                result_msg += "⚠️ 错误：数据库执行错误\n"
        
        # 更新emby2表
        if e2 is not None:
            if sql_update_emby2(Emby2.name == username, lv='b'):
                result_msg += f"🤖  很遗憾 {e2.name} 被 {sign_name} 移出白名单.\n"
            else:
                result_msg += "⚠️ 错误：数据库执行错误\n"
        
        await asyncio.gather(sendMessage(msg, result_msg), deleteMessage(msg))
        LOGGER.info(f"【admin】：{msg.from_user.id} 移除 白名单（用户名） {username}")
    else:
        # 通过tgid查询
        if sql_update_emby(Emby.tg == uid, lv='b'):
            sign_name = f'{msg.sender_chat.title}' if msg.sender_chat else f'[{msg.from_user.first_name}](tg://user?id={msg.from_user.id})'
            await asyncio.gather(sendMessage(msg,
                                             f"🤖 很遗憾 [{first.first_name}](tg://user?id={uid}) 被 {sign_name} 移出白名单."),
                                 deleteMessage(msg))
        else:
            return await sendMessage(msg, '⚠️ 数据库执行错误')
        LOGGER.info(f"【admin】：{msg.from_user.id} 新移除 白名单 {first.first_name}-{uid}")
