"""Transport-independent, durable Telegram / PKCE / device authorization."""

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .models import DesktopSession, Device, LoginChallenge, RefreshToken

PREFIX = "/api/dushengtv/v1"
LOGIN_SECONDS, ACCESS_SECONDS, SESSION_SECONDS = 300, 900, 30 * 86400


class TVError(Exception):
    def __init__(self, code, message, status=403):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def valid(value, pattern, message="请求格式无效"):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise TVError("INVALID_REQUEST", message, 400)
    return value


def canonical_url(value, *, https_only=False):
    try:
        url = urlsplit(value)
        port = url.port
        if (url.scheme not in ({"https"} if https_only else {"https", "http"})
                or not url.hostname or url.username or url.password or url.query or url.fragment
                or "\\" in value or any(c.isspace() for c in value)
                or any(part in {".", ".."} for part in url.path.split("/")) or "%" in url.path):
            raise ValueError()
        host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname.lower()
        if port and port != (443 if url.scheme == "https" else 80):
            host += f":{port}"
        return urlunsplit((url.scheme, host, url.path.rstrip("/"), "", ""))
    except (ValueError, TypeError, AttributeError):
        raise TVError("INVALID_URL", "服务器地址无效", 400) from None


class DesktopAuth:
    def __init__(self, sessions, account_lookup, *, max_devices=3, consent_version="2026-10-03", now=None):
        self.sessions, self.account_lookup = sessions, account_lookup
        self.max_devices, self.consent_version = max_devices, consent_version
        self.now = now or datetime.utcnow

    def start(self, data, public_url, bot_username):
        state = valid(data.get("state"), r"[A-Za-z0-9_-]{32,128}")
        challenge = valid(data.get("codeChallenge"), r"[A-Za-z0-9_-]{43}")
        installation = valid(data.get("installationId"), r"[A-Za-z0-9_-]{16,64}")
        if data.get("codeChallengeMethod") != "S256" or data.get("client") != "DuShengTV":
            raise TVError("INVALID_REQUEST", "仅支持 DuShengTV S256 登录请求", 400)
        link = secrets.token_urlsafe(32)
        row = LoginChallenge(id=uuid4().hex, state_hash=digest(state), code_challenge=challenge,
                             link_hash=digest(link), installation_id=installation,
                             display_code=f"{secrets.randbelow(1000000):06d}", status="pending",
                             expires_at=self.now() + timedelta(seconds=LOGIN_SECONDS))
        with self.sessions.begin() as db:
            db.query(LoginChallenge).filter(LoginChallenge.expires_at < self.now()).delete()
            db.query(RefreshToken).filter(RefreshToken.expires_at < self.now()).delete()
            db.query(DesktopSession).filter(DesktopSession.expires_at < self.now()).delete()
            db.add(row)
        return {"challengeId": row.id, "state": state, "displayCode": row.display_code,
                "authorizationUrl": f"{public_url}{PREFIX}/auth/telegram/authorize?request={link}",
                "botUsername": bot_username, "expiresIn": LOGIN_SECONDS}

    def _challenge(self, db, *, challenge_id=None, link=None):
        query = db.query(LoginChallenge)
        if link is not None:
            valid(link, r"[A-Za-z0-9_-]{43}")
            query = query.filter_by(link_hash=digest(link))
        else:
            valid(challenge_id, r"[a-f0-9]{32}")
            query = query.filter_by(id=challenge_id)
        row = query.with_for_update().one_or_none()
        if row is None or row.expires_at <= self.now() or row.status in {"consumed", "cancelled"}:
            raise TVError("CHALLENGE_EXPIRED", "登录请求已过期，请返回 DuShengTV 重新登录", 410)
        return row

    def landing(self, link):
        with self.sessions.begin() as db:
            row = self._challenge(db, link=link)
            return {"displayCode": row.display_code, "status": row.status}

    def prepare(self, link, tg, display_name, username):
        with self.sessions.begin() as db:
            row = self._challenge(db, link=link)
            if row.status != "pending" or (row.tg is not None and row.tg != tg):
                raise TVError("CHALLENGE_CLAIMED", "该请求已处理或属于其他 Telegram 用户")
            row.tg, row.display_name, row.username = tg, str(display_name)[:128], str(username or "")[:64]
            return {"id": row.id, "displayCode": row.display_code}

    def decide(self, challenge_id, tg, approve):
        with self.sessions.begin() as db:
            row = self._challenge(db, challenge_id=challenge_id)
            if row.status != "pending" or row.tg != tg:
                raise TVError("CHALLENGE_CLAIMED", "该请求已处理或不属于当前用户")
            row.status = "approved" if approve else "denied"
            if approve:
                try:
                    self.account_lookup(db, tg)
                except TVError as error:
                    if error.code not in {"BOT_UNBOUND", "ACCOUNT_DISABLED"}:
                        raise
                    row.status = "unbound" if error.code == "BOT_UNBOUND" else "disabled"
            return row.status

    def _pkce(self, row, data):
        state = valid(data.get("state"), r"[A-Za-z0-9_-]{32,128}")
        verifier = valid(data.get("codeVerifier"), r"[A-Za-z0-9._~-]{43,128}")
        code = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        if not hmac.compare_digest(row.state_hash, digest(state)) or not hmac.compare_digest(row.code_challenge, code):
            raise TVError("STATE_MISMATCH", "登录校验失败，请重新发起授权")

    def poll(self, data, cancel=False):
        with self.sessions.begin() as db:
            row = self._challenge(db, challenge_id=data.get("challengeId"))
            self._pkce(row, data)
            if cancel:
                row.status = "cancelled"
                return {"cancelled": True}
            if row.status in {"pending", "denied"}:
                return {"status": row.status}
            if row.status == "unbound":
                return {"status": "unbound", "bound": False}
            if row.status == "disabled":
                raise TVError("ACCOUNT_DISABLED", "Emby 账号已停用或到期，请先在 Bot 中处理")
            account = self.account_lookup(db, row.tg)
            session = DesktopSession(id=uuid4().hex, tg=row.tg, installation_id=row.installation_id,
                                     emby_user_id=account["embyUserId"], display_name=row.display_name or "Telegram 用户",
                                     username=row.username or "", expires_at=self.now() + timedelta(seconds=SESSION_SECONDS))
            db.add(session)
            result = self._tokens(db, session)
            row.status = "consumed"
            return {**result, "status": "authorized", "bound": True, "state": data["state"]}

    def _tokens(self, db, session):
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        session.access_hash = digest(access)
        session.access_expires_at = self.now() + timedelta(seconds=ACCESS_SECONDS)
        db.add(RefreshToken(token_hash=digest(refresh), session_id=session.id, expires_at=session.expires_at))
        return {"accessToken": access, "refreshToken": refresh, "expiresIn": ACCESS_SECONDS}

    def _live(self, db, session, *, require_device=True):
        if session is None or session.revoked_at or session.expires_at <= self.now():
            raise TVError("TOKEN_EXPIRED", "登录已过期，请重新登录", 401)
        account = self.account_lookup(db, session.tg)
        if account["embyUserId"] != session.emby_user_id:
            raise TVError("BOT_UNBOUND", "Emby 绑定已改变，请重新登录")
        device = None
        if session.device_id:
            device = db.query(Device).filter_by(id=session.device_id, tg=session.tg).with_for_update().one_or_none()
            if not device or device.revoked_at or device.installation_id != session.installation_id:
                raise TVError("DEVICE_REVOKED", "当前设备已被撤销")
        if require_device and device is None:
            raise TVError("DEVICE_REQUIRED", "请先完成设备登记")
        return device

    def _identity(self, db, token, *, require_device=True):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise TVError("TOKEN_EXPIRED", "请先登录 Telegram", 401)
        session = db.query(DesktopSession).filter_by(access_hash=digest(token)).one_or_none()
        if session is None:
            raise TVError("TOKEN_EXPIRED", "登录已过期，请重新登录", 401)
        # Every device mutation locks the account, session, then device, in that order.
        self.account_lookup(db, session.tg)
        session = db.query(DesktopSession).filter_by(access_hash=digest(token)).populate_existing().with_for_update().one_or_none()
        device = self._live(db, session, require_device=require_device)
        if session.access_expires_at <= self.now():
            raise TVError("TOKEN_EXPIRED", "登录令牌已过期", 401)
        return session, device

    def session(self, token):
        with self.sessions.begin() as db:
            session, device = self._identity(db, token, require_device=False)
            return {"authProvider": "telegram", "bound": True, "allowed": True,
                    "user": {"telegramId": str(session.tg), "displayName": session.display_name, "username": session.username},
                    "device": {"installationId": session.installation_id, "allowed": device is not None},
                    "maxDevices": self.max_devices}

    def refresh(self, data):
        token = valid(data.get("refreshToken"), r"[A-Za-z0-9_-]{43}")
        replay = False
        with self.sessions.begin() as db:
            # Lock the family before the token so competing rotations serialize.
            saved = db.get(RefreshToken, digest(token))
            if saved is None:
                raise TVError("TOKEN_EXPIRED", "登录已过期，请重新登录", 401)
            session = db.get(DesktopSession, saved.session_id)
            if session is None:
                raise TVError("TOKEN_EXPIRED", "登录已过期，请重新登录", 401)
            self.account_lookup(db, session.tg)
            session = db.query(DesktopSession).filter_by(id=saved.session_id).populate_existing().with_for_update().one_or_none()
            saved = db.query(RefreshToken).filter_by(token_hash=digest(token)).populate_existing().with_for_update().one()
            if session is None or session.installation_id != data.get("installationId"):
                raise TVError("INVALID_DEVICE", "登录凭据与当前安装不匹配")
            if saved.used_at:
                session.revoked_at = self.now()
                replay = True
            else:
                self._live(db, session)
                saved.used_at = self.now()
                result = self._tokens(db, session)
        if replay:
            raise TVError("TOKEN_REPLAYED", "登录凭据已使用，请重新进行 Telegram 授权", 401)
        return result

    def logout(self, token):
        with self.sessions.begin() as db:
            session = db.query(DesktopSession).filter_by(access_hash=digest(token)).with_for_update().one_or_none()
            if session:
                session.revoked_at = self.now()
        return {"revoked": True}

    def challenge(self, token, installation):
        with self.sessions.begin() as db:
            session, _ = self._identity(db, token, require_device=False)
            if installation != session.installation_id:
                raise TVError("INVALID_DEVICE", "设备安装标识不匹配")
            nonce = secrets.token_urlsafe(32)
            session.nonce_hash, session.nonce_expires_at = digest(nonce), self.now() + timedelta(seconds=60)
            return {"nonce": nonce, "expiresIn": 60}

    def _proof(self, session, proof, fingerprint, expected_key=None):
        try:
            if not isinstance(proof, dict):
                raise ValueError()
            raw, pem = proof["payload"], proof["publicKey"]
            if not isinstance(raw, str) or len(raw) > 2048 or not isinstance(pem, str) or len(pem) > 256:
                raise ValueError()
            key = serialization.load_pem_public_key(pem.encode())
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError()
            canonical = key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
            if expected_key and not hmac.compare_digest(canonical, expected_key):
                raise ValueError()
            key.verify(base64.b64decode(proof["signature"], validate=True), raw.encode("utf-8"))
            payload = json.loads(raw)
            if (not session.nonce_hash or session.nonce_expires_at <= self.now()
                    or not hmac.compare_digest(session.nonce_hash, digest(payload["nonce"]))
                    or payload["installationId"] != session.installation_id or payload["fingerprint"] != fingerprint):
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError, InvalidSignature):
            raise TVError("INVALID_PROOF", "设备签名无效或已过期，请重试") from None
        session.nonce_hash, session.nonce_expires_at = None, None
        return canonical

    def register(self, token, data):
        hardware = data.get("device")
        if not isinstance(hardware, dict) or len(json.dumps(hardware)) > 16384:
            raise TVError("INVALID_REQUEST", "设备信息无效", 400)
        if (not isinstance(hardware.get("deviceName"), str) or len(hardware["deviceName"]) > 128
                or not isinstance(hardware.get("os"), dict) or not isinstance(hardware.get("gpus", []), list)):
            raise TVError("INVALID_REQUEST", "设备名称或系统信息无效", 400)
        fingerprint = valid(hardware.get("fingerprint"), r"[a-f0-9]{64}")
        if data.get("consentVersion") != self.consent_version:
            raise TVError("CONSENT_REQUIRED", "请先同意当前设备信息用途")
        with self.sessions.begin() as db:
            session, current = self._identity(db, token, require_device=False)
            if hardware.get("installationId") != session.installation_id:
                raise TVError("INVALID_DEVICE", "设备安装标识不匹配")
            device = db.query(Device).filter_by(tg=session.tg, installation_id=session.installation_id).with_for_update().one_or_none()
            pem = self._proof(session, data.get("proof"), fingerprint,
                              device.public_key if device and not device.revoked_at else None)
            if (not device or device.revoked_at) and db.query(Device).filter_by(tg=session.tg, revoked_at=None).count() >= self.max_devices:
                raise TVError("DEVICE_LIMIT", f"最多允许 {self.max_devices} 台设备，请先在 Bot 使用 /tvdevices 撤销旧设备")
            if device is None:
                device = Device(id=uuid4().hex, tg=session.tg, installation_id=session.installation_id, created_at=self.now())
                db.add(device)
            # Only the advertised hardware fields are retained, never arbitrary credentials.
            device.hardware = {k: hardware[k] for k in ("deviceName", "os", "cpu", "memoryGB", "gpus", "quality", "componentHashes", "fingerprintVersion") if k in hardware}
            device.fingerprint, device.public_key = fingerprint, pem
            device.consent_version, device.app_version = self.consent_version, str(data.get("appVersion", ""))[:32]
            device.last_seen, device.revoked_at = self.now(), None
            session.device_id = device.id
            return {"allowed": True, "installationId": session.installation_id}

    def heartbeat(self, token, data):
        with self.sessions.begin() as db:
            session, device = self._identity(db, token)
            if data.get("installationId") != session.installation_id or data.get("fingerprint") != device.fingerprint:
                raise TVError("INVALID_DEVICE", "设备标识已改变，请重新登录")
            self._proof(session, data.get("proof"), device.fingerprint, device.public_key)
            device.last_seen = self.now()
            return {"allowed": True, "installationId": session.installation_id}

    def _device_list(self, db, tg):
        devices = db.query(Device).filter_by(tg=tg, revoked_at=None).order_by(Device.last_seen.desc()).all()
        return {"maxDevices": self.max_devices, "devices": [{"id": d.id, "installationId": d.installation_id,
                "name": str(d.hardware.get("deviceName", "Windows")),
                "os": str(d.hardware.get("os", {}).get("platform", "Windows")),
                "gpu": " / ".join(str(g.get("name", "")) for g in d.hardware.get("gpus", []) if isinstance(g, dict)),
                "lastSeen": d.last_seen.isoformat() + "Z", "allowed": True} for d in devices]}

    def devices(self, token):
        with self.sessions.begin() as db:
            session, _ = self._identity(db, token)
            return self._device_list(db, session.tg)

    def bot_devices(self, tg):
        with self.sessions.begin() as db:
            return self._device_list(db, tg)

    def _revoke(self, db, tg, device_id):
        device = db.query(Device).filter_by(tg=tg, id=device_id).with_for_update().one_or_none()
        if device is None:
            raise TVError("DEVICE_NOT_FOUND", "设备不存在", 404)
        device.revoked_at = self.now()
        db.query(DesktopSession).filter_by(tg=tg, device_id=device.id).update({"revoked_at": self.now()})
        return {"revoked": True, "installationId": device.installation_id}

    def revoke(self, token, device_id):
        # Identity transaction ends before device/session revocation, avoiding lock inversion.
        with self.sessions.begin() as db:
            session, _ = self._identity(db, token)
            tg = session.tg
        return self.bot_revoke(tg, device_id)

    def bot_revoke(self, tg, device_id):
        with self.sessions.begin() as db:
            self.account_lookup(db, tg)
            return self._revoke(db, tg, device_id)

    def server_identity(self, token):
        with self.sessions.begin() as db:
            session, _ = self._identity(db, token)
            return {"telegramId": str(session.tg), "embyUserId": session.emby_user_id}
