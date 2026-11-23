"""Loopback-only fixture for the Electron/Node client integration test."""

import json
import os
import re
import socket
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

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
    fixture.auth.account_lookup_by_emby = fixture.m.runtime.account_lookup_by_emby
    with fixture.sessions.begin() as db:
        db.add(fixture.m.LocalUser(embyid="emby-independent", name="independent", lv="b", ex=datetime(2099, 1, 1), expired=0))

    async def fixture_authenticate(username, password):
        accounts = {"bound-viewer": "emby-42", "independent": "emby-independent"}
        if username not in accounts or password != "fixture":
            raise fixture.m.service.TVError("INVALID_CREDENTIALS", "账号或密码错误", 401)
        return accounts[username]

    fixture.m.api.runtime.authenticate_emby = fixture_authenticate
    fixture.m.api.emby_identity = __import__("unittest.mock", fromlist=["AsyncMock"]).AsyncMock(return_value="emby-42")
    media_url = os.environ.get("DUSHENGTV_FIXTURE_EMBY")
    if media_url:
        if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]+", media_url):
            raise ValueError("Expected loopback media fixture")
        fixture.settings.server_urls = [media_url, media_url + "/alternate"]
        viewers = iter([42, 43])
        start = fixture.auth.start

        def confirm_test_viewer(*args, **kwargs):
            challenge = start(*args, **kwargs)
            viewer = next(viewers)
            link = parse_qs(urlsplit(challenge["authorizationUrl"]).query)["request"][0]
            fixture.auth.prepare(link, viewer, f"Fixture viewer {viewer}", f"fixture{viewer}")
            fixture.auth.decide(challenge["challengeId"], viewer, True)
            return challenge

        fixture.auth.start = confirm_test_viewer
        async def fixture_identity(origin, token):
            return {"fixture-42": "emby-42", "fixture-43": "emby-43"}.get(token)
        fixture.m.api.emby_identity = fixture_identity
    app = fixture.m.server.create_app()

    @app.post("/_fixture/decision")
    async def decision(request: Request):
        data = await request.json()
        if data.get("unbound"):
            with fixture.sessions.begin() as db:
                db.get(fixture.m.User, 42).embyid = None
        tg = 43 if data.get("viewer") == 43 else 42
        prepared = fixture.auth.prepare(data["link"], tg, f"Fixture viewer {tg}", f"fixture{tg}")
        if not data.get("prepareOnly"):
            fixture.auth.decide(prepared["id"], tg, data.get("approve", True))
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
