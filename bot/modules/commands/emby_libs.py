import time

from pyrogram import filters

from bot import bot, owner, prefixes, extra_emby_libs, LOGGER, Now
from bot.func_helper.msg_utils import sendMessage, deleteMessage
from bot.sql_helper.sql_emby import get_all_emby, Emby
from bot.func_helper.emby import emby

# embylibs_block
@bot.on_message(filters.command('embylibs_blockall', prefixes) & filters.user(owner))
async def embylibs_blockall(_, msg):
    await deleteMessage(msg)
    reply = await msg.reply(f"🍓 正在处理ing····, 正在更新所有用户的媒体库访问权限")
    rst = get_all_emby(Emby.embyid.isnot(None))
    if rst is None:
        LOGGER.info(
            f"【关闭媒体库任务】 -{msg.from_user.first_name}({msg.from_user.id}) 没有检测到任何emby账户，结束")
        return await reply.edit("⚡【关闭媒体库任务】\n\n结束，没有一个有号的")
    allcount = 0
    successcount = 0
    start = time.perf_counter()
    text = ''
    for i in rst:
        if i.embyid:
            allcount += 1
            try:
                # 使用封装的禁用所有媒体库方法
                re = await emby.disable_all_folders_for_user(i.embyid)
                if re is True:
                    successcount += 1
                    text += f'已关闭了 [{i.name}](tg://user?id={i.tg}) 的媒体库权限\n'
                else:
                    text += f'🌧️ 关闭失败 [{i.name}](tg://user?id={i.tg}) 的媒体库权限\n'
            except Exception as e:
                LOGGER.error(f"关闭媒体库权限失败: {i.name} - {str(e)}")
                text += f'🌧️ 关闭失败 [{i.name}](tg://user?id={i.tg}) 的媒体库权限\n'
    # 防止触发 MESSAGE_TOO_LONG 异常
    n = 1000
    chunks = [text[i:i + n] for i in range(0, len(text), n)]
    for c in chunks:
        await msg.reply(c + f'\n**{Now.strftime("%Y-%m-%d %H:%M:%S")}**')
    end = time.perf_counter()
    times = end - start
    if allcount != 0:
        await sendMessage(msg,
                          text=f"⚡#关闭媒体库任务 done\n  共检索出 {allcount} 个账户，成功关闭 {successcount}个，耗时：{times:.3f}s")
    else:
        await sendMessage(msg, text=f"**#关闭媒体库任务 结束！搞毛，没有人被干掉。**")
    LOGGER.info(
        f"【关闭媒体库任务结束】 - {msg.from_user.id} 共检索出 {allcount} 个账户，成功关闭 {successcount}个，耗时：{times:.3f}s")

# embylibs_unblock
@bot.on_message(filters.command('embylibs_unblockall', prefixes) & filters.user(owner))
async def embylibs_unblockall(_, msg):
    await deleteMessage(msg)
    reply = await msg.reply(f"🍓 正在处理ing····, 正在更新所有用户的媒体库访问权限")
    rst = get_all_emby(Emby.embyid.isnot(None))
    if rst is None:
        LOGGER.info(
            f"【开启媒体库任务】 -{msg.from_user.first_name}({msg.from_user.id}) 没有检测到任何emby账户，结束")
        return await reply.edit("⚡【开启媒体库任务】\n\n结束，没有一个有号的")
    allcount = 0
    successcount = 0
    start = time.perf_counter()
    text = ''
    for i in rst:
        if i.embyid:
            allcount += 1
            try:
                # 使用封装的启用所有媒体库方法
                re = await emby.enable_all_folders_for_user(i.embyid)
                if re is True:
                    successcount += 1
                    text += f'已开启了 [{i.name}](tg://user?id={i.tg}) 的媒体库权限\n'
                else:
                    text += f'🌧️ 开启失败 [{i.name}](tg://user?id={i.tg}) 的媒体库权限\n'
            except Exception as e:
                LOGGER.error(f"开启媒体库权限失败: {i.name} - {str(e)}")
                text += f'🌧️ 开启失败 [{i.name}](tg://user?id={i.tg}) 的媒体库权限\n'
    # 防止触发 MESSAGE_TOO_LONG 异常
    n = 1000
    chunks = [text[i:i + n] for i in range(0, len(text), n)]
    for c in chunks:
        await msg.reply(c + f'\n**{Now.strftime("%Y-%m-%d %H:%M:%S")}**')
    end = time.perf_counter()
    times = end - start
    if allcount != 0:
        await sendMessage(msg,
                          text=f"⚡#开启媒体库任务 done\n  共检索出 {allcount} 个账户，成功开启 {successcount}个，耗时：{times:.3f}s")
    else:
        await sendMessage(msg, text=f"**#开启媒体库任务 结束！搞毛，没有人被干掉。**")
    LOGGER.info(
        f"【开启媒体库任务结束】 - {msg.from_user.id} 共检索出 {allcount} 个账户，成功开启 {successcount}个，耗时：{times:.3f}s")

