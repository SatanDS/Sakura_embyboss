"""Transactional account periods, using naive Asia/Shanghai database times.

Mutations lock the existing Emby row first and never commit the caller's
transaction. Read-time resolution is authoritative; ``lv`` and ``ex`` are
compatibility projections and must not be used to authorize a managed user.
"""

import calendar
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import BigInteger, CheckConstraint, Column, DateTime, Index, Integer, String
from sqlalchemy import inspect as sa_inspect

from bot.sql_helper import Base


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
TIERS = frozenset({"normal", "vip"})
BLOCK_REASONS = frozenset({"expiry", "activity", "admin", "policy", "legacy_unknown"})


class EntitlementError(ValueError):
    """An account change cannot be safely completed."""


class AccountEntitlement(Base):
    __tablename__ = "payment_account_entitlements"

    tg = Column(BigInteger, primary_key=True, autoincrement=False)
    blocked_reason = Column(String(24), nullable=True)
    revision = Column(Integer, nullable=False, default=0)
    active_tier = Column(String(8), nullable=True)


class AccountPeriod(Base):
    __tablename__ = "payment_account_periods"
    __table_args__ = (
        CheckConstraint("ends_at >= starts_at", name="ck_account_period_dates"),
        Index("ix_account_period_tg_dates", "tg", "starts_at", "ends_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    tg = Column(BigInteger, nullable=False, index=True)
    starts_at = Column(DateTime, nullable=False)
    ends_at = Column(DateTime, nullable=False)
    tier = Column(String(8), nullable=False)
    source_key = Column(String(160), nullable=False, unique=True)
    kind = Column(String(16), nullable=False)
    anchor_at = Column(DateTime, nullable=True)
    month_offset = Column(Integer, nullable=True)
    end_month_offset = Column(Integer, nullable=True)
    adjustment_days = Column(Integer, nullable=True)


@dataclass(frozen=True)
class EntitlementResult:
    current_tier: str | None
    current_end: datetime | None
    final_expiry: datetime | None
    blocked_reason: str | None
    revision: int
    upcoming: tuple

    @property
    def blocked(self):
        return self.blocked_reason is not None

    @property
    def tier(self):
        return self.current_tier

    @property
    def end(self):
        return self.current_end

    @property
    def allowed(self):
        return self.current_tier is not None and not self.blocked


def china_now():
    return datetime.now(CHINA_TZ).replace(tzinfo=None)


def local_time(value):
    if not isinstance(value, datetime):
        raise EntitlementError("A valid account datetime is required")
    if value.tzinfo is not None:
        return value.astimezone(CHINA_TZ).replace(tzinfo=None)
    return value


def calendar_month(anchor, offset):
    """Add cumulative months to the original anniversary, avoiding date drift."""
    anchor = local_time(anchor)
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise EntitlementError("Month offset must be an integer")
    month_index = anchor.year * 12 + anchor.month - 1 + offset
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    if not 1 <= year <= 9999:
        raise EntitlementError("Account period exceeds supported dates")
    return anchor.replace(year=year, month=month,
                          day=min(anchor.day, calendar.monthrange(year, month)[1]))


def _tier(tier):
    if tier not in TIERS:
        raise EntitlementError("Tier must be normal or vip")
    return tier


def _positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EntitlementError(f"{name} must be a positive integer")
    return value


def _source(source_key):
    if not isinstance(source_key, str) or not source_key or len(source_key) > 160:
        raise EntitlementError("A unique source key of at most 160 characters is required")
    return source_key


def _lock_user(session, user):
    if user is None or getattr(user, "tg", None) is None:
        raise EntitlementError("An existing Telegram account is required")
    mapper = sa_inspect(type(user), raiseerr=False)
    if mapper is None:
        raise EntitlementError("Account must be a mapped Emby row")
    locked = (session.query(type(user)).filter(type(user).tg == user.tg)
              .with_for_update().one_or_none())
    if locked is None:
        raise EntitlementError("Account no longer exists")
    return locked


def _state(session, user, lock=False):
    query = session.query(AccountEntitlement).filter(AccountEntitlement.tg == user.tg)
    return (query.with_for_update() if lock else query).one_or_none()


def _periods(session, tg):
    return (session.query(AccountPeriod).filter(AccountPeriod.tg == tg)
            .order_by(AccountPeriod.starts_at, AccountPeriod.id).all())


def _replay(session, user, source_key):
    previous = session.query(AccountPeriod).filter(AccountPeriod.source_key == _source(source_key)).one_or_none()
    if previous is not None and previous.tg != user.tg:
        raise EntitlementError("This entitlement source was already used for another account")
    return previous


def _ensure_locked(session, user, now):
    state = _state(session, user, lock=True)
    if state is not None:
        return state
    state = AccountEntitlement(tg=user.tg, revision=0,
                               blocked_reason="legacy_unknown" if user.lv == "c" else None)
    session.add(state)
    expiry = local_time(user.ex) if user.ex is not None else None
    if expiry is not None and user.lv in {"a", "b", "c"}:
        created = local_time(user.cr) if user.cr is not None else min(now, expiry)
        created = min(created, expiry)
        session.add(AccountPeriod(
            tg=user.tg, starts_at=created, ends_at=expiry,
            tier="vip" if user.lv == "a" else "normal", kind="legacy",
            source_key=f"legacy:{user.tg}",
        ))
    session.flush()
    return state


def ensure_legacy_period(session, user, now=None):
    """Adopt an account without changing its existing grade or expiry."""
    user = _lock_user(session, user)
    now = local_time(now) if now is not None else china_now()
    state = _ensure_locked(session, user, now)
    result = resolve_entitlement(session, user, now)
    state.active_tier = result.current_tier
    return state


def resolve_entitlement(session, user, now=None):
    """Return None for unmanaged legacy accounts; otherwise resolve half-open periods."""
    now = local_time(now) if now is not None else china_now()
    state = _state(session, user)
    if state is None:
        return None
    periods = [p for p in _periods(session, user.tg) if p.kind != "adjustment" and p.ends_at > p.starts_at]
    current = [p for p in periods if p.starts_at <= now < p.ends_at]
    if len(current) > 1:
        raise EntitlementError("Overlapping account periods require repair")
    active = current[0] if current else None
    reason = state.blocked_reason
    # A legacy path may still set lv=c; never let stale paid state override a ban.
    if user.lv == "c" and reason is None:
        reason = "legacy_unknown"
    if reason == "expiry" and active is not None:
        reason = None
    if active is None and reason is None:
        reason = "expiry"
    return EntitlementResult(
        current_tier=active.tier if active else None,
        current_end=active.ends_at if active else None,
        final_expiry=max((p.ends_at for p in periods), default=None),
        blocked_reason=reason, revision=state.revision,
        upcoming=tuple(p for p in periods if p.starts_at > now),
    )


def _project(session, user, state, now):
    session.flush()
    result = resolve_entitlement(session, user, now)
    state.active_tier = result.current_tier
    # Keep the cause when a projection disables an expired account; otherwise
    # the next renewal would mistake our own lv=c for an unknown manual ban.
    if result.blocked_reason == "expiry" and state.blocked_reason is None:
        state.blocked_reason = "expiry"
    user.ex = result.final_expiry
    if result.allowed:
        user.lv = "a" if result.current_tier == "vip" else "b"
        user.disabled_at = None
    elif result.blocked_reason != "expiry" or user.embyid:
        user.lv = "c"
    return result


def sync_projection(session, user, now=None):
    """Refresh compatibility fields only; this does not call Emby or commit."""
    user = _lock_user(session, user)
    now = local_time(now) if now is not None else china_now()
    state = _state(session, user, lock=True)
    return _project(session, user, state, now) if state is not None else None


def legacy_update(session, user, changes, now=None, *, block_reason=None,
                  explicit_unblock=False, set_tier=False):
    """Translate legacy writes under the caller's account lock.

    The returned mapping contains ordinary fields only for managed accounts.
    A simultaneous lv/ex renewal preserves queued tiers; a tier-only owner or
    old whitelist action changes the current period, never future purchases.
    """
    now = local_time(now) if now is not None else china_now()
    changes = dict(changes)
    state = _state(session, user, lock=True)
    if state is None and changes.get("lv") == "c" and block_reason is not None:
        state = _ensure_locked(session, user, now)
    if state is None:
        return changes
    next_level = changes.pop("lv", None)
    was_disabled = user.lv == "c"
    has_expiry = "ex" in changes
    target_expiry = changes.pop("ex", user.ex)
    changes.pop("disabled_at", None)
    if next_level == "d":
        # Retain the financial history and freeze its entitlement when the
        # Emby account is removed. Rebinding must use an explicit transfer.
        mark_block(session, user, block_reason or "admin", now)
        changes.update(lv="d", ex=None, disabled_at=None)
        return changes
    if next_level == "c":
        mark_block(session, user, block_reason or "policy", now)
    elif next_level in {"a", "b"} and user.lv == "c":
        if explicit_unblock:
            clear_block(session, user, now, explicit=True)
        elif state.blocked_reason != "expiry":
            raise EntitlementError("Account requires an explicit administrative unblock")
    if has_expiry:
        if target_expiry is None:
            raise EntitlementError("Managed account expiry cannot be erased")
        set_final_expiry(session, user, target_expiry, "legacy-update:" + uuid.uuid4().hex, now)
    if next_level in {"a", "b"} and (set_tier or (not has_expiry and not was_disabled)):
        result = resolve_entitlement(session, user, now)
        if result.current_tier is not None:
            set_current_tier(session, user, "vip" if next_level == "a" else "normal", now)
    _project(session, user, state, now)
    return changes


def set_final_expiry(session, user, target, source_key, now=None):
    """Translate an explicit legacy/admin absolute expiry into a ledger delta."""
    user = _lock_user(session, user)
    now = local_time(now) if now is not None else china_now()
    target = local_time(target)
    state = _ensure_locked(session, user, now)
    current = resolve_entitlement(session, user, now)
    base = max(now, current.final_expiry or now)
    if target == base:
        return _project(session, user, state, now)
    if target > base:
        # Owner time adjustments may preserve a blocked account's time without
        # restoring it. Ordinary redemption uses append_days and rejects bans.
        tail = _tail(session, user, now)
        period = AccountPeriod(tg=user.tg, starts_at=base, ends_at=target,
                               tier=tail.tier if tail else (current.current_tier or "normal"),
                               source_key=_source(source_key), kind="legacy")
        session.add(period)
    else:
        target = max(now, target)
        for period in _periods(session, user.tg):
            if period.kind == "adjustment" or period.ends_at <= target:
                continue
            period.ends_at = max(period.starts_at, target)
            # Retain original month metadata for idempotency/audit. The tail
            # check resets its anchor naturally if the owner shortened it.
        session.add(AccountPeriod(tg=user.tg, starts_at=now, ends_at=now, tier="normal",
                                  source_key=_source(source_key), kind="adjustment"))
    state.revision += 1
    return _project(session, user, state, now)


def _require_redeemable(state, user):
    if state.blocked_reason not in {None, "expiry"}:
        raise EntitlementError("Blocked accounts cannot redeem paid account periods")
    if user.lv == "c" and state.blocked_reason is None:
        raise EntitlementError("Account is disabled for an unknown reason")


def _tail(session, user, now):
    periods = [p for p in _periods(session, user.tg)
               if p.kind != "adjustment" and p.ends_at > max(now, p.starts_at)]
    return max(periods, key=lambda p: (p.ends_at, p.id)) if periods else None


def append_months(session, user, tier, months, source_key, now=None):
    """Append a purchased period; return the identical row on source replay."""
    _tier(tier)
    _positive_int(months, "Months")
    _source(source_key)
    user = _lock_user(session, user)
    previous = _replay(session, user, source_key)
    if previous is not None:
        if previous.kind != "month" or previous.tier != tier or (
                previous.end_month_offset - previous.month_offset != months):
            raise EntitlementError("Entitlement source replay differs from the original request")
        return previous
    now = local_time(now) if now is not None else china_now()
    state = _ensure_locked(session, user, now)
    _require_redeemable(state, user)
    tail = _tail(session, user, now)
    starts = tail.ends_at if tail else now
    if tail is not None and tail.kind == "month" and tail.anchor_at is not None and (
            calendar_month(tail.anchor_at, tail.end_month_offset) == starts):
        anchor, offset = tail.anchor_at, tail.end_month_offset
    else:
        anchor, offset = starts, 0
    period = AccountPeriod(
        tg=user.tg, starts_at=starts, ends_at=calendar_month(anchor, offset + months),
        tier=tier, source_key=source_key, kind="month", anchor_at=anchor,
        month_offset=offset, end_month_offset=offset + months,
    )
    session.add(period)
    state.revision += 1
    _project(session, user, state, now)
    return period


def start_success(session, user, tier, months, source_key, now=None):
    """Activate a registration only after the caller confirms Emby creation."""
    user = _lock_user(session, user)
    if not user.embyid:
        raise EntitlementError("Emby account creation must succeed before activation")
    previous = _replay(session, user, source_key)
    if previous is not None:
        return append_months(session, user, tier, months, source_key, now)
    now = local_time(now) if now is not None else china_now()
    if user.ex is not None or session.query(AccountPeriod).filter(AccountPeriod.tg == user.tg).count():
        raise EntitlementError("Registration code requires an account without existing periods")
    user.cr = now
    return append_months(session, user, tier, months, source_key, now)


def append_days(session, user, days, source_key, now=None, tier=None):
    """Append legacy day-based value after queued purchases without overriding them."""
    _positive_int(days, "Days")
    if tier is not None:
        _tier(tier)
    user = _lock_user(session, user)
    previous = _replay(session, user, source_key)
    if previous is not None:
        if previous.kind != "legacy" or previous.adjustment_days != days or (
                tier is not None and previous.tier != tier):
            raise EntitlementError("Entitlement source replay differs from the original request")
        return previous
    now = local_time(now) if now is not None else china_now()
    state = _ensure_locked(session, user, now)
    _require_redeemable(state, user)
    tail = _tail(session, user, now)
    starts = tail.ends_at if tail else now
    chosen_tier = tier or (tail.tier if tail else ("vip" if user.lv == "a" else "normal"))
    period = AccountPeriod(tg=user.tg, starts_at=starts, ends_at=starts + timedelta(days=days),
                           tier=chosen_tier, source_key=source_key, kind="legacy", adjustment_days=days)
    session.add(period)
    state.revision += 1
    _project(session, user, state, now)
    return period


def adjust_days(session, user, days, source_key, now=None, tier=None):
    """Apply an explicit owner adjustment, consuming future time from the tail."""
    if isinstance(days, bool) or not isinstance(days, int) or days == 0:
        raise EntitlementError("Day adjustment must be a nonzero integer")
    if days > 0:
        return append_days(session, user, days, source_key, now, tier)
    user = _lock_user(session, user)
    previous = _replay(session, user, source_key)
    if previous is not None:
        if previous.kind != "adjustment" or previous.adjustment_days != days:
            raise EntitlementError("Entitlement source replay differs from the original request")
        return previous
    now = local_time(now) if now is not None else china_now()
    state = _ensure_locked(session, user, now)
    periods = [p for p in _periods(session, user.tg)
               if p.kind != "adjustment" and p.ends_at > max(now, p.starts_at)]
    remaining = timedelta(days=-days)
    available = sum((p.ends_at - max(now, p.starts_at) for p in periods), timedelta())
    if remaining > available:
        raise EntitlementError("Cannot remove more than the account's remaining time")
    for period in reversed(periods):
        reduction = min(remaining, period.ends_at - max(now, period.starts_at))
        period.ends_at -= reduction
        period.kind = "legacy"
        period.anchor_at = None
        period.month_offset = period.end_month_offset = None
        remaining -= reduction
        if not remaining:
            break
    marker = AccountPeriod(tg=user.tg, starts_at=now, ends_at=now, tier=tier or "normal",
                           source_key=_source(source_key), kind="adjustment", adjustment_days=days)
    session.add(marker)
    state.revision += 1
    _project(session, user, state, now)
    return marker


def set_current_tier(session, user, tier, now=None):
    """Explicit legacy whitelist/owner change affects the current period only."""
    _tier(tier)
    user = _lock_user(session, user)
    now = local_time(now) if now is not None else china_now()
    state = _ensure_locked(session, user, now)
    _require_redeemable(state, user)
    current = [p for p in _periods(session, user.tg)
               if p.kind != "adjustment" and p.starts_at <= now < p.ends_at]
    if len(current) != 1:
        raise EntitlementError("A current account period is required")
    current[0].tier = tier
    state.revision += 1
    return _project(session, user, state, now)


def mark_block(session, user, reason, now=None):
    """Persist the lifecycle cause before disabling an Emby account."""
    if reason not in BLOCK_REASONS:
        raise EntitlementError("Unknown account block reason")
    user = _lock_user(session, user)
    now = local_time(now) if now is not None else china_now()
    state = _ensure_locked(session, user, now)
    if reason == "expiry" and state.blocked_reason not in {None, "expiry"}:
        raise EntitlementError("Expiry cannot replace another account block reason")
    state.blocked_reason = reason
    state.revision += 1
    if user.lv != "c":
        user.disabled_at = now
    user.lv = "c"
    session.flush()
    return state


def clear_block(session, user, now=None, *, explicit=False):
    """Only an explicit administrative action can clear a non-expiry block."""
    user = _lock_user(session, user)
    now = local_time(now) if now is not None else china_now()
    state = _ensure_locked(session, user, now)
    if state.blocked_reason not in {None, "expiry"} and not explicit:
        raise EntitlementError("This account requires an explicit administrative unblock")
    state.blocked_reason = None
    state.revision += 1
    if user.lv == "c":
        user.lv = "b"
    return _project(session, user, state, now)
