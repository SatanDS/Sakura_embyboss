"""Authenticated watch-party control channel. Each client opens its own media."""

import asyncio
import json
import time
from contextlib import asynccontextmanager

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from .api import TVRoute, body, invoke, limits, token
from .service import PREFIX, TVError
from .watch import invalid

SESSION_CHECK_SECONDS = 10

@asynccontextmanager
async def watch_lifespan(app):
    ticker = asyncio.create_task(app.state.watch.run())
    try:
        yield
    finally:
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)


router = APIRouter(prefix=PREFIX + "/watch/rooms", route_class=TVRoute, lifespan=watch_lifespan)


@router.post("")
async def create(request: Request):
    identity = await invoke("watch_identity", token(request))
    limits.check((identity["account"], "watch-create"), 12)
    return request.app.state.watch.create(identity, await body(request))


@router.post("/{code}/join")
async def join(code: str, request: Request):
    identity = await invoke("watch_identity", token(request))
    limits.check((identity["account"], "watch-join"), 15)
    return request.app.state.watch.join(code, identity)


@router.post("/{code}/leave")
async def leave(code: str, request: Request):
    identity = await invoke("watch_identity", token(request))
    rooms = request.app.state.watch
    room = rooms.room(code)
    rooms.leave(room, rooms.member(room, identity))
    return {"left": True}


@router.websocket("/{code}/socket")
async def socket(ws: WebSocket, code: str):
    rooms = ws.app.state.watch
    try:
        identity = await invoke("watch_identity", token(ws))
        limits.check((identity["account"], "watch-connect"), 30)
        room = rooms.room(code)
        member = rooms.member(room, identity)
    except TVError as err:
        await ws.accept()
        await ws.send_json({"type": "closed", "message": err.message, "code": err.code})
        await ws.close(code=1008)
        return
    await ws.accept()
    # A concurrent leave can remove the reservation while the handshake yields.
    if rooms.rooms.get(code) is not room or room.members.get(member.device) is not member:
        await ws.send_json({"type": "closed", "message": "房间已结束"})
        await ws.close()
        return
    queue = rooms.connect(room, member)

    async def send():
        while True:
            message = await queue.get()
            await asyncio.wait_for(ws.send_json(message), timeout=5)
            if message["type"] == "closed":
                return

    async def receive():
        deadline = time.monotonic() + SESSION_CHECK_SECONDS
        while member.queue is queue:
            try:
                frame = await asyncio.wait_for(ws.receive(), timeout=max(0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                frame = None
            if frame is not None and frame["type"] == "websocket.disconnect":
                return
            data = None
            if frame is not None:
                text = frame.get("text")
                if not isinstance(text, str) or len(text.encode("utf-8")) > 2048:
                    raise invalid()
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise invalid()
                limits.check((member.id, "watch-messages"), 180)
            now = time.monotonic()
            if now >= deadline or data is not None and data.get("type") in ("intent", "status"):
                try:
                    await invoke("check_watch_session", identity["session"])
                except TVError:
                    if member.queue is queue:
                        rooms.leave(room, member, "房主登录状态已改变，房间已结束")
                    return
                deadline = now + SESSION_CHECK_SECONDS
            if member.queue is not queue or data is None:
                continue
            now_ms = rooms.clock()
            try:
                if data.get("type") == "ping":
                    sent = data.get("sentAt")
                    if type(sent) not in (float, int) or not 0 <= sent <= 2 ** 53:
                        raise invalid()
                    queue.put_nowait({"type": "pong", "sentAt": sent, "serverAt": now_ms})
                elif data.get("type") == "status":
                    room.status(member, data, now_ms)
                elif data.get("type") == "intent":
                    room.intent(member, data, now_ms)
                else:
                    raise invalid()
            except TVError as err:
                queue.put_nowait({"type": "error", "message": err.message, "code": err.code})

    sender, receiver = asyncio.create_task(send()), asyncio.create_task(receive())
    try:
        done, _ = await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if receiver in done and member.queue is not queue:
            await asyncio.wait_for(sender, 5)
    except (WebSocketDisconnect, OSError, asyncio.TimeoutError, asyncio.QueueFull, ValueError, TVError):
        pass
    finally:
        rooms.disconnect(room, member, queue)
        for task in (sender, receiver):
            task.cancel()
        await asyncio.gather(sender, receiver, return_exceptions=True)
        try:
            await ws.close()
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
