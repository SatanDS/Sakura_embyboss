"""Polygon quotes and Binance-credit delivery, independent of Stripe Checkout."""
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from .polygon_chain import PolygonGateway, PolygonError, address, transaction_hash, CHAIN_ID, USDT_CONTRACT
from .binance_deposits import BinanceDeposits, DepositError


MIN_USDT_UNITS = 30_000
MAX_USDT_UNITS = 999_999_990_000
QUOTE_MINUTES = 30


def format_usdt(units):
    return f"{units // 1_000_000}.{units % 1_000_000:06d}"


class PolygonPayments:
    crypto_provider = "polygon"
    chain_label = "Polygon PoS"
    chain_id = CHAIN_ID
    token_contract = USDT_CONTRACT

    def _chain_models(self):
        from .models import PolygonSalesConfig, PolygonQuote, PolygonCursor, PolygonReceipt
        return PolygonSalesConfig, PolygonQuote, PolygonCursor, PolygonReceipt

    @property
    def receive_address(self):
        return address(getattr(self.settings, "polygon_receive_address", ""))

    @property
    def receive_memo(self):
        return ""

    def normalize_transaction(self, value):
        return transaction_hash(value)

    def _quote_matches_destination(self, quote):
        return quote.address == self.receive_address and getattr(quote, "memo", "") == self.receive_memo

    @property
    def polygon_gateway(self):
        if getattr(self, "_polygon_gateway", None) is None:
            self._polygon_gateway = PolygonGateway(getattr(self.settings, "polygon_rpc_url", ""))
        return self._polygon_gateway

    @property
    def binance_gateway(self):
        if getattr(self, "_binance_gateway", None) is None:
            self._binance_gateway = BinanceDeposits(getattr(self.settings, "binance_api_key", ""),
                getattr(self.settings, "binance_api_secret", ""), getattr(self.settings, "binance_network", "POL"))
        return self._binance_gateway

    def polygon_info(self):
        PolygonSalesConfig, _, _, _ = self._chain_models()
        with self.session_factory() as session:
            row = session.get(PolygonSalesConfig, 1)
            try:
                receiver = self.receive_address
                self.polygon_gateway
                self.binance_gateway
                ready = True
            except (PolygonError, DepositError):
                receiver, ready = "", False
            return {"enabled": bool(row and row.enabled), "version": row.version if row else 1,
                    "mode": self.mode, "configured": ready, "address": receiver,
                    "min_units": MIN_USDT_UNITS, "chain_id": self.chain_id, "token_contract": self.token_contract,
                    "chain": self.crypto_provider, "label": self.chain_label, "memo": self.receive_memo,
                    "quote_minutes": QUOTE_MINUTES}

    async def save_polygon(self, actor_tg, version, enabled):
        from .models import Audit
        PolygonSalesConfig, _, _, _ = self._chain_models()
        from .service import _capacity_lock, PaymentError
        if type(actor_tg) is not int or actor_tg <= 0 or type(version) is not int or version < 1 or type(enabled) is not bool:
            raise PaymentError("invalid_channels")
        if enabled:
            if self.mode != "live":
                raise PolygonError("polygon_live_only")
            self.settings.validate_common()
            head = await self.polygon_gateway.finalized_block()
            from .service import utcnow
            if abs(head["timestamp"] - int(utcnow().replace(tzinfo=timezone.utc).timestamp())) > 300:
                raise PolygonError("polygon_rpc_stale")
            await self.binance_gateway.validate_address(self.receive_address)
        with self.session_factory.begin() as session:
            _capacity_lock(session)
            row = session.query(PolygonSalesConfig).filter_by(id=1).with_for_update().first()
            if row is None:
                row = PolygonSalesConfig(id=1, version=1, enabled=False)
                session.add(row)
            if row.version != version:
                if row.version != version + 1 or row.enabled != enabled:
                    raise PaymentError("channels_changed")
            else:
                row.enabled, row.version = enabled, row.version + 1
                session.add(Audit(actor_tg=actor_tg, action=self.crypto_provider + "_sales_changed", target_id=self.crypto_provider,
                                  details={"enabled": enabled, "version": row.version}))
        return self.polygon_info()

    def _quote_data(self, quote, *, show_address=False):
        return {"id": quote.id, "product": quote.product_snapshot,
                "amount_units": quote.amount_units, "amount": format_usdt(quote.amount_units),
                "base_amount": format_usdt(quote.product_snapshot["usdt_price_units"]),
                "tail_amount": format_usdt(quote.amount_units - quote.product_snapshot["usdt_price_units"]),
                "expires_at": quote.expires_at, "terms_version": quote.terms_version,
                "address": quote.address if show_address else None,
                "chain_id": self.chain_id, "token_contract": self.token_contract,
                "chain": self.crypto_provider, "label": self.chain_label,
                "memo": getattr(quote, "memo", "") if show_address else None}

    def _polygon_new_sale(self, session):
        PolygonSalesConfig, _, _, _ = self._chain_models()
        from .service import PaymentError
        if not self.settings.enabled:
            raise PaymentError("sales_disabled")
        if self.mode != "live":
            raise PolygonError("polygon_live_only")
        channel = session.query(PolygonSalesConfig).filter_by(id=1).with_for_update().first()
        if not channel or not channel.enabled:
            raise PolygonError("polygon_sales_disabled")

    async def create_polygon_quote(self, buyer, product_id, version, terms_version):
        from .models import Product, Order, new_id
        _, PolygonQuote, _, _ = self._chain_models()
        from .service import _capacity_lock, PaymentError, utcnow
        if type(buyer) is not int or buyer <= 0:
            raise PaymentError("login_required")
        if not self.settings.enabled or self.mode != "live":
            raise PolygonError("polygon_sales_disabled")
        receiver = self.receive_address
        self.settings.validate_common()
        head = await self.polygon_gateway.finalized_block()
        if abs(head["timestamp"] - int(utcnow().replace(tzinfo=timezone.utc).timestamp())) > 300:
            raise PolygonError("polygon_rpc_stale")
        await self.binance_gateway.validate_address(receiver)
        now = utcnow()
        with self.session_factory.begin() as session:
            _capacity_lock(session)
            self._polygon_new_sale(session)
            product = session.query(Product).filter_by(id=product_id).with_for_update().first()
            if not product or not product.active or product.usdt_price_units < MIN_USDT_UNITS:
                raise PaymentError("product_unavailable")
            if type(version) is not int or version != product.version:
                raise PaymentError("product_changed")
            if terms_version != self.terms_version:
                raise PaymentError("terms_required")
            pending = session.query(Order).filter_by(buyer_tg=buyer, product_id=product_id,
                mode="live", payment_state="pending").filter(Order.expires_at > now).with_for_update().first()
            if pending:
                if pending.provider == self.crypto_provider:
                    return self._quote_data(session.get(PolygonQuote, pending.id))
                raise PolygonError("polygon_pending_other_payment")
            previous = session.query(PolygonQuote).filter_by(buyer_tg=buyer, product_id=product_id,
                address=receiver).filter(PolygonQuote.expires_at > now).order_by(PolygonQuote.created_at.desc()).first()
            if (previous and previous.confirmed_at is None and previous.product_snapshot["version"] == version
                    and previous.terms_version == terms_version and self._quote_matches_destination(previous)):
                return self._quote_data(previous)
            from .models import PolygonQuote as LegacyQuote, BscQuote, TonQuote
            recent = sum(session.query(func.count()).select_from(model).filter(
                model.buyer_tg == buyer, model.created_at > now - timedelta(hours=1)).scalar()
                for model in (LegacyQuote, BscQuote, TonQuote))
            if recent >= 10:
                raise PolygonError("polygon_quote_limit")
            base = product.usdt_price_units
            used = {x for (x,) in session.query(PolygonQuote.amount_units).filter(
                PolygonQuote.address == receiver, PolygonQuote.amount_units > base,
                PolygonQuote.amount_units < base + 10000).all()}
            available = [x for x in range(1, 10000) if base + x not in used]
            if not available:
                raise PolygonError("polygon_amount_slots_full")
            quote = PolygonQuote(id=new_id(), buyer_tg=buyer, product_id=product_id,
                product_snapshot=self._product(product), terms_version=terms_version, address=receiver,
                network=self.binance_gateway.network, amount_units=base + secrets.choice(available),
                start_block=head["number"], created_at=now, expires_at=now + timedelta(minutes=QUOTE_MINUTES))
            if self.crypto_provider != "polygon":
                quote.memo = self.receive_memo
            session.add(quote)
            session.flush()
            return self._quote_data(quote)

    def confirm_polygon_quote(self, buyer, quote_id, accepted, terms_version):
        from .models import Order, Product
        _, PolygonQuote, PolygonCursor, _ = self._chain_models()
        from .service import _capacity_lock, PaymentError, reserve_registration, TERMS_HASH, utcnow
        if accepted is not True or terms_version != self.terms_version:
            raise PaymentError("terms_required")
        with self.session_factory.begin() as session:
            _capacity_lock(session)
            quote = session.query(PolygonQuote).filter_by(id=quote_id, buyer_tg=buyer).with_for_update().first()
            if not quote:
                raise PaymentError("not_found")
            existing = session.get(Order, quote.id)
            if existing:
                return self._order(existing)
            self._polygon_new_sale(session)
            now = utcnow()
            if quote.expires_at <= now:
                raise PolygonError("polygon_quote_expired")
            if not self._quote_matches_destination(quote):
                raise PolygonError("polygon_address_changed")
            product = session.query(Product).filter_by(id=quote.product_id).with_for_update().first()
            if not product or not product.active or product.version != quote.product_snapshot["version"]:
                raise PaymentError("product_changed")
            if quote.terms_version != terms_version:
                raise PaymentError("terms_changed")
            other = session.query(Order.id).filter_by(buyer_tg=buyer, product_id=product.id,
                mode="live", payment_state="pending").filter(Order.expires_at > now).first()
            if other:
                raise PolygonError("polygon_pending_other_payment")
            sold = session.query(func.count()).select_from(Order).filter(Order.product_id == product.id,
                Order.mode == "live", Order.payment_state.in_(("pending", "paid"))).scalar()
            if product.sales_limit is not None and sold >= product.sales_limit:
                raise PaymentError("sold_out")
            row = Order(id=quote.id, buyer_tg=buyer, product_id=product.id, product_snapshot=quote.product_snapshot,
                amount_fen=0, amount_usdt_units=quote.amount_units, currency="usdt", provider=self.crypto_provider, mode="live",
                terms_version=terms_version, terms_hash=TERMS_HASH, accepted_at=now,
                payment_state="pending", fulfillment_state="pending", expires_at=quote.expires_at,
                created_at=now, updated_at=now)
            session.add(row)
            if product.kind == "register":
                reserve_registration(session, "order:" + row.id, self.seat_limit)
                row.seat_reserved = True
            quote.confirmed_at = now
            cursor = session.get(PolygonCursor, quote.address)
            if cursor is None:
                session.add(PolygonCursor(address=quote.address, block_number=quote.start_block, revision=0))
            else:
                cursor.block_number = min(cursor.block_number, quote.start_block)
                cursor.revision += 1
            session.flush()
            return self._order(row)

    def polygon_order(self, order_id, buyer=None):
        from .models import Task
        _, PolygonQuote, _, PolygonReceipt = self._chain_models()
        from .service import PaymentError
        order = self.get_order(order_id, buyer)
        if order["provider"] != self.crypto_provider:
            raise PaymentError("not_found")
        with self.session_factory() as session:
            quote = session.get(PolygonQuote, order_id)
            receipt = session.get(PolygonReceipt, order_id)
            data = self._quote_data(quote, show_address=True)
            check = session.query(Task).filter(Task.task_type == "polygon_transaction",
                Task.unique_key.like("polygon-tx:" + order_id + ":%")).order_by(Task.created_at.desc()).first()
            data.update(order=order, tx_hash=receipt.tx_hash if receipt else None,
                state="credited" if receipt and receipt.credited_at else "awaiting_binance" if receipt else "awaiting_transfer",
                check_error=check.last_error if check and check.state == "failed" else None)
            return data

    def submit_polygon_transaction(self, order_id, buyer, tx_hash):
        from .models import Task
        from .service import _capacity_lock, PaymentError, enqueue
        order = self.get_order(order_id, buyer)
        if order["provider"] != self.crypto_provider or order["mode"] != self.mode:
            raise PaymentError("order_mode_mismatch")
        tx_hash = self.normalize_transaction(tx_hash)
        key = "polygon-tx:" + order_id + ":" + tx_hash
        with self.session_factory.begin() as session:
            _capacity_lock(session)
            if session.query(Task.id).filter_by(unique_key=key).first():
                return {"ok": True}
            count = session.query(func.count()).select_from(Task).filter(
                Task.task_type == "polygon_transaction", Task.unique_key.like("polygon-tx:" + order_id + ":%")).scalar()
            if count >= 10:
                raise PolygonError("polygon_quote_limit")
            enqueue(session, key, "polygon_transaction", {"order_id": order_id, "tx_hash": tx_hash})
        return {"ok": True}

    async def check_polygon_transaction(self, order_id, tx_hash):
        _, PolygonQuote, _, _ = self._chain_models()
        from .service import PaymentError
        order = self.get_order(order_id)
        if self.mode != "live" or order["mode"] != "live" or order["provider"] != self.crypto_provider:
            raise PaymentError("order_mode_mismatch")
        with self.session_factory() as session:
            quote = session.get(PolygonQuote, order_id)
            recipient, units, start = quote.address, quote.amount_units, quote.start_block
            memo = getattr(quote, "memo", "")
        transfers = await self.polygon_gateway.transfers(self.normalize_transaction(tx_hash), recipient)
        matches = [t for t in transfers if t.amount_units == units and t.block_number > start and getattr(t, "memo", "") == memo]
        if len(matches) != 1:
            raise PolygonError("polygon_transfer_mismatch")
        await self._record_polygon_transfer(order_id, matches[0])

    async def _record_polygon_transfer(self, order_id, transfer):
        from .models import Order
        _, PolygonQuote, _, PolygonReceipt = self._chain_models()
        from .service import _capacity_lock, PaymentError
        with self.session_factory.begin() as session:
            _capacity_lock(session)
            row = session.query(Order).filter_by(id=order_id).with_for_update().one()
            quote = session.get(PolygonQuote, order_id)
            if (row.provider != self.crypto_provider or row.mode != "live" or self.mode != "live"
                    or transfer.recipient != quote.address or transfer.amount_units != quote.amount_units
                    or getattr(transfer, "memo", "") != getattr(quote, "memo", "")
                    or transfer.block_number <= quote.start_block or not quote.confirmed_at
                    or datetime.utcfromtimestamp(transfer.timestamp) < quote.created_at - timedelta(seconds=10)):
                raise PolygonError("polygon_transfer_mismatch")
            receipt = session.get(PolygonReceipt, order_id)
            if receipt and (receipt.tx_hash, receipt.log_index) != (transfer.tx_hash, transfer.log_index):
                self._polygon_review(session, row, "multiple_transfers")
                return
            duplicate = session.query(PolygonReceipt).filter_by(tx_hash=transfer.tx_hash, log_index=transfer.log_index).first()
            if duplicate and duplicate.order_id != order_id:
                raise PolygonError("polygon_transfer_used")
            if not receipt:
                session.add(PolygonReceipt(order_id=order_id, tx_hash=transfer.tx_hash, log_index=transfer.log_index,
                    block_number=transfer.block_number, amount_units=transfer.amount_units,
                    received_at=datetime.utcfromtimestamp(transfer.timestamp)))
            if row.payment_state == "expired":
                # A late, verified transfer still needs Binance-credit polling.
                # An expired order without a receipt is otherwise not polled.
                row.payment_state = "pending"
                row.archived_at = None
        await self.reconcile_polygon_order(order_id)

    def _polygon_review(self, session, order, reason):
        from .service import enqueue
        order.review_required = True
        order.archived_at = None
        enqueue(session, "polygon-review:" + order.id + ":" + reason, "notify_review", {"order_id": order.id})

    async def reconcile_polygon_order(self, order_id):
        from .models import Order
        _, PolygonQuote, _, PolygonReceipt = self._chain_models()
        from .service import _capacity_lock, PaymentError, enqueue, reserve_registration, utcnow
        if self.mode != "live":
            raise PaymentError("order_mode_mismatch")
        with self.session_factory() as session:
            quote, receipt = session.get(PolygonQuote, order_id), session.get(PolygonReceipt, order_id)
            if not receipt:
                return
            tx_hash, recipient, network, received_at = receipt.tx_hash, quote.address, quote.network, receipt.received_at
            expected_index, expected_units = receipt.log_index, quote.amount_units
            memo = getattr(quote, "memo", "")
        # Reverify the receipt even for manually submitted transaction hashes.
        transfers = await self.polygon_gateway.transfers(tx_hash, recipient)
        match = [t for t in transfers if t.log_index == expected_index and t.amount_units == expected_units
                 and getattr(t, "memo", "") == memo]
        if len(match) != 1:
            raise PolygonError("polygon_transfer_mismatch")
        binance = self.binance_gateway
        if memo != getattr(binance, "memo", ""):
            binance = BinanceDeposits(self.settings.binance_api_key, self.settings.binance_api_secret, network, memo)
        if binance.network != network:
            raise DepositError("binance_network_changed")
        now = utcnow()
        timestamp = int(received_at.replace(tzinfo=timezone.utc).timestamp() * 1000)
        if now - received_at > timedelta(days=88):
            with self.session_factory.begin() as session:
                row = session.query(Order).filter_by(id=order_id).with_for_update().one()
                self._polygon_review(session, row, "deposit_history_too_old")
            return
        deposit_id = await binance.credited_deposit(match[0], start_ms=max(0, timestamp - 300000),
            end_ms=int(now.replace(tzinfo=timezone.utc).timestamp() * 1000))
        if not deposit_id:
            if now - received_at > timedelta(hours=1):
                with self.session_factory.begin() as session:
                    row = session.query(Order).filter_by(id=order_id).with_for_update().one()
                    self._polygon_review(session, row, "deposit_uncredited")
            return
        with self.session_factory.begin() as session:
            _capacity_lock(session)
            row = session.query(Order).filter_by(id=order_id).with_for_update().one()
            receipt = session.query(PolygonReceipt).filter_by(order_id=order_id).with_for_update().one()
            if row.provider != self.crypto_provider or row.mode != "live":
                raise PaymentError("order_mode_mismatch")
            if receipt.deposit_id and receipt.deposit_id != deposit_id:
                raise PolygonError("polygon_transfer_mismatch")
            reused = session.query(PolygonReceipt).filter_by(deposit_id=deposit_id).first()
            if reused and reused.order_id != order_id:
                raise PolygonError("polygon_transfer_used")
            from .models import ChainDepositClaim, PolygonReceipt as LegacyReceipt
            claim = session.get(ChainDepositClaim, deposit_id)
            legacy = session.query(LegacyReceipt).filter_by(deposit_id=deposit_id).first()
            if (claim and claim.order_id != order_id) or (legacy and legacy.order_id != order_id):
                raise PolygonError("polygon_transfer_used")
            if claim is None:
                session.add(ChainDepositClaim(deposit_id=deposit_id, order_id=order_id))
            receipt.deposit_id, receipt.credited_at = deposit_id, receipt.credited_at or now
            row.payment_state, row.paid_at, row.updated_at = "paid", row.paid_at or now, now
            row.archived_at = None
            if row.product_snapshot["kind"] == "register" and not row.seat_reserved and row.fulfillment_state != "issued":
                try:
                    reserve_registration(session, "order:" + order_id, self.seat_limit)
                    row.seat_reserved = True
                except PaymentError as exc:
                    if exc.code != "capacity_full":
                        raise
                    self._polygon_review(session, row, "late_payment_capacity")
                    return
            enqueue(session, "fulfill:" + order_id, "fulfill_order", {"order_id": order_id})

    async def scan_polygon(self):
        from .models import Order
        _, PolygonQuote, PolygonCursor, PolygonReceipt = self._chain_models()
        from .service import _capacity_lock, release_registration, utcnow
        if self.mode != "live":
            return
        with self.session_factory() as session:
            cursors = [(r.address, r.block_number, r.revision) for r in session.query(PolygonCursor).all()]
        if not cursors:
            return
        head = await self.polygon_gateway.finalized_block()
        for recipient, cursor, revision in cursors:
            # Incremental bounded catch-up. A provider failure leaves the cursor
            # untouched; after restart, the same page is processed idempotently.
            for _ in range(4):
                end = min(head["number"], cursor + 499)
                if end <= cursor:
                    break
                txids = await self.polygon_gateway.transaction_ids(recipient, cursor + 1, end)
                for txid in txids:
                    transfers = await self.polygon_gateway.transfers(txid, recipient)
                    for transfer in transfers:
                        with self.session_factory() as session:
                            quote = session.query(PolygonQuote).join(Order, Order.id == PolygonQuote.id).filter(
                                PolygonQuote.address == recipient, PolygonQuote.amount_units == transfer.amount_units,
                                Order.provider == self.crypto_provider, Order.mode == "live").first()
                            order_id = quote.id if (quote and transfer.block_number > quote.start_block
                                and datetime.utcfromtimestamp(transfer.timestamp) >= quote.created_at - timedelta(seconds=10)) else None
                        if order_id:
                            # A temporary Binance outage must not lose the chain
                            # receipt; the ordinary order reconciliation retries.
                            try:
                                await self._record_polygon_transfer(order_id, transfer)
                            except DepositError:
                                pass
                with self.session_factory.begin() as session:
                    _capacity_lock(session)
                    row = session.query(PolygonCursor).filter_by(address=recipient).with_for_update().one()
                    if row.revision != revision or row.block_number != cursor:
                        # A new order rewound this address while RPC was in
                        # flight, or another scanner won. Re-read next pass.
                        break
                    row.block_number = end
                cursor = end
            if cursor >= head["number"]:
                finalized_time = datetime.utcfromtimestamp(head["timestamp"])
                with self.session_factory.begin() as session:
                    _capacity_lock(session)
                    expired = session.query(Order).join(PolygonQuote, PolygonQuote.id == Order.id).filter(
                        PolygonQuote.address == recipient, Order.provider == self.crypto_provider, Order.payment_state == "pending",
                        Order.expires_at < min(finalized_time, utcnow())).with_for_update().all()
                    for row in expired:
                        if session.get(PolygonReceipt, row.id):
                            continue
                        row.payment_state = "expired"
                        if row.seat_reserved:
                            release_registration(session, "order:" + row.id)
                            row.seat_reserved = False
