from bot.func_helper.emby import emby
from pyrogram import filters
from bot import bot, bot_name, LOGGER
from bot.func_helper.filters import admins_on_filter
from bot.func_helper.msg_utils import editMessage
from bot.func_helper.fix_bottons import whitelist_page_ikb, normaluser_page_ikb,devices_page_ikb 
from bot.sql_helper.sql_emby import get_all_emby, Emby
from bot.func_helper.msg_utils import callAnswer
import math
import asyncio
import time
from datetime import datetime
from sqlalchemy import and_


# Telegram usernames are cosmetic in these lists. Keep a short-lived cache so
# opening another page does not issue a request for every row, while allowing
# username changes to become visible without restarting the bot.
_TG_USERNAME_CACHE_TTL = 300
_tg_username_cache = {}


async def _telegram_username(tg_id):
    """Return a displayable Telegram username, or a safe fallback."""
    try:
        tg_id = int(tg_id)
    except (TypeError, ValueError):
        return "未设置"

    now = time.monotonic()
    cached = _tg_username_cache.get(tg_id)
    if cached and cached[0] > now:
        return cached[1]

    try:
        user = await asyncio.wait_for(bot.get_users(tg_id), timeout=3)
        # Pyrogram returns a User for a scalar id. Accept a one-item sequence
        # as well so test doubles and compatible clients remain harmless.
        if isinstance(user, (list, tuple)):
            user = user[0] if user else None
        username = getattr(user, "username", None)
        if isinstance(username, str):
            username = username.strip().lstrip("@")
        if not username:
            username = "未设置"
    except Exception:
        # A Telegram lookup must never prevent an administrator from seeing
        # the account list (for example during a temporary API outage).
        username = "未设置"

    _tg_username_cache[tg_id] = (now + _TG_USERNAME_CACHE_TTL, username)
    return username


async def _telegram_usernames(users):
    """Resolve unique TG ids concurrently while preserving no list ordering."""
    ids = []
    seen = set()
    for user in users:
        try:
            tg_id = int(user.tg)
        except (TypeError, ValueError, AttributeError):
            continue
        if tg_id not in seen:
            seen.add(tg_id)
            ids.append(tg_id)
    values = await asyncio.gather(*(_telegram_username(tg_id) for tg_id in ids))
    return dict(zip(ids, values))


def _active_whitelist_filter():
    return and_(
        Emby.lv == 'a',
        Emby.ex.isnot(None),
        Emby.ex > datetime.now(),
    )


async def _load_users(call, condition, label):
    """Load a list for the admin panel and surface database failures."""
    try:
        users = get_all_emby(condition)
    except Exception:
        LOGGER.exception("用户列表查询失败: %s", label)
        await callAnswer(call, f"⚠️ {label}加载失败，请稍后重试", True)
        return None
    if users is None:
        # get_all_emby historically returns None after swallowing a database
        # exception, so keep this guard even though the query normally raises.
        LOGGER.error("用户列表查询返回空结果: %s", label)
        await callAnswer(call, f"⚠️ {label}加载失败，请检查数据库连接", True)
        return None
    return list(users)


def _page_number(value, total_pages):
    """Clamp callback page values so stale buttons cannot break the panel."""
    try:
        page = int(value)
    except (TypeError, ValueError):
        return 1
    if total_pages <= 0:
        return 1
    return max(1, min(page, total_pages))

@bot.on_callback_query(filters.regex('^whitelist$') & admins_on_filter)
async def list_whitelist(_, call):
    await callAnswer(call, '🔍 白名单用户列表')
    page = 1
    whitelist_users = await _load_users(call, _active_whitelist_filter(), '白名单列表')
    if whitelist_users is None:
        return
    total_users = len(whitelist_users)
    total_pages = max(1, math.ceil(total_users / 20))

    text = await create_whitelist_text(whitelist_users, page)
    keyboard = await whitelist_page_ikb(total_pages, page)

    await editMessage(call, text, buttons=keyboard)
@bot.on_callback_query(filters.regex('^normaluser$') & admins_on_filter)
async def list_normaluser(_, call):
    await callAnswer(call, '🔍 普通用户列表')
    page = 1
    normal_users = await _load_users(call, Emby.lv == 'b', '普通用户列表')
    if normal_users is None:
        return
    total_users = len(normal_users)
    total_pages = max(1, math.ceil(total_users / 20))

    text = await create_normaluser_text(normal_users, page)
    keyboard = await normaluser_page_ikb(total_pages, page)
    await editMessage(call, text, buttons=keyboard)


