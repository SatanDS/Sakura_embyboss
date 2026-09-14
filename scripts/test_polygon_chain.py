"""Polygon receipt verification without network, keys or actual transfers."""
import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock

source = Path(__file__).resolve().parents[1] / "bot/payments/polygon_chain.py"
spec = importlib.util.spec_from_file_location("polygon_chain_under_test", source)
chain = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = chain
spec.loader.exec_module(chain)


class PolygonChainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.address = "0x" + "ab" * 20
        self.tx = "0x" + "cd" * 32
        self.block_hash = "0x" + "12" * 32
        self.gateway = chain.PolygonGateway("https://rpc.example.test/key")
        self.block = {"number": "0x64", "hash": self.block_hash, "timestamp": "0x3e8"}
        self.log = {"address": chain.USDT_CONTRACT, "transactionHash": self.tx, "blockHash": self.block_hash,
                    "blockNumber": "0x64", "logIndex": "0x0", "removed": False,
                    "topics": [chain.TRANSFER_TOPIC, "0x" + "0" * 24 + "11" * 20, "0x" + "0" * 24 + "ab" * 20],
                    "data": "0x" + format(2_010_123, "064x")}
        self.receipt = {"transactionHash": self.tx, "blockNumber": "0x64", "blockHash": self.block_hash,
                        "status": "0x1", "logs": [self.log]}

    def responses(self, *, chain_id="0x89", head=None, block=None):
        self.gateway._rpc = AsyncMock(side_effect=[chain_id, head or self.block, self.receipt, block or self.block])

    async def test_verified_transfer_returns_exact_integer_units_and_log_identity(self):
        self.responses()
        transfers = await self.gateway.transfers(self.tx, self.address)
        self.assertEqual(len(transfers), 1)
        self.assertEqual((transfers[0].tx_hash, transfers[0].log_index, transfers[0].amount_units), (self.tx, 0, 2010123))
        self.assertEqual(transfers[0].recipient, self.address)

    async def test_wrong_network_pending_failed_and_reorg_are_not_payments(self):
        cases = [({"chain_id": "0x1"}, "polygon_wrong_network"),
                 ({"head": {**self.block, "number": "0x63"}}, "polygon_pending_confirmation"),
                 ({"block": {**self.block, "hash": "0x" + "34" * 32}}, "polygon_pending_confirmation")]
        for args, code in cases:
            with self.subTest(code=code):
                self.responses(**args)
                with self.assertRaises(chain.PolygonError) as exc:
                    await self.gateway.transfers(self.tx, self.address)
                self.assertEqual(exc.exception.code, code)
        self.receipt["status"] = "0x0"
        self.responses()
        with self.assertRaises(chain.PolygonError) as exc:
            await self.gateway.transfers(self.tx, self.address)
        self.assertEqual(exc.exception.code, "polygon_transaction_failed")

    async def test_fake_token_wrong_recipient_and_zero_amount_do_not_count(self):
        for changes in [{"address": "0x" + "33" * 20},
                        {"topics": [chain.TRANSFER_TOPIC, self.log["topics"][1], "0x" + "0" * 24 + "22" * 20]},
                        {"data": "0x" + "0" * 64}]:
            self.receipt["logs"] = [{**self.log, **changes}]
            self.responses()
            self.assertEqual(await self.gateway.transfers(self.tx, self.address), [])

    async def test_malformed_duplicate_and_removed_logs_are_rejected(self):
        for changes in [{"removed": True}, {"transactionHash": "0x" + "55" * 32}, {"data": "0x01"},
                        {"blockNumber": "0x65"}, {"logIndex": -1}]:
            self.receipt["logs"] = [{**self.log, **changes}]
            self.responses()
            with self.assertRaises(chain.PolygonError):
                await self.gateway.transfers(self.tx, self.address)
        self.receipt["logs"] = [self.log, copy.deepcopy(self.log)]
        self.responses()
        with self.assertRaises(chain.PolygonError):
            await self.gateway.transfers(self.tx, self.address)

    async def test_empty_pending_receipt_and_missing_finality_stay_pending(self):
        self.gateway._rpc = AsyncMock(side_effect=["0x89", self.block, None])
        with self.assertRaises(chain.PolygonError) as exc:
            await self.gateway.transfers(self.tx, self.address)
        self.assertEqual(exc.exception.code, "polygon_pending_confirmation")
        self.gateway._rpc = AsyncMock(side_effect=["0x89", None])
        with self.assertRaises(chain.PolygonError) as exc:
            await self.gateway.transfers(self.tx, self.address)
        self.assertEqual(exc.exception.code, "polygon_finality_unavailable")

    async def test_scanner_bounds_and_unique_hashes(self):
        self.gateway._rpc = AsyncMock(return_value=[self.log, self.log])
        self.assertEqual(await self.gateway.transaction_ids(self.address, 100, 499), [self.tx])
        for start, end in [(0, 500), (True, 200), (300, 200), (-1, 20)]:
            with self.assertRaises(chain.PolygonError):
                await self.gateway.transaction_ids(self.address, start, end)


if __name__ == "__main__":
    unittest.main(verbosity=2)
