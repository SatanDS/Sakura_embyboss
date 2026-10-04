"""Resolve proxy headers overwritten by the authenticated local gateway."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from bot import config
from bot.func_helper.proxy_ip import is_trusted_proxy, resolve_client_ip


router = APIRouter()


@router.get("/cdn_origin")
async def cdn_origin(request: Request):
    # Same parent dependency as /real_ip: loopback plus the internal line token.
    # The local TLS proxy must overwrite this header with its actual TCP peer.
    peers = request.headers.getlist("X-Proxy-Peer-IP")
    headers = {"Cache-Control": "no-store"}
    if len(peers) != 1:
        raise HTTPException(status_code=400, detail="Invalid gateway IP headers", headers=headers)
    try:
        allowed = is_trusted_proxy(peers[0], getattr(config, "trusted_proxy_cidrs", []))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid gateway peer IP", headers=headers) from None
    if not allowed:
        raise HTTPException(status_code=403, detail="CDN origin peer is not allowed", headers=headers)
    return Response(status_code=204, headers=headers)


@router.get("/real_ip")
async def real_ip(request: Request):
    # The parent router enforces loopback and the shared internal secret.
    peers = request.headers.getlist("X-Proxy-Peer-IP")
    chains = request.headers.getlist("X-Proxy-Forwarded-For")
    if len(peers) != 1 or len(chains) > 1:
        raise HTTPException(status_code=400, detail="Invalid gateway IP headers")
    try:
        address = resolve_client_ip(
            peers[0], chains[0] if chains else "", getattr(config, "trusted_proxy_cidrs", []),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid gateway peer IP") from exc
    return JSONResponse(
        {"client_ip": address},
        headers={"X-Verified-Client-IP": address, "Cache-Control": "no-store"},
    )
