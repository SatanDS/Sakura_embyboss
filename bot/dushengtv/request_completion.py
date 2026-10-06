"""按当前 Emby 用户核对求片入库；季完成以明确总集数和真实分集为准。"""
import asyncio
import json
import time
from collections import OrderedDict
from urllib.parse import quote

import aiohttp

from .request_media import same_identity
from .service import TVError, canonical_url


def matches(row, item):
    expected = "Movie" if item["type"] == "movie" else "Series"
    if not isinstance(row, dict) or row.get("Type") != expected or not row.get("Id"):
        return False
    providers = row.get("ProviderIds")
    if not isinstance(providers, dict):
        return False
    ids = {str(key).casefold(): str(value) for key, value in providers.items()}
    shared = [(key, str(value)) for key, value in item["providerIds"].items() if key.casefold() in ids]
    return bool(shared) and all(ids[key.casefold()] == value for key, value in shared)


def playable(row):
    if any(row.get(key) is True for key in ("IsFolder", "IsMissing", "IsVirtualItem", "IsVirtualUnaired")):
        return False
    if str(row.get("LocationType", "")).casefold() in {"virtual", "placeholder", "offline"}:
        return False
    sources = row.get("MediaSources") or []
    if not isinstance(sources, list):
        return False
    return any(isinstance(source, dict) and isinstance(source.get("Path"), str) and source["Path"].strip()
               and source.get("IsPlaceholder") is not True for source in sources)


def episode_numbers(rows, series_id, season):
    found = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("Type") != "Episode" or not playable(row):
            continue
        if str(row.get("SeriesId") or "") != str(series_id) or type(row.get("ParentIndexNumber")) is not int or row["ParentIndexNumber"] != season:
            continue
        start, end = row.get("IndexNumber"), row.get("IndexNumberEnd", row.get("IndexNumber"))
        if end is None:
            end = start
        if type(start) is int and type(end) is int and 0 <= start <= end <= 10000 and end - start < 1000:
            found.update(range(start, end + 1))
    return found


