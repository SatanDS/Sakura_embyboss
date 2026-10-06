"""Authenticated browse/subscribe workflow. No chat sends or wanted-list writes."""
import asyncio
import json
from collections import OrderedDict
from datetime import datetime, timedelta
import time

from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from .request_media import deduplicate, parse_key
from .request_models import MediaRequest, MediaRequestOwner
from .service import TVError


def serialize(row):
    return {"state": row.state, "requestId": row.key, "key": row.media_key, "season": row.season,
            "item": row.item, "updatedAt": row.updated_at.isoformat() + "Z", "error": row.error}


def canonical_media_key(item):
    # A verified cross-source TMDB mapping shares the same DB exclusion key.
    # Title/year display deduplication is intentionally not used here.
    tmdb = item.get("providerIds", {}).get("Tmdb")
    return f"tmdb:{item['type']}:{tmdb}" if tmdb else item["key"]


class RequestStore:
    def __init__(self, sessions, daily_limit=20):
        self.sessions, self.daily_limit = sessions, daily_limit

    def find(self, tg, media_key):
        with self.sessions() as db:
            rows = db.query(MediaRequest).join(MediaRequestOwner, MediaRequestOwner.request_key == MediaRequest.key).filter(
                MediaRequestOwner.tg == tg, MediaRequest.media_key == media_key).order_by(MediaRequest.updated_at.desc()).all()
            return [serialize(row) for row in rows]

    def mine(self, tg, page):
        with self.sessions() as db:
            rows = db.query(MediaRequest).join(MediaRequestOwner, MediaRequestOwner.request_key == MediaRequest.key).filter(
                MediaRequestOwner.tg == tg).order_by(MediaRequestOwner.created_at.desc()).offset((page - 1) * 30).limit(31).all()
            return {"items": [serialize(row) for row in rows[:30]], "page": page, "hasMore": len(rows) > 30}

    def claim(self, tg, item, season):
        media_key = canonical_media_key(item)
        key = media_key + (f":s{season}" if item["type"] == "tv" else "")
        now = datetime.utcnow()
        # Unique media/season PK is the cross-worker exclusion guard. An uncertain
        # external POST retains pending state; subsequent calls only reconcile it.
        try:
            with self.sessions.begin() as db:
                row = db.query(MediaRequest).filter_by(key=key).with_for_update().one_or_none()
                owner = db.get(MediaRequestOwner, (tg, key))
                if not owner:
                    recent = db.query(MediaRequestOwner).filter(MediaRequestOwner.tg == tg, MediaRequestOwner.created_at >= now - timedelta(days=1)).count()
                    if recent >= self.daily_limit:
                        raise TVError("REQUEST_LIMIT_REACHED", "今日求片数量已达上限，请明天再试", 429)
                    db.add(MediaRequestOwner(tg=tg, request_key=key, created_at=now))
                claimed = row is None or row.state == "failed"
                if row is None:
                    row = MediaRequest(key=key, media_key=media_key, season=season, item=item, state="pending", created_at=now, updated_at=now)
                    db.add(row)
                elif claimed:
                    row.state, row.error, row.updated_at = "pending", None, now
                db.flush()
                return serialize(row), claimed
        except IntegrityError:
            # Another process inserted the same media between SELECT and INSERT.
            with self.sessions.begin() as db:
                row = db.get(MediaRequest, key)
                if row is None:
                    raise
                if db.get(MediaRequestOwner, (tg, key)) is None:
                    db.add(MediaRequestOwner(tg=tg, request_key=key, created_at=now))
                return serialize(row), False

    def update(self, key, state, mp_id=None, error=None):
        with self.sessions.begin() as db:
            row = db.query(MediaRequest).filter_by(key=key).with_for_update().one()
            # Once accepted, a late timed-out caller cannot revert the job.
            if (row.state in {"subscribed", "complete"} and state in {"pending", "failed"}) or (row.state == "complete" and state == "subscribed"):
                return serialize(row)
            row.state, row.error, row.updated_at = state, error, datetime.utcnow()
            if mp_id:
                row.mp_id = mp_id
            return serialize(row)


