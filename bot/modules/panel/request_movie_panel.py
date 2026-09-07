from pyrogram import filters, enums
from bot import bot, moviepilot, bot_photo, LOGGER, sakura_b
from bot.func_helper.msg_utils import callAnswer, editMessage, sendMessage, sendPhoto, callListen
from bot.func_helper.filters import user_in_group_on_filter
from bot.func_helper.fix_bottons import re_download_center_ikb, back_members_ikb, continue_search_ikb, request_record_page_ikb,mp_search_page_ikb
from bot.sql_helper.sql_emby import sql_get_emby, sql_update_emby, Emby
from bot.sql_helper.sql_request_record import sql_add_request_record, sql_get_request_record_by_tg
from bot.func_helper.moviepilot import search, add_download_task 
from bot.func_helper.emby import emby
from bot.func_helper.utils import judge_admins
import asyncio
import math

# 添加全局字典来存储用户搜索记录
user_search_data = {}
ITEMS_PER_PAGE = 10


@bot.on_callback_query(filters.regex('download_center') & user_in_group_on_filter)
async def call_download_center(_, call):
    if not moviepilot.status:
        return await callAnswer(call, '❌ 管理员未开启点播功能', True)
    await callAnswer(call, '🔍 点播中心')
    await editMessage(call, '🔍 欢迎进入点播中心', buttons=re_download_center_ikb)


@bot.on_callback_query(filters.regex('get_resource') & user_in_group_on_filter)
async def download_media(_, call):
    if not moviepilot.status:
        return await callAnswer(call, '❌ 管理员未开启点播功能', True)

    emby_user = sql_get_emby(tg=call.from_user.id)
    if not emby_user:
        return await editMessage(call, '⚠️ 数据库没有你，请重新 /start录入')
    if emby_user.lv is None or emby_user.lv not in ['a', 'b']:
        return await editMessage(call, '🫡 您没有权限使用此功能', buttons=re_download_center_ikb)
    if not judge_admins(emby_user.tg) and moviepilot.lv == 'a' and emby_user.lv != 'a':
        return await editMessage(call, '🫡 您没有权限使用此功能，仅限白名单用户可用', buttons=re_download_center_ikb)

    await asyncio.gather(callAnswer(call, f'🔍 请输入你想求的资源名称'))
    await editMessage(call,
                      f"当前点播费用为: 1GB 消耗 {moviepilot.price} {sakura_b}\n"
                      f"您当前拥有 {emby_user.iv} {sakura_b}\n"
                      f"请在120s内对我发送你想点播的资源名称，\n退出点 /cancel")

    txt = await callListen(call, 120, buttons=re_download_center_ikb)
    if txt is False:
        return
    if txt.text == '/cancel':
        await asyncio.gather(txt.delete(), editMessage(call, '🔍 已取消操作', buttons=back_members_ikb))
        return

    # 记录用户的搜索文本
    user_search_data[call.from_user.id] = txt.text

    # 先查询emby库中是否存在
    await editMessage(call, '🔍 正在查询Emby库，请稍后...')
    emby_results = await emby.get_movies(title=txt.text)
    if emby_results:
        text = "🎯 Emby库中已存在以下相关资源:\n\n"
        for item in emby_results:
            text += f"• {item['title']} ({item['year']})\n"
        text += "\n是否仍要继续搜索站点资源?"
        await editMessage(call, text, buttons=continue_search_ikb)
        return
    # 如果Emby中没有，直接搜索站点资源
    await search_site_resources(call, txt.text)


@bot.on_callback_query(filters.regex('continue_search') & user_in_group_on_filter)
async def continue_search(_, call):
    await callAnswer(call, '🔍 继续搜索')
    # 使用之前保存的搜索文本
    search_text = user_search_data.get(call.from_user.id)
    if not search_text:
        await editMessage(call.message, '❌ 未找到搜索记录，请重新搜索', buttons=re_download_center_ikb)
        return
    await search_site_resources(call, search_text)


@bot.on_callback_query(filters.regex('cancel_search') & user_in_group_on_filter)
async def cancel_search(_, call):
    await callAnswer(call, '❌ 取消搜索')
    # 清除用户的搜索记录
    user_search_data.pop(call.from_user.id, None)
    await editMessage(call.message, '🔍 已取消搜索', buttons=re_download_center_ikb)
