"""Routes mounted exclusively on the dedicated TV listener, never payments."""

import asyncio
import html
import json
import secrets
import time
from collections import OrderedDict, deque

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from bot.func_helper.emby_identity import lookup_user_from_auth_db

from . import runtime
from .avatars import avatar_cache, AvatarUnavailable
from .danmaku import DanmakuError, fetch_danmaku
from .service import PREFIX, TVError, canonical_url

RESPONSE_CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"


class RateLimit:
    def __init__(self):
        self.buckets = OrderedDict()

    def check(self, key, limit, period=60):
        now = time.monotonic()
        bucket = self.buckets.setdefault(key, deque())
        self.buckets.move_to_end(key)
        while bucket and bucket[0] <= now - period:
            bucket.popleft()
        if len(bucket) >= limit:
            raise TVError("RATE_LIMITED", "请求过于频繁，请稍后重试", 429)
        bucket.append(now)
        while len(self.buckets) > 4096:
            self.buckets.popitem(last=False)


limits = RateLimit()


class TVRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe(request):
            try:
                # Do not trust client-supplied forwarded headers.
                peer = request.client.host if request.client else "unknown"
                limits.check((peer, "all"), 6000)
                if request.url.path.endswith("/start"):
                    limits.check((peer, "start"), 120)
                if request.method == "POST":
                    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                        raise TVError("INVALID_REQUEST", "需要 JSON 请求", 415)
                    chunks, size = [], 0
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > 32768:
                            raise TVError("INVALID_REQUEST", "请求内容过大", 413)
                        chunks.append(chunk)
                    request._body = b"".join(chunks)
                response = await handler(request)
            except TVError as error:
                response = JSONResponse({"code": error.code, "message": error.message}, status_code=error.status)
            except (json.JSONDecodeError, UnicodeDecodeError):
                response = JSONResponse({"code": "INVALID_REQUEST", "message": "JSON 请求无效"}, status_code=400)
            except Exception:
                # Never expose callback links, credentials or SQL parameters.
                response = JSONResponse({"code": "SERVICE_UNAVAILABLE", "message": "登录服务暂时不可用，请稍后重试"}, status_code=503)
            response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                     "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"})
            response.headers.setdefault("Content-Security-Policy", RESPONSE_CSP)
            return response
        return safe


router = APIRouter(prefix=PREFIX, route_class=TVRoute)


def token(request):
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or len(header) > 128:
        raise TVError("TOKEN_EXPIRED", "请先登录 Telegram", 401)
    return header[7:]


async def body(request):
    data = await request.json()
    if not isinstance(data, dict):
        raise TVError("INVALID_REQUEST", "需要 JSON 对象", 400)
    return data


async def invoke(method, *args):
    service = runtime.service()
    return await run_in_threadpool(getattr(service, method), *args)


@router.get("/config")
async def public_config():
    cfg, _, username = runtime.settings()
    return {"botUsername": username, "privacyVersion": cfg.privacy_version, "apiVersion": "v1"}


@router.post("/auth/telegram/start")
async def start(request: Request):
    data = await body(request)
    cfg, url, username = runtime.settings()
    limits.check((str(data.get("installationId", ""))[:64], "install-start"), 10)
    return await invoke("start", data, url, username)


