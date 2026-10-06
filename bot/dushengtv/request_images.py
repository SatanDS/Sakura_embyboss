"""求片图片只允许固定来源；共享下载有上限，账户权限由路由逐次校验。"""
import asyncio
import time
from collections import OrderedDict
from urllib.parse import urlsplit

from .request_media import image_url
from .service import TVError

MAX_IMAGE_BYTES = 10 * 1024 * 1024


def image_type(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    raise TVError("REQUEST_IMAGE_INVALID", "图片来源未返回有效图片", 502)


class RequestImages:
    def __init__(self, loader, now=time.monotonic):
        self.loader, self.now = loader, now
        self.cache, self.pending, self.failures = OrderedDict(), {}, OrderedDict()
        self.bytes = 0
        self.slots = asyncio.Semaphore(6)

    async def get(self, value):
        url = image_url(value)
        host = (urlsplit(value).hostname or "").lower() if url else ""
        if not url or not (host == "image.tmdb.org" or host.endswith(".doubanio.com")):
            raise TVError("INVALID_REQUEST_IMAGE", "图片地址无效", 400)
        entry = self.cache.get(url)
        if entry and entry[0] > self.now():
            self.cache.move_to_end(url)
            return entry[1]
        if self.failures.get(url, 0) > self.now():
            raise TVError("REQUEST_IMAGE_UNAVAILABLE", "图片暂时无法加载", 502)
        job = self.pending.get(url)
        if job is None:
            if len(self.pending) >= 256:
                raise TVError("REQUEST_IMAGE_BUSY", "图片加载繁忙，请稍后重试", 503)
            job = asyncio.create_task(self._load(url))
            self.pending[url] = job
            job.add_done_callback(lambda done: self._finished(url, done))
        return await asyncio.shield(job)

    def _finished(self, url, job):
        self.pending.pop(url, None)
        if not job.cancelled():
            job.exception()  # Consume failures even if every waiting client left.

    async def _load(self, url):
        try:
            async with self.slots:
                data, _ = await asyncio.wait_for(self.loader(url), 45)
            if len(data) > MAX_IMAGE_BYTES:
                raise TVError("REQUEST_IMAGE_INVALID", "图片过大", 502)
            result = data, image_type(data)
            previous = self.cache.pop(url, None)
            self.bytes -= len(previous[1][0]) if previous else 0
            self.cache[url] = self.now() + 86400, result
            self.bytes += len(data)
            while self.bytes > 64 * 1024 * 1024 or len(self.cache) > 512:
                _, (_, old) = self.cache.popitem(last=False)
                self.bytes -= len(old[0])
            self.failures.pop(url, None)
            return result
        except Exception as error:
            self.failures[url] = self.now() + 10
            while len(self.failures) > 512:
                self.failures.popitem(last=False)
            if isinstance(error, TVError):
                raise
            raise TVError("REQUEST_IMAGE_UNAVAILABLE", "图片暂时无法加载", 502) from None
