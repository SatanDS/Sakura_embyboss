import json
import re
from bot import LOGGER, moviepilot, save_config
import aiohttp
import asyncio
from urllib.parse import quote, urlparse

# 添加配置类
class MoviePilot:
    def __init__(self):
        self.url = moviepilot.url
        self.username = moviepilot.username 
        self.password = moviepilot.password
        self.access_token = moviepilot.access_token or ''

mp = MoviePilot()

TIMEOUT = 30
DOUBAN_SYNC_PLUGIN_ID = "DoubanSync"
_douban_sync_lock = asyncio.Lock()


def normalize_douban_user_id(value):
    """Return a numeric Douban user id from an id or a profile URL.

    DoubanSync consumes user ids (not display names and not arbitrary URLs).
    Accepting the profile URL here makes the Telegram flow less error-prone,
    while keeping the value written to MoviePilot strictly numeric.
    """
    value = str(value or "").strip()
    if not value:
        return None

    if value.isdigit():
        candidate = value
    else:
        try:
            parsed = urlparse(value)
        except ValueError:
            return None
        if parsed.scheme not in {"http", "https"}:
            return None
        hostname = (parsed.hostname or "").lower()
        if hostname.startswith("www."):
            hostname = hostname[4:]
        if hostname != "douban.com":
            return None
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 2 or parts[0].lower() != "people" or not parts[1].isdigit():
            return None
        candidate = parts[1]

    # Douban IDs are currently short numeric identifiers.  The upper bound
    # prevents accidentally sending a Telegram/Emby id or a huge payload.
    if not re.fullmatch(r"\d{4,20}", candidate):
        return None
    return candidate


def _normalise_douban_users(value):
    """Convert a plugin users value to a de-duplicated list of strings."""
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    elif value is None:
        values = []
    else:
        values = [value]

    result = []
    seen = set()
    for item in values:
        item = str(item).strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _format_douban_users(users):
    """Serialize DoubanSync users with the plugin-required trailing comma."""
    users = _normalise_douban_users(users)
    return f"{','.join(users)}," if users else ""


# aiohttp重试装饰器
def aiohttp_retry(retry_count):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            for i in range(retry_count):
                try:
                    return await func(*args, **kwargs)
                except aiohttp.ClientError:
                    await asyncio.sleep(3)  # 延迟 3 秒后进行重试
            return None

        return wrapper

    return decorator
@aiohttp_retry(3)
async def _do_request(request):
    async with aiohttp.ClientSession() as session:
        async with session.request(method=request['method'], url=request['url'], headers=request['headers'], data=request.get('data')) as response:
            if response.status in (401, 403) and not request.get("_auth_retried"):
                LOGGER.warning(f"MP 请求鉴权失败 ({response.status}), 尝试重新登录")
                success = await login()
                if success:
                    request['headers']['Authorization'] = mp.access_token
                    request['_auth_retried'] = True
                    return await _do_request(request)
                return None
            return await response.json()
async def login():
    if not mp.url or not mp.username or not mp.password:
        LOGGER.error("MP 登录失败：未配置 URL、用户名或密码")
        return False
    url = f"{mp.url.rstrip('/')}/api/v1/login/access-token"
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as session:
            async with session.post(
                url,
                data={'username': mp.username, 'password': mp.password},
                headers=headers,
            ) as response:
                if response.status != 200:
                    LOGGER.error(f"MP 登录失败：HTTP {response.status}")
                    return False
                result = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        LOGGER.error(f"MP 登录失败：{type(exc).__name__}")
        return False
    if (
        isinstance(result, dict)
        and isinstance(result.get('access_token'), str)
        and result['access_token'].strip()
        and isinstance(result.get('token_type'), str)
        and result['token_type'].strip()
    ):
        mp.access_token = result['token_type'] + ' ' + result['access_token']
        moviepilot.access_token = mp.access_token # 保存到config
        save_config()
        LOGGER.info("MP 登录成功, token已保存")
        return True
    else:
        LOGGER.error("MP 登录失败：响应缺少有效的 access_token/token_type")
        return False


