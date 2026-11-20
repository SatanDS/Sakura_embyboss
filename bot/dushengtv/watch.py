"""Ephemeral watch rooms owned by the dedicated, single-process TV listener."""

import asyncio
import re
import secrets
import time
from dataclasses import dataclass

from .service import TVError

CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def invalid(message="房间请求无效"):
    return TVError("WATCH_INVALID", message, 400)


def media_ref(value):
    if not isinstance(value, dict) or set(value) != {"itemId", "mediaSourceId"}:
        raise invalid("需要影片及版本标识")
    if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v) for v in value.values()):
        raise invalid("影片或版本标识无效")
    return dict(value)


def position(value):
    if type(value) not in (float, int) or not 0 <= value <= 366 * 86400:
        raise invalid("播放位置无效")
    return value


@dataclass
class Member:
    id: str
    device: str
    name: str
    offline_since: float
    queue: asyncio.Queue | None = None
    status: str = "loading"
    blocking: bool = False
    buffer_since: float | None = None

    def presence(self, playing):
        online = self.queue is not None
        status = "catchingUp" if playing and online and not self.blocking and self.status != "ready" else self.status
        return {"id": self.id, "name": self.name, "status": status, "online": online}


def finish(queue, message):
    # End even a backlogged or replaced connection with a terminal event.
    while not queue.empty():
        queue.get_nowait()
    queue.put_nowait({"type": "closed", "message": message})


class Room:
    def __init__(self, code, host, media, start, now):
        self.code, self.host = code, host.id
        self.members = {host.device: host}
        self.media, self.position, self.at = media, start, now
        self.state, self.paused_by, self.revision = "lobby", None, 1
        self.broadcast_waiting = []

    def anchor(self, state, now, target=None):
        if target is None:
            target = self.position
            if self.state == "playing":
                target += max(0, now - self.at) / 1000
        self.position, self.state = target, state
        self.at = now + (500 if state == "playing" else 0)
        self.revision += 1

    def waiting(self, now):
        wait = []
        for member in self.members.values():
            if member.queue is None:
                if member.id == self.host:
                    wait.append(member.id)
                continue
            if member.status == "ready":
                continue
            if self.state == "lobby" or (member.blocking and (
                member.buffer_since is None or now - member.buffer_since >= 3000
            )):
                wait.append(member.id)
        return wait

    def settle(self, now):
        wait = self.waiting(now)
        if self.state == "playing" and wait:
            self.anchor("waiting", now)
            return True
        if self.state == "waiting" and not wait:
            self.anchor("playing", now)
            return True
        return wait != self.broadcast_waiting

    def broadcast(self, now):
        self.broadcast_waiting = self.waiting(now)
        snapshot = {"code": self.code, "hostId": self.host, "state": self.state, "pausedBy": self.paused_by,
                    "media": self.media, "position": self.position, "at": self.at, "revision": self.revision,
                    "waitingFor": self.broadcast_waiting,
                    "members": [m.presence(self.state == "playing") for m in self.members.values()]}
        for member in self.members.values():
            if member.queue is not None:
                if member.queue.full():
                    finish(member.queue, "连接过慢，请重新加入")
                else:
                    member.queue.put_nowait({"type": "snapshot", "room": {**snapshot, "selfId": member.id}})

    def status(self, member, data, now):
        if media_ref(data.get("media")) != self.media:
            return
        status = data.get("status")
        if status not in ("ready", "buffering", "loading"):
            raise invalid()
        if status == member.status:
            return
        member.status = status
        if status == "ready":
            member.blocking, member.buffer_since = True, None
        elif member.blocking and member.buffer_since is None:
            member.buffer_since = now
        self.settle(now)
        self.broadcast(now)

    def intent(self, member, data, now):
        action = data.get("action")
        if action not in ("play", "pause", "seek", "switch", "continue"):
            raise invalid()
        if type(data.get("revision")) is not int or data["revision"] != self.revision:
            raise TVError("WATCH_STALE", "房间状态已改变，请重试", 409)
        is_host = member.id == self.host
        if (action in ("seek", "switch", "continue") or action == "play"
                and (self.state == "lobby" or self.paused_by not in (None, member.id))) and not is_host:
            raise TVError("WATCH_FORBIDDEN", "当前成员没有此播放操作权限", 403)
        if action == "switch":
            media, target = media_ref(data.get("media")), position(data.get("position", 0))
            self.media = media
            for peer in self.members.values():
                peer.status, peer.blocking = "loading", peer.queue is not None
                peer.buffer_since = now - 3000 if peer.blocking else None
            self.anchor("waiting" if self.state in ("playing", "waiting") else self.state, now, target)
        elif action == "seek":
            self.anchor(self.state, now, position(data.get("position")))
        elif action == "pause":
            if self.state not in ("playing", "waiting"):
                return
            self.anchor("paused", now)
            self.paused_by = member.id
        else:
            if self.state == "playing":
                return
            if action == "continue":
                if self.state != "waiting":
                    raise invalid("当前没有等待缓冲的成员")
                for peer in self.members.values():
                    if peer.status != "ready":
                        peer.blocking, peer.buffer_since = False, None
            elif self.state == "waiting":
                raise invalid("请等待成员准备就绪")
            if self.waiting(now):
                raise invalid("请等待成员准备就绪")
            self.paused_by = None
            self.anchor("playing", now)
        self.broadcast(now)