@router.get("/auth/telegram/authorize")
async def authorize(request: Request):
    _, _, username = runtime.settings()
    link = request.query_params.get("request", "")
    result = await invoke("landing", link)
    username, payload = html.escape(username), html.escape(f"tvlogin_{link}")
    nonce = secrets.token_urlsafe(24)
    # This page never handles Telegram passwords, OTPs or session credentials.
    return HTMLResponse(f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>DuShengTV · Telegram 登录</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:#10151d;color:#edf2fa;font:17px/1.8 system-ui,sans-serif;display:grid;min-height:100vh;place-items:center}}main{{width:100%;max-width:510px;padding:28px}}h1{{font-size:32px}}.code{{letter-spacing:.3em;font-size:36px;font-weight:700;margin:12px 0}}a,button{{display:block;width:100%;text-align:center;padding:12px;color:white;border-radius:12px;text-decoration:none;font:inherit}}.primary{{background:#299fda}}.secondary,button{{background:#1e2a38;border:1px solid #425168;margin-top:12px;cursor:pointer}}small,#copy-status{{color:#b7c2d1}}.fallback{{border-top:1px solid #425168;margin-top:24px;padding-top:8px}}textarea{{display:block;width:100%;padding:12px;border:1px solid #425168;border-radius:8px;background:#0c1118;color:#edf2fa;font:14px/1.6 monospace;resize:none}}#copy-status{{min-height:1.8em;font-size:14px}}</style>
<main><h1>登录 DuShengTV</h1><p>确认下面的验证码与 TV 客户端一致，再前往 Telegram Bot 确认登录。</p>
<p class="code">{html.escape(result['displayCode'])}</p>
<a class="primary" href="tg://resolve?domain={username}&amp;start={payload}" rel="noreferrer">打开 Telegram</a>
<a class="secondary" href="https://t.me/{username}?start={payload}" rel="noreferrer">使用网页链接打开 @{username}</a>
<p>进入 Bot 聊天后，点击底部的「开始 / Start」发送本次登录请求；即使以前登录过，也要发送新的请求。</p>
<p>点击 Bot 的「确认登录」后，返回 DuShengTV，客户端会自动完成登录。</p>
<div class="fallback"><p>如果只打开了聊天、没有出现确认卡，可复制下面的完整指令，粘贴到 @{username} 私聊并发送。</p>
<textarea id="login-command" rows="2" readonly spellcheck="false" aria-label="本次登录指令">/start {payload}</textarea>
<button id="copy-command" type="button">复制本次登录指令</button>
<p id="copy-status" role="status" aria-live="polite"></p></div>
<small>请求 5 分钟内有效。请勿转发本页或登录指令；如果不是你本人发起，请在 Bot 中拒绝。</small></main>
<script nonce="{nonce}">
const command = document.getElementById('login-command');
const status = document.getElementById('copy-status');
document.getElementById('copy-command').addEventListener('click', async () => {{
  try {{
    await navigator.clipboard.writeText(command.value);
    status.textContent = '已复制，请粘贴到 Bot 私聊并发送，再核对确认码。';
  }} catch {{
    command.focus(); command.select();
    status.textContent = '已选中指令，请手动复制并发送到 Bot 私聊。';
  }}
}});
</script></html>""", headers={"Content-Security-Policy": f"{RESPONSE_CSP}; script-src 'nonce-{nonce}'"})


@router.post("/auth/telegram/poll")
async def poll(request: Request):
    data = await body(request)
    limits.check((str(data.get("challengeId", ""))[:32], "poll"), 90)
    return await invoke("poll", data)


@router.post("/auth/telegram/cancel")
async def cancel(request: Request):
    return await invoke("poll", await body(request), True)


@router.get("/session")
async def session_info(request: Request):
    return await invoke("session", token(request))


@router.get("/profile/avatar")
async def profile_avatar(request: Request):
    bearer = token(request)
    identity = await invoke("server_identity", bearer)
    limits.check((identity["telegramId"], "avatar"), 30)
    try:
        image = await avatar_cache.get(identity["telegramId"])
    except AvatarUnavailable:
        raise TVError("AVATAR_UNAVAILABLE", "头像暂时无法加载", 503) from None
    # A picture is private account data. Recheck logout, revocation and binding
    # after the potentially slow Telegram fetch before returning any bytes.
    if await invoke("server_identity", bearer) != identity:
        raise TVError("TOKEN_EXPIRED", "登录状态已改变", 401)
    if image is None:
        return Response(status_code=204)
    data, media_type = image
    return Response(content=data, media_type=media_type)


@router.post("/session/refresh")
async def refresh(request: Request):
    return await invoke("refresh", await body(request))


@router.post("/session/logout")
async def logout(request: Request):
    return await invoke("logout", token(request))


@router.post("/devices/challenge")
async def device_challenge(request: Request):
    return await invoke("challenge", token(request), (await body(request)).get("installationId"))


@router.post("/devices/register")
async def register(request: Request):
    return await invoke("register", token(request), await body(request))


@router.post("/devices/heartbeat")
async def heartbeat(request: Request):
    return await invoke("heartbeat", token(request), await body(request))


@router.get("/devices")
async def devices(request: Request):
    return await invoke("devices", token(request))


@router.get("/settings/cloud")
async def cloud_settings_get(request: Request):
    bearer = token(request)
    limits.check((bearer, "cloud-read"), 60)
    return await invoke("cloud_settings", bearer)


@router.post("/settings/cloud")
async def cloud_settings_save(request: Request):
    bearer = token(request)
    limits.check((bearer, "cloud-write"), 10)
    return await invoke("cloud_settings", bearer, await body(request))


@router.delete("/devices/{device_id}")
async def revoke(request: Request, device_id: str):
    return await invoke("revoke", token(request), device_id)


async def emby_identity(server_url, access_token):
    db_path = runtime.emby_auth_db_path()
    if not db_path:
        raise TVError("EMBY_AUTH_NOT_CONFIGURED", "Bot 尚未配置 Emby 令牌验证，请管理员挂载认证数据库并设置 emby_auth_db_path", 503)
    user_id, reason = await run_in_threadpool(lookup_user_from_auth_db, access_token, db_path)
    if not user_id:
        if reason.startswith("Emby authentication database"):
            raise TVError("EMBY_AUTH_UNAVAILABLE", "Bot 无法读取 Emby 认证数据库，请管理员检查挂载和 emby_auth_db_path", 503)
        raise TVError("SERVER_NOT_BOUND", "Emby 登录令牌无效或账号不可用，请重新登录 Emby")
    # Emby 4.9 has no /Users/Me, and /Users/{Id} alone does not prove token
    # ownership. This GUID comes exclusively from the read-only token DB.
    # A live request then checks that Emby still accepts the user's token
    # and that the canonical account is enabled. Never use the Bot API key.
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=False) as client:
            async with client.get(f"{server_url}/Users/{user_id}", headers={"X-Emby-Token": access_token}, allow_redirects=False) as response:
                if response.status != 200:
                    if response.status in (400, 401, 403, 404):
                        raise TVError("SERVER_NOT_BOUND", "Emby 登录令牌无效或账号不可用，请重新登录 Emby")
                    raise TVError("EMBY_UNAVAILABLE", "Bot 暂时无法连接 Emby 验证服务，请稍后重试", 502)
                # Bound the response even when Content-Length is absent.
                raw = await response.content.read(65537)
                if len(raw) > 65536:
                    raise TVError("EMBY_UNAVAILABLE", "Emby 验证响应无效，请稍后重试", 502)
                user = json.loads(raw)
                if not isinstance(user, dict) or user.get("Id") != user_id or not isinstance(user.get("Policy"), dict):
                    raise TVError("EMBY_UNAVAILABLE", "Emby 验证响应无效，请稍后重试", 502)
                if user["Policy"].get("IsDisabled"):
                    raise TVError("SERVER_NOT_BOUND", "Emby 账号已停用")
                return user_id
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        raise TVError("EMBY_UNAVAILABLE", "Bot 暂时无法连接 Emby 验证服务，请稍后重试", 502) from None


@router.post("/servers/authorize")
async def server_authorize(request: Request):
    bearer, data = token(request), await body(request)
    identity = await invoke("server_identity", bearer)
    cfg, _, _ = runtime.settings()
    server_url = canonical_url(data.get("serverUrl"))
    allowed = {canonical_url(url) for url in cfg.server_urls}
    if server_url not in allowed or data.get("embyUserId") != identity["embyUserId"]:
        raise TVError("SERVER_NOT_BOUND", "此服务器或 Emby 用户未绑定当前 Telegram 账号")
    access_token = data.get("embyAccessToken")
    if not isinstance(access_token, str) or not 1 <= len(access_token) <= 4096 or any(ord(c) < 32 for c in access_token):
        raise TVError("INVALID_REQUEST", "Emby 凭据格式无效", 400)
    # Whitelisted public URLs are aliases of the Bot's primary Emby. Verify
    # the user's token at that configured origin; regional CDN DNS may send
    # the Bot to a different or unavailable edge than the desktop client.
    if await emby_identity(runtime.emby_origin(), access_token) != identity["embyUserId"]:
        raise TVError("SERVER_NOT_BOUND", "Emby 令牌不属于当前 Telegram 绑定的账号")
    # Recheck revocation and binding after the outbound request.
    if await invoke("server_identity", bearer) != identity:
        raise TVError("SERVER_NOT_BOUND", "账号绑定已改变，请重新登录")
    return {**identity, "allowed": True, "bound": True, "serverUrl": server_url}


@router.post("/providers/danmaku")
async def danmaku(request: Request):
    bearer = token(request)
    identity = await invoke("server_identity", bearer)
    limits.check((identity["telegramId"], "danmaku"), 12)
    data = await body(request)
    try:
        result = await fetch_danmaku(data)
    except DanmakuError as error:
        # Recheck authorization even when the provider fails or times out.
        await invoke("server_identity", bearer)
        raise TVError(error.code, error.message, error.status) from None
    if await invoke("server_identity", bearer) != identity:
        raise TVError("TOKEN_EXPIRED", "登录状态已改变", 401)
    return result