async def get_douban_sync_config():
    """Read the complete DoubanSync configuration from MoviePilot.

    MoviePilot's plugin endpoint returns the stored configuration directly;
    keeping this helper separate ensures callers never overwrite unrelated
    fields such as ``cron`` or ``search_download``.
    """
    if not mp.url:
        return False, None, "未配置 MoviePilot 地址"

    url = f"{mp.url.rstrip('/')}/api/v1/plugin/{quote(DOUBAN_SYNC_PLUGIN_ID, safe='')}"
    request = {
        'method': 'GET',
        'url': url,
        'headers': {'Authorization': mp.access_token},
    }
    try:
        result = await _do_request(request)
    except Exception as exc:
        LOGGER.error(f"读取 MoviePilot 豆瓣想看配置失败: {exc}")
        return False, None, "无法连接 MoviePilot"

    if not isinstance(result, dict):
        return False, None, "MoviePilot 返回了无效的插件配置"
    if result.get("success") is False:
        return False, None, result.get("message") or "豆瓣想看插件不存在或未启用"
    # Some compatible MP builds wrap responses in ``data``.  Support both
    # forms without changing the normal v2 response shape.
    config = result.get("data") if isinstance(result.get("data"), dict) else result
    if not isinstance(config, dict) or "users" not in config:
        return False, None, "未找到 DoubanSync 插件或 users 配置"
    return True, config, None


async def update_douban_sync_users(douban_user_id, previous_user_id=None):
    """Add a Douban ID to DoubanSync's global users list.

    The GET/merge/PUT sequence is serialized because MoviePilot only exposes
    a full-config PUT endpoint.  ``previous_user_id`` is removed only when it
    is no longer referenced by another Telegram account; callers may omit it
    when they only want append semantics.
    """
    normalized = normalize_douban_user_id(douban_user_id)
    if not normalized:
        return False, "豆瓣 ID 格式无效"
    previous = normalize_douban_user_id(previous_user_id) if previous_user_id else None

    async with _douban_sync_lock:
        ok, plugin_config, error = await get_douban_sync_config()
        if not ok:
            return False, error

        users = _normalise_douban_users(plugin_config.get("users"))
        if previous and previous != normalized:
            users = [item for item in users if item != previous]
        if normalized not in users:
            users.append(normalized)

        # Preserve the plugin's existing value type (the official plugin uses
        # a comma-separated string) and all unrelated settings.
        # DoubanSync expects the automatically managed list to end with an
        # ASCII comma.  Keep this delimiter even for a single submitted ID.
        plugin_config["users"] = _format_douban_users(users)
        url = f"{mp.url.rstrip('/')}/api/v1/plugin/{quote(DOUBAN_SYNC_PLUGIN_ID, safe='')}"
        request = {
            'method': 'PUT',
            'url': url,
            'headers': {
                'Authorization': mp.access_token,
                'Content-Type': 'application/json',
            },
            'data': json.dumps(plugin_config, ensure_ascii=False),
        }
        try:
            result = await _do_request(request)
        except Exception as exc:
            LOGGER.error(f"更新 MoviePilot 豆瓣想看用户失败: {exc}")
            return False, "更新 MoviePilot 插件失败"
        if not isinstance(result, dict) or result.get("success") is False:
            message = result.get("message") if isinstance(result, dict) else None
            return False, message or "MoviePilot 拒绝更新插件配置"
        return True, normalized


async def remove_douban_sync_user(douban_user_id):
    """Remove one id from DoubanSync while preserving every other setting."""
    normalized = normalize_douban_user_id(douban_user_id)
    if not normalized:
        return False, "豆瓣 ID 格式无效"

    async with _douban_sync_lock:
        ok, plugin_config, error = await get_douban_sync_config()
        if not ok:
            return False, error
        configured_users = _normalise_douban_users(plugin_config.get("users"))
        users = [item for item in configured_users if item != normalized]
        if len(users) == len(configured_users):
            return True, normalized
        plugin_config["users"] = _format_douban_users(users)
        url = f"{mp.url.rstrip('/')}/api/v1/plugin/{quote(DOUBAN_SYNC_PLUGIN_ID, safe='')}"
        request = {
            'method': 'PUT',
            'url': url,
            'headers': {
                'Authorization': mp.access_token,
                'Content-Type': 'application/json',
            },
            'data': json.dumps(plugin_config, ensure_ascii=False),
        }
        try:
            result = await _do_request(request)
        except Exception as exc:
            LOGGER.error(f"移除 MoviePilot 豆瓣想看用户失败: {exc}")
            return False, "更新 MoviePilot 插件失败"
        if not isinstance(result, dict) or result.get("success") is False:
            message = result.get("message") if isinstance(result, dict) else None
            return False, message or "MoviePilot 拒绝更新插件配置"
        return True, normalized

