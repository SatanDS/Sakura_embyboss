"""Durable browser-bound Telegram approval, independent of Telegram transport."""

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import or_

from bot.payments.models import BrowserChallenge, BrowserSession


LOGIN_SECONDS = 300
SESSION_SECONDS = 86400
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")


BrowserLogin = BrowserChallenge


class LoginError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def token_hash(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def valid_token(value):
    return isinstance(value, str) and _TOKEN.fullmatch(value) is not None


def session_csrf(value):
    return hmac.new(value.encode("ascii"), b"payment-browser-csrf-v1", hashlib.sha256).hexdigest()


def public_origin(settings):
    try:
        parsed = urlsplit(settings.public_url)
        port = parsed.port
    except (TypeError, ValueError):
        raise LoginError("service_unavailable") from None
    local_test = (parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                  and not settings.cookie_secure and not settings.live_mode)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or (parsed.scheme != "https" and not (local_test and parsed.scheme == "http"))
            or (not settings.cookie_secure and not local_test)):
        raise LoginError("service_unavailable")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname.lower()
    suffix = f":{port}" if port and port != (443 if parsed.scheme == "https" else 80) else ""
    return f"{parsed.scheme}://{host}{suffix}"


class BrowserAuth:
    def __init__(self, session_factory, *, now=None):
        self.sessions = session_factory
        self.now = now or datetime.utcnow

    def start(self, browser_token=None):
        browser_token = browser_token if valid_token(browser_token) else secrets.token_urlsafe(32)
        browser_digest = token_hash(browser_token)
        now = self.now()
        link_token = secrets.token_urlsafe(32)
        challenge_id = secrets.token_hex(16)
        display_code = f"{secrets.randbelow(1000000):06d}"
        with self.sessions.begin() as session:
            recent = session.query(BrowserLogin).filter(BrowserLogin.created_at > now - timedelta(minutes=10))
            if (recent.filter_by(browser_hash=browser_digest).count() >= 8
                    or recent.count() >= 600):
                raise LoginError("rate_limited")
            session.query(BrowserLogin).filter_by(browser_hash=browser_digest).filter(
                BrowserLogin.state.in_(("pending", "approved"))).update(
                    {"state": "cancelled"}, synchronize_session=False)
            session.add(BrowserLogin(id=challenge_id, token_hash=token_hash(link_token),
                                     browser_hash=browser_digest, display_code=display_code,
                                     state="pending", created_at=now,
                                     expires_at=now + timedelta(seconds=LOGIN_SECONDS)))
        return {"id": challenge_id, "token": link_token, "browser_token": browser_token,
                "display_code": display_code}

    def prepare(self, link_token, telegram_id):
        if not valid_token(link_token) or type(telegram_id) is not int or telegram_id <= 0:
            raise LoginError("challenge_invalid")
        now = self.now()
        with self.sessions.begin() as session:
            row = session.query(BrowserLogin).filter_by(token_hash=token_hash(link_token)).with_for_update().first()
            if row is None or row.state != "pending" or row.expires_at <= now:
                raise LoginError("challenge_expired")
            if row.approver_tg is not None and row.approver_tg != telegram_id:
                raise LoginError("challenge_invalid")
            changed = session.query(BrowserLogin).filter_by(id=row.id, state="pending").filter(
                or_(BrowserLogin.approver_tg.is_(None), BrowserLogin.approver_tg == telegram_id)
            ).update({"approver_tg": telegram_id}, synchronize_session=False)
            if changed != 1:
                raise LoginError("challenge_invalid")
            return {"id": row.id, "display_code": row.display_code}

    def decide(self, challenge_id, telegram_id, *, approve):
        if not isinstance(challenge_id, str) or not re.fullmatch(r"[a-f0-9]{32}", challenge_id):
            raise LoginError("challenge_invalid")
        now = self.now()
        with self.sessions.begin() as session:
            changed = session.query(BrowserLogin).filter_by(
                id=challenge_id, state="pending", approver_tg=telegram_id
            ).filter(BrowserLogin.expires_at > now).update(
                {"state": "approved" if approve else "denied", "decided_at": now},
                synchronize_session=False)
            if changed != 1:
                raise LoginError("challenge_expired")

    def poll(self, browser_token):
        if not valid_token(browser_token):
            raise LoginError("challenge_expired")
        now = self.now()
        digest = token_hash(browser_token)
        with self.sessions.begin() as session:
            row = session.query(BrowserLogin).filter_by(browser_hash=digest).order_by(
                BrowserLogin.created_at.desc(), BrowserLogin.id.desc()).with_for_update().first()
            if row is None or row.expires_at <= now or row.state in {"cancelled", "consumed"}:
                raise LoginError("challenge_expired")
            if row.state == "denied":
                raise LoginError("challenge_denied")
            if row.state != "approved":
                return None
            # The conditional transition also enforces single consumption on
            # SQLite, whose SELECT FOR UPDATE is ignored.
            changed = session.query(BrowserLogin).filter_by(id=row.id, state="approved").update(
                {"state": "consumed"}, synchronize_session=False)
            if changed != 1:
                raise LoginError("challenge_expired")
            session_token = secrets.token_urlsafe(32)
            session.add(BrowserSession(token_hash=token_hash(session_token), browser_hash=digest,
                                       telegram_id=row.approver_tg, created_at=now,
                                       expires_at=now + timedelta(seconds=SESSION_SECONDS)))
            return session_token

    def identity(self, session_token, browser_token):
        if not valid_token(session_token) or not valid_token(browser_token):
            raise LoginError("login_required")
        with self.sessions() as session:
            row = session.get(BrowserSession, token_hash(session_token))
            if (row is None or row.expires_at <= self.now()
                    or not hmac.compare_digest(row.browser_hash, token_hash(browser_token))):
                raise LoginError("login_required")
            return row.telegram_id

    def logout(self, session_token, browser_token):
        self.identity(session_token, browser_token)
        with self.sessions.begin() as session:
            session.query(BrowserSession).filter_by(token_hash=token_hash(session_token)).delete()
            session.query(BrowserLogin).filter_by(browser_hash=token_hash(browser_token)).filter(
                BrowserLogin.state.in_(("pending", "approved"))).update(
                    {"state": "cancelled"}, synchronize_session=False)

    def cleanup(self):
        now = self.now()
        with self.sessions.begin() as session:
            session.query(BrowserSession).filter(BrowserSession.expires_at <= now).delete()
            session.query(BrowserLogin).filter(BrowserLogin.expires_at <= now - timedelta(days=1)).delete()
