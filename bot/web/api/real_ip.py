"""Resolve proxy headers overwritten by the authenticated local gateway."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from bot import config
from bot.func_helper.proxy_ip import resolve_client_ip


router = APIRouter()


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
