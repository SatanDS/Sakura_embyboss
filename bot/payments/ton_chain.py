"""TON mainnet USDT proof via Toncenter v3 finalized Jetton actions.

An indexer is a trusted data provider, just as the EVM RPC provider is. Credit
also requires a separate successful Binance deposit with a matching transaction.
"""
import base64
import asyncio
import binascii
import json
import re
import time
import weakref
from urllib.parse import urlsplit

import aiohttp

from .polygon_chain import PolygonError, Transfer

USDT_MASTER = "0:b113a994b5024a16719f69139328eb759596c38a25f59028b146fecdc3621dfe"
_limits = weakref.WeakKeyDictionary()


def address(value):
    if not isinstance(value, str):
        raise PolygonError("polygon_invalid_address")
    if re.fullmatch(r"0:[0-9a-fA-F]{64}", value):
        raw = value.lower()
    else:
        try:
            if not re.fullmatch(r"[A-Za-z0-9_+/\-]{48}", value):
                raise ValueError()
            data = base64.b64decode(value, altchars=b"-_", validate=True)
            if (len(data) != 36 or data[0] not in (0x11, 0x51) or data[1] != 0
                    or binascii.crc_hqx(data[:34], 0).to_bytes(2, "big") != data[34:]):
                raise ValueError()
            raw = "0:" + data[2:34].hex()
        except (ValueError, UnicodeError):
            raise PolygonError("polygon_invalid_address") from None
    if int(raw[2:], 16) == 0:
        raise PolygonError("polygon_invalid_address")
    return raw


def friendly_address(value):
    raw = address(value)
    data = b"\x51\x00" + bytes.fromhex(raw[2:])
    return base64.urlsafe_b64encode(data + binascii.crc_hqx(data, 0).to_bytes(2, "big")).decode()


def transaction_hash(value):
    if isinstance(value, str):
        if re.fullmatch(r"(?:0x)?[0-9a-fA-F]{64}", value):
            return "0x" + value.removeprefix("0x").lower()
        try:
            if not re.fullmatch(r"[A-Za-z0-9_+/\-]{43}=?", value):
                raise ValueError()
            data = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
            if len(data) == 32:
                return "0x" + data.hex()
        except (ValueError, UnicodeError):
            pass
    raise PolygonError("polygon_invalid_transaction")


def number(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value):
        return int(value)
    raise PolygonError("polygon_invalid_response")


class TonGateway:
    chain_id = -239
    token_contract = USDT_MASTER
    decimals = 6

    def __init__(self, api_url, api_key=""):
        parsed = urlsplit(api_url)
        # Do not send a Toncenter key to an arbitrary server.
        if api_url.rstrip("/") != "https://toncenter.com/api/v3" or parsed.query or parsed.fragment:
            raise PolygonError("polygon_rpc_unconfigured")
        self._url, self._key = api_url.rstrip("/"), api_key

    async def _get(self, path, params=None):
        loop = asyncio.get_running_loop()
        limits = _limits.setdefault(loop, [asyncio.Lock(), 0.0])
        # Unauthenticated Toncenter permits roughly one request per second.
        # Coordinate all service instances so concurrent order polling also obeys it.
        async with limits[0]:
            await asyncio.sleep(max(0, limits[1] - time.monotonic()))
            limits[1] = time.monotonic() + (0.25 if self._key else 2.1)
            return await self._request(path, params)

    async def _request(self, path, params=None):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=False) as client:
                async with client.get(self._url + path, params=params or {},
                        headers={"X-API-Key": self._key} if self._key else {}, allow_redirects=False) as response:
                    if response.status != 200:
                        raise ValueError()
                    size, chunks = 0, []
                    async for chunk in response.content.iter_chunked(8192):
                        size += len(chunk)
                        if size > 2 * 1024 * 1024:
                            raise ValueError()
                        chunks.append(chunk)
                    data = json.loads(b"".join(chunks))
                    if not isinstance(data, dict) or "error" in data:
                        raise ValueError()
                    return data
        except (aiohttp.ClientError, TimeoutError, ValueError, UnicodeError):
            raise PolygonError("polygon_rpc_unavailable") from None

    async def finalized_block(self):
        data = await self._get("/masterchainInfo")
        block = data.get("last")
        if not isinstance(block, dict) or block.get("global_id") != -239 or block.get("workchain") != -1:
            raise PolygonError("polygon_wrong_network")
        return {"number": number(block.get("seqno")), "timestamp": number(block.get("gen_utime")),
                "hash": transaction_hash(block.get("root_hash"))}

    async def transfers(self, identifier, recipient):
        identifier, recipient = transaction_hash(identifier), address(recipient)
        head = await self.finalized_block()
        # Receipts store action_id as the stable event identity. Users normally
        # submit a transaction hash; all aliases must resolve to that same action.
        actions = []
        for field in ("action_id", "tx_hash"):
            data = await self._get("/actions", {field: identifier[2:], "action_type": "jetton_transfer", "limit": 100})
            rows = data.get("actions")
            if not isinstance(rows, list) or len(rows) >= 100:
                raise PolygonError("polygon_invalid_response")
            if rows:
                actions = rows
                break
        if not actions:
            raise PolygonError("polygon_pending_confirmation")
        result, seen = [], set()
        for action in actions:
            if not isinstance(action, dict) or action.get("type") != "jetton_transfer":
                raise PolygonError("polygon_invalid_response")
            details = action.get("details")
            if not isinstance(details, dict):
                raise PolygonError("polygon_invalid_response")
            if address(details.get("asset")) != USDT_MASTER or address(details.get("receiver")) != recipient:
                continue
            event_id = transaction_hash(action.get("action_id"))
            aliases = action.get("transactions")
            if not isinstance(aliases, list) or not 1 <= len(aliases) <= 256:
                raise PolygonError("polygon_invalid_response")
            aliases = tuple(sorted({transaction_hash(v) for v in aliases}))
            if identifier != event_id and identifier not in aliases:
                raise PolygonError("polygon_transfer_mismatch")
            if action.get("success") is not True:
                raise PolygonError("polygon_transaction_failed")
            block = number(action.get("trace_mc_seqno_end"))
            if action.get("finality") != "finalized" or block > head["number"]:
                raise PolygonError("polygon_pending_confirmation")
            stamp = number(action.get("end_utime"))
            if stamp > head["timestamp"] or event_id in seen:
                raise PolygonError("polygon_invalid_response")
            seen.add(event_id)
            memo = details.get("comment") or ""
            if not isinstance(memo, str) or len(memo) > 128 or details.get("is_encrypted_comment") is not False:
                raise PolygonError("polygon_transfer_mismatch")
            amount = number(details.get("amount"))
            if amount:
                result.append(Transfer(event_id, 0, block, head["hash"], stamp,
                    address(details.get("sender")), recipient, amount, memo, aliases))
        return result