class RequestCompletion:
    def __init__(self, origin, api_key, gateway, budget_seconds=6):
        self.origin, self.api_key, self.gateway = canonical_url(origin), api_key, gateway
        self.budget_seconds = budget_seconds
        self.slots = asyncio.Semaphore(3)
        self.metadata, self.pending = OrderedDict(), {}

    async def _metadata(self, key, load):
        old = self.metadata.get(key)
        if old and old[0] > time.monotonic():
            self.metadata.move_to_end(key)
            return old[1]
        async def bounded():
            async with self.slots:
                return await load()
        task = self.pending.get(key)
        if task is None:
            if len(self.pending) >= 128:
                raise TVError("REQUEST_SERVICE_BUSY", "求片核对繁忙", 429)
            # Include time waiting for the global three slots, so abandoned
            # page loads cannot leave an unlimited-duration metadata queue.
            task = asyncio.create_task(asyncio.wait_for(bounded(), 40))
            self.pending[key] = task
            def finished(done):
                if self.pending.get(key) is done:
                    self.pending.pop(key, None)
                if not done.cancelled():
                    if done.exception() is None:
                        self.metadata[key] = time.monotonic() + 300, done.result()
                        while len(self.metadata) > 256:
                            self.metadata.popitem(last=False)
            task.add_done_callback(finished)
        try:
            value = await asyncio.shield(task)
            self.metadata[key] = time.monotonic() + 300, value
            while len(self.metadata) > 256:
                self.metadata.popitem(last=False)
            return value
        finally:
            if task.done():
                self.pending.pop(key, None)

    async def expected(self, item, season):
        tmdb = item.get("providerIds", {}).get("Tmdb")
        if not tmdb or type(season) is not int:
            return None
        fresh = await self._metadata(("media", tmdb), lambda: self.gateway.detail(f"tmdb:tv:{tmdb}"))
        if not same_identity(item, fresh):
            return None
        counts = [row.get("episodeCount") for media in (item, fresh) for row in media.get("seasons", [])
                  if row.get("number") == season and type(row.get("episodeCount")) is int and row["episodeCount"] > 0]
        if not counts:
            return None
        rows = await self._metadata(("episodes", tmdb, season), lambda: self.gateway.request("GET", f"/tmdb/{tmdb}/{season}"))
        if not isinstance(rows, list) or not rows or len(rows) > 1000:
            return None
        # A known larger total may not be silently reduced to the aired subset.
        numbers = set()
        for row in rows:
            if not isinstance(row, dict) or type(row.get("season_number")) is not int or row["season_number"] != season or type(row.get("episode_number")) is not int or not 0 <= row["episode_number"] <= 10000:
                return None
            if row.get("show_id") is not None and str(row["show_id"]) != str(tmdb):
                return None
            numbers.add(row["episode_number"])
        return numbers if len(numbers) == len(rows) == max(counts) else None

    async def _items(self, client, path, params):
        found = []
        for start in range(0, 1000, 200):
            async with self.slots:
                async with client.get(self.origin + path, params={**params, "StartIndex": start, "Limit": 200},
                                      headers={"X-Emby-Token": self.api_key}, allow_redirects=False) as response:
                    if response.status != 200:
                        raise TVError("LIBRARY_UNAVAILABLE", "媒体库暂时无法核对", 502)
                    raw = await response.content.read(4 * 1024 * 1024 + 1)
                    if len(raw) > 4 * 1024 * 1024:
                        raise ValueError()
                    data = json.loads(raw)
            rows = data.get("Items")
            if not isinstance(rows, list):
                raise ValueError()
            found.extend(rows)
            total = data.get("TotalRecordCount")
            if (type(total) is int and len(found) >= total) or (total is None and len(rows) < 200):
                return found
            if not rows:
                raise ValueError()
        raise TVError("LIBRARY_UNAVAILABLE", "媒体库核对结果过多", 502)

    async def __call__(self, identity, records):
        started = time.monotonic()
        records = [row for row in records if row.get("item", {}).get("providerIds")]
        if not records:
            return {}
        providers = sorted({f"{key}.{value}" for row in records for key, value in row["item"]["providerIds"].items()})
        user = quote(str(identity["embyUserId"]), safe="")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), trust_env=False) as client:
            try:
                rows = await asyncio.wait_for(self._items(client, f"/Users/{user}/Items", {"Recursive": "true", "IncludeItemTypes": "Movie,Series",
                    "Fields": "ProviderIds,MediaSources", "AnyProviderIdEquals": ",".join(providers)}), self.budget_seconds)
            except (TVError, aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
                return {}
            async def check(record):
                item = record["item"]
                candidates = [row for row in rows if matches(row, item)]
                if item["type"] == "movie":
                    return record["requestId"], any(playable(row) for row in candidates)
                if not candidates:
                    return record["requestId"], False
                try:
                    expected = await self.expected(item, record.get("season"))
                    if not expected:
                        return record["requestId"], None
                    available = set()
                    for series in candidates:
                        episodes = await self._items(client, "/Shows/" + quote(str(series["Id"]), safe="") + "/Episodes", {
                            "UserId": identity["embyUserId"], "Season": record["season"], "Fields": "MediaSources",
                            "IsMissing": "false", "IsVirtualUnaired": "false"})
                        available.update(episode_numbers(episodes, series["Id"], record["season"]))
                    return record["requestId"], expected <= available
                except (TVError, aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError):
                    return record["requestId"], None
            tasks = [asyncio.create_task(check(row)) for row in records]
            try:
                done, _ = await asyncio.wait(tasks, timeout=max(0, self.budget_seconds - (time.monotonic() - started)))
                # Slow seasons must not delay already-confirmed movies/seasons
                # or block the list beyond one total six-second budget.
                return dict(task.result() for task in done if not task.cancelled() and task.exception() is None)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
