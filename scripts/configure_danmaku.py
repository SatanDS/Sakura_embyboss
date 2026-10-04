#!/usr/bin/env python3
"""Verify the same-host danmu TOKEN, then update only the Bot's danmaku settings."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


KEYS = ("TGBOT_DANMU_API_URL", "TGBOT_DANMU_API_TOKEN")


def read_token(source):
    try:
        contents = Path(source).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise ValueError("Cannot read the danmu config/.env file.") from None
    values = re.findall(r"(?m)^\s*(?:export\s+)?TOKEN\s*=([^\r\n]*)$", contents)
    if len(values) != 1:
        raise ValueError("The danmu config must contain exactly one TOKEN entry.")
    try:
        parts = shlex.split(values[0], comments=True, posix=True)
    except ValueError:
        raise ValueError("The danmu TOKEN entry has invalid quoting.") from None
    if len(parts) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", parts[0]):
        raise ValueError("The danmu TOKEN must contain 32-256 URL-safe characters.")
    return parts[0]


def verify_token(token, port):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Port must be an integer from 1 to 65535.")
    origin = f"http://127.0.0.1:{port}"
    request = Request(origin + "/api/v1/dushengtv/danmaku", method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        data=json.dumps({"title": "DuShengTV configuration check", "type": "Episode", "season": 0, "episode": 0}).encode())
    try:
        # Validate the actual running service, without any proxy environment or redirects.
        from urllib.request import HTTPRedirectHandler
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=10) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("Unexpected danmu validation response size.")
            result = json.loads(raw)
            if response.status != 200 or not isinstance(result, dict) or result.get("available") is not False or result.get("comments") != []:
                raise ValueError("Unexpected danmu validation response.")
    except HTTPError as error:
        if error.code in (401, 403):
            raise ValueError("The running danmu service rejected config/.env TOKEN. Check container TOKEN overrides; Bot settings were not changed.") from None
        raise ValueError(f"Danmu validation returned HTTP {error.code}; Bot settings were not changed.") from None
    except (URLError, TimeoutError, OSError):
        raise ValueError("Cannot connect to the local danmu service; Bot settings were not changed.") from None
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        raise ValueError("Danmu validation returned invalid JSON; Bot settings were not changed.") from None
    return origin


def configure(danmu_env, bot_env, port=9321):
    destination = Path(bot_env).absolute()
    if destination.is_symlink() or not destination.is_file():
        raise ValueError("The Bot .env must be an existing regular file, not a symlink.")
    original = destination.read_bytes()
    try:
        text = original.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("The Bot .env must be valid UTF-8.") from None
    token = read_token(danmu_env)
    origin = verify_token(token, port)
    pattern = re.compile(r"^\s*(?:export\s+)?(?:TGBOT_DANMU_API_URL|TGBOT_DANMU_API_TOKEN)\s*=")
    preserved = [line for line in text.splitlines(keepends=True) if not pattern.match(line)]
    updated = "".join(preserved)
    if updated and not updated.endswith(("\n", "\r")):
        updated += "\n"
    updated += f"TGBOT_DANMU_API_URL={origin}\nTGBOT_DANMU_API_TOKEN={token}\n"

    backup_parent = destination.parent / "db_backup"
    if backup_parent.is_symlink():
        raise ValueError("Refusing a symlink backup directory.")
    backup_parent.mkdir(mode=0o700, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="danmaku-env-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-"), dir=backup_parent)) / ".env"
    with backup.open("xb") as stream:
        os.chmod(backup, 0o600)
        stream.write(original)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=destination.parent, prefix=".danmaku-env-", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        if destination.is_symlink() or destination.read_bytes() != original:
            raise ValueError("The Bot .env changed during validation; retry after finishing other edits.")
        if os.name != "nt":
            previous = destination.stat()
            os.chown(temporary, previous.st_uid, previous.st_gid)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--danmu-env", required=True)
    parser.add_argument("--bot-env", default=str(Path(__file__).resolve().parents[1] / ".env"))
    parser.add_argument("--port", type=int, default=9321)
    args = parser.parse_args()
    try:
        backup = configure(args.danmu_env, args.bot_env, args.port)
    except ValueError as error:
        print(f"Configuration failed: {error}")
        return 1
    except OSError:
        print("Configuration failed: check file permissions. No credential values were printed.")
        return 1
    print("TOKEN accepted by the running danmu service. Updated the two Bot danmaku settings.")
    print(f"Previous Bot .env backed up at: {backup}")
    print("Next: docker compose up -d --no-deps --force-recreate embyboss")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
