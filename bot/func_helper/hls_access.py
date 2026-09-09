"""Short-lived identity continuity for Emby HLS URLs without a client token."""
from dataclasses import dataclass, field
import re
from urllib.parse import parse_qs, urlsplit

from cacheout import Cache


@dataclass(frozen=True)
class HLSBinding:
    user_id: str
    token: str = field(repr=False)


class HLSAccess:
    def __init__(self):
        self.cache = Cache(maxsize=2048, ttl=3600)

    def context(self, host, uri):
        if not uri or len(uri) > 16384:
            return None
        try:
            parsed = urlsplit(uri)
            query = parse_qs(parsed.query, keep_blank_values=True)
        except ValueError:
            return None
        session_ids = [value for key, values in query.items() if key.lower() == 'playsessionid' for value in values]
        if len(session_ids) != 1 or not re.fullmatch(r'[0-9a-fA-F]{32}', session_ids[0]):
            return None
        match = re.fullmatch(
            r'/(?:emby/)?(videos|audio)/([A-Za-z0-9_-]+)/(master\.m3u8|main\.m3u8|hls\d*/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+)',
            parsed.path, re.IGNORECASE,
        )
        if not match:
            return None
        key = (host.lower(), session_ids[0].lower(), match[1].lower(), match[2].lower())
        return key, match[3].lower() in ('master.m3u8', 'main.m3u8')

    def register(self, host, uri, user_id, token):
        context = self.context(host, uri)
        if not context or not context[1] or not user_id or not token:
            return True
        key, _ = context
        existing = self.cache.get(key)
        if existing and existing.user_id != user_id:
            return False
        self.cache.set(key, HLSBinding(user_id, token))
        return True

    def lookup(self, host, uri):
        context = self.context(host, uri)
        if not context or context[1]:
            return None
        return self.cache.get(context[0])

    def refresh(self, host, uri, binding):
        context = self.context(host, uri)
        if context:
            self.cache.set(context[0], binding)

    def discard(self, host, uri):
        context = self.context(host, uri)
        if context:
            self.cache.delete(context[0])


def has_explicit_credential(token, auth_header, uri):
    if token or len(uri) > 16384 or len(auth_header or '') > 8192:
        return True
    if re.search(r'(?i)\bToken\s*=|^\s*Bearer(?:\s|$)', auth_header or ''):
        return True
    try:
        query = parse_qs(urlsplit(uri).query, keep_blank_values=True)
    except ValueError:
        return True
    credential_keys = {'api_key', 'token', 'x-emby-token', 'authorization', 'x-emby-authorization'}
    return any(key.lower() in credential_keys for key in query)