@bot.on_message(filters.command('extraembylibs_blockall', prefixes) & filters.user(owner))
async def extraembylibs_blockall(_, msg):
    await deleteMessage(msg)
    reply = await msg.reply(f"🍓 正在处理ing····, 正在更新所有用户的额外媒体库访问权限")

    rst = get_all_emby(Emby.embyid.isnot(None))
    if rst is None:
        LOGGER.info(
            f"【关闭额外媒体库任务】 -{msg.from_user.first_name}({msg.from_user.id}) 没有检测到任何emby账户，结束")
        return await reply.edit("⚡【关闭额外媒体库任务】\n\n结束，没有一个有号的")

    allcount = 0
    successcount = 0
    start = time.perf_counter()
    text = ''
    for i in rst:
        if i.embyid:
            allcount += 1
            try:
                # 使用封装的隐藏额外媒体库方法
                re = await emby.hide_folders_by_names(i.embyid, extra_emby_libs or [])
                if re is True:
                    successcount += 1
                    text += f'已关闭了 [{i.name}](tg://user?id={i.tg}) 的额外媒体库权限\n'
                else:
                    text += f'🌧️ 关闭失败 [{i.name}](tg://user?id={i.tg}) 的额外媒体库权限\n'
            except Exception as e:
                LOGGER.error(f"关闭额外媒体库权限失败: {i.name} - {str(e)}")
                text += f'🌧️ 关闭失败 [{i.name}](tg://user?id={i.tg}) 的额外媒体库权限\n'
    # 防止触发 MESSAGE_TOO_LONG 异常
    n = 1000
    chunks = [text[i:i + n] for i in range(0, len(text), n)]
    for c in chunks:
        await msg.reply(c + f'\n**{Now.strftime("%Y-%m-%d %H:%M:%S")}**')
    end = time.perf_counter()
    times = end - start
    if allcount != 0:
        await sendMessage(msg,
                          text=f"⚡#关闭额外媒体库任务 done\n  共检索出 {allcount} 个账户，成功关闭 {successcount}个，耗时：{times:.3f}s")
    else:
        await sendMessage(msg, text=f"**#关闭额外媒体库任务 结束！搞毛，没有人被干掉。**")
    LOGGER.info(
        f"【关闭额外媒体库任务结束】 - {msg.from_user.id} 共检索出 {allcount} 个账户，成功关闭 {successcount}个，耗时：{times:.3f}s")


@bot.on_message(filters.command('extraembylibs_unblockall', prefixes) & filters.user(owner))
async def extraembylibs_unblockall(_, msg):
    await deleteMessage(msg)
    reply = await msg.reply(f"🍓 正在处理ing····, 正在更新所有用户的额外媒体库访问权限")

    rst = get_all_emby(Emby.embyid.isnot(None))
    if rst is None:
        LOGGER.info(
            f"【开启额外媒体库任务】 -{msg.from_user.first_name}({msg.from_user.id}) 没有检测到任何emby账户，结束")
        return await reply.edit("⚡【开启额外媒体库任务】\n\n结束，没有一个有号的")

    allcount = 0
    successcount = 0
    start = time.perf_counter()
    text = ''
    for i in rst:
        if i.embyid:
            allcount += 1
            try:
                # 使用封装的显示额外媒体库方法
                re = await emby.show_folders_by_names(i.embyid, extra_emby_libs or [])
                if re is True:
                    successcount += 1
                    text += f'已开启了 [{i.name}](tg://user?id={i.tg}) 的额外媒体库权限\n'
                else:
                    text += f'🌧️ 开启失败 [{i.name}](tg://user?id={i.tg}) 的额外媒体库权限\n'
            except Exception as e:
                LOGGER.error(f"开启额外媒体库权限失败: {i.name} - {str(e)}")
                text += f'🌧️ 开启失败 [{i.name}](tg://user?id={i.tg}) 的额外媒体库权限\n'
    # 防止触发 MESSAGE_TOO_LONG 异常
    n = 1000
    chunks = [text[i:i + n] for i in range(0, len(text), n)]
    for c in chunks:
        await msg.reply(c + f'\n**{Now.strftime("%Y-%m-%d %H:%M:%S")}**')
    end = time.perf_counter()
    times = end - start
    if allcount != 0:
        await sendMessage(msg,
                          text=f"⚡#开启额外媒体库任务 done\n  共检索出 {allcount} 个账户，成功开启 {successcount}个，耗时：{times:.3f}s")
    else:
        await sendMessage(msg, text=f"**#开启额外媒体库任务 结束！搞毛，没有人被干掉。**")
    LOGGER.info(
        f"【开启额外媒体库任务结束】 - {msg.from_user.id} 共检索出 {allcount} 个账户，成功开启 {successcount}个，耗时：{times:.3f}s")
