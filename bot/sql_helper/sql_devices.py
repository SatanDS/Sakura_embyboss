"""Persistent client-device observations and optional per-account limits."""

from datetime import datetime
import hashlib
from typing import Any, Dict, List, Optional

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, UniqueConstraint

from bot import LOGGER
from bot.sql_helper import Base, Session


class EmbyDevice(Base):
    __tablename__ = "emby_devices"
    id = Column(Integer, primary_key=True, autoincrement=True)
    emby_user_id = Column(String(255), nullable=False, index=True)
    tg = Column(BigInteger, nullable=True, index=True)
    device_key_hash = Column(String(64), nullable=False)
    key_type = Column(String(16), nullable=False, default="device_id")
    device_name = Column(String(255), nullable=True)
    client_name = Column(String(255), nullable=True)
    client_version = Column(String(128), nullable=True)
    first_seen = Column(DateTime, nullable=False)
    last_seen = Column(DateTime, nullable=False)
    seen_count = Column(Integer, nullable=False, default=1)
    last_ip_hash = Column(String(64), nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    unbind_month = Column(String(7), nullable=True)
    unbind_count = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("emby_user_id", "device_key_hash", name="uq_emby_device_key"),
    )


class EmbyDevicePolicy(Base):
    __tablename__ = "emby_device_policies"
    tg = Column(BigInteger, primary_key=True, autoincrement=False)
    device_limit = Column(Integer, nullable=True)
    unbind_limit_per_month = Column(Integer, nullable=True)
    updated_at = Column(DateTime, nullable=False)


def sql_get_device_policy(tg: int) -> Dict[str, Any]:
    with Session() as session:
        row = session.query(EmbyDevicePolicy).filter(EmbyDevicePolicy.tg == int(tg)).one_or_none()
        if row is None:
            return {}
        return {
            "device_limit": row.device_limit,
            "unbind_limit_per_month": row.unbind_limit_per_month,
        }


def sql_set_device_policy(tg: int, *, device_limit: Optional[int], unbind_limit_per_month: Optional[int]) -> bool:
    with Session() as session:
        row = session.query(EmbyDevicePolicy).filter(EmbyDevicePolicy.tg == int(tg)).one_or_none()
        if row is None:
            row = EmbyDevicePolicy(tg=int(tg))
            session.add(row)
        row.device_limit = device_limit
        row.unbind_limit_per_month = unbind_limit_per_month
        row.updated_at = datetime.utcnow()
        session.commit()
        return True


def make_device_key_hash(
    device_id: Optional[str],
    *,
    client_name: Optional[str] = None,
    device_name: Optional[str] = None,
    client_version: Optional[str] = None,
) -> tuple[str, str]:
    """Return a non-reversible key and its confidence class.

    DeviceId is preferred.  The metadata fallback is deliberately marked low
    confidence because browser profiles and app updates can change it.
    """
    values = [str(value or "").strip() for value in (device_id, client_name, device_name, client_version)]
    if values[0]:
        material, key_type = "id:" + values[0], "device_id"
    else:
        fallback = "|".join(value.casefold() for value in values[1:])
        if not fallback.strip("|"):
            return "", "unknown"
        material, key_type = "meta:" + fallback, "metadata"
    digest = hashlib.sha256(("sakura-emby-device-v1:" + material).encode("utf-8")).hexdigest()
    return digest, key_type


def _row_dict(row: EmbyDevice) -> Dict[str, Any]:
    return {
        "id": row.id,
        "emby_user_id": row.emby_user_id,
        "tg": row.tg,
        "device_key_hash": row.device_key_hash,
        "key_type": row.key_type,
        "device_name": row.device_name or "",
        "client_name": row.client_name or "",
        "client_version": row.client_version or "",
        "first_seen": row.first_seen,
        "last_seen": row.last_seen,
        "seen_count": row.seen_count,
        "revoked_at": row.revoked_at,
        "revoked": row.revoked_at is not None,
        "unbind_month": row.unbind_month or "",
        "unbind_count": int(row.unbind_count or 0),
    }


