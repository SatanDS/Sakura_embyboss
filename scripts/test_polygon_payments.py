"""Synthetic Polygon/Binance order lifecycle, never real money or messages."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from scripts.test_payment_core import load_payment_modules


class PolygonPaymentTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.base, cls.mods = load_payment_modules()
        cls.models, cls.module = cls.mods['models'], cls.mods['service']

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.engine = create_engine('sqlite:///' + str(Path(self.temp.name)/'test.db'), connect_args={'timeout':30})
        self.base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.address = '0x' + 'ab'*20
        self.settings = SimpleNamespace(mode='live', enabled=True, polygon_receive_address=self.address,
            polygon_rpc_url='https://rpc.example.test', binance_api_key='fixture', binance_api_secret='fixture',
            binance_network='POL', code_key=base64.urlsafe_b64encode(b'k'*32).decode(), terms_version=self.module.TERMS_VERSION,
            checkout_minutes=30, seat_limit=10, validate_common=lambda:None, test_buyer_ids=())
        self.gateway = SimpleNamespace(create_checkout=AsyncMock())
        self.notify = AsyncMock()
        self.ps = self.module.PaymentService(self.sessions, self.settings, self.gateway, self.notify)
        self.ps._polygon_gateway = SimpleNamespace(finalized_block=AsyncMock(return_value={'number':100, 'timestamp':int(datetime.now(timezone.utc).timestamp())}),
            transfers=AsyncMock(), transaction_ids=AsyncMock(return_value=[]))
        self.ps._binance_gateway = SimpleNamespace(network='POL', validate_address=AsyncMock(return_value=True),
            credited_deposit=AsyncMock(return_value='deposit1'))
        with self.sessions.begin() as session:
            self.module.seed_products(session)
            session.add(self.models.Product(id='p1', title='Whitelist', kind='register', tier='vip', months=1,
                price_fen=100, usdt_price_units=2_000_000, version=1, active=True))
            session.add(self.models.PolygonSalesConfig(id=1, version=1, enabled=True))

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    async def quote(self, buyer=42):
        return await self.ps.create_polygon_quote(buyer, 'p1', 1, self.settings.terms_version)

    def confirm(self, quote, buyer=42):
        return self.ps.confirm_polygon_quote(buyer, quote['id'], True, self.settings.terms_version)

    def transfer(self, quote, **changes):
        from bot.payments.polygon_chain import Transfer
        fields = dict(tx_hash='0x'+'12'*32, log_index=0, block_number=101, block_hash='0x'+'34'*32,
            timestamp=int(datetime.now(timezone.utc).timestamp()), sender='0x'+'56'*20, recipient=self.address,
            amount_units=quote['amount_units'])
        return Transfer(**{**fields, **changes})

    async def test_quote_and_confirmation_preserve_fixed_price_and_unique_tail(self):
        first = await self.quote()
        self.assertIsNone(first['address'])
        self.assertGreater(first['amount_units'], 2_000_000)
        self.assertLess(first['amount_units'], 2_010_000)
        self.assertEqual((await self.quote())['id'], first['id'])
        second = await self.quote(43)
        self.assertNotEqual(first['amount_units'], second['amount_units'])
        with self.assertRaises(self.module.PaymentError):
            self.ps.confirm_polygon_quote(43, first['id'], True, self.settings.terms_version)
        with self.assertRaises(self.module.PaymentError):
            self.ps.confirm_polygon_quote(42, first['id'], False, self.settings.terms_version)
        order = self.confirm(first)
        self.assertEqual(self.confirm(first)['id'], order['id'])
        self.assertEqual((order['currency'], order['provider'], order['amount_usdt_units']), ('usdt','polygon',first['amount_units']))
        self.assertEqual(self.ps.polygon_order(order['id'],42)['address'], self.address)
        self.gateway.create_checkout.assert_not_awaited()
        with self.sessions() as session:
            self.assertEqual(session.query(self.models.Order).count(),1)
            self.assertTrue(session.get(self.models.Order, order['id']).seat_reserved)

    async def test_price_change_terms_expiry_and_disabled_sales_cannot_confirm(self):
        q = await self.quote()
        with self.sessions.begin() as session:
            session.get(self.models.Product,'p1').version=2
        with self.assertRaises(self.module.PaymentError): self.confirm(q)
        with self.sessions.begin() as session:
            session.get(self.models.Product,'p1').version=1
            session.get(self.models.PolygonQuote,q['id']).expires_at=datetime.utcnow()-timedelta(seconds=1)
        with self.assertRaises(ValueError): self.confirm(q)
        q2 = await self.quote()
        self.settings.enabled=False
        with self.assertRaises(self.module.PaymentError): self.confirm(q2)
        with self.sessions() as session: self.assertEqual(session.query(self.models.Order).count(),0)

    async def test_uncredited_deposit_cannot_issue_then_retries_issue_one_code(self):
        q=await self.quote();order=self.confirm(q);transfer=self.transfer(q)
        self.ps._polygon_gateway.transfers.return_value=[transfer]
        self.ps._binance_gateway.credited_deposit.return_value=None
        await self.ps.check_polygon_transaction(order['id'],transfer.tx_hash)
        self.assertEqual(self.ps.get_order(order['id'])['payment_state'],'pending')
        with self.assertRaises(self.module.PaymentError):self.ps.fulfill_order(order['id'])
        self.ps._binance_gateway.credited_deposit.return_value='deposit1'
        await self.ps.reconcile_order(order['id'])
        await self.ps.process_tasks(limit=10)
        await self.ps.reconcile_order(order['id'])
        self.ps.fulfill_order(order['id'])
        self.notify.assert_awaited_once()
        with self.sessions() as session:self.assertEqual(session.query(self.models.Code).count(),1)
        self.assertTrue(self.ps.reveal_code(order['id'],42).startswith('DuSheng-Pay_'))

    async def test_other_transaction_amount_network_and_stripe_session_cannot_pay(self):
        q=await self.quote();order=self.confirm(q)
        for values in ({'amount_units':q['amount_units']-1},{'block_number':100}):
            self.ps._polygon_gateway.transfers.return_value=[self.transfer(q,**values)]
            with self.assertRaises(ValueError):await self.ps.check_polygon_transaction(order['id'],'0x'+'12'*32)
        with self.assertRaises(self.module.PaymentError):
            await self.ps.reconcile_order(order['id'],session_hint='cs_fake')
        with self.sessions() as session:
            row=session.get(self.models.Order,order['id'])
            with self.assertRaises(self.module.PaymentError):self.ps._validate_session(row,{})
        self.ps._binance_gateway.credited_deposit.assert_not_awaited()
        self.assertEqual(self.ps.get_order(order['id'])['payment_state'],'pending')

    async def test_disabled_channel_and_global_sales_still_process_existing_payment(self):
        q=await self.quote();order=self.confirm(q);transfer=self.transfer(q)
        with self.sessions.begin() as session:session.get(self.models.PolygonSalesConfig,1).enabled=False
        self.settings.enabled=False
        self.ps._polygon_gateway.transfers.return_value=[transfer]
        await self.ps.check_polygon_transaction(order['id'],transfer.tx_hash)
        await self.ps.process_tasks(limit=10)
        self.assertEqual(self.ps.get_order(order['id'])['fulfillment_state'],'issued')

    async def test_expired_amount_is_never_reused_and_late_payment_still_delivered(self):
        q=await self.quote();order=self.confirm(q)
        with self.sessions.begin() as session:
            row=session.get(self.models.Order,order['id']);row.payment_state='expired';row.seat_reserved=False
            session.get(self.models.PolygonQuote,order['id']).expires_at=datetime.utcnow()-timedelta(minutes=1)
            self.module.release_registration(session,'order:'+order['id'])
        q2=await self.quote()
        self.assertNotEqual(q['amount_units'],q2['amount_units'])
        self.ps._polygon_gateway.transfers.return_value=[self.transfer(q)]
        await self.ps.check_polygon_transaction(order['id'],'0x'+'12'*32)
        await self.ps.process_tasks(limit=10)
        self.assertEqual(self.ps.get_order(order['id'])['fulfillment_state'],'issued')

    async def test_duplicate_binance_deposit_cannot_fund_two_orders(self):
        q=await self.quote();order=self.confirm(q)
        self.ps._polygon_gateway.transfers.return_value=[self.transfer(q)]
        await self.ps.check_polygon_transaction(order['id'],'0x'+'12'*32)
        q2=await self.quote(43);order2=self.confirm(q2,43)
        tx2=self.transfer(q2,tx_hash='0x'+'78'*32)
        self.ps._polygon_gateway.transfers.return_value=[tx2]
        with self.assertRaises(ValueError):await self.ps.check_polygon_transaction(order2['id'],tx2.tx_hash)
        self.assertEqual(self.ps.get_order(order2['id'])['payment_state'],'pending')

    async def test_concurrent_confirmation_cannot_duplicate_orders_or_reservations(self):
        q=await self.quote();gate=Barrier(4)
        def confirm(_):
            gate.wait(timeout=10)
            return self.confirm(q)['id']
        with ThreadPoolExecutor(max_workers=4) as pool:ids=list(pool.map(confirm,range(4)))
        self.assertEqual(len(set(ids)),1)
        with self.sessions() as session:
            self.assertEqual(session.query(self.models.Order).count(),1)
            self.assertEqual(session.query(self.models.RegistrationReservation).filter_by(state='active').count(),1)

    async def test_scan_persists_cursor_and_finds_payment_without_client_hash(self):
        q=await self.quote();order=self.confirm(q);transfer=self.transfer(q)
        self.ps._polygon_gateway.finalized_block.return_value={'number':105,'timestamp':int(datetime.now(timezone.utc).timestamp())}
        self.ps._polygon_gateway.transfers.return_value=[transfer]
        self.ps._polygon_gateway.transaction_ids.return_value=[transfer.tx_hash]
        await self.ps.scan_polygon()
        await self.ps.process_tasks(limit=10)
        self.assertEqual(self.ps.get_order(order['id'])['fulfillment_state'],'issued')
        with self.sessions() as session:self.assertEqual(session.get(self.models.PolygonCursor,self.address).block_number,105)
        self.ps._polygon_gateway.transaction_ids.reset_mock()
        await self.ps.scan_polygon()
        self.ps._polygon_gateway.transaction_ids.assert_not_awaited()

    async def test_scan_outage_does_not_advance_cursor_or_release_seat(self):
        q=await self.quote();order=self.confirm(q)
        with self.sessions.begin() as session:session.get(self.models.Order,order['id']).expires_at=datetime.utcnow()-timedelta(minutes=1)
        self.ps._polygon_gateway.finalized_block.return_value={'number':105,'timestamp':int(datetime.now(timezone.utc).timestamp())}
        self.ps._polygon_gateway.transaction_ids.side_effect=TimeoutError()
        with self.assertRaises(TimeoutError):await self.ps.scan_polygon()
        with self.sessions() as session:
            self.assertEqual(session.get(self.models.PolygonCursor,self.address).block_number,100)
            self.assertTrue(session.get(self.models.Order,order['id']).seat_reserved)

    async def test_confirmation_during_scan_does_not_skip_early_transfer(self):
        # Another order already made this address scannable. An unconfirmed
        # quote's transfer is seen while no corresponding Order exists yet.
        q=await self.quote()
        with self.sessions.begin() as session:
            session.add(self.models.PolygonCursor(address=self.address,block_number=100,revision=0))
        self.ps._polygon_gateway.finalized_block.return_value={'number':105,'timestamp':int(datetime.now(timezone.utc).timestamp())}
        async def scan(_recipient, _start, _end):
            self.confirm(q)
            return []
        self.ps._polygon_gateway.transaction_ids.side_effect=scan
        await self.ps.scan_polygon()
        with self.sessions() as session:
            cursor=session.get(self.models.PolygonCursor,self.address)
            self.assertEqual((cursor.block_number,cursor.revision),(100,1))
        self.ps._polygon_gateway.transaction_ids.side_effect=None
        self.ps._polygon_gateway.transaction_ids.return_value=['0x'+'12'*32]
        self.ps._polygon_gateway.transfers.return_value=[self.transfer(q)]
        await self.ps.scan_polygon()
        await self.ps.process_tasks(limit=10)
        self.assertEqual(self.ps.get_order(q['id'])['fulfillment_state'],'issued')

    async def test_late_chain_transfer_waits_for_credit_and_keeps_reconciling(self):
        q=await self.quote();order=self.confirm(q)
        with self.sessions.begin() as session:
            row=session.get(self.models.Order,order['id']);row.payment_state='expired';row.seat_reserved=False
            self.module.release_registration(session,'order:'+order['id'])
        self.ps._polygon_gateway.transfers.return_value=[self.transfer(q)]
        self.ps._binance_gateway.credited_deposit.return_value=None
        await self.ps.check_polygon_transaction(order['id'],'0x'+'12'*32)
        self.assertEqual(self.ps.get_order(order['id'])['payment_state'],'pending')
        self.ps._binance_gateway.credited_deposit.return_value='deposit1'
        await self.ps.reconcile_orders()
        await self.ps.process_tasks(limit=10)
        self.assertEqual(self.ps.get_order(order['id'])['fulfillment_state'],'issued')

    async def test_bad_submitted_transaction_reports_failure_without_endless_retry(self):
        q=await self.quote();self.confirm(q)
        self.ps._polygon_gateway.transfers.return_value=[]
        self.ps.submit_polygon_transaction(q['id'],42,'0x'+'12'*32)
        await self.ps.process_tasks(limit=10)
        self.assertEqual(self.ps.polygon_order(q['id'],42)['check_error'],'polygon_transfer_mismatch')
        with self.sessions() as session:
            task=session.query(self.models.Task).filter_by(task_type='polygon_transaction').one()
            self.assertEqual(task.state,'failed')
        with self.assertRaises(self.module.PaymentError):
            self.ps.submit_polygon_transaction(q['id'],43,'0x'+'12'*32)

    async def test_pending_stripe_order_blocks_duplicate_polygon_checkout(self):
        old=self.ps.create_order(42,'p1',1,self.settings.terms_version,True)
        with self.assertRaises(ValueError):await self.quote()
        with self.sessions() as session:self.assertEqual(session.query(self.models.Order).count(),1)
        with self.sessions.begin() as session:session.get(self.models.Order,old['id']).payment_state='expired'
        q=await self.quote();self.confirm(q)
        with self.assertRaises(self.module.PaymentError):self.ps.create_order(42,'p1',1,self.settings.terms_version,True)

    async def test_polygon_paid_registration_waits_if_late_payment_has_no_capacity(self):
        q=await self.quote();order=self.confirm(q)
        with self.sessions.begin() as session:
            row=session.get(self.models.Order,order['id']);row.payment_state='expired';row.seat_reserved=False
            self.module.release_registration(session,'order:'+order['id'])
        self.ps._polygon_gateway.transfers.return_value=[self.transfer(q)]
        self.settings.seat_limit=1
        with patch.object(self.module,'actual_account_count',return_value=1):
            await self.ps.check_polygon_transaction(order['id'],'0x'+'12'*32)
        saved=self.ps.get_order(order['id'])
        self.assertEqual(saved['payment_state'],'paid')
        self.assertTrue(saved['review_required'])
        with self.assertRaises(self.module.PaymentError):self.ps.fulfill_order(order['id'])
        self.settings.seat_limit=10
        await self.ps.reconcile_order(order['id'])
        await self.ps.process_tasks(limit=10)
        self.assertEqual(self.ps.get_order(order['id'])['fulfillment_state'],'issued')

    async def test_usdt_price_is_independent_and_only_two_decimal_base_is_accepted(self):
        data=dict(id='p1',version=1,title='Whitelist',kind='register',tier='vip',months=1,price_fen=0,
                  active=True,usdt_price_units=3000000)
        saved=self.ps.save_product(1,data)
        self.assertEqual((saved['price_fen'],saved['usdt_price_units']),(0,3000000))
        for price in (True,20000,3000010):
            with self.assertRaises(self.module.PaymentError):self.ps.save_product(1,{**data,'version':2,'usdt_price_units':price})

    async def test_migration_repeat_preserves_existing_stripe_and_polygon_records(self):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        path=Path(__file__).resolve().parents[1]/'bot/sql_helper/alembic/versions/20260914_09_add_polygon_payments.py'
        spec=importlib.util.spec_from_file_location('polygon_migration_test',path)
        migration=importlib.util.module_from_spec(spec);spec.loader.exec_module(migration)
        engine=create_engine('sqlite:///:memory:')
        try:
            with engine.begin() as conn:
                conn.execute(text('CREATE TABLE payment_products (id VARCHAR(32) PRIMARY KEY, price_fen INTEGER)'))
                conn.execute(text('CREATE TABLE payment_orders (id VARCHAR(32) PRIMARY KEY, amount_fen INTEGER, currency VARCHAR(3))'))
                conn.execute(text("INSERT INTO payment_products VALUES ('legacy', 100)"))
                conn.execute(text("INSERT INTO payment_orders VALUES ('legacy',100,'cny')"))
                with Operations.context(MigrationContext.configure(conn)):
                    migration.upgrade();migration.upgrade()
                self.assertEqual(conn.execute(text('SELECT amount_fen,currency,provider,amount_usdt_units FROM payment_orders')).one(),
                                 (100,'cny','stripe',None))
                self.assertEqual(conn.execute(text('SELECT usdt_price_units FROM payment_products')).scalar(),0)
                self.assertEqual(conn.execute(text('SELECT enabled FROM payment_polygon_config')).scalar(),False)
        finally:engine.dispose()
        q=await self.quote();self.confirm(q)
        with self.engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            migration.upgrade();migration.upgrade()
        self.assertEqual(self.ps.polygon_order(q['id'],42)['amount_units'],q['amount_units'])
        self.assertTrue(self.ps.polygon_info()['enabled'])


if __name__=='__main__':unittest.main(verbosity=2)
