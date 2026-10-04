#!/usr/bin/env python3
"""Diagnose the running Bot's danmaku connection without exposing credentials.

From the Bot checkout (no image rebuild needed):
docker compose exec -T embyboss python - --title 'Movie' --year 2024 < scripts/diagnose_danmaku.py
"""
import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit

import aiohttp


SAFE_MESSAGES = {
    "弹幕服务验证失败": "TOKEN_REJECTED",
    "请为弹幕服务配置至少 32 位的独立 TOKEN": "SERVICE_TOKEN_NOT_CONFIGURED",
    "弹幕源暂时无法响应，请稍后重试": "PROVIDER_HTTP_ERROR",
    "弹幕匹配服务暂时不可用，请稍后重试": "MATCH_FAILED",
    "弹幕源返回的内容格式无效": "PROVIDER_FORMAT_ERROR",
    "弹幕源返回了无效的匹配编号": "INVALID_MATCH_ID",
    "弹幕服务暂时不可用，请稍后重试": "ADAPTER_INTERNAL_ERROR",
    "弹幕源响应过大": "PROVIDER_RESPONSE_TOO_LARGE",
    "弹幕源响应超时，请稍后重试": "PROVIDER_TIMEOUT",
}


def load_adapter():
    root = Path.cwd() if __file__ == "<stdin>" else Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("diagnose_danmaku_adapter", root / "bot/dushengtv/danmaku.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def inspect_response(client, method, address, **options):
    started = monotonic()
    try:
        async with client.request(method, address, allow_redirects=False, **options) as response:
            raw = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                raw.extend(chunk)
                if len(raw) > 20 * 1024 * 1024:
                    return {"http": response.status, "error": "RESPONSE_TOO_LARGE"}, None
            summary = {"http": response.status, "seconds": round(monotonic() - started, 2)}
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                return {**summary, "error": "INVALID_JSON"}, None
            if isinstance(data, dict):
                code = SAFE_MESSAGES.get(data.get("message")) if isinstance(data.get("message"), str) else None
                if code:
                    summary["error"] = code
            return summary, data
    except asyncio.TimeoutError:
        return {"error": "TIMEOUT"}, None
    except aiohttp.ClientConnectorCertificateError:
        return {"error": "TLS_CERTIFICATE_ERROR"}, None
    except aiohttp.ClientConnectionError:
        return {"error": "CONNECTION_FAILED"}, None
    except aiohttp.ClientError:
        return {"error": "HTTP_TRANSPORT_ERROR"}, None


async def diagnose(adapter, metadata):
    report = {"diagnostic": "dushengtv-danmaku-v1"}
    try:
        settings = adapter.endpoint_settings()
        metadata = adapter.request_metadata(metadata)
    except adapter.DanmakuError:
        return {**report, "result": "INVALID_BOT_CONFIGURATION_OR_METADATA"}
    if settings is None:
        return {**report, "result": "BOT_ENV_NOT_LOADED"}
    endpoint, secret = settings
    parsed = urlsplit(endpoint)
    report["connection"] = "loopback" if parsed.hostname in {"127.0.0.1", "::1", "localhost"} else "remote_or_container"
    report["configured_port"] = parsed.port or (443 if parsed.scheme == "https" else 80)
    origin = endpoint[:-len(adapter.ADAPTER_PATH)]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=75), trust_env=False) as client:
        report["health"], health = await inspect_response(client, "GET", origin + "/healthz")
        if report["health"].get("http") != 200 or not isinstance(health, dict) or health.get("status") != "ok":
            return {**report, "result": "HEALTH_FAILED_CHECK_BOT_URL_OR_NETWORK"}
        headers = {"Authorization": "Bearer " + secret, "Accept": "application/json"}
        report["authentication"], probe = await inspect_response(client, "POST", endpoint, headers=headers,
            json={"title": "DuShengTV diagnostic", "type": "Episode", "season": 0, "episode": 0})
        if report["authentication"].get("http") != 200:
            return {**report, "result": "AUTH_OR_ADAPTER_FAILED"}
        if not isinstance(probe, dict) or probe.get("available") is not False or probe.get("comments") != []:
            return {**report, "result": "UNEXPECTED_ADAPTER_RESPONSE"}
        report["movie"], data = await inspect_response(client, "POST", endpoint, headers=headers, json=metadata)
        if report["movie"].get("http") != 200:
            return {**report, "result": "LIVE_PROVIDER_FAILED"}
        try:
            normalized = adapter.response_comments(data)
        except (adapter.DanmakuError, TypeError, ValueError):
            return {**report, "result": "BOT_RESPONSE_CONTRACT_FAILED"}
        report["movie"]["available"] = normalized["available"]
        report["movie"]["comments"] = len(normalized["comments"])
        return {**report, "result": "READY" if normalized["available"] and normalized["comments"] else "NO_MATCH_OR_COMMENTS"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", required=True)
    parser.add_argument("--year", type=int)
    args = parser.parse_args()
    try:
        adapter = load_adapter()
    except (OSError, ImportError, AttributeError):
        print(json.dumps({"result": "BOT_IMAGE_MISSING_DANMAKU_ADAPTER"}))
        return 1
    try:
        report = asyncio.run(diagnose(adapter, {"title": args.title, "type": "Movie", "year": args.year}))
    except Exception:
        # Do not print exception strings: networking errors may contain secrets.
        report = {"result": "DIAGNOSTIC_FAILED"}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["result"] in {"READY", "NO_MATCH_OR_COMMENTS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