@bot.on_callback_query(filters.regex('^whitelist:') & admins_on_filter)
async def whitelist_page(_, call):
    requested_page = call.data.split(':', 1)[1]
    whitelist_users = await _load_users(call, _active_whitelist_filter(), '白名单列表')
    if whitelist_users is None:
        return
    total_users = len(whitelist_users)
    total_pages = max(1, math.ceil(total_users / 20))
    page = _page_number(requested_page, total_pages)
    await callAnswer(call, f'🔍 打开第{page}页')

    text = await create_whitelist_text(whitelist_users, page)
    keyboard = await whitelist_page_ikb(total_pages, page)

    await editMessage(call, text, buttons=keyboard)

@bot.on_callback_query(filters.regex('^normaluser:') & admins_on_filter)
async def normaluser_page(_, call):
    requested_page = call.data.split(':', 1)[1]
    normal_users = await _load_users(call, Emby.lv == 'b', '普通用户列表')
    if normal_users is None:
        return
    total_users = len(normal_users)
    total_pages = max(1, math.ceil(total_users / 20))
    page = _page_number(requested_page, total_pages)
    await callAnswer(call, f'🔍 打开第{page}页')

    text = await create_normaluser_text(normal_users, page)
    keyboard = await normaluser_page_ikb(total_pages, page)

    await editMessage(call, text, buttons=keyboard)

async def create_whitelist_text(users, page):
    start = (page - 1) * 20
    end = start + 20
    text = "**白名单用户列表**\n\n"
    page_users = users[start:end]
    if not page_users:
        text += "暂无白名单用户。\n"
    usernames = await _telegram_usernames(page_users)
    for user in page_users:
        expires = user.ex.strftime('%Y-%m-%d %H:%M:%S') if user.ex else '未设置'
        tg_username = usernames.get(int(user.tg), "未设置")
        text += f"TGID: `{user.tg}` | TG用户名: `{tg_username}` | Emby用户名: [{user.name}](tg://user?id={user.tg}) | 到期: `{expires}`\n"
    text += f"第 {page} 页,共 {max(1, math.ceil(len(users) / 20))} 页, 共 {len(users)} 人"
    return text

async def create_normaluser_text(users, page):
    start = (page - 1) * 20
    end = start + 20
    text = "**普通用户列表**\n\n"
    page_users = users[start:end]
    if not page_users:
        text += "暂无普通用户。\n"
    usernames = await _telegram_usernames(page_users)
    for user in page_users:
        expires = user.ex.strftime('%Y-%m-%d %H:%M:%S') if user.ex else '未设置'
        tg_username = usernames.get(int(user.tg), "未设置")
        text += f"TGID: `{user.tg}` | TG用户名: `{tg_username}` | Emby用户名: [{user.name}](tg://user?id={user.tg}) | 到期: `{expires}`\n"
    text += f"第 {page} 页,共 {max(1, math.ceil(len(users) / 20))} 页, 共 {len(users)} 人"
    return text

@bot.on_callback_query(filters.regex('^user_devices$|^devices:') & admins_on_filter)
async def user_devices(_, call):
    # 获取页码
    if call.data == 'user_devices':
        page = 1
        await callAnswer(call, '🔍 用户设备列表')
    else:
        page = int(call.data.split(':')[1])
        await callAnswer(call, f'🔍 打开第{page}页')

    page_size = 20
    # 计算offset
    offset = (page - 1) * page_size
    
    # 获取用户设备信息
    success, result, has_prev, has_next = await emby.get_emby_user_devices(offset=offset, limit=page_size)
    if not success:
        return await callAnswer(call, '🤕 Emby 服务器连接失败!')

    text = '**💠 用户设备列表**\n\n'
    for name, device_count, ip_count in result:
        text += f'用户名: [{name}](https://t.me/{bot_name}?start=userip-{name}) | 设备: {device_count} | IP: {ip_count}\n'
    text += f"\n第 {page} 页"
    await editMessage(call, text, buttons=devices_page_ikb(has_prev, has_next, page))


@bot.on_callback_query(filters.regex(r'^devices_page_info$') & admins_on_filter)
async def devices_page_info(_, call):
    """Answer taps on the non-actionable device-list page indicator."""
    await callAnswer(call, '当前页不可点击')