@bot.on_callback_query(filters.regex('cancel_download') & user_in_group_on_filter)
async def cancel_download(_, call):
    await callAnswer(call, '❌ 取消下载')
    user_search_data.pop(call.from_user.id, None)
    await editMessage(call.message, '🔍 已取消下载', buttons=re_download_center_ikb)

async def search_site_resources(call, keyword, page=1, all_result=None):
    """搜索站点资源并显示结果"""
    try:
        if page == 1:
            await editMessage(call.message, '🔍 正在搜索站点资源，请稍后...')
        if all_result is None:
            success, all_result = await search(keyword)
            if not success:
                await editMessage(call.message, '🤷‍♂️ 搜索站点资源失败，请稍后再试', buttons=re_download_center_ikb)
                return
        if all_result is None or len(all_result) == 0:
            await editMessage(call.message, '🤷‍♂️ 没有找到相关资源', buttons=re_download_center_ikb)
            return

        # 计算分页
        start_idx = (page - 1) * ITEMS_PER_PAGE
        end_idx = start_idx + ITEMS_PER_PAGE
        page_items = all_result[start_idx:end_idx]
        total_pages = math.ceil(len(all_result) / ITEMS_PER_PAGE)

        # 保存搜索结果到用户数据
        user_search_data[call.from_user.id] = {
            'keyword': keyword,
            'all_result': all_result,
            'current_page': page,
            'total_pages': total_pages
        }

        # 显示当前页的搜索结果
        for index, item in enumerate(page_items, start=start_idx + 1):
            text = format_resource_info(index, item)
            item['tg_log'] = text
            await sendMessage(call.message, text, send=True, chat_id=call.from_user.id)

        # 创建分页按钮
        keyboard = mp_search_page_ikb(page > 1, page < total_pages, page)
        pagination_text = f"第 {page}/{total_pages} 页 | 共 {len(all_result)} 个资源"
        await sendPhoto(
            call.message,
            photo=bot_photo,
            caption=f"请点击下载按钮选择下载，如果没有合适的资源，请翻页查询\n\n{pagination_text}", 
            send=True, 
            chat_id=call.from_user.id,
            buttons=keyboard
        )

    except Exception as e:
        LOGGER.error(f"搜索站点资源时出错: {str(e)}")
        await editMessage(call.message, '❌ 搜索过程中出错', buttons=re_download_center_ikb)


def format_resource_info(index, item):
    """格式化资源信息显示"""
    text = f"资源编号: `{index}`\n标题：{item['title']}"

    # 年份信息
    if item["year"]:
        text += f"\n年份：{item['year']}"

    # 类型信息
    type_info = item["type"] if item["type"] and item["type"] != "未知" else "电影"
    text += f"\n类型：{type_info}"

    # 大小信息
    if item["size"]:
        size_in_bytes = int(item["size"])
        size_in_gb = size_in_bytes / (1024 * 1024 * 1024)
        text += f"\n大小：{size_in_gb:.2f} GB"

    # 标签信息
    if item["labels"]:
        text += f"\n标签：{item['labels']}"

    # 资源组信息
    if item["seeders"]:
        text += f"\n种子数：{item['seeders']}"

    # 媒体信息
    media_info = []
    if item["resource_pix"]:
        media_info.append(item["resource_pix"])
    if item["video_encode"]:
        media_info.append(item["video_encode"])
    if item["audio_encode"]:
        media_info.append(item["audio_encode"])
    if media_info:
        text += f"\n媒体信息：{' | '.join(media_info)}"

    # 描述信息
    if item["description"]:
        text += f"\n描述：{item['description']}"

    return text


