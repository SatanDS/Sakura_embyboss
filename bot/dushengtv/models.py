"""Desktop credentials are stored as hashes; timestamps are naive UTC."""

from sqlalchemy import BigInteger, Column, DateTime, Integer, JSON, String, Text, UniqueConstraint

from bot.sql_helper import Base


class CloudSettings(Base):
    __tablename__ = "tv_cloud_settings"
    tg = Column(BigInteger, primary_key=True, autoincrement=False)
    schema_version = Column(Integer, nullable=False)
    revision = Column(Integer, nullable=False)
    settings = Column(JSON, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class LoginChallenge(Base):
    __tablename__ = "tv_login_challenges"
    id = Column(String(32), primary_key=True)
    state_hash = Column(String(64), nullable=False)
    code_challenge = Column(String(43), nullable=False)
    link_hash = Column(String(64), nullable=False, unique=True)
    installation_id = Column(String(64), nullable=False)
    display_code = Column(String(8), nullable=False)
    status = Column(String(16), nullable=False, default="pending")
    tg = Column(BigInteger, nullable=True)
    display_name = Column(String(128), nullable=True)
    username = Column(String(64), nullable=True)
    expires_at = Column(DateTime, nullable=False, index=True)


class Device(Base):
    __tablename__ = "tv_devices"
    __table_args__ = (UniqueConstraint("tg", "installation_id", name="uq_tv_device_owner_install"),)
    id = Column(String(32), primary_key=True)
    tg = Column(BigInteger, nullable=False, index=True)
    installation_id = Column(String(64), nullable=False)
    fingerprint = Column(String(64), nullable=False)
    public_key = Column(Text, nullable=False)
    hardware = Column(JSON, nullable=False)
    consent_version = Column(String(64), nullable=False)
    app_version = Column(String(32), nullable=False)
    created_at = Column(DateTime, nullable=False)
    last_seen = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)


class DesktopSession(Base):
    __tablename__ = "tv_sessions"
    id = Column(String(32), primary_key=True)
    tg = Column(BigInteger, nullable=False, index=True)
    installation_id = Column(String(64), nullable=False)
    emby_user_id = Column(String(255), nullable=False)
    display_name = Column(String(128), nullable=False)
    username = Column(String(64), nullable=False)
    device_id = Column(String(32), nullable=True, index=True)
    access_hash = Column(String(64), nullable=False, unique=True)
    access_expires_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    revoked_at = Column(DateTime, nullable=True)
    nonce_hash = Column(String(64), nullable=True)
    nonce_expires_at = Column(DateTime, nullable=True)


class RefreshToken(Base):
    __tablename__ = "tv_refresh_tokens"
    token_hash = Column(String(64), primary_key=True)
    session_id = Column(String(32), nullable=False, index=True)
    used_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False, index=True)
