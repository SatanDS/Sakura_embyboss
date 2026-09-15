"""Cross-network proof, precision, replay and migration checks; synthetic funds only."""
import asyncio
import base64
import copy
from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace, ModuleType
import sys
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from scripts.test_payment_core import load_payment_modules

ROOT = Path(__file__).resolve().parents[1]
package = ModuleType('usdt_network_fixture')
package.__path__ = [str(ROOT / 'bot/payments')]
sys.modules[package.__name__] = package
ton = importlib.import_module(package.__name__ + '.ton_chain')
bsc = importlib.import_module(package.__name__ + '.bsc_chain')
binance = importlib.import_module(package.__name__ + '.binance_deposits')
evm = importlib.import_module(package.__name__ + '.polygon_chain')


class NetworkProofTests(unittest.IsolatedAsyncioTestCase):
    def test_ton_address_checksum_network_and_hash_representations(self):
        friendly = 'UQA41HBtHmtiqZPdbKMLoOxKg2VWnFJqP_Lo0XRtQmkaTFrv'
        raw = ton.address(friendly)
        self.assertEqual(ton.address(raw.upper().replace('0:', '0:')), raw)
        self.assertEqual(ton.friendly_address(raw), friendly)
        for invalid in (friendly[:-1]+'a', 'k'+friendly[1:], '0:'+'0'*64, '0x'+'12'*20):
            with self.assertRaises(ValueError): ton.address(invalid)
        self.assertEqual(ton.transaction_hash(base64.b64encode(b'x'*32).decode()), '0x'+(b'x'*32).hex())

    async def test_bsc_18_decimal_conversion_never_rounds_an_underpayment(self):
        gateway = bsc.BscGateway('https://rpc.test')
        tx, bh, receiver = '0x'+'12'*32, '0x'+'34'*32, '0x'+'56'*20
        block = {'number':'0x64','hash':bh,'timestamp':'0x3e8'}
        log = {'address':gateway.token_contract,'transactionHash':tx,'blockHash':bh,'blockNumber':'0x64',
            'logIndex':'0x0','topics':[evm.TRANSFER_TOPIC,'0x'+'00'*12+'ab'*20,'0x'+'00'*12+'56'*20]}
        for amount, expected in ((101234*10**12, 101234), (101234*10**12-1, None), (101234, None)):
            receipt = {'status':'0x1','transactionHash':tx,'blockHash':bh,'blockNumber':'0x64',
                'logs':[{**log,'data':'0x'+format(amount,'064x')}]}
            gateway._rpc = AsyncMock(side_effect=['0x38',block,receipt,block])
            rows = await gateway.transfers(tx, receiver)
            self.assertEqual(rows[0].amount_units if rows else None, expected)
        gateway._rpc = AsyncMock(return_value='0x89')
        with self.assertRaises(ValueError): await gateway.finalized_block()

    def action(self):
        return dict(action_id=base64.b64encode(b'a'*32).decode(), type='jetton_transfer',
            success=True, finality='finalized', trace_mc_seqno_end=100, end_utime=1000,
            transactions=[base64.b64encode(b't'*32).decode()],
            details=dict(asset=ton.USDT_MASTER, sender='0:'+'11'*32, receiver='0:'+'22'*32,
                amount='101234', comment='7788', is_encrypted_comment=False))

    async def test_ton_only_completed_real_usdt_action_produces_proof(self):
        action = self.action()
        gateway = ton.TonGateway('https://toncenter.com/api/v3')
        gateway.finalized_block = AsyncMock(return_value={'number':101,'timestamp':1001,'hash':'0x'+'33'*32})
        gateway._get = AsyncMock(side_effect=[{'actions':[]},{'actions':[action]}])
        transfer = (await gateway.transfers(action['transactions'][0], action['details']['receiver']))[0]
        self.assertEqual((transfer.amount_units,transfer.memo), (101234,'7788'))
        self.assertEqual(transfer.tx_hash,ton.transaction_hash(action['action_id']))
        self.assertIn(ton.transaction_hash(action['transactions'][0]), transfer.binance_tx_hashes)
        for change in ({'success':False},{'success':1},{'finality':'pending'},{'trace_mc_seqno_end':102},
                       {'transactions':[base64.b64encode(b'z'*32).decode()]}):
            gateway._get = AsyncMock(return_value={'actions':[{**action,**change}]})
            with self.subTest(change=change), self.assertRaises(ValueError):
                await gateway.transfers(action['transactions'][0],action['details']['receiver'])
        for change in ({'asset':'0:'+'44'*32},{'receiver':'0:'+'55'*32}):
            gateway._get = AsyncMock(return_value={'actions':[{**action,'details':{**action['details'],**change}}]})
            self.assertEqual(await gateway.transfers(action['action_id'],action['details']['receiver']),[])

    async def test_ton_binance_requires_matching_memo_and_transaction_alias(self):
        gateway = binance.BinanceDeposits('fixture','secret','TON','7788')
        recipient = '0:'+'22'*32
        transfer = SimpleNamespace(tx_hash='0x'+'aa'*32, binance_tx_hashes=('0x'+'bb'*32,),
            recipient=recipient, amount_units=101234, memo='7788')
        row = dict(coin='USDT', network='TON', txId='bb'*32, address=ton.friendly_address(recipient),
            addressTag='7788', amount='0.101234', id='credit1', status=1, transferType=0)
        gateway._get = AsyncMock(return_value=[row])
        self.assertEqual(await gateway.credited_deposit(transfer,start_ms=0,end_ms=1000),'credit1')
        for change in ({'addressTag':''},{'status':6},{'transferType':1},{'txId':'cc'*32},{'network':'BSC'}):
            gateway._get = AsyncMock(return_value=[{**row,**change}])
            self.assertIsNone(await gateway.credited_deposit(transfer,start_ms=0,end_ms=1000))
        gateway._get = AsyncMock(return_value={'address':ton.friendly_address(recipient),'tag':'different'})
        with self.assertRaises(ValueError): await gateway.validate_address(recipient)


class NetworkLifecycleTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.base, cls.mods = load_payment_modules()
        cls.models, cls.module = cls.mods['models'], cls.mods['service']

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.engine = create_engine('sqlite:///'+str(Path(self.temp.name)/'payments.db'),connect_args={'timeout':30})
        self.base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine,expire_on_commit=False)
        self.settings = SimpleNamespace(mode='live',live_mode=True,enabled=True,polygon_receive_address='0x'+'12'*20,
            polygon_rpc_url='https://rpc.test',bsc_receive_address='0x'+'12'*20,bsc_rpc_url='https://bsc.test',
            ton_receive_address='0:'+'22'*32,ton_receive_memo='7788',ton_api_url='https://toncenter.com/api/v3',
            binance_api_key='fixture',binance_api_secret='fixture',binance_network='MATIC',
            code_key=base64.urlsafe_b64encode(b'k'*32).decode(),terms_version=self.module.TERMS_VERSION,
            seat_limit=10,checkout_minutes=30,validate_common=lambda:None)
        self.ps = self.module.PaymentService(self.sessions,self.settings,SimpleNamespace(),AsyncMock())
        self.networks = {}
        for chain in ('polygon','bsc','ton'):
            service = self.module.network_service(self.ps,chain)
            rpc = SimpleNamespace(finalized_block=AsyncMock(return_value={'number':100,'timestamp':int(datetime.now(timezone.utc).timestamp())}),
                transfers=AsyncMock(),transaction_ids=AsyncMock(return_value=[]))
            deposits = SimpleNamespace(network={'polygon':'MATIC','bsc':'BSC','ton':'TON'}[chain],memo='7788' if chain=='ton' else '',
                normalize_address=ton.address if chain=='ton' else evm.address,
                validate_address=AsyncMock(return_value=True),credited_deposit=AsyncMock(return_value='credit-'+chain))
            if chain=='polygon': service._polygon_gateway,service._binance_gateway=rpc,deposits
            else: service._network_gateway,service._network_binance=rpc,deposits
            self.networks[chain]=service
        with self.sessions.begin() as s:
            self.module.seed_products(s)
            s.add(self.models.Product(id='p',title='fixture',kind='renew',tier='normal',months=1,price_fen=100,
                usdt_price_units=100000,version=1,active=True))
            for service in self.networks.values():
                Config,_,_,_=service._chain_models();s.add(Config(id=1,enabled=True,version=1))

    def tearDown(self):
        self.engine.dispose();self.temp.cleanup()

    async def order(self,chain,buyer):
        service=self.networks[chain]
        quote=await service.create_polygon_quote(buyer,'p',1,self.settings.terms_version)
        order=service.confirm_polygon_quote(buyer,quote['id'],True,self.settings.terms_version)
        return quote,order

    def transfer(self,chain,quote):
        service=self.networks[chain]
        return evm.Transfer('0x'+'34'*32,0,101,'0x'+'56'*32,int(datetime.now(timezone.utc).timestamp()),
            '0x'+'78'*20,service.receive_address,quote['amount_units'],service.receive_memo)

    async def test_cross_network_pending_order_cannot_create_a_second_payment(self):
        _,order=await self.order('polygon',42)
        for chain in ('bsc','ton'):
            with self.assertRaises(ValueError): await self.order(chain,42)
            with self.assertRaises(ValueError): self.networks[chain].polygon_order(order['id'],42)
        with self.sessions() as s:self.assertEqual(s.query(self.models.Order).count(),1)

    async def test_cross_network_deposit_replay_rejected_and_retry_issues_original_code(self):
        q1,o1=await self.order('bsc',42);q2,o2=await self.order('ton',43)
        for chain,q in (('bsc',q1),('ton',q2)):
            service=self.networks[chain];service.polygon_gateway.transfers.return_value=[self.transfer(chain,q)]
            service.binance_gateway.credited_deposit.return_value='one-binance-deposit'
        await self.networks['bsc'].check_polygon_transaction(o1['id'],'0x'+'34'*32)
        self.ps.fulfill_order(o1['id']);first=self.ps.reveal_code(o1['id'],42)
        self.ps.fulfill_order(o1['id']);self.assertEqual(first,self.ps.reveal_code(o1['id'],42))
        with self.assertRaises(ValueError): await self.networks['ton'].check_polygon_transaction(o2['id'],'0x'+'34'*32)
        self.assertEqual(self.ps.get_order(o2['id'])['payment_state'],'pending')
        with self.sessions() as s:self.assertEqual(s.query(self.models.Code).count(),1)

    async def test_ton_memo_wrong_or_changed_cannot_confirm_or_deliver(self):
        service=self.networks['ton'];q=await service.create_polygon_quote(42,'p',1,self.settings.terms_version)
        self.settings.ton_receive_memo='changed'
        with self.assertRaises(ValueError):service.confirm_polygon_quote(42,q['id'],True,self.settings.terms_version)
        self.settings.ton_receive_memo='7788';service.confirm_polygon_quote(42,q['id'],True,self.settings.terms_version)
        transfer=self.transfer('ton',q)
        service.polygon_gateway.transfers.return_value=[evm.Transfer(**{**transfer.__dict__,'memo':''})]
        with self.assertRaises(ValueError):await service.check_polygon_transaction(q['id'],transfer.tx_hash)
        with self.assertRaises(ValueError):self.ps.fulfill_order(q['id'])

    async def test_pausing_new_network_preserves_old_orders_and_other_networks(self):
        q,o=await self.order('bsc',42);service=self.networks['bsc']
        await service.save_polygon(1,1,False)
        with self.assertRaises(ValueError):await self.order('bsc',43)
        await self.order('polygon',43)
        service.polygon_gateway.transfers.return_value=[self.transfer('bsc',q)]
        await service.check_polygon_transaction(o['id'],'0x'+'34'*32)
        self.ps.fulfill_order(o['id'])
        self.assertEqual(self.ps.get_order(o['id'])['fulfillment_state'],'issued')

    async def test_ton_late_credit_discovery_survives_expiry_and_sales_pause(self):
        q,o=await self.order('ton',42);service=self.networks['ton']
        with self.sessions.begin() as s:
            s.get(self.models.Order,o['id']).payment_state='expired'
        self.settings.enabled=False
        service.polygon_gateway.transfers.return_value=[self.transfer('ton',q)]
        service.binance_gateway.recent_deposits=AsyncMock(return_value=[dict(coin='USDT',network='TON',
            address=ton.friendly_address(service.receive_address),addressTag='7788',txId='34'*32,
            amount=str(q['amount_units']/1000000),status=1,transferType=0)])
        await service.scan_polygon()
        self.ps.fulfill_order(o['id'])
        await service.scan_polygon()
        restarted=self.module.PaymentService(self.sessions,self.settings,SimpleNamespace())
        restarted.fulfill_order(o['id'])
        with self.sessions() as s:self.assertEqual(s.query(self.models.Code).count(),1)
        self.assertEqual(restarted.get_order(o['id'])['fulfillment_state'],'issued')

    async def test_ton_history_failure_does_not_expire_or_deliver_order(self):
        q,o=await self.order('ton',42);service=self.networks['ton']
        with self.sessions.begin() as s:s.get(self.models.Order,o['id']).expires_at=datetime.utcnow()-timedelta(minutes=10)
        service.binance_gateway.recent_deposits=AsyncMock(side_effect=ValueError('fixture outage'))
        with self.assertRaises(ValueError):await service.scan_polygon()
        self.assertEqual(self.ps.get_order(o['id'])['payment_state'],'pending')
        with self.sessions() as s:self.assertEqual(s.query(self.models.Code).count(),0)

    async def test_one_unverifiable_candidate_does_not_pin_other_registration_seats(self):
        with self.sessions.begin() as s:s.get(self.models.Product,'p').kind='register'
        q1,o1=await self.order('bsc',42);q2,o2=await self.order('bsc',43)
        service=self.networks['bsc']
        with self.sessions.begin() as s:
            for order in (o1,o2):
                s.get(self.models.Order,order['id']).expires_at=datetime.utcnow()-timedelta(minutes=10)
        chain_error=self.module.PolygonError('polygon_pending_confirmation')
        service.polygon_gateway.transfers.side_effect=chain_error
        service.binance_gateway.recent_deposits=AsyncMock(return_value=[dict(coin='USDT',network='BSC',
            address=service.receive_address,addressTag='',txId='0x'+'34'*32,
            amount=f"0.{q1['amount_units']:06d}",status=1,transferType=0)])
        with self.assertRaises(self.module.PolygonError):await service.scan_polygon()
        with self.sessions() as s:
            first,second=s.get(self.models.Order,o1['id']),s.get(self.models.Order,o2['id'])
            self.assertEqual((first.payment_state,first.seat_reserved),('pending',True))
            self.assertEqual((second.payment_state,second.seat_reserved),('expired',False))
            self.assertEqual(s.query(self.models.Code).count(),0)

    async def test_bsc_history_discovery_rechecks_receipt_without_log_scanning(self):
        q,o=await self.order('bsc',42);service=self.networks['bsc']
        transfer=self.transfer('bsc',q)
        service.polygon_gateway.transfers.return_value=[transfer]
        service.binance_gateway.recent_deposits=AsyncMock(return_value=[dict(coin='USDT',network='BSC',
            address=service.receive_address,addressTag='',txId=transfer.tx_hash,
            amount=f"0.{q['amount_units']:06d}",status=1,transferType=0)])
        await service.scan_polygon()
        self.assertEqual(self.ps.get_order(o['id'])['payment_state'],'paid')
        self.assertEqual(service.polygon_gateway.transfers.await_count,2)
        service.polygon_gateway.transaction_ids.assert_not_awaited()

    async def test_network_quotes_share_hourly_limit_and_consent_is_required(self):
        for n in range(10):
            chain=('polygon','bsc','ton')[n%3]
            service=self.networks[chain]
            q=await service.create_polygon_quote(42,'p',1,self.settings.terms_version)
            with self.assertRaises(ValueError):service.confirm_polygon_quote(42,q['id'],False,self.settings.terms_version)
            _,Quote,_,_=service._chain_models()
            with self.sessions.begin() as s:s.get(Quote,q['id']).expires_at=datetime.utcnow()-timedelta(seconds=1)
        with self.assertRaises(ValueError):await self.networks['bsc'].create_polygon_quote(42,'p',1,self.settings.terms_version)

    async def test_migration_reentry_keeps_old_quotes_codes_and_channel_states(self):
        q,o=await self.order('polygon',42)
        service=self.networks['polygon']
        service.polygon_gateway.transfers.return_value=[self.transfer('polygon',q)]
        await service.check_polygon_transaction(o['id'],'0x'+'34'*32)
        self.ps.fulfill_order(o['id'])
        original=self.ps.reveal_code(o['id'],42)
        # Simulate pre-upgrade state: new tables absent, old paid code retained.
        added=[t for t in self.base.metadata.tables.values() if t.name.startswith(('payment_bsc_','payment_ton_'))
            or t.name=='payment_chain_deposit_claims']
        self.base.metadata.drop_all(self.engine,tables=added)
        spec=importlib.util.spec_from_file_location('network_migration',ROOT/'bot/sql_helper/alembic/versions/20260915_10_add_usdt_networks.py')
        migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        with self.engine.begin() as conn,Operations.context(MigrationContext.configure(conn)):
            migration.upgrade();migration.upgrade()
        self.assertEqual(self.ps.polygon_order(o['id'],42)['amount_units'],q['amount_units'])
        self.assertTrue(self.ps.polygon_info()['enabled'])
        self.assertEqual(self.ps.reveal_code(o['id'],42),original)
        with self.sessions() as s:
            self.assertEqual(s.get(self.models.ChainDepositClaim,'credit-polygon').order_id,o['id'])
            self.assertFalse(s.get(self.models.BscSalesConfig,1).enabled)
            self.assertFalse(s.get(self.models.TonSalesConfig,1).enabled)


if __name__=='__main__':unittest.main(verbosity=2)