async def handle_resource_selection(call, result):
    while True:
        emby_user = sql_get_emby(tg=call.from_user.id)
        msg = await sendPhoto(call, photo=bot_photo, caption="【选择资源编号】：\n请在120s内对我发送你的资源编号，\n退出点 /cancel", send=True, chat_id=call.from_user.id)
        txt = await callListen(call, 120, buttons=re_download_center_ikb)
        if txt is False:
            user_search_data.pop(call.from_user.id, None)

            await asyncio.gather(editMessage(msg, '🔍 已取消操作', buttons=back_members_ikb))
            return
        elif txt.text == '/cancel':
            user_search_data.pop(call.from_user.id, None)
            await asyncio.gather(editMessage(msg, '🔍 已取消操作', buttons=back_members_ikb))
            return
        else:
            try:
                await editMessage(msg, '🔍 正在处理，请稍后')
                index = int(txt.text)
                size = result[index-1]['size'] / (1024 * 1024 * 1024)
                need_cost = math.ceil(size) * moviepilot.price
                if need_cost > emby_user.iv:
                    await editMessage(msg, f"❌ 您的{sakura_b}不足，此资源需要 {need_cost}{sakura_b}\n请选择其他资源编号", buttons=re_download_center_ikb)
                    continue
                torrent_info = result[index-1]['torrent_info']
                # 兼容mp v2的api，加入了torrent_in
                param = {**torrent_info, 'torrent_in': torrent_info}
                success, download_id = await add_download_task(param)
                user_search_data.pop(call.from_user.id, None)
                if success:
                    log = f"【下载任务】：#{call.from_user.id} [{call.from_user.first_name}](tg://user?id={call.from_user.id}) 已成功添加到下载队列，此次消耗 {need_cost}{sakura_b}\n下载ID：{download_id}"
                    download_log = f"{log}\n详情：{result[index-1]['tg_log']}"
                    LOGGER.info(log)
                    sql_update_emby(Emby.tg == call.from_user.id,
                                    iv=emby_user.iv - need_cost)
                    sql_add_request_record(
                        call.from_user.id, download_id, result[index-1]['title'], download_log, need_cost)
                    if moviepilot.download_log_chatid:
                        try:
                            await sendMessage(call, download_log, send=True, chat_id=moviepilot.download_log_chatid)
                        except Exception as e:
                            LOGGER.error(f"[MoviePilot] 发送下载日志通知到{moviepilot.download_log_chatid}失败: {str(e)}")
                    await editMessage(msg, f"🎉 已成功添加到下载队列，此次消耗 {need_cost}{sakura_b}\n🔖下载ID：`{download_id}`", buttons=re_download_center_ikb, parse_mode=enums.ParseMode.MARKDOWN)
                    return
                else:
                    LOGGER.error(f"【下载任务】：{call.from_user.id} 添加下载任务失败!")
                    await editMessage(msg, f"❌ 添加下载任务失败!", buttons=re_download_center_ikb)
                    return
            except IndexError:
                await editMessage(msg, '❌ 输入错误，请重新输入，退出点 /cancel', buttons=re_download_center_ikb)
                continue
            except ValueError:
                await editMessage(msg, '❌ 输入错误，请重新输入，退出点 /cancel', buttons=re_download_center_ikb)
                continue
            except:
                await editMessage(msg, '❌ 呜呜呜，出错了', buttons=re_download_center_ikb)
                return


user_data = {}


@bot.on_callback_query(filters.regex('download_rate') & user_in_group_on_filter)
async def call_rate(_, call):
    if not moviepilot.status:
        return await callAnswer(call, '❌ 管理员未开启点播功能', True)
    await callAnswer(call, '📈 查看点播下载任务')
    request_record, has_prev, has_next = sql_get_request_record_by_tg(
        call.from_user.id)
    if request_record is None:
        return await editMessage(call, '🤷‍♂️ 您还没有点播记录，快去点播吧', buttons=re_download_center_ikb)
    text = get_request_record_text(request_record)
    user_data[call.from_user.id] = {'request_record_page': 1}
    await editMessage(call, text, buttons=request_record_page_ikb(has_prev, has_next))


@bot.on_callback_query(filters.regex('request_record_prev') & user_in_group_on_filter)
async def request_record_prev(_, call):
    await callAnswer(call, '正在加载上一页')
    if user_data.get(call.from_user.id) is None:
        user_data[call.from_user.id] = {'request_record_page': 1}
    page = user_data[call.from_user.id]['request_record_page'] - 1
    if page <= 0:
        page = 1
    request_record, has_prev, has_next = sql_get_request_record_by_tg(
        call.from_user.id, page=page)
    user_data[call.from_user.id]['request_record_page'] = page
    text = get_request_record_text(request_record)
    await editMessage(call, text, buttons=request_record_page_ikb(has_prev, has_next))


