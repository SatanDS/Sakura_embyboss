"""Read-only deployment check; prints no keys, signed URLs or deposit records."""
import argparse
import asyncio
import importlib
import json
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace


async def check(chains):
    root = Path(__file__).resolve().parents[1]
    package = ModuleType("payment_connectivity_check")
    package.__path__ = [str(root / "bot/payments")]
    sys.modules[package.__name__] = package
    load = lambda name: importlib.import_module(package.__name__ + "." + name)
    Settings, Binance = load("settings").PaymentSettings, load("binance_deposits").BinanceDeposits
    raw = json.loads((root / "config.json").read_text(encoding="utf-8"))
    settings = Settings.from_config(SimpleNamespace(payments=SimpleNamespace(**raw["payments"])))
    failed = False
    for chain in chains:
        stage = "CONFIG"
        try:
            settings.validate_common()
            if not settings.live_mode:
                raise ValueError("live mode required")
            if chain == "polygon":
                gateway = load("polygon_chain").PolygonGateway(settings.polygon_rpc_url)
                receiver, network, memo = settings.polygon_receive_address, settings.binance_network, ""
            elif chain == "bsc":
                gateway = load("bsc_chain").BscGateway(settings.bsc_rpc_url)
                receiver, network, memo = settings.bsc_receive_address, "BSC", ""
            else:
                if not settings.ton_api_key:
                    raise ValueError("Toncenter key required")
                gateway = load("ton_chain").TonGateway(settings.ton_api_url, settings.ton_api_key)
                receiver, network, memo = settings.ton_receive_address, "TON", settings.ton_receive_memo
            stage = "CHAIN"
            head = await gateway.finalized_block()
            lag = abs(int(time.time()) - head["timestamp"])
            if lag > 300:
                raise ValueError("stale chain data")
            print(chain.upper(), "FINALIZED_LAG_SECONDS=", lag, flush=True)
            if chain != "ton":
                decimals = await gateway._rpc("eth_call", [{"to":gateway.token_contract,"data":"0x313ce567"},"finalized"])
                if int(decimals, 16) != gateway.decimals:
                    raise ValueError("token decimals mismatch")
                print(chain.upper(), "USDT_DECIMALS=", gateway.decimals, flush=True)
            stage = "BINANCE_ADDRESS_MEMO"
            account = Binance(settings.binance_api_key, settings.binance_api_secret, network, memo)
            await account.validate_address(receiver)
            print(chain.upper(), "BINANCE_ADDRESS_MEMO_OK", flush=True)
            stage = "BINANCE_HISTORY"
            now = int(time.time() * 1000)
            rows = await account._get("/sapi/v1/capital/deposit/hisrec", {"coin":"USDT","startTime":now-86400000,"endTime":now,"limit":1})
            if not isinstance(rows, list):
                raise ValueError("invalid history")
            print(chain.upper(), "CHECK_OK", flush=True)
        except Exception as exc:
            failed = True
            print(chain.upper(), stage + "_FAIL=" + str(getattr(exc,"code",type(exc).__name__)), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chains", nargs="+", choices=("polygon","bsc","ton"))
    raise SystemExit(asyncio.run(check(parser.parse_args().chains)))