def sql_observe_device(
    *,
    emby_user_id: str,
    tg: Optional[int],
    device_id: Optional[str],
    device_name: Optional[str],
    client_name: Optional[str],
    client_version: Optional[str],
    limit: int,
    enforce: bool,
    exempt: bool = False,
) -> Dict[str, Any]:
    """Record one authenticated device and atomically evaluate its quota."""
    key_hash, key_type = make_device_key_hash(
        device_id,
        client_name=client_name,
        device_name=device_name,
        client_version=client_version,
    )
    if not key_hash:
        return {
            "allowed": True,
            "recorded": False,
            "high_confidence": False,
            "device_count": 0,
            "reason": "missing_device_identity",
        }

    now = datetime.utcnow()
    emby_user_id = str(emby_user_id).strip()
    if not emby_user_id:
        return {"allowed": False, "recorded": False, "reason": "missing_user"}

    with Session() as session:
        try:
            # Lock the existing account row before counting devices.  The
            # device query can return an empty set on a first login, and an
            # empty-set lock would otherwise allow two new devices to race
            # past the quota.
            if tg is not None:
                try:
                    from bot.sql_helper.sql_emby import Emby

                    session.query(Emby.tg).filter(
                        Emby.tg == int(tg)
                    ).with_for_update().one_or_none()
                except (ImportError, AttributeError):
                    pass
            rows: List[EmbyDevice] = (
                session.query(EmbyDevice)
                .filter(EmbyDevice.emby_user_id == emby_user_id)
                .with_for_update()
                .all()
            )
            row = next((item for item in rows if item.device_key_hash == key_hash), None)
            if row is not None:
                if row.revoked_at is not None:
                    active_count = sum(item.revoked_at is None for item in rows)
                    if not enforce or exempt or key_type != "device_id" or active_count < limit:
                        row.revoked_at = None
                        row.last_seen = now
                        row.seen_count = int(row.seen_count or 0) + 1
                        row.device_name = (device_name or row.device_name or "")[:255]
                        row.client_name = (client_name or row.client_name or "")[:255]
                        row.client_version = (client_version or row.client_version or "")[:128]
                        session.commit()
                        return {
                            "allowed": True,
                            "recorded": True,
                            "high_confidence": row.key_type == "device_id",
                            "device_count": active_count + 1,
                            "reason": "device_rebound",
                            "device": _row_dict(row),
                        }
                    session.rollback()
                    return {
                        "allowed": False,
                        "recorded": False,
                        "high_confidence": row.key_type == "device_id",
                        "device_count": sum(item.revoked_at is None for item in rows),
                        "reason": "device_revoked",
                        "device": _row_dict(row),
                    }
                if row.last_seen and (now - row.last_seen).total_seconds() < 60:
                    session.rollback()
                    return {
                        "allowed": True,
                        "recorded": False,
                        "high_confidence": row.key_type == "device_id",
                        "device_count": sum(item.revoked_at is None for item in rows),
                        "reason": "known_device_recent",
                        "device": _row_dict(row),
                    }
                row.last_seen = now
                row.seen_count = int(row.seen_count or 0) + 1
                row.tg = tg if tg is not None else row.tg
                row.device_name = (device_name or row.device_name or "")[:255]
                row.client_name = (client_name or row.client_name or "")[:255]
                row.client_version = (client_version or row.client_version or "")[:128]
                session.commit()
                return {
                    "allowed": True,
                    "recorded": True,
                    "high_confidence": row.key_type == "device_id",
                    "device_count": sum(item.revoked_at is None for item in rows),
                    "reason": "known_device",
                    "device": _row_dict(row),
                }

            active_count = sum(item.revoked_at is None for item in rows)
            over_limit = bool(limit > 0 and active_count >= limit)
            if enforce and key_type == "device_id" and over_limit and not exempt:
                session.rollback()
                return {
                    "allowed": False,
                    "recorded": False,
                    "high_confidence": key_type == "device_id",
                    "device_count": active_count,
                    "reason": "device_limit",
                }

            row = EmbyDevice(
                emby_user_id=emby_user_id,
                tg=tg,
                device_key_hash=key_hash,
                key_type=key_type,
                device_name=(device_name or "")[:255],
                client_name=(client_name or "")[:255],
                client_version=(client_version or "")[:128],
                first_seen=now,
                last_seen=now,
                seen_count=1,
            )
            session.add(row)
            session.commit()
            return {
                "allowed": True,
                "recorded": True,
                "high_confidence": key_type == "device_id",
                "device_count": active_count + 1,
                "over_limit": over_limit,
                "reason": "new_device",
                "device": _row_dict(row),
            }
        except Exception:
            session.rollback()
            LOGGER.exception("Failed to record Emby device for user %s", emby_user_id)
            raise


def sql_list_devices(*, emby_user_id: Optional[str] = None, tg: Optional[int] = None) -> List[Dict[str, Any]]:
    with Session() as session:
        query = session.query(EmbyDevice)
        if emby_user_id:
            query = query.filter(EmbyDevice.emby_user_id == str(emby_user_id))
        elif tg is not None:
            query = query.filter(EmbyDevice.tg == tg)
        else:
            return []
        return [_row_dict(row) for row in query.order_by(EmbyDevice.last_seen.desc()).all()]


def sql_revoke_device(device_row_id: int) -> bool:
    with Session() as session:
        row = session.query(EmbyDevice).filter(EmbyDevice.id == int(device_row_id)).one_or_none()
        if row is None:
            return False
        row.revoked_at = datetime.utcnow()
        session.commit()
        return True


def sql_reset_devices(*, emby_user_id: Optional[str] = None, tg: Optional[int] = None) -> int:
    with Session() as session:
        query = session.query(EmbyDevice)
        if emby_user_id:
            query = query.filter(EmbyDevice.emby_user_id == str(emby_user_id))
        elif tg is not None:
            query = query.filter(EmbyDevice.tg == tg)
        else:
            return 0
        count = query.update({EmbyDevice.revoked_at: datetime.utcnow()}, synchronize_session=False)
        session.commit()
        return int(count)


def sql_unbind_device(*, device_row_id: int, tg: int, monthly_limit: int) -> Dict[str, Any]:
    """Revoke one user-owned device, enforcing a calendar-month allowance."""
    now = datetime.utcnow()
    month = now.strftime("%Y-%m")
    with Session() as session:
        row = (
            session.query(EmbyDevice)
            .filter(EmbyDevice.id == int(device_row_id), EmbyDevice.tg == int(tg))
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            return {"ok": False, "reason": "not_found"}
        if row.revoked_at is not None:
            return {"ok": False, "reason": "already_unbound"}
        # Lock this user's device rows before counting so two simultaneous
        # unbind requests cannot both pass the monthly allowance.
        user_rows = session.query(EmbyDevice).filter(
            EmbyDevice.tg == int(tg),
        ).with_for_update().all()
        used = sum(
            item.unbind_month == month and int(item.unbind_count or 0) > 0
            for item in user_rows
        )
        if int(monthly_limit) <= int(used):
            session.rollback()
            return {
                "ok": False,
                "reason": "monthly_limit",
                "used": int(used),
                "limit": int(monthly_limit),
            }
        row.revoked_at = now
        row.unbind_month = month
        row.unbind_count = int(used) + 1
        session.commit()
        return {"ok": True, "used": int(used) + 1, "limit": int(monthly_limit), "device": _row_dict(row)}
