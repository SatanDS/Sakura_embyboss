"""Transactional orders, verified Stripe fulfillment, and a durable task outbox."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, inspect, or_, text
from sqlalchemy.exc import IntegrityError

from .crypto import CodeCipher
from .channels import ChannelError, LEGACY_CHANNELS, normalize_channels
from .models import (Audit, Code, Event, Order, PaymentCapacity, PaymentChannelConfig,
                     Product, RegistrationReservation, Task, new_id)
from .entitlements import append_months, ensure_legacy_period, start_success, sync_projection, EntitlementError


TERMS_VERSION = "2026-09-09-v1"
TERMS_TEXT = (
    "请确认套餐及兑换用途。付款成功后将发放可转赠、仅限一次兑换的兑换码。"
    "除法律强制要求或支付平台要求外，不接受因买错套餐、不再使用或已转赠等原因提出的退款申请。"
)
TERMS_HASH = hashlib.sha256(TERMS_TEXT.encode("utf-8")).hexdigest()
FINAL_DISPUTES = {None, "won", "warning_closed"}
CHECKOUT_RECOVERY_REQUIRED = "checkout_recovery_required"


def _emby_model():
    # Keep importing payment settings and migrations independent of the legacy
    # account module; that module initializes the application's DB engine.
    from bot.sql_helper.sql_emby import Emby
    return Emby


class PaymentError(ValueError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _capacity_lock(session):
    # Insert the singleton before reading it.  Under MySQL REPEATABLE READ, a
    # preliminary SELECT can establish a stale snapshot while another creator
    # waits on the row lock, allowing duplicate pending orders after the wait.
    dialect = session.get_bind().dialect.name
    if dialect == "mysql":
        session.execute(text(
            "INSERT INTO payment_capacity (id, revision) VALUES (1, 0) "
            "ON DUPLICATE KEY UPDATE id = VALUES(id)"
        ))
    elif dialect == "postgresql":
        session.execute(text(
            "INSERT INTO payment_capacity (id, revision) VALUES (1, 0) "
            "ON CONFLICT (id) DO NOTHING"
        ))
    else:
        session.execute(text(
            "INSERT OR IGNORE INTO payment_capacity (id, revision) VALUES (1, 0)"
        ))
    # An UPDATE also serializes reservations on SQLite, where FOR UPDATE is ignored.
    session.query(PaymentCapacity).filter_by(id=1).update(
        {PaymentCapacity.revision: PaymentCapacity.revision + 1}, synchronize_session=False,
    )


def actual_account_count(session):
    inspector = inspect(session.get_bind())
    tables = [name for name in ("emby", "emby2") if inspector.has_table(name)]
    if not tables:
        return 0
    union = " UNION ".join(
        "SELECT embyid FROM " + name + " WHERE embyid IS NOT NULL AND embyid <> ''" for name in tables
    )
    return session.execute(text("SELECT COUNT(*) FROM (" + union + ") AS accounts")).scalar_one()


def reserve_registration(session, key, limit, *, expires_at=None, actual_count=None):
    """Call in the same transaction as free-registration or order creation."""
    _capacity_lock(session)
    existing = session.get(RegistrationReservation, key)
    if existing and existing.state == "active":
        return existing
    count = actual_account_count(session) if actual_count is None else actual_count
    held = session.query(func.count()).select_from(RegistrationReservation).filter_by(state="active").scalar()
    if limit and count + held >= limit:
        raise PaymentError("capacity_full", "注册名额已满")
    if existing is None:
        existing = RegistrationReservation(key=key)
        session.add(existing)
    existing.state = "active"
    existing.expires_at = expires_at
    session.flush()
    return existing


def release_registration(session, key, *, consumed=False):
    _capacity_lock(session)
    row = session.get(RegistrationReservation, key)
    if row:
        row.state = "consumed" if consumed else "released"


def enqueue(session, unique_key, task_type, payload, *, now=None):
    existing = session.query(Task).filter_by(unique_key=unique_key).first()
    if existing:
        return existing
    try:
        with session.begin_nested():
            # MySQL commonly stores DATETIME at whole-second precision. A
            # task scheduled at the exact current instant can therefore round
            # into the next second and be missed by the current worker pass.
            # Explicit schedules (for example the 30-minute Checkout expiry
            # delay) are preserved; implicit immediate work gets a one-second
            # grace in the past.
            scheduled = now if now is not None else utcnow() - timedelta(seconds=1)
            task = Task(unique_key=unique_key, task_type=task_type, payload=payload, next_run=scheduled)
            session.add(task)
            session.flush()
        return task
    except IntegrityError:
        return session.query(Task).filter_by(unique_key=unique_key).one()


def seed_products(session):
    for kind in ("register", "renew"):
        for tier in ("normal", "vip"):
            for months in (1, 3, 6, 12):
                product_id = hashlib.sha256(f"sakura:{kind}:{tier}:{months}".encode()).hexdigest()[:32]
                if session.get(Product, product_id) is None:
                    title = ("VIP" if tier == "vip" else "普通") + ("注册码" if kind == "register" else "续期码")
                    session.add(Product(id=product_id, title=f"{title} {months} 个月", kind=kind,
                                        tier=tier, months=months, price_fen=0, active=False, version=1))
    _capacity_lock(session)
    for mode in ("test", "live"):
        if session.get(PaymentChannelConfig, mode) is None:
            session.add(PaymentChannelConfig(mode=mode, version=1, channels=dict(LEGACY_CHANNELS)))


class PaymentService:
    def __init__(self, session_factory, settings, gateway, notification_handler=None):
        self.session_factory = session_factory
        self.settings = settings
        self.gateway = gateway
        self.notification_handler = notification_handler

    @property
    def terms_version(self):
        return getattr(self.settings, "terms_version", TERMS_VERSION)

    @property
    def code_key(self):
        return getattr(self.settings, "code_key", getattr(self.settings, "code_encryption_key", ""))

    @property
    def mode(self):
        return getattr(self.settings, "mode", "live" if getattr(self.settings, "live_mode", False) else "test")

    @property
    def test_buyer_ids(self):
        return frozenset(getattr(self.settings, "test_buyer_ids", ()) or ())

    def _assert_buyer_allowed(self, buyer_tg):
        if self.mode == "test" and buyer_tg not in self.test_buyer_ids:
            raise PaymentError("test_buyer_not_allowed", "测试支付仅允许配置的测试账号")

    @property
    def seat_limit(self):
        return int(getattr(self.settings, "seat_limit", 0) or 0)

    @property
    def cipher(self):
        return CodeCipher(self.code_key)

    @staticmethod
    def _product(product):
        return {key: getattr(product, key) for key in (
            "id", "title", "kind", "tier", "months", "price_fen", "version", "active", "sales_limit",
        )}

    @staticmethod
    def _order(order):
        result = {key: getattr(order, key) for key in (
            "id", "buyer_tg", "product_id", "product_snapshot", "amount_fen", "currency", "terms_version",
            "mode", "archived_at", "payment_channels_snapshot",
            "accepted_at", "payment_state", "fulfillment_state", "checkout_url", "expires_at", "created_at",
            "stripe_session_id", "stripe_payment_intent_id", "refunded", "dispute_status", "review_required",
        )}
        result["expires_timestamp"] = int(order.expires_at.replace(tzinfo=timezone.utc).timestamp())
        result["product"] = result["product_snapshot"]
        result["checkout_recovery_required"] = order.last_error == CHECKOUT_RECOVERY_REQUIRED
        return result

    def seed_products(self):
        with self.session_factory.begin() as session:
            seed_products(session)

    @staticmethod
    def _channel_data(row, mode):
        return {"mode": mode, "version": row.version if row else 1,
                "channels": normalize_channels(row.channels if row else LEGACY_CHANNELS)}

    @staticmethod
    def _locked_channels(session, mode):
        # Call after the capacity lock, before any product lock.
        row = session.query(PaymentChannelConfig).filter_by(mode=mode).with_for_update().first()
        if row is None:
            row = PaymentChannelConfig(mode=mode, version=1, channels=dict(LEGACY_CHANNELS))
            session.add(row)
            session.flush()
        return row

    def get_channels(self):
        with self.session_factory() as session:
            return self._channel_data(session.get(PaymentChannelConfig, self.mode), self.mode)

    @staticmethod
    def _channel_save_replayed(session, row, actor_tg, channels, version, request_id):
        if row.channels != channels or row.version != version + 1:
            return False
        audit = session.query(Audit).filter_by(
            actor_tg=actor_tg, action="payment_channels_saved", target_id="channels:" + row.mode,
        ).filter(Audit.details["request_id"].as_string() == request_id,
                 Audit.details["version"].as_integer() == row.version).first()
        return audit is not None

    async def save_channels(self, actor_tg, data):
        if not isinstance(data, dict) or type(actor_tg) is not int or actor_tg <= 0:
            raise PaymentError("invalid_channels", "支付渠道配置无效")
        try:
            channels = normalize_channels(data.get("channels"))
        except ChannelError as exc:
            raise PaymentError(exc.code) from exc
        version = data.get("version")
        if type(version) is not int or version < 1:
            raise PaymentError("invalid_channels", "支付渠道版本无效")
        try:
            request_id = str(UUID(data["request_id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise PaymentError("invalid_channels", "支付渠道请求编号无效") from None
        mode = self.mode
        if data.get("mode", mode) != mode:
            raise PaymentError("channel_mode_mismatch", "支付环境已改变，请刷新后重试")
        with self.session_factory.begin() as session:
            _capacity_lock(session)
            row = self._locked_channels(session, mode)
            if self._channel_save_replayed(session, row, actor_tg, channels, version, request_id):
                return self._channel_data(row, mode)
            if row.version != version:
                raise PaymentError("channels_changed", "支付渠道已被修改，请刷新后重试")
            if row.channels == channels and (row.stripe_configuration_id or not any(channels.values())):
                return self._channel_data(row, mode)
        # Publish an immutable Stripe configuration outside the database transaction.
        # A competing administrator may win the revision check; their settings stay intact.
        site = str(getattr(self.settings, "public_url", "")).rstrip("/")
        material = json.dumps([site, mode, version, channels, request_id], sort_keys=True, separators=(",", ":"))
        idempotency_key = "payment-channels:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
        configuration_id = None
        if any(channels.values()):
            try:
                configuration_id = await self.gateway.publish_channel_configuration(channels, idempotency_key)
            except ChannelError as exc:
                raise PaymentError(exc.code) from exc
            if (not isinstance(configuration_id, str) or not configuration_id.startswith("pmc_")
                    or not 4 < len(configuration_id) <= 255):
                raise PaymentError("payment_channel_config_invalid", "支付平台未返回有效渠道配置")
        with self.session_factory.begin() as session:
            _capacity_lock(session)
            row = self._locked_channels(session, mode)
            if self.mode != mode:
                raise PaymentError("channel_mode_mismatch", "支付环境已改变，请刷新后重试")
            if self._channel_save_replayed(session, row, actor_tg, channels, version, request_id):
                return self._channel_data(row, mode)
            if row.version != version:
                raise PaymentError("channels_changed", "支付渠道已被修改，请刷新后重试")
            row.channels = channels
            row.stripe_configuration_id = configuration_id
            row.version += 1
            session.add(Audit(actor_tg=actor_tg, action="payment_channels_saved", target_id="channels:" + mode,
                              details={"version": row.version, "mode": mode, "channels": channels,
                                       "request_id": request_id}))
            return self._channel_data(row, mode)

    def list_products(self, include_inactive=False):
        with self.session_factory() as session:
            query = session.query(Product)
            if not include_inactive:
                query = query.filter_by(active=True)
            return [self._product(p) for p in query.order_by(Product.kind, Product.tier, Product.months).all()]

    def save_product(self, actor_tg, data):
        kind, tier, months = data.get("kind"), data.get("tier"), data.get("months")
        price, sales_limit = data.get("price_fen"), data.get("sales_limit")
        title = str(data.get("title", "")).strip()
        if kind not in {"register", "renew"} or tier not in {"normal", "vip"} or type(months) is not int or months not in {1, 3, 6, 12}:
            raise PaymentError("invalid_product", "套餐类型或月份无效")
        if not title or len(title) > 120 or type(price) is not int or not 0 <= price <= 99999999:
            raise PaymentError("invalid_product", "套餐名称或价格无效")
        active = data.get("active", False)
        if type(active) is not bool or (active and price <= 0):
            raise PaymentError("invalid_product", "上架套餐必须设置有效价格")
        if sales_limit is not None and (type(sales_limit) is not int or sales_limit < 1):
            raise PaymentError("invalid_product", "销售上限必须为正整数")
        with self.session_factory.begin() as session:
            product = session.query(Product).filter_by(id=data.get("id")).with_for_update().first() if data.get("id") else None
            if data.get("id") and product is None:
                raise PaymentError("not_found")
            if product is None:
                product = Product(id=new_id(), version=1)
                session.add(product)
            else:
                if type(data.get("version")) is not int or data["version"] != product.version:
                    raise PaymentError("product_changed", "套餐已被修改，请刷新后重试")
                product.version += 1
            for key, value in dict(title=title, kind=kind, tier=tier, months=months, price_fen=price,
                                   active=active, sales_limit=sales_limit, updated_at=utcnow()).items():
                setattr(product, key, value)
            session.add(Audit(actor_tg=actor_tg, action="product_saved", target_id=product.id,
                              details={"version": product.version, "price_fen": price, "active": active}))
            session.flush()
            return self._product(product)

    def create_order(self, buyer_tg, product_id, product_version, terms_version, accepted, now=None):
        if not self.settings.enabled:
            raise PaymentError("sales_disabled", "当前暂停销售")
        if type(buyer_tg) is not int or buyer_tg <= 0:
            raise PaymentError("login_required")
        self._assert_buyer_allowed(buyer_tg)
        if accepted is not True or terms_version != self.terms_version:
            raise PaymentError("terms_required", "请阅读并确认当前购买须知")
        now = now or utcnow()
        with self.session_factory.begin() as session:
            # Serialize creation before product locking to maintain a common capacity lock order.
            _capacity_lock(session)
            channel_row = self._locked_channels(session, self.mode)
            channels = normalize_channels(channel_row.channels)
            if not any(channels.values()):
                raise PaymentError("payment_channels_disabled", "当前暂无可用支付方式")
            product = session.query(Product).filter_by(id=product_id).with_for_update().first()
            if not product or not product.active or product.price_fen <= 0:
                raise PaymentError("product_unavailable", "套餐尚未上架或已经停售")
            if type(product_version) is not int or product.version != product_version:
                raise PaymentError("product_changed", "套餐信息已改变，请重新确认")
            pending = session.query(Order).filter_by(buyer_tg=buyer_tg, product_id=product_id,
                                                    payment_state="pending", mode=self.mode).filter(
                Order.expires_at > now).order_by(Order.created_at.desc()).with_for_update().all()
            for previous in pending:
                if previous.product_snapshot["version"] == product.version and previous.terms_version == terms_version:
                    return self._order(previous)
            sold = session.query(func.count()).select_from(Order).filter(
                Order.product_id == product_id, Order.mode == self.mode,
                Order.payment_state.in_(("pending", "paid"))).scalar()
            if product.sales_limit is not None and sold >= product.sales_limit:
                raise PaymentError("sold_out", "套餐已售完")
            order = Order(id=new_id(), buyer_tg=buyer_tg, product_id=product.id,
                          product_snapshot=self._product(product), amount_fen=product.price_fen, currency="cny",
                          payment_channels_snapshot={"version": channel_row.version, "channels": channels,
                                                     "configuration_id": channel_row.stripe_configuration_id},
                          mode=self.mode,
                          terms_version=terms_version, terms_hash=TERMS_HASH, accepted_at=now,
                          payment_state="pending", fulfillment_state="pending", created_at=now, updated_at=now,
                          # Stripe requires at least 30 minutes at request arrival, including network transit.
                          expires_at=now + timedelta(minutes=max(30, self.settings.checkout_minutes), seconds=60))
            session.add(order)
            if product.kind == "register":
                reserve_registration(session, "order:" + order.id, self.seat_limit)
                order.seat_reserved = True
            enqueue(session, "checkout:" + order.id, "create_checkout", {"order_id": order.id}, now=now + timedelta(seconds=30))
            session.flush()
            return self._order(order)

    async def create_checkout(self, buyer_tg, product_id, product_version, terms_version, accepted, now=None):
        order = self.create_order(buyer_tg, product_id, product_version, terms_version, accepted, now)
        if not order["checkout_url"]:
            await self._create_checkout(order["id"])
        result = self.get_order(order["id"], buyer_tg)
        if result["checkout_recovery_required"] and not result["checkout_url"]:
            raise PaymentError(CHECKOUT_RECOVERY_REQUIRED)
        return result

    def _queue_checkout_recovery(self, order_id, *, retry=False):
        with self.session_factory.begin() as session:
            row = session.query(Order).filter_by(id=order_id).with_for_update().one()
            if row.mode != self.mode:
                raise PaymentError("order_mode_mismatch")
            if row.stripe_session_id or row.payment_state != "pending":
                return False
            row.last_error = CHECKOUT_RECOVERY_REQUIRED
            row.updated_at = utcnow()
            task = enqueue(session, "recover-checkout:" + order_id, "recover_checkout", {"order_id": order_id})
            if retry and task.state == "done":
                task.state, task.attempts, task.last_error = "pending", 0, None
                task.next_run = utcnow() - timedelta(seconds=1)
            return True

    def _review_unresolved_checkout(self, order_id):
        with self.session_factory.begin() as session:
            row = session.query(Order).filter_by(id=order_id).with_for_update().one()
            if row.stripe_session_id or row.payment_state != "pending":
                return
            row.review_required = True
            row.archived_at = None
            row.last_error = CHECKOUT_RECOVERY_REQUIRED
            row.updated_at = utcnow()
            enqueue(session, "review:" + order_id + ":checkout-unresolved", "notify_review", {"order_id": order_id})

    async def _recover_checkout(self, order_id):
        order = self.get_order(order_id)
        if order["mode"] != self.mode:
            raise PaymentError("order_mode_mismatch")
        if order["stripe_session_id"]:
            return await self.reconcile_order(order_id)
        if order["payment_state"] != "pending":
            return
        from .stripe_gateway import CheckoutRecoveryError
        try:
            session_id = await self.gateway.find_checkout(order)
            if session_id:
                return await self.reconcile_order(order_id, session_hint=session_id)
        except CheckoutRecoveryError:
            pass
        except PaymentError as exc:
            if exc.code not in {"stripe_order_mismatch", "stripe_session_invalid", "missing_payment_intent"}:
                raise
        # A missing or conflicting match is not evidence that payment never
        # happened. Keep the order and its seat; late webhooks can still bind it.
        self._review_unresolved_checkout(order_id)

    async def _create_checkout(self, order_id):
        order = self.get_order(order_id)
        if order["mode"] != self.mode:
            raise PaymentError("order_mode_mismatch", "订单不属于当前支付环境")
        if order["stripe_session_id"] or order["payment_state"] != "pending":
            return
        if order["checkout_recovery_required"]:
            return
        if order["expires_at"] <= utcnow() + timedelta(minutes=30, seconds=5):
            self._queue_checkout_recovery(order_id)
            return
        from stripe import InvalidRequestError
        try:
            response = await self.gateway.create_checkout(order)
        except InvalidRequestError as exc:
            if exc.param != "expires_at":
                raise
            self._queue_checkout_recovery(order_id)
            return
        with self.session_factory.begin() as session:
            row = session.query(Order).filter_by(id=order_id).with_for_update().one()
            self._validate_session(row, response)
            if row.stripe_session_id and row.stripe_session_id != response["id"]:
                raise PaymentError("session_mismatch")
            row.stripe_session_id = response["id"]
            row.checkout_url = response.get("url")
            if row.last_error == CHECKOUT_RECOVERY_REQUIRED:
                row.last_error = None
            row.updated_at = utcnow()

    def list_orders(self, buyer_tg=None, limit=100, *, mode=None, archived=False):
        mode = self.mode if mode is None else mode
        if mode not in {"live", "test", "all"} or (archived is not None and type(archived) is not bool):
            raise PaymentError("invalid_order_filter", "订单筛选条件无效")
        with self.session_factory() as session:
            query = session.query(Order)
            if buyer_tg is not None:
                query = query.filter_by(buyer_tg=buyer_tg)
            if mode != "all":
                query = query.filter_by(mode=mode)
            if archived is not None:
                query = query.filter(Order.archived_at.isnot(None) if archived else Order.archived_at.is_(None))
            return [self._order(order) for order in query.order_by(Order.created_at.desc()).limit(min(500, max(1, limit))).all()]

    def set_orders_archived(self, order_ids, actor_tg, archived):
        if (not isinstance(order_ids, (list, tuple)) or not 1 <= len(order_ids) <= 100
                or any(not isinstance(value, str) or not 1 <= len(value) <= 32 for value in order_ids)
                or type(actor_tg) is not int or actor_tg <= 0 or type(archived) is not bool):
            raise PaymentError("invalid_archive_request", "请选择 1 至 100 个订单")
        order_ids = sorted(set(order_ids))
        with self.session_factory.begin() as session:
            rows = session.query(Order).filter(Order.id.in_(order_ids)).order_by(Order.id).with_for_update().all()
            if len(rows) != len(order_ids):
                raise PaymentError("not_found", "部分订单不存在，未修改任何订单")
            if archived:
                coded_ids = {value for (value,) in session.query(Code.order_id).filter(Code.order_id.in_(order_ids)).all()}
                for row in rows:
                    if row.mode == "test":
                        continue
                    if (row.mode != "live" or row.payment_state not in {"pending", "expired"}
                            or row.id in coded_ids or row.refunded or row.dispute_status or row.review_required):
                        raise PaymentError("archive_not_allowed", "只能归档测试订单或尚未收款发码的正式订单")
            now, changed = utcnow(), 0
            for row in rows:
                if (row.archived_at is not None) == archived:
                    continue
                row.archived_at = now if archived else None
                session.add(Audit(actor_tg=actor_tg, action="order_archived" if archived else "order_restored",
                                  target_id=row.id, details={"mode": row.mode, "payment_state": row.payment_state}))
                changed += 1
            return {"ok": True, "changed": changed}

    def get_order(self, order_id, buyer_tg=None):
        with self.session_factory() as session:
            query = session.query(Order).filter_by(id=order_id)
            if buyer_tg is not None:
                query = query.filter_by(buyer_tg=buyer_tg)
            order = query.first()
            if not order:
                raise PaymentError("not_found", "订单不存在")
            result = self._order(order)
            code = session.query(Code).filter_by(order_id=order.id).first()
            result["code_state"] = code.state if code else None
            result["redeemed_at"] = code.redeemed_at if code else None
            return result

    def reveal_code(self, order_id, buyer_tg=None):
        order = self.get_order(order_id, buyer_tg)
        if order["mode"] != self.mode:
            raise PaymentError("order_mode_mismatch", "订单不属于当前支付环境")
        with self.session_factory() as session:
            code = session.query(Code).filter_by(order_id=order_id).first()
            if code is None:
                return None
            if code.state == "held":
                raise PaymentError("code_held", "该兑换码暂时不可用，请联系管理员")
            return self.cipher.reveal(code.ciphertext, order_id)

    def claim_code(self, plaintext, user_tg, *, expected_kind=None):
        """Atomically reserve a paid code for one Telegram user.

        Registration is the only flow that should claim a code before an
        Emby account exists. Rejecting other kinds before changing the state
        prevents a recipient without an account from accidentally locking a
        renewal code that was meant to be transferred to another user.
        """
        from .crypto import code_hash
        with self.session_factory.begin() as session:
            row = session.query(Code).filter_by(token_hash=code_hash(plaintext)).with_for_update().first()
            if row is None:
                raise PaymentError("invalid_code", "兑换码无效")
            if row.state == "held":
                raise PaymentError("code_held", "此兑换码暂时被冻结，请联系管理员")
            if row.state == "redeemed":
                raise PaymentError("code_used", "兑换码已被使用")
            if row.mode != self.mode:
                raise PaymentError("code_mode_mismatch", "兑换码不属于当前支付环境")
            if expected_kind is not None and row.kind != expected_kind:
                raise PaymentError("wrong_code_kind", "该兑换码不能用于当前操作")
            if row.state == "claimed" and row.redeemer_tg != user_tg:
                raise PaymentError("code_claimed", "兑换码正在被其他用户处理")
            row.state = "claimed"
            row.redeemer_tg = user_tg
            row.claimed_at = row.claimed_at or utcnow()
            return {"id": row.id, "kind": row.kind, "tier": row.tier, "months": row.months,
                    "order_id": row.order_id, "redeemer_tg": row.redeemer_tg}

    def pending_code(self, user_tg):
        with self.session_factory() as session:
            row = session.query(Code).filter_by(redeemer_tg=user_tg, state="claimed", mode=self.mode).order_by(Code.claimed_at.desc()).first()
            if not row:
                return None
            return {"id": row.id, "kind": row.kind, "tier": row.tier, "months": row.months,
                    "order_id": row.order_id}

    def redeem_renewal(self, plaintext, user_tg):
        from .crypto import code_hash
        with self.session_factory.begin() as session:
            row = session.query(Code).filter_by(token_hash=code_hash(plaintext)).with_for_update().one_or_none()
            if row is None or row.state == "held":
                raise PaymentError("invalid_code", "兑换码无效或已冻结")
            if row.state == "redeemed":
                raise PaymentError("code_used", "兑换码已被使用")
            if row.mode != self.mode:
                raise PaymentError("code_mode_mismatch", "兑换码不属于当前支付环境")
            if row.kind != "renew":
                raise PaymentError("wrong_code_kind", "此兑换码用于注册新账号")
            Emby = _emby_model()
            user = session.query(Emby).filter(Emby.tg == user_tg).with_for_update().one_or_none()
            if user is None or not user.embyid:
                raise PaymentError("account_required", "请先注册 Emby 账号")
            try:
                period = append_months(session, user, row.tier, row.months,
                                       "payment-code:" + row.id)
                result = sync_projection(session, user)
            except EntitlementError as exc:
                raise PaymentError("entitlement_rejected", str(exc)) from exc
            row.state, row.redeemer_tg, row.redeemed_at = "redeemed", user_tg, utcnow()
            if user.lv == "c" and result.allowed:
                user.lv = "a" if result.current_tier == "vip" else "b"
            return {"months": row.months, "tier": row.tier, "embyid": user.embyid,
                    "restored": result.allowed}

    def finalize_registration(self, code_id, user_tg, embyid, name, pwd, pwd2, created_at, expires_at):
        with self.session_factory.begin() as session:
            row = session.query(Code).filter_by(id=code_id).with_for_update().one()
            if row.state == "redeemed":
                return False
            if row.state != "claimed" or row.redeemer_tg != user_tg or row.kind != "register":
                raise PaymentError("code_claim_required")
            if row.mode != self.mode:
                raise PaymentError("code_mode_mismatch", "兑换码不属于当前支付环境")
            Emby = _emby_model()
            user = session.query(Emby).filter(Emby.tg == user_tg).with_for_update().one()
            if user.embyid:
                raise PaymentError("account_exists")
            user.embyid, user.name, user.pwd, user.pwd2 = embyid, name, pwd, pwd2
            # Registration starts a calendar-month entitlement, not the
            # temporary day-based expiry returned by Emby account creation.
            user.cr, user.ex = created_at, None
            try:
                start_success(session, user, row.tier, row.months, "payment-code:" + row.id, created_at)
            except EntitlementError as exc:
                raise PaymentError("entitlement_rejected", str(exc)) from exc
            row.state, row.redeemed_at = "redeemed", utcnow()
            release_registration(session, "order:" + row.order_id, consumed=True)
            session.query(Order).filter_by(id=row.order_id).update({Order.seat_reserved: False})
            return True

    def resend_code(self, order_id, actor_tg):
        with self.session_factory.begin() as session:
            order = session.query(Order).filter_by(id=order_id).with_for_update().first()
            if not order or order.fulfillment_state != "issued":
                raise PaymentError("code_unavailable", "订单尚未发码")
            enqueue(session, "resend:" + new_id(), "notify_code", {"order_id": order_id, "buyer_tg": order.buyer_tg})
            session.add(Audit(actor_tg=actor_tg, action="code_resent", target_id=order_id, details={}))
        return {"ok": True}

    def _validate_session(self, order, response):
        if order.mode != self.mode:
            raise PaymentError("order_mode_mismatch", "订单不属于当前支付环境")
        intent = response.get("payment_intent")
        intent_id = intent.get("id") if isinstance(intent, dict) else intent
        expected_live = self.mode == "live"
        if (response.get("mode") != "payment" or response.get("livemode") is not expected_live
                or response.get("currency") != order.currency or type(response.get("amount_total")) is not int
                or response["amount_total"] != order.amount_fen
                or response.get("client_reference_id") != order.id
                or (response.get("metadata") or {}).get("order_id") != order.id
                or (order.stripe_session_id and response.get("id") != order.stripe_session_id)
                or (order.stripe_payment_intent_id and intent_id != order.stripe_payment_intent_id)):
            raise PaymentError("stripe_order_mismatch", "支付信息与订单不一致")
        if not isinstance(response.get("id"), str) or not response["id"].startswith("cs_"):
            raise PaymentError("stripe_session_invalid")
        return intent_id

    def ingest_webhook(self, raw, signature):
        if len(raw) > 1024 * 1024:
            raise PaymentError("event_too_large")
        event = self.gateway.verify_event(raw, signature)
        if event.get("livemode") is not (self.mode == "live"):
            raise PaymentError("stripe_mode_mismatch")
        event_id, event_type = event.get("id"), event.get("type")
        if not isinstance(event_id, str) or not event_id.startswith("evt_") or not isinstance(event_type, str):
            raise PaymentError("invalid_event")
        obj = (event.get("data") or {}).get("object") or {}
        intent = obj.get("payment_intent")
        payload = {"object_id": obj.get("id"), "order_id": (obj.get("metadata") or {}).get("order_id"),
                   "payment_intent": intent.get("id") if isinstance(intent, dict) else intent}
        accepted_types = {"checkout.session.completed", "checkout.session.async_payment_succeeded",
                          "checkout.session.async_payment_failed", "checkout.session.expired", "charge.refunded",
                          "charge.dispute.created", "charge.dispute.updated", "charge.dispute.closed"}
        try:
            with self.session_factory.begin() as session:
                if session.get(Event, event_id):
                    return event_id
                session.add(Event(id=event_id, event_type=event_type, payload=payload,
                                  state="pending" if event_type in accepted_types else "ignored"))
                if event_type in accepted_types:
                    enqueue(session, "event:" + event_id, "stripe_event", {"event_id": event_id})
        except IntegrityError:
            with self.session_factory() as session:
                if session.get(Event, event_id) is None:
                    raise
        return event_id

    async def _process_event(self, event_id):
        with self.session_factory() as session:
            event = session.get(Event, event_id)
            payload, event_type = dict(event.payload), event.event_type
            order = session.get(Order, payload.get("order_id")) if payload.get("order_id") else None
            if order is None and event_type.startswith("checkout.session."):
                order = session.query(Order).filter_by(stripe_session_id=payload.get("object_id")).first()
            if order is None and payload.get("payment_intent"):
                order = session.query(Order).filter_by(stripe_payment_intent_id=payload["payment_intent"]).first()
            order_id = order.id if order else None
        if order_id:
            await self.reconcile_order(order_id, session_hint=payload.get("object_id") if event_type.startswith("checkout.session.") else None)
        elif event_type.startswith("charge."):
            # A charge event may precede checkout completion; retry instead of dropping it.
            raise PaymentError("order_not_linked")
        with self.session_factory.begin() as session:
            event = session.get(Event, event_id)
            event.state = "processed" if order_id else "ignored"
            event.processed_at = utcnow()

    async def reconcile_order(self, order_id, session_hint=None):
        order = self.get_order(order_id)
        if order["mode"] != self.mode:
            raise PaymentError("order_mode_mismatch", "订单不属于当前支付环境")
        session_id = order["stripe_session_id"] or session_hint
        if not session_id:
            await self._create_checkout(order_id)
            refreshed = self.get_order(order_id)
            session_id = refreshed["stripe_session_id"]
            if not session_id and (refreshed["checkout_recovery_required"] or refreshed["payment_state"] == "expired"):
                return
        if not session_id:
            raise PaymentError("checkout_unresolved")
        provider = await self.gateway.retrieve_checkout(session_id)
        with self.session_factory.begin() as session:
            # Always lock capacity before an order, including release and late fulfillment.
            _capacity_lock(session)
            row = session.query(Order).filter_by(id=order_id).with_for_update().one()
            intent_id = self._validate_session(row, provider)
            previous_payment_state = row.payment_state
            previous_review = (row.review_required, row.refunded, row.dispute_status)
            row.stripe_session_id = provider["id"]
            row.checkout_url = provider.get("url") or row.checkout_url
            if intent_id:
                row.stripe_payment_intent_id = intent_id
            row.refunded = bool(provider.get("charge_refunded", False))
            row.dispute_status = provider.get("dispute_status")
            code = session.query(Code).filter_by(order_id=order_id).with_for_update().first()
            held = row.refunded or row.dispute_status not in FINAL_DISPUTES
            if held:
                row.review_required = True
                if code and code.state == "issued":
                    code.state, code.held_reason = "held", "external_payment_issue"
                enqueue(session, "review:" + order_id + ":" + str(row.refunded) + ":" + str(row.dispute_status),
                        "notify_review", {"order_id": order_id})
            elif code and code.state == "held" and code.held_reason == "external_payment_issue":
                code.state, code.held_reason = "issued", None
                row.review_required = False
            if provider.get("payment_status") == "paid":
                if not intent_id:
                    raise PaymentError("missing_payment_intent")
                row.payment_state = "paid"
                row.paid_at = row.paid_at or utcnow()
                if row.product_snapshot["kind"] == "register" and not row.seat_reserved and (not code or code.state != "redeemed"):
                    # A verified late success is still owed a seat, even after its old reservation was released.
                    reserve_registration(session, "order:" + order_id, self.seat_limit)
                    row.seat_reserved = True
                if not code:
                    enqueue(session, "fulfill:" + order_id, "fulfill_order", {"order_id": order_id})
            elif provider.get("status") == "expired" and row.payment_state != "paid":
                row.payment_state = "expired"
                if row.seat_reserved:
                    release_registration(session, "order:" + order_id)
                    row.seat_reserved = False
            if ((row.payment_state == "paid" and previous_payment_state != "paid")
                    or (row.review_required and previous_review !=
                        (row.review_required, row.refunded, row.dispute_status))):
                row.archived_at = None
            row.last_error = None
            row.updated_at = utcnow()

    def fulfill_order(self, order_id):
        with self.session_factory.begin() as session:
            order = session.query(Order).filter_by(id=order_id).with_for_update().one()
            if order.mode != self.mode:
                raise PaymentError("order_mode_mismatch", "订单不属于当前支付环境")
            if order.payment_state != "paid":
                raise PaymentError("payment_unconfirmed")
            code = session.query(Code).filter_by(order_id=order_id).first()
            if not code:
                _, digest, ciphertext = self.cipher.issue(order_id, prefix="DuSheng-Pay_")
                held = order.refunded or order.dispute_status not in FINAL_DISPUTES
                product = order.product_snapshot
                code = Code(order_id=order_id, token_hash=digest, ciphertext=ciphertext,
                            kind=product["kind"], tier=product["tier"], months=product["months"], mode=order.mode,
                            state="held" if held else "issued", held_reason="external_payment_issue" if held else None)
                session.add(code)
                session.flush()
            order.fulfillment_state = "issued"
            if code.state == "issued":
                enqueue(session, "notify:" + order_id, "notify_code", {"order_id": order_id, "buyer_tg": order.buyer_tg})
            else:
                enqueue(session, "review:" + order_id + ":held", "notify_review", {"order_id": order_id})

    def schedule_reconcile(self, order_id, actor_tg=None, *, audit=True):
        order = self.get_order(order_id)
        if order["checkout_recovery_required"] and not order["stripe_session_id"]:
            if audit:
                self._queue_checkout_recovery(order_id, retry=True)
                with self.session_factory.begin() as session:
                    session.add(Audit(actor_tg=actor_tg, action="checkout_recovery_requested", target_id=order_id, details={}))
            return {"ok": True}
        with self.session_factory.begin() as session:
            # Order pages poll every few seconds while an asynchronous payment
            # settles. Use a short deterministic bucket so polling cannot
            # create an unbounded task and audit stream.
            slot = int(utcnow().replace(tzinfo=timezone.utc).timestamp()) // 60
            enqueue(session, f"manual-reconcile:{order_id}:{slot}", "reconcile_order", {"order_id": order_id})
            if audit:
                session.add(Audit(actor_tg=actor_tg, action="reconcile_requested", target_id=order_id, details={}))
        return {"ok": True}

    async def reconcile_orders(self):
        with self.session_factory.begin() as session:
            orders = session.query(Order).filter(or_(Order.payment_state == "pending", Order.review_required.is_(True),
                                                       Order.fulfillment_state != "issued"),
                                                  Order.mode == self.mode).all()
            slot = int(utcnow().replace(tzinfo=timezone.utc).timestamp()) // 300
            for order in orders:
                if order.payment_state == "expired" or (
                        not order.stripe_session_id and order.last_error == CHECKOUT_RECOVERY_REQUIRED):
                    continue
                enqueue(session, f"reconcile:{order.id}:{slot}", "reconcile_order", {"order_id": order.id})
            return len(orders)

    def _claim_task(self, now):
        with self.session_factory.begin() as session:
            eligible = or_(Task.state == "pending", (Task.state == "processing") & (Task.lease_until < now))
            candidates = session.query(Task.id).filter(eligible, Task.next_run <= now).order_by(Task.next_run).limit(20).all()
            for (task_id,) in candidates:
                token = new_id()
                count = session.query(Task).filter(Task.id == task_id, eligible, Task.next_run <= now).update({
                    Task.state: "processing", Task.lease_token: token, Task.lease_until: now + timedelta(minutes=3),
                    Task.attempts: Task.attempts + 1,
                }, synchronize_session=False)
                if count:
                    task = session.get(Task, task_id)
                    return task.id, token, task.task_type, dict(task.payload), task.attempts
        return None

    async def process_tasks(self, limit=50):
        processed = 0
        for _ in range(min(100, max(1, limit))):
            claimed = self._claim_task(utcnow())
            if not claimed:
                break
            task_id, token, task_type, payload, attempts = claimed
            error = None
            try:
                if task_type == "create_checkout":
                    await self._create_checkout(payload["order_id"])
                elif task_type == "recover_checkout":
                    await self._recover_checkout(payload["order_id"])
                elif task_type == "stripe_event":
                    await self._process_event(payload["event_id"])
                elif task_type == "reconcile_order":
                    await self.reconcile_order(payload["order_id"])
                elif task_type == "fulfill_order":
                    self.fulfill_order(payload["order_id"])
                elif task_type in {"notify_code", "notify_review"}:
                    if not self.notification_handler:
                        raise PaymentError("notification_handler_unavailable")
                    await self.notification_handler(task_type, payload)
                else:
                    raise PaymentError("unknown_task_type")
            except Exception as exc:
                error = exc.code if isinstance(exc, PaymentError) else type(exc).__name__
                from bot import LOGGER
                from .diagnostics import log_payment_error
                # Periodic reconciliation recreates ordinary tasks after a
                # mode switch. Recovery has a single durable task, so retain
                # it silently until its original environment is restored.
                stale_mode_task = (
                    isinstance(exc, PaymentError)
                    and exc.code == "order_mode_mismatch"
                    and task_type in {"reconcile_order", "create_checkout", "recover_checkout"}
                )
                if stale_mode_task:
                    if task_type != "recover_checkout":
                        error = None
                else:
                    log_payment_error(LOGGER, "task_" + task_type, exc)
            with self.session_factory.begin() as session:
                task = session.query(Task).filter_by(id=task_id, lease_token=token).with_for_update().first()
                if task:
                    task.state = "pending" if error else "done"
                    task.last_error = error
                    task.lease_token, task.lease_until = None, None
                    if error:
                        delay = 300 if error == "order_mode_mismatch" else min(3600, 2 ** min(attempts, 11))
                        task.next_run = utcnow() + timedelta(seconds=delay)
            processed += 1
        return processed