@bot.on_callback_query(filters.regex('request_record_next') & user_in_group_on_filter)
async def request_record_next(_, call):
    await callAnswer(call, '正在加载下一页')
    if user_data.get(call.from_user.id) is None:
        user_data[call.from_user.id] = {'request_record_page': 1}
    page = user_data[call.from_user.id]['request_record_page'] + 1
    request_record, has_prev, has_next = sql_get_request_record_by_tg(
        call.from_user.id, page=page)
    user_data[call.from_user.id]['request_record_page'] = page
    text = get_request_record_text(request_record)
    await editMessage(call, text, buttons=request_record_page_ikb(has_prev, has_next))


def get_download_text(download_tasks, request_record):
    text = '📈 点播记录\n'
    for index, item in enumerate(request_record, start=1):
        for download_task in download_tasks:
            if download_task['download_id'] == item.download_id:
                progress = download_task['progress']
                progress_text = ''
                if progress is None:
                    progress_text = '未知'
                else:
                    progress = round(progress, 1)
                    left_progress = '🟩' * int(progress/10)
                    right_progress = '⬜️' * (10 - int(progress // 10))
                    progress_text = f"{left_progress}{right_progress} {progress}%"
                text += f"「{index}」：{item.request_name} \n"
                text += f"状态：{'正在下载' if download_task['state'] == 'downloading' else ''} {progress_text}\n"
                text += f"剩余时间：{download_task['left_time']}\n"
                break
        else:
            left_progress = '🟩' * 10
            progress_text = f"{left_progress} 100%"
            text += f"「{index}」：{item.request_name} \n状态：已完成 {progress_text}\n"
    return text
def get_request_record_text(request_record):
    text = '📈 点播记录\n'
    for index, item in enumerate(request_record, start=1):
        progress = item.progress
        progress_text = ''
        if item.transfer_state is not None:
            if item.transfer_state:
                text += f"「{index}」：{item.request_name} \n状态：已入库 📽️\n"
            else:
                text += f"「{index}」：{item.request_name} \n状态：入库失败 🚫\n"
        else:
            if progress is None:
                progress_text = '未知'
            else:
                progress = round(progress, 1)
                left_progress = '🟩' * int(progress/10)
                right_progress = '⬜️' * (10 - int(progress // 10))
                progress_text = f"{left_progress}{right_progress} {progress}%"
            download_state_text = '正在排队'
            if item.download_state == 'downloading':
                download_state_text = '正在下载'
            elif item.download_state == 'completed':
                download_state_text = '已完成'
            elif item.download_state == 'failed':
                download_state_text = '下载失败'
            text += f"「{index}」：{item.request_name} \n状态：{download_state_text} {progress_text}\n 剩余时间：{item.left_time}\n"
    return text

# 添加新的回调处理函数
@bot.on_callback_query(filters.regex('^mp_search_prev_page$') & user_in_group_on_filter)
async def handle_prev_page(_, call):
    user_data = user_search_data.get(call.from_user.id)
    if not user_data:
        return await callAnswer(call, '❌ 搜索会话已过期，请重新搜索', True)
    
    new_page = user_data['current_page'] - 1
    await callAnswer(call, f'📃 正在加载第 {new_page} 页')
    all_result = user_data['all_result']
    keyword = user_data['keyword']
    await search_site_resources(call, keyword, new_page, all_result)

@bot.on_callback_query(filters.regex('^mp_search_next_page$') & user_in_group_on_filter)
async def handle_next_page(_, call):
    user_data = user_search_data.get(call.from_user.id)
    if not user_data:
        return await callAnswer(call, '❌ 搜索会话已过期，请重新搜索', True)
    
    new_page = user_data['current_page'] + 1
    await callAnswer(call, f'📃 正在加载第 {new_page} 页')
    all_result = user_data['all_result']
    keyword = user_data['keyword']
    await search_site_resources(call, keyword, new_page, all_result)

@bot.on_callback_query(filters.regex('^mp_search_select_download$') & user_in_group_on_filter)
async def handle_select_download(_, call):
    user_data = user_search_data.get(call.from_user.id)
    if not user_data:
        return await callAnswer(call, '❌ 搜索会话已过期，请重新搜索', True)
    
    await callAnswer(call, '💾 进入资源选择')
    await handle_resource_selection(call, user_data['all_result'])
