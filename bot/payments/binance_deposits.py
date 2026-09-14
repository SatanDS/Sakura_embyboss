"""Read-only Binance deposit verification. No trade, transfer or withdrawal API."""

import hashlib
import hmac
import json
import re
import time
from decimal import Decimal
from urllib.parse import urlencode

import aiohttp

from .polygon_chain import address, PolygonError


class DepositError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def usdt_units(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,18})?", value):
        raise DepositError("binance_invalid_deposit")
    exact = Decimal(value) * 1_000_000
    if exact != exact.to_integral_value() or exact <= 0:
        raise DepositError("binance_invalid_deposit")
    return int(exact)


class BinanceDeposits:
    BASE_URL = "https://api.binance.com"
    ALLOWED_PATHS = frozenset({"/sapi/v1/capital/deposit/address", "/sapi/v1/capital/deposit/hisrec"})

    def __init__(self, api_key, api_secret, network="POL"):
        if (not isinstance(api_key, str) or not api_key or not isinstance(api_secret, str) or not api_secret
                or network not in {"POL", "MATIC"}):
            raise DepositError("binance_readonly_unconfigured")
        self._api_key, self._api_secret, self.network = api_key, api_secret, network

    async def _get(self, path, params):
        if path not in self.ALLOWED_PATHS:
            raise DepositError("binance_invalid_operation")
        query = urlencode({**params, "recvWindow": 5000, "timestamp": int(time.time() * 1000)})
        signature = hmac.new(self._api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), trust_env=False) as client:
                async with client.get(self.BASE_URL + path + "?" + query + "&signature=" + signature,
                                      headers={"X-MBX-APIKEY": self._api_key}, allow_redirects=False) as response:
                    if response.status != 200:
                        raise DepositError("binance_query_failed")
                    size, chunks = 0, []
                    async for chunk in response.content.iter_chunked(8192):
                        size += len(chunk)
                        if size > 2 * 1024 * 1024:
                            raise DepositError("binance_query_failed")
                        chunks.append(chunk)
                    return json.loads(b"".join(chunks))
        except (aiohttp.ClientError, TimeoutError, ValueError, UnicodeError):
            raise DepositError("binance_query_failed") from None

    async def validate_address(self, expected):
        expected = address(expected)
        data = await self._get("/sapi/v1/capital/deposit/address", {"coin": "USDT", "network": self.network})
        try:
            matched = isinstance(data, dict) and address(data.get("address")) == expected
        except PolygonError:
            matched = False
        if not matched:
            raise DepositError("binance_address_mismatch")
        return True

    async def credited_deposit(self, transfer, *, start_ms, end_ms):
        if (type(start_ms) is not int or type(end_ms) is not int or start_ms < 0 or start_ms > end_ms
                or end_ms - start_ms > 89 * 24 * 60 * 60 * 1000):
            raise DepositError("binance_invalid_window")
        # Do not confuse Binance-internal transfer records with on-chain credit.
        params = {"coin": "USDT", "txId": transfer.tx_hash, "startTime": start_ms,
                  "endTime": end_ms, "limit": 1000}
        candidate, seen = None, set()
        for page in range(10):
            data = await self._get("/sapi/v1/capital/deposit/hisrec", {**params, "offset": page * 1000})
            if not isinstance(data, list) or len(data) > 1000:
                raise DepositError("binance_invalid_deposit")
            for row in data:
                if not isinstance(row, dict):
                    raise DepositError("binance_invalid_deposit")
                if (row.get("coin") != "USDT" or row.get("network") != self.network
                        or str(row.get("txId", "")).lower() != transfer.tx_hash
                        or str(row.get("address", "")).lower() != transfer.recipient):
                    continue
                if type(row.get("transferType")) not in (int, str) or row["transferType"] not in (0, "0"):
                    continue
                # Only fully successful credited deposits count. Pending,
                # rejected, locked, and travel-rule holds never trigger delivery.
                if type(row.get("status")) is not int or row["status"] != 1:
                    continue
                travel_rule = row.get("travelRuleStatus", 0)
                if type(travel_rule) is not int or travel_rule != 0:
                    continue
                if usdt_units(row.get("amount")) != transfer.amount_units:
                    continue
                deposit_id = row.get("id")
                if not isinstance(deposit_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", deposit_id):
                    raise DepositError("binance_invalid_deposit")
                if deposit_id in seen:
                    raise DepositError("binance_invalid_deposit")
                seen.add(deposit_id)
                if candidate is not None:
                    raise DepositError("binance_ambiguous_deposit")
                candidate = deposit_id
            if len(data) < 1000:
                return candidate
        raise DepositError("binance_query_incomplete")
