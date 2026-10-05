"""Loopback-only fixture for the Electron/Node client integration test."""

import json
import socket
from datetime import datetime

import uvicorn
from fastapi import Request

from test_dushengtv import DesktopTests


def main():
    DesktopTests.setUpClass()
    fixture = DesktopTests()
    fixture.setUp()
    fixture.now = datetime.utcnow()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    fixture.patches[1].stop()
    from unittest.mock import patch
    fixture.patches[1] = patch.object(fixture.m.api.runtime, "settings", return_value=(fixture.settings, base, "example_bot"))
    fixture.patches[1].start()
    fixture.m.api.emby_identity = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(return_value="emby-42")
    app = fixture.m.server.create_app()

    @app.post("/_fixture/decision")
    async def decision(request: Request):
        data = await request.json()
        if data.get("unbound"):
            with fixture.sessions.begin() as db:
                db.get(fixture.m.User, 42).embyid = None
        prepared = fixture.auth.prepare(data["link"], 42, "Fixture viewer", "fixture")
        if not data.get("prepareOnly"):
            fixture.auth.decide(prepared["id"], 42, data.get("approve", True))
        return {"ok": True}

    @app.on_event("startup")
    async def ready():
        print(json.dumps({"base": base}), flush=True)

    try:
        uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="error")).run(sockets=[sock])
    finally:
        fixture.tearDown()
        DesktopTests.tearDownClass()


if __name__ == "__main__":
    main()
