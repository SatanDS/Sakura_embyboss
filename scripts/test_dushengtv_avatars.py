"""Offline photo cache checks; never imports the live Bot or its configuration."""
import asyncio
import importlib.util
import io
import types
import unittest
from pathlib import Path
from unittest.mock import patch

source = Path(__file__).resolve().parents[1] / "bot/dushengtv/avatars.py"
spec = importlib.util.spec_from_file_location("avatar_under_test", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
PHOTO = (b"\xff\xd8\xffexample", "image/jpeg")


class AvatarTests(unittest.IsolatedAsyncioTestCase):
    async def test_telegram_loader_uses_small_photo_and_returns_only_image_bytes(self):
        calls = []
        async def photos(tg, limit):
            self.assertEqual((tg, limit), (42, 1))
            yield types.SimpleNamespace(thumbs=[types.SimpleNamespace(width=160, height=160, file_size=20, file_id="private-file-id")])
        async def download(file_id, in_memory):
            calls.append((file_id, in_memory))
            stream = io.BytesIO(PHOTO[0]); stream.seek(len(PHOTO[0]))
            return stream
        fake = types.SimpleNamespace(bot=types.SimpleNamespace(get_chat_photos=photos, download_media=download))
        with patch.dict("sys.modules", {"bot": fake}):
            self.assertEqual(await module.download_avatar("42"), PHOTO)
        self.assertEqual(calls, [("private-file-id", True)])

    async def test_merge_expiry_and_deleted_photo(self):
        now, calls, photo = [0], [], [PHOTO]
        async def loader(tg):
            calls.append(tg)
            await asyncio.sleep(0)
            return photo[0]
        cache = module.TelegramAvatarCache(loader, lambda: now[0])
        self.assertEqual(await asyncio.gather(*(cache.get("42") for _ in range(10))), [PHOTO] * 10)
        self.assertEqual(calls, ["42"])
        await cache.get("42")
        self.assertEqual(len(calls), 1)
        now[0] += module.TTL_SECONDS + 1
        photo[0] = None
        self.assertIsNone(await cache.get("42"))
        self.assertEqual(len(calls), 2)

    async def test_failed_lookup_keeps_picture_briefly_and_rejects_active_formats(self):
        now, fail = [0], [False]
        async def loader(_):
            if fail[0]:
                raise RuntimeError("private Telegram metadata must not escape")
            return PHOTO
        cache = module.TelegramAvatarCache(loader, lambda: now[0])
        await cache.get("42")
        fail[0] = True
        now[0] = 301
        self.assertEqual(await cache.get("42"), PHOTO)
        now[0] = 3601
        with self.assertRaises(module.AvatarUnavailable):
            await cache.get("42")
        async def invalid(_):
            return b"<svg onload='bad()'/>", "image/svg+xml"
        with self.assertRaises(module.AvatarUnavailable):
            await module.TelegramAvatarCache(invalid).get("43")


if __name__ == "__main__":
    unittest.main()
