"""Network-scoped use of the existing quote, credit and fulfillment lifecycle."""
from datetime import timedelta, timezone

from .polygon_payments import PolygonPayments
from .polygon_chain import address, PolygonError
from .binance_deposits import BinanceDeposits, DepositError, usdt_units
from .bsc_chain import BscGateway
from .ton_chain import TonGateway, address as ton_address, transaction_hash as ton_hash, friendly_address

CHAINS = ("polygon", "bsc", "ton")


class NetworkPayments(PolygonPayments):
    def __init__(self, service, chain):
        if chain not in ("bsc", "ton"):
            raise PolygonError("polygon_wrong_network")
        self.service, self.crypto_provider = service, chain
        self.chain_label = "BNB Smart Chain (BEP20)" if chain == "bsc" else "TON"
        cls = BscGateway if chain == "bsc" else TonGateway
        self.chain_id, self.token_contract = cls.chain_id, cls.token_contract

    def __getattr__(self, key):
        # Reuse the caller's transaction factory, settings and shared order/code
        # methods. Never mutate the singleton PaymentService to switch chains.
        return getattr(self.service, key)

    def _chain_models(self):
        from . import models
        prefix = "Bsc" if self.crypto_provider == "bsc" else "Ton"
        return tuple(getattr(models, prefix + suffix) for suffix in ("SalesConfig", "Quote", "Cursor", "Receipt"))

    @property
    def receive_address(self):
        normalize = address if self.crypto_provider == "bsc" else ton_address
        return normalize(getattr(self.settings, self.crypto_provider + "_receive_address", ""))

    @property
    def receive_memo(self):
        return getattr(self.settings, "ton_receive_memo", "") if self.crypto_provider == "ton" else ""

    @property
    def polygon_gateway(self):
        if "_network_gateway" not in self.__dict__:
            if self.crypto_provider == "ton" and not getattr(self.settings, "ton_api_key", ""):
                raise PolygonError("polygon_rpc_unconfigured")
            self._network_gateway = (BscGateway(getattr(self.settings, "bsc_rpc_url", "")) if self.crypto_provider == "bsc"
                else TonGateway(getattr(self.settings, "ton_api_url", ""), getattr(self.settings, "ton_api_key", "")))
        return self._network_gateway

    @property
    def binance_gateway(self):
        if "_network_binance" not in self.__dict__:
            self._network_binance = BinanceDeposits(getattr(self.settings, "binance_api_key", ""),
                getattr(self.settings, "binance_api_secret", ""), self.crypto_provider.upper(), self.receive_memo)
        return self._network_binance

    def normalize_transaction(self, value):
        return ton_hash(value) if self.crypto_provider == "ton" else super().normalize_transaction(value)

    def _quote_data(self, quote, *, show_address=False):
        data = super()._quote_data(quote, show_address=show_address)
        if show_address and self.crypto_provider == "ton":
            data["address"] = friendly_address(quote.address)
        return data

    def polygon_info(self):
        data = super().polygon_info()
        if data["address"] and self.crypto_provider == "ton":
            data["address"] = friendly_address(data["address"])
        return data

    async def scan_polygon(self):
        await self._scan_binance_chain()

    async def _scan_binance_chain(self):
        """Discover new-network deposits, then independently verify on-chain proofs.

        Binance history is reread without a persisted pagination offset. It can
        report pending credits too; neither that report nor an action alone pays
        an order. Expired orders remain eligible for late delivery.
        """
        from .models import Order
        from .service import _capacity_lock, release_registration, utcnow
        _, Quote, _, Receipt = self._chain_models()
        if self.mode != "live":
            return
        with self.session_factory() as session:
            oldest = session.query(Quote.created_at).join(Order, Order.id == Quote.id).order_by(Quote.created_at).first()
        if not oldest:
            return
        now = utcnow()
        start = max(oldest[0] - timedelta(minutes=5), now - timedelta(days=88))
        rows = await self.binance_gateway.recent_deposits(
            int(start.replace(tzinfo=timezone.utc).timestamp() * 1000),
            int(now.replace(tzinfo=timezone.utc).timestamp() * 1000))
        head = await self.polygon_gateway.finalized_block()
        if abs(head["timestamp"] - int(now.replace(tzinfo=timezone.utc).timestamp())) > 300:
            raise PolygonError("polygon_rpc_stale")
        error, blocked_orders = None, set()
        for row in rows:
            if (row.get("coin") != "USDT" or row.get("network") != self.crypto_provider.upper()
                    or type(row.get("transferType")) not in (str, int) or row["transferType"] not in (0, "0")):
                continue
            try:
                recipient = self.binance_gateway.normalize_address(row.get("address"))
                units, tx = usdt_units(row.get("amount")), self.normalize_transaction(row.get("txId"))
            except (PolygonError, DepositError):
                continue
            with self.session_factory() as session:
                quote = session.query(Quote).join(Order, Order.id == Quote.id).filter(
                    Quote.address == recipient, Quote.amount_units == units, Quote.memo == (row.get("addressTag") or ""),
                    Order.provider == self.crypto_provider, Order.mode == "live").first()
                receipt = session.get(Receipt, quote.id) if quote else None
                order_id = quote.id if quote and not (receipt and receipt.credited_at) else None
            if order_id:
                try:
                    await self.check_polygon_transaction(order_id, tx)
                except (PolygonError, DepositError) as exc:
                    error = exc
                    blocked_orders.add(order_id)
        with self.session_factory.begin() as session:
            _capacity_lock(session)
            expired = session.query(Order).join(Quote, Quote.id == Order.id).filter(
                Order.provider == self.crypto_provider, Order.mode == "live", Order.payment_state == "pending",
                Order.expires_at < now - timedelta(minutes=5)).with_for_update().all()
            for order in expired:
                if order.id in blocked_orders or session.get(Receipt, order.id):
                    continue
                order.payment_state = "expired"
                if order.seat_reserved:
                    release_registration(session, "order:" + order.id)
                    order.seat_reserved = False
        if error:
            # Preserve only the affected candidate's seat. One malformed old
            # transaction must not pin all other expired registrations forever.
            raise error


def network_service(service, chain):
    if chain == "polygon":
        return service
    return NetworkPayments(service, chain)
