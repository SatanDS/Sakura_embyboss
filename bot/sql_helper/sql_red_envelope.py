"""Durable red-envelope escrow and exactly-once claim ledger."""

import random
from datetime import datetime

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, String, Text, func

from bot import LOGGER
from bot.sql_helper import Base, Session
from bot.sql_helper.sql_emby import Emby


MAX_POINTS = 2**31 - 1


class RedEnvelopeRecord(Base):
    __tablename__ = "red_envelopes"

    id = Column(String(64), primary_key=True)
    sender_id = Column(BigInteger, nullable=False, index=True)
    sender_name = Column(String(255), nullable=False)
    total_amount = Column(Integer, nullable=False)
    remaining_amount = Column(Integer, nullable=False)
    total_members = Column(Integer, nullable=False)
    remaining_members = Column(Integer, nullable=False)
    envelope_type = Column(String(16), nullable=False)
    target_user = Column(BigInteger, nullable=True)
    message = Column(Text, nullable=False)
    state = Column(String(16), nullable=False, index=True)
    refunded_amount = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class RedEnvelopeClaim(Base):
    __tablename__ = "red_envelope_claims"

    envelope_id = Column(
        String(64), ForeignKey("red_envelopes.id"), primary_key=True
    )
    tg = Column(BigInteger, primary_key=True)
    amount = Column(Integer, nullable=False)
    name = Column(String(255), nullable=False)
    claimed_at = Column(DateTime, nullable=False, default=datetime.now)


def _snapshot(row):
    return {
        "id": row.id,
        "sender_id": row.sender_id,
        "sender_name": row.sender_name,
        "total_amount": row.total_amount,
        "remaining_amount": row.remaining_amount,
        "total_members": row.total_members,
        "remaining_members": row.remaining_members,
        "envelope_type": row.envelope_type,
        "target_user": row.target_user,
        "message": row.message,
        "state": row.state,
        "refunded_amount": row.refunded_amount,
        "version": row.version,
        "created_at": row.created_at,
    }


def sql_create_red_envelope(
    envelope_id,
    sender_id,
    sender_name,
    money,
    members,
    envelope_type,
    target_user,
    message,
):
    if (type(money) is not int or type(members) is not int or money < 5
            or members < 1 or money < members or money > MAX_POINTS):
        return "invalid"
    if envelope_type not in {"random", "equal", "private"}:
        return "invalid"
    if envelope_type == "private" and (type(target_user) is not int or members != 1):
        return "invalid"
    if envelope_type != "private" and target_user is not None:
        return "invalid"

    with Session() as session:
        try:
            debited = (
                session.query(Emby)
                .filter(Emby.tg == sender_id, Emby.iv >= money)
                .update({Emby.iv: Emby.iv - money}, synchronize_session=False)
            )
            if debited != 1:
                session.rollback()
                return "insufficient"
            session.add(RedEnvelopeRecord(
                id=envelope_id,
                sender_id=sender_id,
                sender_name=sender_name or "Anonymous",
                total_amount=money,
                remaining_amount=money,
                total_members=members,
                remaining_members=members,
                envelope_type=envelope_type,
                target_user=target_user,
                message=message or "",
                state="pending",
                refunded_amount=0,
                version=0,
            ))
            session.commit()
            return "ok"
        except Exception as exc:
            LOGGER.error(f"Red envelope creation failed: {type(exc).__name__}")
            session.rollback()
            return "error"


def sql_activate_red_envelope(envelope_id):
    with Session() as session:
        try:
            row = (session.query(RedEnvelopeRecord)
                   .filter_by(id=envelope_id, state="pending")
                   .with_for_update().one_or_none())
            if row is None:
                return False
            row.state = "open"
            row.version += 1
            session.commit()
            return True
        except Exception as exc:
            LOGGER.error(f"Red envelope activation failed: {type(exc).__name__}")
            session.rollback()
            return False


def sql_get_red_envelope(envelope_id):
    with Session() as session:
        try:
            row = session.query(RedEnvelopeRecord).filter_by(id=envelope_id).one_or_none()
            if row is None:
                return None
            return _snapshot(row)
        except Exception as exc:
            LOGGER.error(f"Red envelope lookup failed: {type(exc).__name__}")
            return None


