"""Independent TV HTTP listener sharing only the Bot account database."""

from fastapi import FastAPI

from .api import router
from .requests_api import router as requests_router


def create_app():
    app = FastAPI(title="DuShengTV API", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)
    app.include_router(requests_router)
    return app


async def start():
    from bot import LOGGER, config
    import uvicorn
    from contextlib import contextmanager

    class TVServer(uvicorn.Server):
        @contextmanager
        def capture_signals(self):
            yield

    cfg = config.dushengtv
    if not cfg.enabled:
        return
    if cfg.http_port == config.api.http_port:
        LOGGER.error("DuShengTV 必须使用独立端口，不能与续费/API 服务共用")
        return
    try:
        from .runtime import settings
        settings()
        server = TVServer(uvicorn.Config(create_app(), host=cfg.http_host, port=cfg.http_port,
                                               proxy_headers=False, access_log=False, log_level="warning"))
        # Pyrogram owns process signal handling and the event loop.
        server.install_signal_handlers = lambda: None
        LOGGER.info(f"DuShengTV 独立 API 启动：{cfg.http_host}:{cfg.http_port}")
        await server.serve()
    except (Exception, SystemExit):
        LOGGER.error("DuShengTV API 启动失败，请检查独立端口、HTTPS 地址及 Bot 用户名配置")
