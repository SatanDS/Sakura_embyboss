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
from .payment import router as payment_router
from bot import bot_token, LOGGER, config

emby_api_route = APIRouter(prefix="/emby", tags=["对接Emby的接口"])
user_api_route = APIRouter(prefix="/user", tags=["对接用户信息的接口"])
auth_api_route = APIRouter(prefix="/auth", tags=["用户认证接口"])
payment_api_route = APIRouter(tags=["支付"])

async def verify_token(request: Request):
    """Authenticate integrations with a separate header-based API key."""
    api_config = getattr(config, "api", None)
    provided = request.headers.get("X-API-Key", "")
    expected = str(getattr(api_config, "api_key", "") or "")
    if provided:
        if (
            not 32 <= len(expected) <= 4096
            or expected == bot_token
            or expected == getattr(api_config, "line_report_token", None)
        ):
            raise HTTPException(status_code=503, detail="API key is not configured")
        if len(provided) > 4096 or not secrets.compare_digest(
            provided.encode("utf-8"), expected.encode("utf-8")
        ):
            raise HTTPException(status_code=403, detail="Invalid API key")
        return True

    # Temporary migration path for integrations unable to send custom headers.
    if getattr(api_config, "allow_legacy_bot_token", False):
        legacy_token = request.query_params.get("token", "")
        if legacy_token and len(legacy_token) <= 4096 and secrets.compare_digest(
            legacy_token.encode("utf-8"), bot_token.encode("utf-8")
        ):
            return True
    raise HTTPException(status_code=401, detail="X-API-Key header required")


async def verify_loopback_request(request: Request):
    """Only allow calls originating from the same host."""
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
        or not secrets.compare_digest(provided_token.encode("utf-8"), expected_token.encode("utf-8"))
    ):
        LOGGER.warning("Invalid or missing line enforcement token")
        raise HTTPException(status_code=403, detail="Invalid internal token")
    return True


# Backwards-compatible name for callers that imported the old dependency.
verify_internal_request = verify_loopback_request

emby_api_route.include_router(
    ban_playlist_route,
    dependencies=[Depends(verify_line_report_request)],
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
payment_api_route.include_router(payment_router)

