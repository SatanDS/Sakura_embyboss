"""Run regressions with example configuration and external connections disabled."""

import builtins
import io
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

from loguru import logger


ROOT = Path(__file__).resolve().parents[1]


def main():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    fixture = (ROOT / "config_example.json").read_text(encoding="utf-8")
    real_open = builtins.open
    real_connect = socket.socket.connect

    def isolated_open(file, mode="r", *args, **kwargs):
        try:
            path = Path(file).resolve()
        except TypeError:
            return real_open(file, mode, *args, **kwargs)
        if path == ROOT / "config.json":
            return io.StringIO(fixture if "r" in mode else "")
        return real_open(file, mode, *args, **kwargs)

    def isolated_connect(sock, address):
        # Windows asyncio creates its wakeup socket pair through loopback TCP.
        caller = sys._getframe(1)
        if caller.f_code.co_name == "socketpair" and caller.f_globals.get("__name__") == "socket":
            return real_connect(sock, address)
        raise AssertionError("External connections are disabled during offline tests")

    logger.remove()
    with (
        patch.dict(os.environ, {
            "SAKURA_RUNNING_MIGRATIONS": "1", "REGISTER_QUEUE_REAL": "0",
            "CADDY_BIN": "", "NGINX_BIN": "",
            "TGBOT_MYSQL_TEST": "0", "TGBOT_EMBY_TEST": "0", "TGBOT_VIP_TEST": "0",
        }),
        patch("builtins.open", isolated_open),
        patch.object(logger, "add", return_value=0),
        patch.object(socket.socket, "connect", isolated_connect),
        patch.object(socket.socket, "connect_ex", side_effect=AssertionError("External connections are disabled")),
    ):
        suite = unittest.defaultTestLoader.discover(str(ROOT / "scripts"), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