def sql_claim_red_envelope(envelope_id, tg, name):
    with Session() as session:
        try:
            row = (session.query(RedEnvelopeRecord).filter_by(id=envelope_id)
                   .with_for_update().one_or_none())
            if row is None:
                return {"status": "not_found"}
            existing_claim = session.query(RedEnvelopeClaim).filter_by(
                envelope_id=envelope_id, tg=tg
            ).one_or_none()
            if existing_claim is not None:
                result = {
                    "status": "already_claimed",
                    "amount": existing_claim.amount,
                    "completed": row.state == "completed",
                }
                if result["completed"]:
                    result["envelope"] = _snapshot(row)
                    result["claims"] = [
                        {"tg": claim.tg, "amount": claim.amount, "name": claim.name}
                        for claim in session.query(RedEnvelopeClaim)
                        .filter_by(envelope_id=envelope_id)
                        .all()
                    ]
                return result
            if row.state != "open":
                return {"status": "closed"}
            if row.envelope_type == "private" and row.target_user != tg:
                return {"status": "forbidden"}

            account = session.query(Emby).filter_by(tg=tg).with_for_update().one_or_none()
            if account is None:
                return {"status": "not_registered"}

            if row.envelope_type == "private":
                amount = row.remaining_amount
            elif row.envelope_type == "equal":
                amount = (row.remaining_amount if row.remaining_members == 1
                          else row.total_amount // row.total_members)
            elif row.remaining_members == 1:
                amount = row.remaining_amount
            else:
                ceiling = 2 * row.remaining_amount / row.remaining_members
                amount = int(random.uniform(1, ceiling))

            if amount <= 0 or amount > row.remaining_amount:
                return {"status": "invalid_state"}

            credited = (
                session.query(Emby)
                .filter(Emby.tg == tg)
                .filter(func.coalesce(Emby.iv, 0) <= MAX_POINTS - amount)
                .update(
                    {Emby.iv: func.coalesce(Emby.iv, 0) + amount},
                    synchronize_session=False,
                )
            )
            if credited != 1:
                return {"status": "balance_limit"}

            remaining_amount = row.remaining_amount - amount
            remaining_members = row.remaining_members - 1
            next_state = "completed" if remaining_members == 0 else "open"
            changed = (
                session.query(RedEnvelopeRecord)
                .filter(
                    RedEnvelopeRecord.id == envelope_id,
                    RedEnvelopeRecord.state == "open",
                    RedEnvelopeRecord.version == row.version,
                    RedEnvelopeRecord.remaining_amount == row.remaining_amount,
                    RedEnvelopeRecord.remaining_members == row.remaining_members,
                )
                .update(
                    {
                        RedEnvelopeRecord.remaining_amount: remaining_amount,
                        RedEnvelopeRecord.remaining_members: remaining_members,
                        RedEnvelopeRecord.state: next_state,
                        RedEnvelopeRecord.version: RedEnvelopeRecord.version + 1,
                    },
                    synchronize_session=False,
                )
            )
            if changed != 1:
                session.rollback()
                return {"status": "error"}

            session.add(RedEnvelopeClaim(
                envelope_id=envelope_id,
                tg=tg,
                amount=amount,
                name=name or "Anonymous",
            ))
            session.flush()
            session.refresh(row)
            result = {"status": "ok", "amount": amount, "completed": row.state == "completed"}
            if result["completed"]:
                result["envelope"] = _snapshot(row)
                result["claims"] = [
                    {"tg": claim.tg, "amount": claim.amount, "name": claim.name}
                    for claim in session.query(RedEnvelopeClaim)
                    .filter_by(envelope_id=envelope_id)
                    .all()
                ]
            session.commit()
            return result
        except Exception as exc:
            LOGGER.error(f"Red envelope claim failed: {type(exc).__name__}")
            session.rollback()
            return {"status": "error"}


def sql_refund_pending_red_envelope(envelope_id):
    with Session() as session:
        try:
            row = (session.query(RedEnvelopeRecord).filter_by(id=envelope_id, state="pending")
                   .with_for_update().one_or_none())
            if row is None:
                return False
            refund = row.remaining_amount
            credited = (
                session.query(Emby)
                .filter(Emby.tg == row.sender_id)
                .filter(func.coalesce(Emby.iv, 0) <= MAX_POINTS - refund)
                .update(
                    {Emby.iv: func.coalesce(Emby.iv, 0) + refund},
                    synchronize_session=False,
                )
            )
            if credited != 1:
                LOGGER.warning(f"Pending red envelope refund exceeds balance limit: {envelope_id}")
                return False
            row.remaining_amount = 0
            row.remaining_members = 0
            row.refunded_amount += refund
            row.state = "refunded"
            row.version += 1
            session.commit()
            return True
        except Exception as exc:
            LOGGER.error(f"Red envelope refund failed: {type(exc).__name__}")
            session.rollback()
            return False


def sql_recover_pending_red_envelopes():
    with Session() as session:
        try:
            pending = [row.id for row in session.query(RedEnvelopeRecord.id)
                       .filter_by(state="pending").all()]
        except Exception as exc:
            LOGGER.error(f"Pending red envelope recovery scan failed: {type(exc).__name__}")
            return 0, 1

    recovered = 0
    failed = 0
    for envelope_id in pending:
        if sql_refund_pending_red_envelope(envelope_id):
            recovered += 1
        else:
            failed += 1
    return recovered, failed
