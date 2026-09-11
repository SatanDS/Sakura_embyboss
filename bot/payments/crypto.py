"""Payment codes are recoverable for the purchaser, but never stored in plaintext."""

import base64
import hashlib
import re
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,14}_$")


def code_hash(token):
    return hashlib.sha256(str(token).strip().encode("ascii")).hexdigest()


class CodeCipher:
    def __init__(self, key):
        try:
            raw = base64.urlsafe_b64decode(str(key) + "=" * (-len(str(key)) % 4))
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid payment code encryption key") from exc
        if len(raw) != 32:
            raise ValueError("Payment code encryption key must contain 32 bytes")
        self._cipher = AESGCM(raw)

    def issue(self, order_id, prefix="Pay_"):
        if not isinstance(prefix, str) or not _PREFIX_RE.fullmatch(prefix):
            raise ValueError("Invalid payment code prefix")
        token = prefix + secrets.token_urlsafe(32)
        nonce = secrets.token_bytes(12)
        encrypted = self._cipher.encrypt(nonce, token.encode("ascii"), order_id.encode("ascii"))
        return token, code_hash(token), base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")

    def reveal(self, ciphertext, order_id):
        raw = base64.urlsafe_b64decode(ciphertext)
        return self._cipher.decrypt(raw[:12], raw[12:], order_id.encode("ascii")).decode("ascii")
