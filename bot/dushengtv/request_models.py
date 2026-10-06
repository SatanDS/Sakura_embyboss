"""Shared MoviePilot jobs and per-Telegram ownership, without provider secrets."""

from datetime import datetime
from sqlalchemy import BigInteger, Column, DateTime, Integer, JSON, String
from bot.sql_helper import Base


class MediaRequest(Base):
    __tablename__ = "tv_media_requests"
    key = Column(String(100), primary_key=True)
    media_key = Column(String(80), nullable=False, index=True)
    season = Column(Integer, nullable=True)
    item = Column(JSON, nullable=False)
    state = Column(String(20), nullable=False, default="pending")
    mp_id = Column(String(40), nullable=True)
    error = Column(String(60), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class MediaRequestOwner(Base):
    __tablename__ = "tv_media_request_owners"
    tg = Column(BigInteger, primary_key=True, autoincrement=False)
    request_key = Column(String(100), primary_key=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