async def search(title):
    """
    搜索资源
    Args:
        title: 搜索关键词
    Returns:
        (success, results)
        success: bool 是否成功
        results: list 搜索结果列表
    """
    if title is None:
        return False, []
        
    url = f"{mp.url}/api/v1/search/title?keyword={title}"
    headers = {'Authorization': mp.access_token}
    request = {'method': 'GET', 'url': url, 'headers': headers}
    try:
        data = await _do_request(request)
        results = []
        if data.get("success", False):
            data = data["data"]
            for item in data:
                meta_info = item.get("meta_info", {})
                torrent_info = item.get("torrent_info", {})
                
                seeders = torrent_info.get("seeders", "0")
                try:
                    seeders = int(seeders) if seeders else 0
                except (ValueError, TypeError):
                    seeders = 0
                result = {
                    "title": meta_info.get("title", ""),
                    "year": meta_info.get("year", ""),
                    "type": meta_info.get("type", ""),
                    "resource_pix": meta_info.get("resource_pix", ""),
                    "video_encode": meta_info.get("video_encode", ""),
                    "audio_encode": meta_info.get("audio_encode", ""),
                    "resource_team": meta_info.get("resource_team", ""),
                    "seeders": seeders,
                    "size": torrent_info.get("size", "0"),
                    "labels": torrent_info.get("labels", ""),
                    "description": torrent_info.get("description", ""),
                    "torrent_info": torrent_info,
                }
                results.append(result)
                
        # 只按做种数排序,移除数量限制
        results.sort(key=lambda x: x["seeders"], reverse=True)
            
        LOGGER.info("MP Search successful!")
        return True, results
    except Exception as e:
        LOGGER.error(f"MP Search failed: {str(e)}")
        return False, []


async def add_download_task(param):
    if param is None:
        return False, None
    url = f"{mp.url}/api/v1/download/add"
    headers = {'Content-Type': 'application/json',
               'Authorization': mp.access_token}
    jsonData = json.dumps(param)
    request = {'method': 'POST', 'url': url,
               'headers': headers, 'data': jsonData}
    try:
        result = await _do_request(request)
        if result.get("success", False):
            LOGGER.info(f"MP 添加下载任务成功, ID: {result['data']['download_id']}")
            return True, result["data"]["download_id"]
        else:
            LOGGER.error(f"MP 添加下载任务失败: {result}")
            return False, None
    except Exception as e:
        LOGGER.error(f"MP 添加下载任务失败: {e}")
        return False, None

async def get_download_task():
    url = f"{mp.url}/api/v1/download?name=下载"
    headers = {'Authorization': mp.access_token}
    request = {'method': 'GET', 'url': url, 'headers': headers}
    try:
        result = await _do_request(request)
        data = []
        for item in result:
            data.append(
                {'download_id': item['hash'],
                 'state': item['state'],
                 'progress': item['progress'],
                 'left_time': item['left_time']
                 })
        return data
    except Exception as e:
        LOGGER.error(f"MP 获取下载任务失败: {e}")
        return None
async def get_history_transfer_task_by_title_download_id(title, download_id, page = 1, count = 50):
    url = f"{mp.url}/api/v1/history/transfer?title={title}&page={page}&count={count}"
    headers = {'Authorization': mp.access_token}
    request = {'method': 'GET', 'url': url, 'headers': headers}
    try:
        result = await _do_request(request)
        if result and result.get("success", False) and result.get("data", []):
            for item in result["data"]["list"]:
                if item['download_hash'] == download_id:
                    return item['status']
            return None
        else:
            LOGGER.error(f"MP 获取历史转移任务失败: {result}")
            return None
    except Exception as e:
        LOGGER.error(f"MP 获取历史转移任务失败: {e}")
        return None
