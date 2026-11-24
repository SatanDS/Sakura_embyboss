"""Authenticated watch-party control channel. Each client opens its own media."""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import timedelta

import aiohttp
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect

from .api import TVRoute, body, invoke, limits, token
from .service import PREFIX, TVError
from . import runtime
from .watch import CHAT_CLIENT_ID, invalid

SESSION_CHECK_SECONDS = 10
VOICE_DELETE_DELAYS = (0, 1, 3, 7)


async def close_voice_room(name, config):
    # A closed control connection cannot relay the room-ended event to every caller.
    from livekit import api

    for delay in VOICE_DELETE_DELAYS:
        if delay:
            await asyncio.sleep(delay)
        try:
            async with api.LiveKitAPI(config.get("api_url") or config["url"], config["key"], config["secret"],
                                      timeout=aiohttp.ClientTimeout(total=3)) as client:
                await client.room.delete_room(api.DeleteRoomRequest(room=name))
            return
        except api.TwirpError as error:
            if error.code == "not_found":
                return
            if error.status < 500 and error.status not in (408, 429):
                break
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            continue
        except Exception:
            break
    logging.getLogger(__name__).warning("LiveKit 语音房间清理失败，请检查网关管理地址与签名配置")

@asynccontextmanager
async def watch_lifespan(app):
    ticker = asyncio.create_task(app.state.watch.run())
    try:
        yield
    finally:
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)
        await app.state.watch.shutdown()


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


@router.get("/{code}/chat")
async def chat_history(code: str, request: Request):
    identity = await invoke("watch_identity", token(request))
    limits.check((identity["account"], "watch-chat-history"), 60)
    rooms = request.app.state.watch
    room = rooms.room(code)
    rooms.member(room, identity)
    try:
        after = int(request.query_params.get("after", "0"))
    except ValueError:
        raise TVError("WATCH_INVALID", "聊天游标无效", 400) from None
    if after < 0 or after > 2 ** 53:
        raise TVError("WATCH_INVALID", "聊天游标无效", 400)
    return {"messages": room.chat_history(after)}


@router.post("/{code}/voice/token")
async def voice_token(code: str, request: Request):
    identity = await invoke("watch_identity", token(request))
    limits.check((identity["account"], "watch-voice-token"), 30)
    rooms = request.app.state.watch
    room = rooms.room(code)
    member = rooms.member(room, identity)
    config = runtime.livekit()
    if not config:
        raise TVError("VOICE_NOT_CONFIGURED", "房间语音服务尚未配置", 503)
    try:
        from livekit import api
        access = (api.AccessToken(config["key"], config["secret"])
                  .with_identity(member.id)
                  .with_name(member.name)
                  .with_ttl(timedelta(minutes=10))
                  .with_grants(api.VideoGrants(room_join=True, room=room.voice_name,
                                               can_publish=True, can_subscribe=True,
                                               can_publish_data=False,
                                               can_publish_sources=["microphone"])))
        signed = access.to_jwt()
    except ImportError:
        raise TVError("VOICE_NOT_CONFIGURED", "房间语音服务尚未安装", 503) from None
    room.voice_config = config
    return {"url": config["url"], "room": room.voice_name, "token": signed,
            "expiresAt": int(time.time()) + 600}


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
                if not isinstance(text, str) or len(text.encode("utf-8")) > 4096:
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
                elif data.get("type") == "chat.send":
                    limits.check((member.id, "watch-chat"), 30)
                    room.chat(member, data, now_ms)
                else:
                    raise invalid()
            except TVError as err:
                response = {"type": "error", "message": err.message, "code": err.code}
                if data.get("type") == "chat.send":
                    response["type"] = "chat.error"
                    client_id = data.get("clientMessageId")
                    if isinstance(client_id, str) and CHAT_CLIENT_ID.fullmatch(client_id):
                        response["clientMessageId"] = client_id
                queue.put_nowait(response)

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
