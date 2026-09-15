"""Read-only Polygon PoS USDT0 receipt verification; never signs transactions."""

from dataclasses import dataclass
import re
from urllib.parse import urlsplit

import aiohttp


CHAIN_ID = 137
# Official Polygon deployment: https://docs.usdt0.to/technical-documentation/deployments
USDT_CONTRACT = "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"
USDT_DECIMALS = 6
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
MAX_RPC_BYTES = 2 * 1024 * 1024


class PolygonError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value) or int(value, 16) == 0:
        raise PolygonError("polygon_invalid_address")
    return value.lower()


def transaction_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise PolygonError("polygon_invalid_transaction")
    return value.lower()


def quantity(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", value):
        raise PolygonError("polygon_invalid_response")
    if len(value) > 66:
        raise PolygonError("polygon_invalid_response")
    return int(value, 16)


@dataclass(frozen=True)
class Transfer:
    tx_hash: str
    log_index: int
    block_number: int
    block_hash: str
    timestamp: int
    sender: str
    recipient: str
    amount_units: int
    memo: str = ""
    binance_tx_hashes: tuple = ()


class PolygonGateway:
    chain_id = CHAIN_ID
    token_contract = USDT_CONTRACT
    decimals = USDT_DECIMALS

    def __init__(self, rpc_url):
        if not isinstance(rpc_url, str) or any(c.isspace() for c in rpc_url):
            raise PolygonError("polygon_rpc_unconfigured")
        parsed = urlsplit(rpc_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or "\\" in rpc_url):
            raise PolygonError("polygon_rpc_unconfigured")
        self._url = rpc_url

    async def _rpc(self, method, params):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), trust_env=False) as client:
                async with client.post(self._url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                       allow_redirects=False) as response:
                    if response.status != 200:
                        raise PolygonError("polygon_rpc_unavailable")
                    size, chunks = 0, []
                    async for chunk in response.content.iter_chunked(8192):
                        size += len(chunk)
                        if size > MAX_RPC_BYTES:
                            raise PolygonError("polygon_rpc_unavailable")
                        chunks.append(chunk)
                    import json
                    result = json.loads(b"".join(chunks))
                    if (not isinstance(result, dict) or result.get("jsonrpc") != "2.0" or result.get("id") != 1
                            or "error" in result or "result" not in result):
                        raise PolygonError("polygon_rpc_unavailable")
                    return result["result"]
        except (aiohttp.ClientError, TimeoutError, ValueError, UnicodeError):
            # RPC URLs can contain API keys; never include request/response data.
            raise PolygonError("polygon_rpc_unavailable") from None

    async def finalized_block(self):
        chain = await self._rpc("eth_chainId", [])
        if quantity(chain) != self.chain_id:
            raise PolygonError("polygon_wrong_network")
        block = await self._rpc("eth_getBlockByNumber", ["finalized", False])
        if not isinstance(block, dict):
            raise PolygonError("polygon_finality_unavailable")
        return {"number": quantity(block.get("number")), "hash": transaction_hash(block.get("hash")),
                "timestamp": quantity(block.get("timestamp"))}

    async def transfers(self, tx_hash, recipient):
        tx_hash, recipient = transaction_hash(tx_hash), address(recipient)
        head = await self.finalized_block()
        receipt = await self._rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            raise PolygonError("polygon_pending_confirmation")
        if not isinstance(receipt, dict) or transaction_hash(receipt.get("transactionHash")) != tx_hash:
            raise PolygonError("polygon_invalid_response")
        number, block_hash = quantity(receipt.get("blockNumber")), transaction_hash(receipt.get("blockHash"))
        if quantity(receipt.get("status")) != 1:
            raise PolygonError("polygon_transaction_failed")
        if number > head["number"]:
            raise PolygonError("polygon_pending_confirmation")
        block = await self._rpc("eth_getBlockByNumber", [hex(number), False])
        if (not isinstance(block, dict) or quantity(block.get("number")) != number
                or transaction_hash(block.get("hash")) != block_hash
                or (number == head["number"] and block_hash != head["hash"])):
            raise PolygonError("polygon_pending_confirmation")
        timestamp = quantity(block.get("timestamp"))
        if timestamp > head["timestamp"]:
            raise PolygonError("polygon_invalid_response")
        logs = receipt.get("logs")
        if not isinstance(logs, list) or len(logs) > 4096:
            raise PolygonError("polygon_invalid_response")
        result, seen = [], set()
        for log in logs:
            if not isinstance(log, dict):
                raise PolygonError("polygon_invalid_response")
            if str(log.get("address", "")).lower() != self.token_contract:
                continue
            topics = log.get("topics")
            if not isinstance(topics, list) or not topics or str(topics[0]).lower() != TRANSFER_TOPIC:
                continue
            if (len(topics) != 3 or not all(isinstance(t, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", t) for t in topics)
                    or any(int(t[2:26], 16) != 0 for t in topics[1:])
                    or not isinstance(log.get("data"), str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", log["data"])):
                raise PolygonError("polygon_invalid_response")
            target = "0x" + topics[2][-40:].lower()
            if target != recipient:
                continue
            if (log.get("removed", False) is not False or transaction_hash(log.get("transactionHash")) != tx_hash
                    or quantity(log.get("blockNumber")) != number or transaction_hash(log.get("blockHash")) != block_hash):
                raise PolygonError("polygon_invalid_response")
            index = quantity(log.get("logIndex"))
            if index in seen:
                raise PolygonError("polygon_invalid_response")
            seen.add(index)
            units = int(log["data"], 16)
            divisor = 10 ** (self.decimals - 6)
            # Quotes use six decimal places. Never round an underpayment up.
            if units % divisor:
                continue
            units //= divisor
            if units:
                result.append(Transfer(tx_hash, index, number, block_hash, timestamp,
                                       "0x" + topics[1][-40:].lower(), recipient, units))
        return result

    async def transaction_ids(self, recipient, from_block, to_block):
        """Bound a scanner page; callers persist progress only after processing it."""
        recipient = address(recipient)
        if (type(from_block) is not int or type(to_block) is not int or from_block < 0
                or to_block < from_block or to_block - from_block >= 500):
            raise PolygonError("polygon_invalid_block_range")
        rows = await self._rpc("eth_getLogs", [{"address": self.token_contract, "fromBlock": hex(from_block),
            "toBlock": hex(to_block), "topics": [TRANSFER_TOPIC, None, "0x" + recipient[2:].rjust(64, "0")]}])
        if not isinstance(rows, list) or len(rows) > 4096:
            raise PolygonError("polygon_invalid_response")
        try:
            return list(dict.fromkeys(transaction_hash(row["transactionHash"]) for row in rows))
        except (KeyError, TypeError):
            raise PolygonError("polygon_invalid_response") from None
