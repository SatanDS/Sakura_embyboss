"""Bounded MoviePilot v2 transport; never retry a possibly accepted mutation."""
import asyncio
import json
from urllib.parse import quote, urlsplit

import aiohttp

from .request_media import SOURCES, normalize, parse_key, same_identity
from .request_images import RequestImages, MAX_IMAGE_BYTES, image_type
from .service import TVError, canonical_url


class MoviePilotGateway:
    def __init__(self, origin, token="", username="", password=""):
        self.origin = canonical_url(origin)
        self.token, self.username, self.password = token, username, password
        self._login_lock = asyncio.Lock()
        self._resource_lock = asyncio.Lock()
        self._cookies = {}
        self.images = RequestImages(self._image)

    async def _request(self, method, path, *, params=None, data=None, auth=True, binary=False):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35), trust_env=False) as client:
                async with client.request(method, self.origin + "/api/v1" + path, params=params,
                                          json=data if auth else None, data=data if not auth else None,
                                          headers={"Authorization": self.token} if auth else {}, cookies=self._cookies,
                                          allow_redirects=False) as response:
                    self._cookies.update({key: value.value for key, value in response.cookies.items()})
                    if response.status == 401:
                        raise TVError("MOVIEPILOT_AUTH", "MoviePilot 认证失败，请管理员检查配置", 503)
                    if response.status == 403:
                        raise TVError("MOVIEPILOT_FORBIDDEN", "MoviePilot 账号没有此操作权限", 503)
                    if response.status == 429:
                        raise TVError("MOVIEPILOT_RATE_LIMITED", "MoviePilot 请求过于频繁，请稍后重试", 429)
                    if response.status == 422:
                        raise TVError("MOVIEPILOT_API_VERSION", "MoviePilot API 版本不兼容，请管理员更新 MoviePilot", 502)
                    if response.status != 200:
                        raise TVError("MOVIEPILOT_UNAVAILABLE", "MoviePilot 暂时无法响应，请稍后重试", 502)
                    bound = MAX_IMAGE_BYTES if binary else 4 * 1024 * 1024
                    if response.content_length and response.content_length > bound:
                        raise TVError("MOVIEPILOT_INVALID_RESPONSE", "MoviePilot 返回内容过大", 502)
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        raw.extend(chunk)
                        if len(raw) > bound:
                            raise TVError("MOVIEPILOT_INVALID_RESPONSE", "MoviePilot 返回内容过大", 502)
                    if binary:
                        return bytes(raw), image_type(raw)
                    return json.loads(raw)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            raise TVError("MOVIEPILOT_UNAVAILABLE", "MoviePilot 暂时无法响应，请稍后重试", 502) from None

    async def request(self, method, path, **kwargs):
        previous = self.token
        try:
            return await self._request(method, path, **kwargs)
        except TVError as error:
            if error.code != "MOVIEPILOT_AUTH" or not self.username or not self.password:
                raise
        # A 401 is a definitive rejection, so refreshing then retrying is safe.
        async with self._login_lock:
            if previous == self.token:
                data = await self._request("POST", "/login/access-token", auth=False,
                                           data={"username": self.username, "password": self.password})
                if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"]:
                    raise TVError("MOVIEPILOT_AUTH", "MoviePilot 认证失败，请管理员检查配置", 503)
                self.token = "Bearer " + data["access_token"]
        return await self._request(method, path, **kwargs)

    async def _image(self, url):
        # Match MP's image API: Douban bypasses its outbound proxy; TMDB may
        # use the configured proxy. Resource cookies stay solely on the Bot.
        proxy = "0" if urlsplit(url).hostname.endswith(".doubanio.com") else "1"
        params = {"imgurl": url, "cache": "true"}
        cookies = dict(self._cookies)
        try:
            return await self._request("GET", "/system/img/" + proxy, params=params, binary=True)
        except TVError as error:
            if error.code != "MOVIEPILOT_AUTH":
                raise
        # Recent MP separates resource Cookies from its API Bearer token.
        # A harmless authenticated GET refreshes the resource Cookie once.
        async with self._resource_lock:
            if self._cookies == cookies:
                await self.request("GET", "/user/current")
        return await self._request("GET", "/system/img/" + proxy, params=params, binary=True)

    async def recommend(self, source, kind, page):
        endpoint = f"/recommend/{source}_{'movies' if kind == 'movie' else 'tvs'}"
        params = {"page": page}
        if source == "douban":
            params.update({"tags": "华语" if kind == "movie" else "国产剧", "sort": "T", "count": 12})
        else:
            params["sort_by"] = "popularity.desc"
        rows = await self.request("GET", endpoint, params=params)
        if not isinstance(rows, list):
            raise TVError("MOVIEPILOT_INVALID_RESPONSE", "MoviePilot 热门影片响应无效", 502)
        return [item for row in rows[:100] if (item := normalize(row, source, kind))]

    async def detail(self, key):
        source, kind, media_id = parse_key(key)
        # v2 uses tmdb:ID/douban:ID. The current API requires a numeric ID
        # plus media_source; only an explicit 422 permits this read-only fallback.
        try:
            data = await self.request("GET", f"/media/{source}:{media_id}", params={"type_name": "电影" if kind == "movie" else "电视剧"})
        except TVError as error:
            if error.code != "MOVIEPILOT_API_VERSION":
                raise
            data = await self.request("GET", f"/media/{media_id}", params={"media_source": SOURCES[source], "type_name": "电影" if kind == "movie" else "电视剧"})
        item = normalize(data, source, kind)
        if not item or item["id"] != media_id:
            raise TVError("MEDIA_NOT_FOUND", "没有找到此影片的准确资料", 404)
        if kind == "tv" and not item["seasons"]:
            rows = await self.request("GET", "/media/seasons", params={"mediaid": f"{source}:{media_id}", "media_source": SOURCES[source], "media_id": media_id})
            item["seasons"] = (normalize({**data, "seasons": rows if isinstance(rows, list) else []}, source, kind) or item)["seasons"]
        return item

    async def find_subscription(self, item, season):
        # Do not send title/year: some versions fall back to approximate identity.
        seen_pages = set()
        for page in range(1, 11):
            rows = await self.request("GET", "/subscribe/", params={"page": page, "count": 100})
            if not isinstance(rows, list):
                raise TVError("MOVIEPILOT_INVALID_RESPONSE", "MoviePilot 订阅响应无效", 502)
            signature = tuple(str(row.get("id")) for row in rows if isinstance(row, dict))
            if signature in seen_pages:
                return None  # older MP ignores pagination and returns the full list
            seen_pages.add(signature)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                media = {**row, "title": row.get("name")}
                candidate = normalize(media, "tmdb", item["type"]) or normalize(media, "douban", item["type"])
                if candidate and same_identity(item, candidate) and (item["type"] == "movie" or row.get("season") == season):
                    if row.get("id"):
                        return str(row["id"])
            if len(rows) != 100:
                return None
        raise TVError("MOVIEPILOT_TOO_MANY_SUBSCRIPTIONS", "订阅数量过多，无法安全核对重复请求，请联系管理员", 503)

    async def subscribe(self, item, season):
        source, kind, media_id = parse_key(item["key"])
        payload = {"name": item["title"], "type": "电影" if kind == "movie" else "电视剧", "year": str(item["year"] or ""),
                   "media_source": SOURCES[source], "media_id": media_id,
                   "tmdbid" if source == "tmdb" else "doubanid": int(media_id) if source == "tmdb" else media_id}
        if kind == "tv":
            payload["season"] = season
        data = await self.request("POST", "/subscribe/", data=payload)
        if not isinstance(data, dict) or data.get("success") is not True or not isinstance(data.get("data"), dict) or not data["data"].get("id"):
            raise TVError("SUBSCRIPTION_REJECTED", "MoviePilot 未接受订阅，请管理员检查片源识别与订阅配置", 409)
        return str(data["data"]["id"])


