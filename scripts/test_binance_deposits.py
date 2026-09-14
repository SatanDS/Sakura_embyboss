"""Binance credit verification with synthetic records only."""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock

root = Path(__file__).resolve().parents[1] / 'bot/payments'
package = ModuleType('binance_deposit_test')
package.__path__ = [str(root)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location('binance_deposit_test.binance_deposits', root / 'binance_deposits.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BinanceDepositTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gateway = module.BinanceDeposits('fixture', 'secret')
        self.transfer = SimpleNamespace(tx_hash='0x'+'12'*32, recipient='0x'+'34'*20, amount_units=2012345)
        self.deposit = dict(id='deposit1', coin='USDT', network='POL', txId=self.transfer.tx_hash,
                            address=self.transfer.recipient, amount='2.01234500', status=1, transferType=0, travelRuleStatus=0)

    async def test_only_exact_confirmed_external_deposit_matches(self):
        self.gateway._get = AsyncMock(return_value=[self.deposit])
        result = await self.gateway.credited_deposit(self.transfer, start_ms=1000, end_ms=2000)
        self.assertEqual(result, 'deposit1')
        self.assertEqual(self.gateway._get.await_args.args[0], '/sapi/v1/capital/deposit/hisrec')

    async def test_wrong_network_amount_token_or_pending_internal_do_not_match(self):
        for patch in ({'coin':'POL'}, {'network':'BSC'}, {'amount':'2.01'}, {'status':0}, {'status':6},
                      {'status':True}, {'travelRuleStatus':1}, {'transferType':1}, {'address':'0x'+'56'*20},
                      {'txId':'0x'+'78'*32}):
            self.gateway._get = AsyncMock(return_value=[{**self.deposit, **patch}])
            self.assertIsNone(await self.gateway.credited_deposit(self.transfer, start_ms=1000, end_ms=2000))

    async def test_api_error_empty_result_and_ambiguous_credit_cannot_pay(self):
        for data in ({'code':-2015}, [{'id':'x'}]*1001, [self.deposit, {**self.deposit,'id':'deposit2'}]):
            self.gateway._get = AsyncMock(return_value=data)
            with self.assertRaises(module.DepositError):
                await self.gateway.credited_deposit(self.transfer, start_ms=1000, end_ms=2000)
        self.gateway._get = AsyncMock(return_value=[])
        self.assertIsNone(await self.gateway.credited_deposit(self.transfer, start_ms=1000, end_ms=2000))

    async def test_account_address_must_match_configured_recipient(self):
        self.gateway._get = AsyncMock(return_value={'address':self.transfer.recipient})
        self.assertTrue(await self.gateway.validate_address(self.transfer.recipient))
        self.gateway._get.return_value = {'address':'0x'+'56'*20}
        with self.assertRaises(module.DepositError):
            await self.gateway.validate_address(self.transfer.recipient)

    def test_amounts_reject_float_rounding_and_nonfinite_values(self):
        self.assertEqual(module.usdt_units('2.012345000000000000'), 2012345)
        for value in ('NaN','1e2','2.0123451','-1','0',2.012345,True):
            with self.assertRaises(module.DepositError):
                module.usdt_units(value)


if __name__ == '__main__':
    unittest.main(verbosity=2)
