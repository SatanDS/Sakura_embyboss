"""Private Telegram photo bytes; Telegram file identifiers never leave the Bot."""

import asyncio
import time
from collections import OrderedDict

MAX_BYTES = 512 * 1024
TTL_SECONDS = 300


class AvatarUnavailable(Exception):
    pass


def image_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise AvatarUnavailable()


async def download_avatar(tg):
    from bot import bot

    async for photo in bot.get_chat_photos(int(tg), limit=1):
        thumbs = [thumb for thumb in (photo.thumbs or []) if thumb.width >= 80 and thumb.height >= 80]
        chosen = min(thumbs, key=lambda thumb: thumb.width * thumb.height) if thumbs else photo
        if chosen.file_size and chosen.file_size > MAX_BYTES:
            raise AvatarUnavailable()
        stream = await bot.download_media(chosen.file_id, in_memory=True)
        if stream is None:
            raise AvatarUnavailable()
        try:
            stream.seek(0)
            data = stream.read(MAX_BYTES + 1)
        finally:
            stream.close()
        if not data or len(data) > MAX_BYTES:
            raise AvatarUnavailable()
        return data, image_type(data)
    return None


class TelegramAvatarCache:
    def __init__(self, loader=download_avatar, now=time.monotonic):
        self.loader, self.now = loader, now
        self.entries, self.pending = OrderedDict(), {}

    async def get(self, tg):
        tg = str(tg)
        entry = self.entries.get(tg)
        if entry and self.now() < entry["expires"]:
            self.entries.move_to_end(tg)
            if entry["unavailable"]:
                raise AvatarUnavailable()
            return entry["image"]
        task = self.pending.get(tg)
        if task is None:
            task = asyncio.create_task(self._load(tg, entry))
            self.pending[tg] = task
            task.add_done_callback(lambda done: self.pending.pop(tg, None))
        return await asyncio.shield(task)

    async def _load(self, tg, previous):
        now = self.now()
        try:
            image = await asyncio.wait_for(self.loader(tg), timeout=8)
            if image is not None:
                data, _ = image
                if not data or len(data) > MAX_BYTES:
                    raise AvatarUnavailable()
                image = (data, image_type(data))
            entry = {"image": image, "expires": now + TTL_SECONDS, "stale_until": now + 3600, "unavailable": False}
        except Exception:
            # A short Telegram outage must not erase an already loaded picture.
            if previous and previous["image"] and now < previous["stale_until"]:
                entry = {**previous, "expires": now + 30}
            else:
                entry = {"image": None, "expires": now + 30, "stale_until": now, "unavailable": True}
        self.entries[tg] = entry
        self.entries.move_to_end(tg)
        while len(self.entries) > 128:
            self.entries.popitem(last=False)
        if entry["unavailable"]:
            raise AvatarUnavailable()
        return entry["image"]


avatar_cache = TelegramAvatarCache()
