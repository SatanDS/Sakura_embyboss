"""Commerce persistence. All timestamps are naive UTC unless explicitly documented."""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, Column, DateTime, Integer, JSON, String, Text

from bot.sql_helper import Base


def new_id():
    return uuid4().hex


class Product(Base):
    __tablename__ = "payment_products"
    id = Column(String(32), primary_key=True, default=new_id)
    title = Column(String(120), nullable=False)
    kind = Column(String(16), nullable=False)
    tier = Column(String(16), nullable=False)
    months = Column(Integer, nullable=False)
    price_fen = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=1)
    active = Column(Boolean, nullable=False, default=False)
    sales_limit = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Order(Base):
    __tablename__ = "payment_orders"
    id = Column(String(32), primary_key=True, default=new_id)
    buyer_tg = Column(BigInteger, nullable=False, index=True)
    product_id = Column(String(32), nullable=False, index=True)
    product_snapshot = Column(JSON, nullable=False)
    payment_channels_snapshot = Column(JSON, nullable=True)
    amount_fen = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False, default="cny")
    mode = Column(String(8), nullable=False, default="live", server_default="live")
    terms_version = Column(String(64), nullable=False)
    terms_hash = Column(String(64), nullable=False)
    accepted_at = Column(DateTime, nullable=False)
    payment_state = Column(String(24), nullable=False, default="pending", index=True)
    fulfillment_state = Column(String(24), nullable=False, default="pending")
    stripe_session_id = Column(String(255), nullable=True, unique=True)
    stripe_payment_intent_id = Column(String(255), nullable=True, unique=True)
    checkout_url = Column(Text, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    seat_reserved = Column(Boolean, nullable=False, default=False)
    refunded = Column(Boolean, nullable=False, default=False)
    dispute_status = Column(String(32), nullable=True)
    review_required = Column(Boolean, nullable=False, default=False)
    last_error = Column(String(120), nullable=True)
    paid_at = Column(DateTime, nullable=True)
    archived_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Code(Base):
    __tablename__ = "payment_codes"
    id = Column(String(32), primary_key=True, default=new_id)
    order_id = Column(String(32), nullable=False, unique=True)
    token_hash = Column(String(64), nullable=False, unique=True)
    ciphertext = Column(Text, nullable=False)
    kind = Column(String(16), nullable=False)
    tier = Column(String(16), nullable=False)
    months = Column(Integer, nullable=False)
    mode = Column(String(8), nullable=False, default="live", server_default="live")
    state = Column(String(16), nullable=False, default="issued")
    redeemer_tg = Column(BigInteger, nullable=True)
    claim_key = Column(String(64), nullable=True)
    claim_metadata = Column(JSON, nullable=True)
    claimed_at = Column(DateTime, nullable=True)
    redeemed_at = Column(DateTime, nullable=True)
    held_reason = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Event(Base):
    __tablename__ = "payment_events"
    id = Column(String(255), primary_key=True)
    event_type = Column(String(100), nullable=False)
    payload = Column(JSON, nullable=False)
    state = Column(String(20), nullable=False, default="pending")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    processed_at = Column(DateTime, nullable=True)


class Task(Base):
    __tablename__ = "payment_tasks"
    id = Column(String(32), primary_key=True, default=new_id)
    unique_key = Column(String(255), nullable=False, unique=True)
    task_type = Column(String(32), nullable=False)
    payload = Column(JSON, nullable=False)
    state = Column(String(16), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    lease_token = Column(String(32), nullable=True)
    lease_until = Column(DateTime, nullable=True)
    next_run = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    last_error = Column(String(120), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class Audit(Base):
    __tablename__ = "payment_audits"
    id = Column(String(32), primary_key=True, default=new_id)
    actor_tg = Column(BigInteger, nullable=True)
    action = Column(String(64), nullable=False)
    target_id = Column(String(64), nullable=True)
    details = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class PaymentCapacity(Base):
    __tablename__ = "payment_capacity"
    id = Column(Integer, primary_key=True, autoincrement=False)
    revision = Column(BigInteger, nullable=False, default=0)


class PaymentChannelConfig(Base):
    __tablename__ = "payment_channel_configs"
    mode = Column(String(8), primary_key=True)
    version = Column(Integer, nullable=False, default=1)
    channels = Column(JSON, nullable=False)
    stripe_configuration_id = Column(String(255), nullable=True)


class RegistrationReservation(Base):
    __tablename__ = "payment_registration_reservations"
    key = Column(String(100), primary_key=True)
    state = Column(String(16), nullable=False, default="active", index=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class BrowserChallenge(Base):
    __tablename__ = "payment_browser_challenges"
    id = Column(String(32), primary_key=True)
    token_hash = Column(String(64), nullable=False, unique=True)
    browser_hash = Column(String(64), nullable=False, index=True)
    display_code = Column(String(6), nullable=False)
    approver_tg = Column(BigInteger, nullable=True)
    state = Column(String(16), nullable=False, default="pending")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)
    decided_at = Column(DateTime, nullable=True)


class BrowserSession(Base):
    __tablename__ = "payment_browser_sessions"
    token_hash = Column(String(64), primary_key=True)
    browser_hash = Column(String(64), nullable=False)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)
