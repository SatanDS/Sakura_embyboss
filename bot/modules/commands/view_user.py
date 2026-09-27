from bot.func_helper.emby import emby
from pyrogram import filters, enums
from pyrogram.errors import FloodWait, MessageNotModified
from html import escape
from bot import bot, bot_name, LOGGER
from bot.func_helper.filters import admins_on_filter
from bot.func_helper.msg_utils import editMessage
from bot.func_helper.fix_bottons import whitelist_page_ikb, normaluser_page_ikb,devices_page_ikb 
from bot.sql_helper.sql_emby import get_all_emby, Emby
from bot.func_helper.msg_utils import callAnswer
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
        LOGGER.exception("用户列表查询失败: {}", label)
        await callAnswer(call, f"⚠️ {label}加载失败，请稍后重试", True)
        return None
    if users is None:
        # get_all_emby historically returns None after swallowing a database
        # exception. Do not mistake that sentinel for an empty user list.
        LOGGER.error("用户列表查询返回空结果: {}", label)
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


_USER_LIST_PAGE_SIZE = 20
_USER_LIST_PAGE_BUDGET = 3800  # Leave room for the title and page counters.


def _user_tg_id(user):
    try:
        value = int(getattr(user, 'tg', None))
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def _user_expiry(user):
    expiry = getattr(user, 'ex', None)
    return expiry.strftime('%Y-%m-%d %H:%M:%S') if expiry else '未设置'


def _user_row(user, username):
    # Account names are user input: HTML escaping preserves names containing
    # Markdown or HTML without breaking Telegram's entity parser.
    tg_id = _user_tg_id(user)
    tg_value = escape(str(getattr(user, 'tg', None) or '未设置'))
    name = escape(str(getattr(user, 'name', None) or '未设置'))
    link = f'<a href="tg://user?id={tg_id}">{name}</a>' if tg_id else name
    return (f'TGID: <code>{tg_value}</code> | TG用户名: <code>{escape(username)}</code>'
            f' | Emby用户名: {link} | 到期: <code>{_user_expiry(user)}</code>\n')


def _user_pages(users):
    """Keep every account while respecting the text limit, even for long names."""
    pages = [[]]
    size = 0
    for user in users:
        # A Telegram username is at most 32 ASCII characters. Reserve that
        # space before lookups so pagination stays stable across cache changes.
        row_size = len(_user_row(user, 'x' * 32).encode('utf-16-le')) // 2
        if pages[-1] and (len(pages[-1]) >= _USER_LIST_PAGE_SIZE or
                          size + row_size > _USER_LIST_PAGE_BUDGET):
            pages.append([])
            size = 0
        pages[-1].append(user)
        size += row_size
    return pages


async def _edit_user_list(call, text, keyboard, label):
    """Open a text panel from media; later pages edit that same text panel."""
    try:
        message = call.message
        options = dict(text=text, parse_mode=enums.ParseMode.HTML,
                       disable_web_page_preview=True, reply_markup=keyboard)
        if message.media:
            # The admin menu starts as a photo. Captions allow only 1024
            # characters, so a populated list must use a text message instead.
            await message.reply_text(**options, quote=False, disable_notification=True)
        else:
            await message.edit_text(**options)
    except MessageNotModified:
        # Clicking the current page again is a successful no-op.
        return True
    except FloodWait as exc:
        await callAnswer(call, f'⚠️ 操作频繁，请 {exc.value} 秒后重试', True)
        return False
    except Exception as exc:
        LOGGER.error('用户列表显示失败: {} error={}', label,
                     getattr(exc, 'ID', None) or type(exc).__name__)
        await callAnswer(call, f'⚠️ {label}显示失败，请稍后重试', True)
        return False
    return True


async def _show_user_list(call, condition, label, requested_page=1):
    users = await _load_users(call, condition, label)
    if users is None:
        return
    try:
        total_pages = len(_user_pages(users))
        page = _page_number(requested_page, total_pages)
        if label == '白名单列表':
            text = await create_whitelist_text(users, page)
            keyboard = await whitelist_page_ikb(total_pages, page)
        else:
            text = await create_normaluser_text(users, page)
            keyboard = await normaluser_page_ikb(total_pages, page)
    except Exception as exc:
        LOGGER.error('用户列表生成失败: {} error={}', label, type(exc).__name__)
        await callAnswer(call, f'⚠️ {label}生成失败，请稍后重试', True)
        return
    # Answer each callback once so a later error is not hidden by an earlier
    # success toast. Username lookups have a shared maximum wait of 3 seconds.
    if await _edit_user_list(call, text, keyboard, label):
        await callAnswer(call, f'🔍 {label} · 第{page}页')


@bot.on_callback_query(filters.regex('^whitelist$') & admins_on_filter)
async def list_whitelist(_, call):
    await _show_user_list(call, _active_whitelist_filter(), '白名单列表')


@bot.on_callback_query(filters.regex('^normaluser$') & admins_on_filter)
async def list_normaluser(_, call):
    await _show_user_list(call, Emby.lv == 'b', '普通用户列表')


@bot.on_callback_query(filters.regex('^whitelist:') & admins_on_filter)
async def whitelist_page(_, call):
    await _show_user_list(call, _active_whitelist_filter(), '白名单列表',
                          call.data.split(':', 1)[1])


@bot.on_callback_query(filters.regex('^normaluser:') & admins_on_filter)
async def normaluser_page(_, call):
    await _show_user_list(call, Emby.lv == 'b', '普通用户列表',
                          call.data.split(':', 1)[1])


async def _create_user_text(users, page, title, empty_text):
    pages = _user_pages(users)
    page = _page_number(page, len(pages))
    page_users = pages[page - 1]
    text = f'<b>{title}</b>\n\n'
    if not page_users:
        text += empty_text + '\n'
    usernames = await _telegram_usernames(page_users)
    for user in page_users:
        text += _user_row(user, usernames.get(_user_tg_id(user), '未设置'))
    return text + f'第 {page} 页,共 {len(pages)} 页, 共 {len(users)} 人'


async def create_whitelist_text(users, page):
    return await _create_user_text(users, page, '白名单用户列表', '暂无白名单用户。')


async def create_normaluser_text(users, page):
    return await _create_user_text(users, page, '普通用户列表', '暂无普通用户。')


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
