"""Persistence helpers for Telegram users' Douban bindings."""

from datetime import datetime

from sqlalchemy import BigInteger, Column, DateTime, String, func

from bot import LOGGER
from bot.sql_helper import Base, Session


class MoviePilotDoubanUser(Base):
    """The latest Douban id submitted by each Telegram user."""

    __tablename__ = "moviepilot_douban_users"

    tg = Column(BigInteger, primary_key=True, autoincrement=False)
    douban_id = Column(String(20), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)


def sql_get_moviepilot_douban(tg):
    with Session() as session:
        try:
            return session.query(MoviePilotDoubanUser).filter(
                MoviePilotDoubanUser.tg == tg
            ).first()
        except Exception as exc:
            LOGGER.error(f"查询豆瓣绑定失败: {exc}")
            return None


def sql_count_moviepilot_douban(douban_id):
    with Session() as session:
        try:
            return session.query(func.count(MoviePilotDoubanUser.tg)).filter(
                MoviePilotDoubanUser.douban_id == str(douban_id)
            ).scalar() or 0
        except Exception as exc:
            LOGGER.error(f"统计豆瓣绑定失败: {exc}")
            return 0


def sql_upsert_moviepilot_douban(tg, douban_id):
    with Session() as session:
        try:
            row = session.query(MoviePilotDoubanUser).filter(
                MoviePilotDoubanUser.tg == tg
            ).first()
            if row is None:
                row = MoviePilotDoubanUser(tg=tg, douban_id=str(douban_id))
                session.add(row)
            else:
                row.douban_id = str(douban_id)
                row.updated_at = datetime.now()
            session.commit()
            return True
        except Exception as exc:
            session.rollback()
            LOGGER.error(f"保存豆瓣绑定失败: {exc}")
            return False


def sql_delete_moviepilot_douban(tg):
    with Session() as session:
        try:
            row = session.query(MoviePilotDoubanUser).filter(
                MoviePilotDoubanUser.tg == tg
            ).first()
            if row is None:
                return True
            session.delete(row)
            session.commit()
            return True
        except Exception as exc:
            session.rollback()
            LOGGER.error(f"删除豆瓣绑定失败: {exc}")
            return False
