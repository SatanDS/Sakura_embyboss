"""Private, bounded transport to the configured DuShengTV danmu adapter."""

import asyncio
import ipaddress
import json
import math
import os
import re
from urllib.parse import urlsplit

import aiohttp

ADAPTER_PATH = "/api/v1/dushengtv/danmaku"
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 12 * 1024 * 1024
REQUEST_TIMEOUT = 70
UNAVAILABLE = "弹幕服务暂时不可用，请稍后重试或导入本地弹幕"


class DanmakuError(Exception):
    def __init__(self, code, message, status=502):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def endpoint_settings():
    """The endpoint and its credential are never supplied by the desktop."""
    origin = os.getenv("TGBOT_DANMU_API_URL", "").strip()
    secret = os.getenv("TGBOT_DANMU_API_TOKEN", "")
    if not origin and not secret:
        return None
    try:
        url = urlsplit(origin)
        port = url.port
        if (url.scheme not in {"http", "https"} or not url.hostname
                or url.username is not None or url.password is not None
                or url.query or url.fragment or url.path not in {"", "/"}
                or any(c in origin for c in "\\?#%") or any(c.isspace() or ord(c) < 32 for c in origin)
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError()
        if url.scheme == "http":
            try:
                address = ipaddress.ip_address(url.hostname)
                local = address.is_loopback or any(address in network for network in (
                    ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
                    ipaddress.ip_network("192.168.0.0/16"), ipaddress.ip_network("fc00::/7")))
            except ValueError:
                # Compose service names are single DNS labels, e.g. danmu-api.
                local = bool(re.fullmatch(r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", url.hostname))
            if not local:
                raise ValueError()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", secret):
            raise ValueError()
    except (ValueError, TypeError):
        raise DanmakuError("DANMAKU_NOT_CONFIGURED", "弹幕服务配置无效，请联系管理员", 503) from None
    return origin.rstrip("/") + ADAPTER_PATH, secret


def request_metadata(data):
    def invalid(message="影片信息无效，请更新客户端后重试"):
        raise DanmakuError("INVALID_REQUEST", message, 400)

    if not isinstance(data, dict):
        invalid()
    title = data.get("title")
    if not isinstance(title, str) or not title.strip() or any(ord(c) < 32 or ord(c) == 127 for c in title) or "://" in title:
        invalid("影片标题无效")
    try:
        # JavaScript endpoints count UTF-16 units, including emoji surrogate pairs.
        if len(title.strip().encode("utf-16-le")) > 512:
            invalid("影片标题无效")
    except UnicodeError:
        invalid("影片标题无效")
    kind = data.get("type")
    if kind is None:
        kind = "Episode" if data.get("episode") is not None else "Movie"
    if not isinstance(kind, str) or kind not in {"Movie", "Episode", "local"}:
        invalid()
    result = {"title": title.strip(), "type": kind}
    for key, low, high in (("season", 0, 999), ("episode", 0, 99999), ("year", 1800, 2200)):
        value = data.get(key)
        if value is None:
            continue
        if type(value) is not int or not low <= value <= high:
            invalid()
        result[key] = value
    if kind == "Episode" and ("season" not in result or "episode" not in result):
        invalid("缺少季数或集数，请更新客户端或检查影片信息")
    ids = data.get("providerIds", {})
    if not isinstance(ids, dict) or len(ids) > 16:
        invalid()
    if any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", key)
           or not isinstance(value, str) or len(value) > 128
           or any(ord(c) < 32 for c in value) for key, value in ids.items()):
        invalid()
    result["providerIds"] = ids
    return result


def response_comments(data):
    if not isinstance(data, dict) or type(data.get("available")) is not bool or not isinstance(data.get("comments"), list):
        raise DanmakuError("DANMAKU_UNAVAILABLE", UNAVAILABLE)
    if not data["available"]:
        message = "無彈幕匹配"
        # The adapter also discovers specials in local filenames. Allow only its
        # known safe message, never arbitrary upstream errors or credentials.
        if data.get("message") == "特殊季或第 0 集暂不支持自动匹配，请在播放器导入本地弹幕":
            message = "特殊集暂不支持自动匹配，请导入本地弹幕"
        return {"available": False, "comments": [], "message": message}
    comments, size = [], 0
    for value in data["comments"]:
        if not isinstance(value, dict):
            continue
        seconds, mode, color, text = (value.get(key) for key in ("time", "mode", "color", "text"))
        if (type(seconds) not in {int, float} or seconds < 0 or seconds > 1e9 or not math.isfinite(seconds)
                or type(mode) is not int or mode not in {1, 4, 5}
                or not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color)
                or not isinstance(text, str) or not text.strip() or len(text) > 300):
            continue
        comment = {"time": seconds, "mode": mode, "color": color, "text": text}
        size += len(json.dumps(comment, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
        if size > MAX_OUTPUT_BYTES:
            break
        comments.append(comment)
        if len(comments) >= 50000:
            break
    result = {"available": True, "comments": comments}
    match = data.get("match")
    if isinstance(match, dict):
        safe_match = {key: match[key] for key in ("animeTitle", "episodeTitle")
                      if isinstance(match.get(key), str) and len(match[key]) <= 256}
        if type(match.get("episodeId")) is int and 0 < match["episodeId"] <= 9007199254740991:
            safe_match["episodeId"] = match["episodeId"]
        if safe_match:
            result["match"] = safe_match
    return result


async def fetch_danmaku(data):
    settings = endpoint_settings()
    if settings is None:
        return {"available": False, "comments": [], "message": "暂未配置弹幕供应商，可导入本地弹幕"}
    endpoint, secret = settings
    metadata = request_metadata(data)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT), trust_env=False) as client:
            async with client.post(endpoint, json=metadata, headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"},
                                   allow_redirects=False) as response:
                if response.status == 429:
                    raise DanmakuError("DANMAKU_RATE_LIMITED", "弹幕请求过于频繁，请稍后重试", 429)
                if response.status == 504:
                    raise DanmakuError("DANMAKU_TIMEOUT", "弹幕加载超时，请稍后重试", 504)
                if response.status != 200:
                    # Upstream 401/403 are provider failures, never TG failures.
                    raise DanmakuError("DANMAKU_UNAVAILABLE", UNAVAILABLE)
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise DanmakuError("DANMAKU_UNAVAILABLE", UNAVAILABLE)
                result = response_comments(json.loads(raw))
                if not result["available"] and metadata["type"] == "Episode" and (metadata.get("season") == 0 or metadata.get("episode") == 0):
                    result["message"] = "特殊集暂不支持自动匹配，请导入本地弹幕"
                return result
    except asyncio.TimeoutError:
        raise DanmakuError("DANMAKU_TIMEOUT", "弹幕加载超时，请稍后重试", 504) from None
    except (aiohttp.ClientError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise DanmakuError("DANMAKU_UNAVAILABLE", UNAVAILABLE) from None