class WatchRooms:
    def __init__(self, clock=None):
        # Use a monotonic elapsed interval for playback while exposing epoch milliseconds.
        epoch = time.time() - time.monotonic()
        self.clock = clock or (lambda: int((epoch + time.monotonic()) * 1000))
        self.rooms, self.devices = {}, {}

    def room(self, code):
        room = self.rooms.get(code)
        if room is None:
            raise TVError("WATCH_NOT_FOUND", "房间不存在或已结束", 404)
        return room

    def member(self, room, identity):
        member = room.members.get(identity["device"])
        if member is None:
            raise TVError("WATCH_FORBIDDEN", "请先加入此房间", 403)
        return member

    def reserve(self, identity):
        if identity["device"] in self.devices:
            raise TVError("WATCH_BUSY", "请先退出当前房间", 409)
        return Member(secrets.token_urlsafe(12), identity["device"], identity["name"], self.clock())

    def create(self, identity, data):
        media, start = media_ref(data.get("media")), position(data.get("position", 0))
        if len(self.rooms) >= 2048:
            raise TVError("WATCH_BUSY", "房间已满，请稍后重试", 503)
        host = self.reserve(identity)
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
            if code not in self.rooms:
                break
        self.devices[host.device] = code
        self.rooms[code] = Room(code, host, media, start, self.clock())
        return {"code": code, "selfId": host.id}

    def join(self, code, identity):
        room = self.room(code)
        member = room.members.get(identity["device"])
        if member is None:
            if len(room.members) >= 8:
                raise TVError("WATCH_FULL", "房间已满（最多 8 人）", 409)
            member = self.reserve(identity)
            room.members[member.device] = member
            self.devices[member.device] = code
            room.broadcast(self.clock())
        return {"code": code, "selfId": member.id}

    def connect(self, room, member):
        if member.queue is not None:
            finish(member.queue, "已从另一连接加入")
        now = self.clock()
        member.queue = asyncio.Queue(maxsize=32)
        member.status, member.buffer_since = "loading", None
        member.blocking = member.id == room.host
        room.settle(now)
        room.broadcast(now)
        return member.queue

    def disconnect(self, room, member, queue):
        if member.queue is not queue or self.rooms.get(room.code) is not room:
            return
        now = self.clock()
        member.queue, member.offline_since = None, now
        member.blocking, member.buffer_since = False, None
        room.settle(now)
        room.broadcast(now)

    def leave(self, room, member, reason="房主已解散房间"):
        if member.id == room.host:
            self.rooms.pop(room.code)
            for peer in room.members.values():
                self.devices.pop(peer.device, None)
                if peer.queue is not None:
                    finish(peer.queue, reason)
                    peer.queue = None
        else:
            if member.queue is not None:
                finish(member.queue, "已离开房间")
                member.queue = None
            del room.members[member.device]
            self.devices.pop(member.device, None)
            now = self.clock()
            room.settle(now)
            room.broadcast(now)

    def tick(self):
        now = self.clock()
        for room in list(self.rooms.values()):
            for peer in list(room.members.values()):
                if peer.queue is None and now - peer.offline_since >= 60000:
                    self.leave(room, peer, "房主断开连接，房间已结束")
                    if peer.id == room.host:
                        break
            else:
                if room.settle(now):
                    room.broadcast(now)

    async def run(self):
        while True:
            await asyncio.sleep(0.25)
            self.tick()