class MediaRequests:
    def __init__(self, gateway, store, library, completion=None):
        self.gateway, self.store, self.library = gateway, store, library
        self.completion = completion
        self.observations = OrderedDict()
        self.cache, self.pending = OrderedDict(), {}

    async def cached(self, key, loader, ttl):
        previous = self.cache.get(key)
        if previous and previous[0] > time.monotonic():
            self.cache.move_to_end(key)
            return previous[1]
        job = self.pending.get(key)
        if job is None:
            if len(self.pending) >= 32:
                raise TVError("REQUEST_SERVICE_BUSY", "求片服务繁忙，请稍后重试", 429)
            job = asyncio.create_task(loader())
            self.pending[key] = job
            def complete(task):
                if self.pending.get(key) is task:
                    self.pending.pop(key, None)
                if not task.cancelled():
                    # Consume errors even when all waiting desktop requests were
                    # cancelled; the next request may try the lookup again.
                    task.exception()
            job.add_done_callback(complete)
        try:
            value = await asyncio.shield(job)
            self.cache[key] = (time.monotonic() + ttl, value)
            while len(self.cache) > 256:
                self.cache.popitem(last=False)
            return value
        finally:
            if job.done():
                self.pending.pop(key, None)

    async def catalog(self, kind, page):
        async def load():
            values = await asyncio.gather(self.gateway.recommend("tmdb", kind, page), self.gateway.recommend("douban", kind, page), return_exceptions=True)
            warnings = []
            for i, source in enumerate(("tmdb", "douban")):
                if isinstance(values[i], BaseException):
                    warnings.append({"source": source, "code": getattr(values[i], "code", "SOURCE_UNAVAILABLE"), "message": "TMDB 热门暂时不可用" if i == 0 else "豆瓣国产补充暂时不可用"})
                    values[i] = []
            if len(warnings) == 2:
                raise TVError("CATALOG_UNAVAILABLE", "热门影视暂时无法加载，请稍后重试", 502)
            items = deduplicate(*values)
            return {"items": items, "page": page, "hasMore": len(values[0]) >= 20 or len(values[1]) >= 12, "warnings": warnings, "enabled": True}
        return await self.cached(("catalog", kind, page), load, 900)

    async def media(self, key):
        parse_key(key)
        return await self.cached(("detail", key), lambda: self.gateway.detail(key), 3600)

    async def reconcile(self, identity, records):
        if not self.completion or not records:
            return records
        candidates = [row for row in records if row["state"] in {"pending", "subscribed", "complete"}]
        if not candidates:
            return records
        scope = str(identity["telegramId"]), str(identity["embyUserId"])
        def observation_key(record):
            item = record.get("item") or {}
            return (*scope, record["requestId"], json.dumps([item.get("providerIds"), item.get("seasons")], sort_keys=True))
        key = ("completion", *scope, tuple(sorted(observation_key(row) for row in candidates)))
        try:
            result = await self.cached(key, lambda: self.completion(identity, candidates), 30)
        except (TVError, asyncio.TimeoutError, OSError, ValueError, TypeError):
            result = {}
        output = []
        for record in records:
            arrived = result.get(record["requestId"])
            observed = observation_key(record)
            if arrived is True or arrived is False:
                self.observations[observed] = arrived
                self.observations.move_to_end(observed)
                while len(self.observations) > 2048:
                    self.observations.popitem(last=False)
            else:
                arrived = self.observations.get(observed)
            if arrived is True:
                if record["state"] != "complete":
                    record = await run_in_threadpool(self.store.update, record["requestId"], "complete")
                output.append({**record, "state": "complete"})
            elif arrived is not True and record["state"] == "complete":
                # The shared job remembers delivery. Current-user visibility
                # must not globally downgrade another owner's delivered job.
                output.append({**record, "state": "subscribed"})
            else:
                output.append(record)
        return output

    async def mine(self, identity, page):
        result = await run_in_threadpool(self.store.mine, int(identity["telegramId"]), page)
        return {**result, "items": await self.reconcile(identity, result["items"])}

    async def detail(self, identity, key):
        item = await self.media(key)
        library, subscriptions = await asyncio.gather(self.library(identity, item), run_in_threadpool(self.store.find, int(identity["telegramId"]), canonical_media_key(item)))
        if self.completion:
            subscriptions = await self.reconcile(identity, subscriptions)
        elif library["available"]:
            for record in subscriptions:
                # A Series entry alone does not establish that a requested season
                # has arrived, so never mark a TV subscription complete here.
                if item["type"] == "movie" and record["state"] != "complete":
                    await run_in_threadpool(self.store.update, record["requestId"], "complete")
                    record["state"] = "complete"
        return {"item": item, "library": library, "subscription": subscriptions[0] if subscriptions else {"state": "none"},
                "subscriptions": subscriptions, "canSubscribe": not library["available"]}

    async def subscribe(self, identity, key, season, reauthorize):
        item = await self.media(key)
        if item["type"] == "tv":
            if type(season) is not int or not 0 <= season <= 999:
                raise TVError("INVALID_REQUEST", "请选择正确的季数", 400)
            if not item["seasons"] or season not in {row["number"] for row in item["seasons"]}:
                raise TVError("SEASON_UNAVAILABLE", "无法确认此季资料，请更新资料后重试", 409)
        elif season is not None:
            raise TVError("INVALID_REQUEST", "电影不能指定季数", 400)
        library = await self.library(identity, item)
        if library["available"]:
            return {"state": "available", "library": library, "duplicate": True}
        await reauthorize()
        record, claimed = await run_in_threadpool(self.store.claim, int(identity["telegramId"]), item, season)
        if record["state"] in {"subscribed", "complete"}:
            return {**record, "duplicate": True}
        submitted = False
        try:
            existing = await self.gateway.find_subscription(item, season)
            if existing:
                record = await run_in_threadpool(self.store.update, record["requestId"], "subscribed", existing)
                return {**record, "duplicate": True}
            if not claimed:
                return {**record, "duplicate": True, "message": "订阅结果正在核对，尚未重复提交"}
            await reauthorize()  # logout/expiry while slow catalog/library lookup must prevent a POST
            submitted = True
            mp_id = await self.gateway.subscribe(item, season)
            record = await run_in_threadpool(self.store.update, record["requestId"], "subscribed", mp_id)
            return {**record, "duplicate": False}
        except TVError as error:
            # A POST timeout can mean accepted. Retain uncertainty, never blindly
            # retry a mutating call. Definitive rejects can be corrected/retried.
            definitive = not submitted or error.code in {"SUBSCRIPTION_REJECTED", "MOVIEPILOT_AUTH", "MOVIEPILOT_FORBIDDEN", "MOVIEPILOT_API_VERSION", "MOVIEPILOT_RATE_LIMITED", "TOKEN_EXPIRED", "ACCOUNT_DISABLED"}
            if claimed:
                await run_in_threadpool(self.store.update, record["requestId"], "failed" if definitive else "pending", None, error.code)
            raise
