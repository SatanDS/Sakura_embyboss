#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
__init__.py - 
Author:susu
Date:2024/8/27
"""
from fastapi import APIRouter, Request, HTTPException, Depends
import secrets
from .ban_playlist import route as ban_playlist_route
from .webhook.favorites import router as favorites_router
from .webhook.media import router as media_router
from .webhook.client_filter import router as client_filter_router
from .webhook.line_report import router as line_report_router
from .user_info import route as user_info_route
from .login import router as login_router
from bot import bot_token, LOGGER, config

emby_api_route = APIRouter(prefix="/emby", tags=["对接Emby的接口"])
user_api_route = APIRouter(prefix="/user", tags=["对接用户信息的接口"])
auth_api_route = APIRouter(prefix="/auth", tags=["用户认证接口"])

async def verify_token(request: Request):
    """验证API请求的token"""
    try:
        # 从URL参数中获取token
        token = request.query_params.get("token")
        if not token:
            raise HTTPException(status_code=401, detail="No token provided")
        # 验证token是否与bot token匹配
        if token != bot_token:
            LOGGER.warning("Invalid API token attempt")
            raise HTTPException(status_code=403, detail="Invalid token")
        return True
    except HTTPException:
        raise
    except Exception as e:
        LOGGER.error(f"Token verification error: {str(e)}")
        raise HTTPException(status_code=500, detail="Token verification failed")


async def verify_loopback_request(request: Request):
    """Only allow calls originating from the same host.

    This dependency is kept for the legacy playlist protection endpoint,
    whose Caddy integration predates the shared line-report secret.
    """
    client_host = request.client.host if request.client else ""
    allowed_hosts = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}
    if client_host not in allowed_hosts:
        raise HTTPException(status_code=403, detail="Internal endpoint")
    return True


async def verify_line_report_request(request: Request):
    """Require loopback plus the CDN→Caddy→Bot shared secret.

    The source-address check protects the endpoint when Caddy runs with host
    networking.  The shared token is still required so a process that can
    reach the API over a non-loopback path cannot invoke enforcement by merely
    spoofing the usual query parameters. ``compare_digest`` avoids a timing
    side channel, and an unset configured token deliberately denies every
    request until the operator configures it.
    """
    await verify_loopback_request(request)

    expected_token = str(getattr(getattr(config, "api", None), "line_report_token", "") or "")
    provided_token = request.headers.get("X-DuSheng-Line-Token", "")
    # The deployment guide uses a 32-byte (64 hex character) secret. Reject
    # empty/oversized values here so an accidental weak configuration cannot
    # silently protect the endpoint.
    if (
        len(expected_token) < 32
        or len(expected_token) > 4096
        or len(provided_token) > 4096
        or not provided_token
        or not secrets.compare_digest(provided_token, expected_token)
    ):
        LOGGER.warning("Invalid or missing line enforcement token")
        raise HTTPException(status_code=403, detail="Invalid internal token")
    return True


# Backwards-compatible name for callers that imported the old dependency.
verify_internal_request = verify_loopback_request

emby_api_route.include_router(
    ban_playlist_route,
    dependencies=[Depends(verify_loopback_request)],
)
emby_api_route.include_router(
    favorites_router,
    dependencies=[Depends(verify_token)]
)
emby_api_route.include_router(
    media_router,
    dependencies=[Depends(verify_token)]
)
emby_api_route.include_router(
    client_filter_router,
    dependencies=[Depends(verify_token)]
)
emby_api_route.include_router(
    line_report_router,
    dependencies=[Depends(verify_line_report_request)],
)
user_api_route.include_router(
    user_info_route,
    dependencies=[Depends(verify_token)]
)
auth_api_route.include_router(
    login_router,
    dependencies=[Depends(verify_token)]
)