async def library_lookup(origin, api_key, user_id, item):
    """User-scoped Emby query; enforce IDs again because filters vary by version."""
    if not item["providerIds"]:
        return {"available": False, "items": []}
    params = {"Recursive": "true", "IncludeItemTypes": "Movie" if item["type"] == "movie" else "Series",
              "Fields": "ProviderIds", "Limit": 100,
              "AnyProviderIdEquals": ",".join(f"{key}.{value}" for key, value in item["providerIds"].items())}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), trust_env=False) as client:
            async with client.get(canonical_url(origin) + "/Users/" + quote(user_id, safe="") + "/Items", params=params,
                                  headers={"X-Emby-Token": api_key}, allow_redirects=False) as response:
                if response.status != 200:
                    raise TVError("LIBRARY_UNAVAILABLE", "媒体库暂时无法核对，请稍后重试", 502)
                raw = await response.content.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError()
                data = json.loads(raw)
        found = []
        for row in data.get("Items", []):
            ids = {key.casefold(): str(value) for key, value in row.get("ProviderIds", {}).items()}
            shared = {key: value for key, value in item["providerIds"].items() if key.casefold() in ids}
            if shared and all(ids[key.casefold()] == value for key, value in shared.items()) and row.get("Id"):
                found.append({"id": str(row["Id"]), "name": str(row.get("Name") or item["title"]), "type": row.get("Type")})
        return {"available": bool(found), "items": found}
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, AttributeError):
        raise TVError("LIBRARY_UNAVAILABLE", "媒体库暂时无法核对，请稍后重试", 502) from None
