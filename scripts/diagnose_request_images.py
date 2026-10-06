"""只读检查 MoviePilot 图片链路；不输出凭据、不订阅、不刷新 TG 会话。"""
import argparse
import asyncio
import importlib
import json
from pathlib import Path
import sys
import time
import types
from urllib.parse import urlsplit


def field_shape(value):
    result = {"type": type(value).__name__}
    if isinstance(value, str):
        result["present"] = bool(value)
        result["host"] = urlsplit(value).hostname or ("relative" if value.startswith("/") else "")
    elif isinstance(value, dict):
        result["keys"] = sorted(str(key) for key in value)[:10]
    return result


async def diagnose(config_path):
    # Do not import bot.__init__: that would start unrelated production work.
    root = Path.cwd()
    if not (root / "bot" / "dushengtv" / "request_gateway.py").exists():
        root = Path(__file__).resolve().parents[1]
    package = types.ModuleType("bot")
    package.__path__ = [str(root / "bot")]
    sys.modules["bot"] = package
    # Gateway shares pure TV error definitions with the session module. Its
    # model declarations need a Base, but diagnosis must not connect to DB.
    from sqlalchemy.orm import declarative_base
    sql = types.ModuleType("bot.sql_helper")
    sql.Base = declarative_base()
    sys.modules["bot.sql_helper"] = sql
    gateway_module = importlib.import_module("bot.dushengtv.request_gateway")
    media = importlib.import_module("bot.dushengtv.request_media")
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8-sig")).get("moviepilot", {})
    report = {"diagnostic": "dushengtv-request-images-v1", "sources": []}
    origin = cfg.get("url") or cfg.get("host")
    if not origin:
        return {**report, "result": "MOVIEPILOT_NOT_CONFIGURED"}
    gateway = gateway_module.MoviePilotGateway(origin, cfg.get("access_token") or "", cfg.get("username") or "", cfg.get("password") or "")
    for source in ("tmdb", "douban"):
        entry = {"source": source, "images": []}
        report["sources"].append(entry)
        try:
            raw = await gateway.request("GET", f"/recommend/{source}_movies", params={"page": 1, **({"tags": "华语", "count": 3} if source == "douban" else {})})
            selected = next((row for row in raw if isinstance(row, dict) and media.normalize(row, source, "movie")), None)
            if selected is None:
                entry["result"] = "NO_CATALOG_ITEM"
                continue
            entry["rawPoster"] = field_shape(selected.get("poster_path"))
            item = media.normalize(selected, source, "movie")
            detail = await gateway.detail(item["key"])
            candidates = [("poster", item["poster"]), ("backdrop", detail["backdrop"])]
            photo = next((person["photo"] for person in detail["cast"] if person["photo"]), "")
            candidates.append(("actor", photo))
            for role, url in candidates:
                image = {"role": role, "present": bool(url)}
                entry["images"].append(image)
                if not url:
                    continue
                started = time.monotonic()
                try:
                    data, mime = await gateway.images.get(url)
                    image.update({"result": "OK", "bytes": len(data), "mime": mime})
                except Exception as error:
                    image.update({"result": getattr(error, "code", type(error).__name__), "http": getattr(error, "status", None)})
                image["seconds"] = round(time.monotonic() - started, 2)
            entry["result"] = "CHECKED"
        except Exception as error:
            entry["result"] = getattr(error, "code", type(error).__name__)
            entry["http"] = getattr(error, "status", None)
    report["result"] = "CHECK_OUTPUT"
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(diagnose(args.config)), ensure_ascii=False, indent=2))
    except Exception as error:
        # Error strings may embed URLs. Only stable codes/types are printable.
        print(json.dumps({"diagnostic": "dushengtv-request-images-v1", "result": getattr(error, "code", type(error).__name__)}, indent=2))
