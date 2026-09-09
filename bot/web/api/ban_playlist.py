"""Authenticate playlist mutations before applying account restrictions."""
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bot import LOGGER, group, bot
from bot.func_helper.emby import emby
from bot.sql_helper.sql_emby import sql_get_emby_by_embyid, sql_update_emby, Emby
from .webhook.line_report import resolve_user_context

route = APIRouter()


def is_playlist_mutation(method: str, uri: str) -> bool:
    if len(uri) > 16384:
        return False
    try:
        path = urlsplit(uri).path
    except ValueError:
        return False
    if method == "POST":
        return bool(re.fullmatch(
            r"/emby/Playlists(?:/[^/]+/Items(?:/[^/]+/Move/[^/]+)?)?", path, re.IGNORECASE
        ))
    return method == "DELETE" and bool(re.fullmatch(
        r"/emby/Playlists/[^/]+/Items/[^/]+", path, re.IGNORECASE
    ))


@route.get("/ban_playlist")
async def ban_playlist(request: Request, eid: str = ""):
    original_uri = request.headers.get("X-Original-URI", "")
    original_method = request.headers.get("X-Original-Method", "").upper()
    if not is_playlist_mutation(original_method, original_uri):
        return JSONResponse(status_code=400, content={"is_baned": False, "message": "Invalid playlist operation"})

    canonical_id, _, _ = await resolve_user_context(
        user_id=eid,
        token=request.headers.get("X-Emby-Token", ""),
        auth_header=request.headers.get("X-Emby-Authorization") or request.headers.get("Authorization", ""),
        original_request_uri=original_uri,
    )
    if not canonical_id:
        return JSONResponse(status_code=403, content={"is_baned": False, "message": "Unable to authenticate Emby user"})

    user = sql_get_emby_by_embyid(canonical_id)
    if not await emby.emby_change_policy(emby_id=canonical_id, disable=True):
        return JSONResponse(status_code=502, content={"is_baned": False, "message": "Emby policy update failed"})

    # Notification failures must not leave the local account marked active.
    if user is not None and not sql_update_emby(Emby.embyid == canonical_id, lv="c"):
        LOGGER.error("Playlist restriction applied in Emby but local state update failed")
        return JSONResponse(status_code=503, content={"is_baned": True, "message": "Local account update failed"})

    notification = f"播放列表操作已拦截，Emby 用户 {canonical_id} 已封禁。"
    try:
        out = await bot.send_message(group[0], notification)
        if user is not None and user.tg:
            await out.forward(user.tg)
    except Exception as exc:
        LOGGER.warning(f"Playlist restriction notification failed: {type(exc).__name__}")
    LOGGER.warning(notification)
    # This response also terminates the original request at the gateway.
    return JSONResponse(status_code=403, content={
        "user_id": user.tg if user is not None else None,
        "embyid": canonical_id,
        "is_baned": True,
        "message": "Playlist operation blocked",
    })
