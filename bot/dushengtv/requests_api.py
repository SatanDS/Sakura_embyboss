"""Private desktop API for discovery and direct MoviePilot subscriptions."""
from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from .api import TVRoute, body, invoke, limits, token
from .request_gateway import MoviePilotGateway, library_lookup
from .request_completion import RequestCompletion
from .requests import MediaRequests, RequestStore
from .service import PREFIX, TVError, canonical_url

router = APIRouter(prefix=PREFIX + "/requests", route_class=TVRoute)
_instance = None
_configuration = None


def permission(identity):
    from bot import config
    from bot.sql_helper import Session
    from bot.sql_helper.sql_emby import Emby
    cfg = config.moviepilot
    if not getattr(config.dushengtv, "requests_enabled", True) or not (cfg.status or cfg.douban_status) or not cfg.url:
        raise TVError("REQUESTS_NOT_CONFIGURED", "管理员尚未启用求片服务", 503)
    tg = int(identity["telegramId"])
    with Session() as db:
        user = db.query(Emby).filter_by(tg=tg).one_or_none()
        admins = [int(value) for value in getattr(config, "admins", [])]
        is_admin = tg == config.owner or tg in admins
        if not user or (cfg.lv == "a" and user.lv != "a" and not is_admin):
            raise TVError("REQUESTS_FORBIDDEN", "当前账号没有求片权限", 403)


def service():
    global _instance, _configuration
    from bot import config
    from bot.sql_helper import Session
    cfg = config.moviepilot
    configuration = (cfg.url, cfg.access_token, cfg.username, cfg.password, config.emby_url, config.emby_api,
                     tuple(config.dushengtv.server_urls), getattr(config.dushengtv, "requests_daily_limit", 20))
    if configuration != _configuration:
        async def library(identity, item):
            result = await library_lookup(config.emby_url, config.emby_api, identity["embyUserId"], item)
            aliases = config.dushengtv.server_urls
            server = canonical_url(aliases[0] if aliases else config.emby_url)
            for row in result["items"]:
                row["serverUrl"] = server
            result["serverUrls"] = [canonical_url(url) for url in aliases]
            return result
        gateway = MoviePilotGateway(cfg.url, cfg.access_token or "", cfg.username or "", cfg.password or "")
        _instance = MediaRequests(gateway, RequestStore(Session, getattr(config.dushengtv, "requests_daily_limit", 20)), library,
                                  RequestCompletion(config.emby_url, config.emby_api, gateway))
        _configuration = configuration
    return _instance


async def authorized(request):
    bearer = token(request)
    identity = await invoke("server_identity", bearer)
    await run_in_threadpool(permission, identity)
    return bearer, identity


async def recheck(bearer, identity):
    if await invoke("server_identity", bearer) != identity:
        raise TVError("TOKEN_EXPIRED", "登录状态已改变", 401)
    await run_in_threadpool(permission, identity)


def page_number(value):
    try:
        page = int(value)
        if not 1 <= page <= 100:
            raise ValueError()
        return page
    except (TypeError, ValueError):
        raise TVError("INVALID_REQUEST", "页码无效", 400) from None


@router.get("/catalog")
async def catalog(request: Request):
    bearer, identity = await authorized(request)
    limits.check((identity["telegramId"], "request-catalog"), 30)
    kind = request.query_params.get("type", "movie")
    if kind not in {"movie", "tv"}:
        raise TVError("INVALID_REQUEST", "仅支持电影或电视剧", 400)
    result = await service().catalog(kind, page_number(request.query_params.get("page", "1")))
    await recheck(bearer, identity)
    return result


@router.get("/image")
async def image(request: Request):
    bearer, identity = await authorized(request)
    limits.check((identity["telegramId"], "request-image"), 360)
    data, mime = await service().gateway.images.get(request.query_params.get("url"))
    await recheck(bearer, identity)
    return Response(data, media_type=mime)


@router.get("/detail")
async def detail(request: Request):
    bearer, identity = await authorized(request)
    limits.check((identity["telegramId"], "request-detail"), 60)
    result = await service().detail(identity, request.query_params.get("key"))
    await recheck(bearer, identity)
    return result


@router.post("/subscribe")
async def subscribe(request: Request):
    bearer, identity = await authorized(request)
    limits.check((identity["telegramId"], "request-subscribe"), 10)
    data = await body(request)
    result = await service().subscribe(identity, data.get("key"), data.get("season"), lambda: recheck(bearer, identity))
    await recheck(bearer, identity)
    return result


@router.get("/mine")
async def mine(request: Request):
    bearer, identity = await authorized(request)
    limits.check((identity["telegramId"], "request-mine"), 30)
    result = await service().mine(identity, page_number(request.query_params.get("page", "1")))
    await recheck(bearer, identity)
    return result
