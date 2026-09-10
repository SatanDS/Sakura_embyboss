#! /usr/bin/python3
# -*- coding: utf-8 -*-
"""
line_report - 接收网关转发的线路访问信息，用于检测用户的播放线路
Author: dddddluo
Date:2026/4/15

使用方式：
    推荐由 Caddy forward_auth 同步调用；旧版 nginx mirror 也可调用，但必须
    在请求中带 X-DuSheng-Line-Token。Bot 收到后进行线路权限检查，若违规则
    终止会话/封禁用户。
"""
from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from bot.sql_helper.sql_emby import (
    Emby,
    sql_get_emby_by_embyid,
    sql_update_emby,
)
from bot import LOGGER, bot, config
from bot.func_helper.emby import emby
from bot.func_helper.hls_access import HLSAccess, has_explicit_credential
import json
import asyncio
import os
import re
import sqlite3
import uuid
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from urllib.parse import parse_qs, quote, urlparse

router = APIRouter()
_hls_access = HLSAccess()

# 违规冷却缓存: {user_id: last_violation_time}
_violation_cooldown: Dict[str, datetime] = {}

# Values received through a reverse-proxy request are untrusted.  Keep the
# parser bounded so a malformed URI/header cannot cause excessive work or end
# up in a log message.  Emby user IDs are normally short UUIDs and access
# tokens are well below this limit.
_MAX_USER_ID_LENGTH = 255
_MAX_IDENTIFIER_LENGTH = 512
_MAX_TOKEN_LENGTH = 4096
_MAX_AUTH_HEADER_LENGTH = 8192
_MAX_REQUEST_URI_LENGTH = 16384
_MAX_PATH_LENGTH = 4096

def is_in_cooldown(user_id: str) -> bool:
    """检查用户是否在冷却期内（冷却期内的重复上报直接忽略）"""
    cooldown_seconds = getattr(config, "line_filter_cooldown_seconds", 60)
    last_time = _violation_cooldown.get(user_id)
    if last_time and datetime.now() - last_time < timedelta(seconds=cooldown_seconds):
        return True
    return False


def update_cooldown(user_id: str) -> None:
    """更新用户的冷却时间戳，并清理过期条目"""
    cooldown_seconds = getattr(config, "line_filter_cooldown_seconds", 60)
    _violation_cooldown[user_id] = datetime.now()
    # 顺手清理已过期的条目，防止内存无限增长
    expired = [
        uid for uid, t in _violation_cooldown.items()
        if datetime.now() - t >= timedelta(seconds=cooldown_seconds)
    ]
    for uid in expired:
        del _violation_cooldown[uid]

# ==================== 线路权限控制 ====================

def extract_host_port(url: str) -> Tuple[Optional[str], Optional[int]]:
    """从URL中提取主机名和端口"""
    try:
        if not url:
            return None, None
        if not url.startswith(('http://', 'https://')):
            url = f"http://{url}"
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port
        return host, port
    except Exception as e:
        LOGGER.error(f"解析URL失败: {url} - {str(e)}")
        return None, None


def normalize_line_url(url: str) -> str:
    """标准化线路URL，用于比较"""
    if not url:
        return ""
    url = url.lower().strip()
    url = re.sub(r'^https?://', '', url)
    url = url.rstrip('/')
    return url


def is_whitelist_line(session_server_address: str) -> bool:
    """
    检查会话使用的是否是白名单线路
    :param session_server_address: 会话中的服务器地址
    :return: True 如果是白名单线路
    """
    whitelist_line = getattr(config, "emby_whitelist_line", None)
    if not whitelist_line:
        return False

    session_normalized = normalize_line_url(session_server_address)
    if not session_normalized:
        return False

    # 支持字符串或列表两种配置格式
    lines = whitelist_line if isinstance(whitelist_line, list) else [whitelist_line]
    for line in lines:
        whitelist_normalized = normalize_line_url(line)
        if whitelist_normalized and session_normalized == whitelist_normalized:
            return True
    return False


_NORMAL_LINE_NAMES = {
    "normal",
    "normal_line",
    "normalline",
    "public",
    "default",
}
_VIP_LINE_NAMES = {
    "vip",
    "vip_line",
    "vipline",
    "whitelist",
    "whitelist_line",
    "whitelistline",
    "white",
}


def classify_line_request(host: str, line: str) -> Optional[str]:
    """Return ``normal``/``vip`` for a configured gateway endpoint.

    ``host`` is the authoritative value supplied by the gateway route.  The
    line label is checked as a second, independent invariant so a malformed
    or misconfigured proxy cannot silently turn an unknown endpoint into a
    normal (allowed) line.  Missing/unknown hosts and labels fail closed.
    """
    host_normalized = normalize_line_url(host)
    line_normalized = normalize_line_url(line)
    normal_configured = normalize_line_url(getattr(config, "emby_line", ""))
    vip_configured = normalize_line_url(getattr(config, "emby_whitelist_line", ""))

    if not host_normalized:
        return None

    if vip_configured and host_normalized == vip_configured:
        host_role = "vip"
    elif normal_configured and host_normalized == normal_configured:
        host_role = "normal"
    else:
        return None

    # Caddy's template uses ``normal`` and ``vip``.  Keep a few explicit
    # aliases for existing deployments, but never accept an arbitrary label.
    if line_normalized in _VIP_LINE_NAMES:
        line_role = "vip"
    elif line_normalized in _NORMAL_LINE_NAMES:
        line_role = "normal"
    elif line_normalized == vip_configured:
        line_role = "vip"
    elif line_normalized == normal_configured:
        line_role = "normal"
    else:
        return None

    return host_role if line_role == host_role else None


def is_user_whitelisted(user_details: Optional[Emby]) -> bool:
    """
    检查用户是否是白名单用户
    :param user_details: 用户详情
    :return: True 如果是仍在订阅期内的白名单用户 (lv='a')
    """
    if not user_details:
        return False
    # Whitelist is a subscription entitlement, not a permanent override.
    # Legacy rows without ``ex`` must not keep access to the VIP line.
    return user_details.lv == 'a' and bool(
        user_details.ex and user_details.ex > datetime.now()
    )


def effective_line_entitlement(user_details: Optional[Emby]):
    """Resolve a managed paid period at request time; preserve legacy rows."""
    if not user_details:
        return None
    if not hasattr(config, "payments"):
        return user_details
    try:
        from bot.payments.entitlements import resolve_entitlement, AccountEntitlement
        from bot.sql_helper import Session
    except (ImportError, AttributeError):
        # Lightweight integrations/tests that do not load the optional ledger
        # retain the legacy lv/ex projection.
        return user_details
    try:
        with Session() as session:
            managed = session.query(AccountEntitlement).filter_by(tg=user_details.tg).first()
            if managed is None:
                return user_details
            current = session.query(Emby).filter(Emby.tg == user_details.tg).first()
            result = resolve_entitlement(session, current)
            if result is None:
                return user_details
            from types import SimpleNamespace
            # A blocked account must never regain VIP access merely because a
            # paid period still has time left (admin/policy bans are separate
            # from subscription expiry).
            projected_level = ('a' if result.allowed and result.current_tier == 'vip'
                               else 'b' if result.allowed else 'c')
            return SimpleNamespace(lv=projected_level, ex=result.current_end,
                                   tg=user_details.tg, name=user_details.name)
    except Exception as exc:
        raise RuntimeError("payment entitlement lookup failed") from exc


async def get_session_server_address(session_id: str) -> Optional[str]:
    """
    通过 Emby API 获取会话的服务器地址
    :param session_id: 会话ID
    :return: 服务器地址或None
    """
    try:
        result = await emby._request('GET', '/emby/Sessions')
        if not result.success or not result.data:
            LOGGER.error(f"获取会话信息失败: {result.error}")
            return None
        for session in result.data:
            if session.get("Id") == session_id:
                LOGGER.debug(f"Session详情: {json.dumps(session, ensure_ascii=False, indent=2)}")
                return session.get("ServerId") or session.get("ServerAddress")
        return None
    except Exception as e:
        LOGGER.error(f"获取会话服务器地址异常: {str(e)}")
        return None


def parse_emby_authorization(auth_header: str) -> Dict[str, str]:
    """解析 Emby Authorization/X-Emby-Authorization 头"""
    if not auth_header or len(auth_header) > _MAX_AUTH_HEADER_LENGTH:
        return {}

    # Emby normally sends quoted values, but some clients omit the quotes.
    # Accept both forms while keeping the accepted grammar deliberately
    # narrow.  Canonicalise known field names so casing cannot bypass the
    # consistency checks below.
    aliases = {
        "userid": "UserId",
        "deviceid": "DeviceId",
        "client": "Client",
        "device": "Device",
        "version": "Version",
        "token": "Token",
    }
    result: Dict[str, str] = {}
    pattern = re.compile(
        r'([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(?:"([^"]*)"|([^,\s]+))'
    )
    for match in pattern.finditer(auth_header):
        raw_key = match.group(1)
        value = match.group(2) if match.group(2) is not None else match.group(3)
        if not value:
            continue
        key = aliases.get(raw_key.lower(), raw_key)
        result[key] = value
    return result


def parse_original_request_uri(request_uri: str) -> Dict[str, str]:
    """从 nginx 转发的原始 request_uri 中提取查询参数"""
    if not request_uri or len(request_uri) > _MAX_REQUEST_URI_LENGTH:
        return {}

    try:
        parsed = urlparse(request_uri)
        query = parse_qs(parsed.query, keep_blank_values=False)
        aliases = {
            "userid": "userId",
            "deviceid": "DeviceId",
            "x-emby-device-id": "X-Emby-Device-Id",
            "sessionid": "SessionId",
            "playsessionid": "PlaySessionId",
            "x-emby-token": "X-Emby-Token",
            "x-emby-authorization": "X-Emby-Authorization",
            "authorization": "Authorization",
            "token": "token",
            "api_key": "api_key",
        }
        result: Dict[str, str] = {}
        for key, values in query.items():
            if not values or not values[0]:
                continue
            canonical_key = aliases.get(key.lower(), key)
            # Keep the first value, matching the old parse_qs behaviour and
            # avoiding ambiguity when a client repeats a credential field.
            result.setdefault(canonical_key, values[0])
        return result
    except Exception as e:
        # Never include the URI itself in logs: it may contain api_key or a
        # user token.  The caller can still diagnose the failure by type.
        LOGGER.error(f"解析原始 request_uri 失败 ({type(e).__name__})")
        return {}


def redact_request_uri(request_uri: str) -> str:
    """对 request_uri 中的敏感查询参数做脱敏，避免日志泄露 token/api_key"""
    if not request_uri:
        return ""

    if len(request_uri) > _MAX_REQUEST_URI_LENGTH:
        return "<redacted>"

    try:
        parsed = urlparse(request_uri)
        query = parse_qs(parsed.query, keep_blank_values=True)
        sensitive_keys = {
            "api_key",
            "x-emby-token",
            "token",
            "authorization",
            "x-emby-authorization",
            "playsessionid",
        }

        redacted_query = []
        for key, values in query.items():
            if key.lower() in sensitive_keys:
                redacted_query.extend((key, "***") for _ in values)
            else:
                redacted_query.extend((key, value) for value in values)

        redacted_query_string = "&".join(f"{key}={value}" for key, value in redacted_query)
        redacted_uri = parsed.path or ""
        if redacted_query_string:
            redacted_uri = f"{redacted_uri}?{redacted_query_string}"
        if parsed.fragment:
            redacted_uri = f"{redacted_uri}#{parsed.fragment}"
        return redacted_uri
    except Exception as e:
        # Do not log the malformed URI; it may contain a credential.
        LOGGER.error(f"脱敏原始 request_uri 失败 ({type(e).__name__})")
        return "<redacted>"


def normalize_identifier(value: Optional[str]) -> str:
    """统一清洗用于匹配的标识符"""
    if value is None:
        return ""
    return str(value).strip()


def _bounded_identifier(value: Optional[str], limit: int = _MAX_IDENTIFIER_LENGTH) -> str:
    """Return a trimmed identifier, or an empty value when it is oversized."""
    normalized = normalize_identifier(value)
    if not normalized or len(normalized) > limit:
        return ""
    return normalized


async def fetch_active_sessions() -> List[Dict[str, Any]]:
    """获取当前活跃会话列表"""
    ok, sessions, _ = await _fetch_active_sessions_result()
    return sessions if ok else []


async def _fetch_active_sessions_result() -> Tuple[bool, List[Dict[str, Any]], str]:
    """Fetch active sessions while preserving whether the lookup failed.

    ``[]`` is a valid result (there may simply be no active sessions), but it
    must not be confused with a transport/API failure when deciding whether a
    VIP request can be authorized.
    """
    try:
        result = await emby._request("GET", "/emby/Sessions")
        if not result.success:
            error = normalize_identifier(getattr(result, "error", "")) or "Emby sessions request failed"
            LOGGER.error(f"获取活跃会话失败: {error[:300]}")
            return False, [], error[:300]
        if not isinstance(result.data, list):
            LOGGER.error("获取活跃会话失败: Emby 返回格式不是列表")
            return False, [], "Invalid Emby sessions response"
        # Ignore malformed entries rather than letting a client-controlled
        # request trigger an exception while matching sessions.
        sessions = [item for item in result.data if isinstance(item, dict)]
        return True, sessions, ""
    except Exception as e:
        LOGGER.error(f"获取活跃会话异常: {type(e).__name__}")
        return False, [], f"Emby sessions lookup error: {type(e).__name__}"


def _configured_auth_db_path() -> str:
    """Return the optional read-only Emby token database path.

    The path must point to a file mounted into the Bot container.  It is
    deliberately operator-configured; never infer it from a client request.
    """
    value = getattr(config, "emby_auth_db_path", None) or os.getenv(
        "EMBY_AUTH_DB_PATH", ""
    )
    return _bounded_identifier(value, _MAX_PATH_LENGTH)


def _map_auth_db_user_ids(raw_user_ids: List[Any], auth_db_path: str) -> set[str]:
    """Normalize token user IDs, including Emby's internal numeric IDs.

    Some Emby authentication migrations keep ``Tokens_2.UserId`` as the
    integer ``LocalUsersv2.Id`` instead of the public GUID.  Resolve that
    server-owned ID through the read-only users database; never use a client
    supplied userId for this mapping.
    """
    mapped_ids: set[str] = set()
    internal_ids: set[int] = set()

    for raw_user_id in raw_user_ids:
        value = _bounded_identifier(raw_user_id, _MAX_USER_ID_LENGTH)
        if not value:
            return set()
        try:
            mapped_ids.add(uuid.UUID(value).hex)
            continue
        except (ValueError, AttributeError, TypeError):
            pass
        try:
            internal_ids.add(int(value))
        except (ValueError, TypeError):
            return set()

    if not internal_ids:
        return mapped_ids

    base_dir = os.path.dirname(os.path.abspath(auth_db_path))
    users_db_paths = (
        os.path.join(base_dir, "users.db"),
        os.path.join(os.path.dirname(base_dir), "users.db"),
    )
    users_db_path = next((path for path in users_db_paths if os.path.isfile(path)), "")
    if not users_db_path:
        return set()

    resolved_internal_ids: set[int] = set()
    try:
        uri = f"file:{quote(os.path.abspath(users_db_path), safe='/')}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as users_connection:
            users_connection.execute("PRAGMA query_only=ON")
            table_names = {
                row[0]
                for row in users_connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name IN ('LocalUsersv2', 'Users')"
                )
            }
            for table_name in ("LocalUsersv2", "Users"):
                if table_name not in table_names:
                    continue
                for internal_id in internal_ids:
                    row = users_connection.execute(
                        "SELECT guid FROM " + table_name + " "
                        "WHERE Id = ? LIMIT 1",
                        (internal_id,),
                    ).fetchone()
                    if not row or row[0] is None:
                        continue
                    blob = row[0]
                    try:
                        if isinstance(blob, (bytes, bytearray, memoryview)):
                            mapped_ids.add(uuid.UUID(bytes_le=bytes(blob)).hex)
                        else:
                            mapped_ids.add(uuid.UUID(str(blob)).hex)
                        resolved_internal_ids.add(internal_id)
                    except (ValueError, AttributeError, TypeError):
                        continue
    except (sqlite3.Error, OSError):
        return set()

    if resolved_internal_ids != internal_ids:
        return set()
    return mapped_ids


def _lookup_user_from_auth_db_sync(token: str, db_path: str) -> Tuple[str, str]:
    """Resolve an Emby access token through its authentication token table.

    Emby 4.9 does not expose a safe ``/Users/Me`` endpoint and its
    ``/Users/{Id}`` endpoint accepts any authenticated user's token for an
    arbitrary path ID.  The local Tokens table is the authoritative token to
    user binding, so a read-only query is the only database fallback used
    here.  Some Emby migrations leave the current table as ``Tokens_2``;
    both known names are queried using static SQL identifiers.  The token
    itself is always passed as a bound SQL parameter.
    """
    if not db_path:
        return "", "Emby authentication database is not configured"
    if not os.path.isfile(db_path):
        return "", "Emby authentication database is unavailable"

    try:
        uri = f"file:{quote(os.path.abspath(db_path), safe='/')}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            connection.execute("PRAGMA query_only=ON")
            table_names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name IN ('Tokens', 'Tokens_2')"
                )
            }
            rows = []
            for table_name in ("Tokens", "Tokens_2"):
                if table_name not in table_names:
                    continue
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(" + table_name + ")"
                    )
                }
                if not {"AccessToken", "UserId", "IsActive"}.issubset(columns):
                    continue
                token_rows = connection.execute(
                        "SELECT DISTINCT UserId FROM " + table_name + " "
                        "WHERE AccessToken = ? AND IsActive = 1 LIMIT 3",
                        (token,),
                    ).fetchall()
                if len(token_rows) >= 3:
                    return "", "Emby token has ambiguous user bindings"
                rows.extend(token_rows)
    except (sqlite3.Error, OSError) as exc:
        return "", f"Emby authentication database lookup failed: {type(exc).__name__}"

    user_ids = _map_auth_db_user_ids(
        [row[0] for row in rows if row],
        db_path,
    )
    user_ids.discard("")
    if len(user_ids) != 1:
        return "", "Emby token is not bound to one active user"
    return next(iter(user_ids)), "emby.authentication_db"


async def _get_user_from_auth_db(token: str) -> Tuple[str, str]:
    """Run the local SQLite lookup off the event loop."""
    db_path = _configured_auth_db_path()
    if not db_path:
        return "", "Emby authentication database is not configured"
    return await asyncio.to_thread(_lookup_user_from_auth_db_sync, token, db_path)


async def _get_user_from_token(
    token: str,
    auth_header: str = "",
) -> Tuple[str, str]:
    """Authenticate a client token as the playback user.

    Emby 4.9 treats ``/Users/Me`` as a GUID and returns
    ``Unrecognized Guid format``.  If the optional read-only Tokens database
    is mounted, it supplies the canonical token owner.  Older Emby versions
    may still answer ``/Users/Me`` and remain supported as a compatibility
    path.  A client-provided UserId is never used as an identity source.
    """
    bounded_token = _bounded_identifier(token, _MAX_TOKEN_LENGTH)
    if not bounded_token:
        return "", "missing token"

    # Once an authentication DB path is configured it is authoritative.  A
    # missing/unreadable DB or an unknown token must not fall back to the Bot's
    # API key or to a client supplied UserId; doing so would reintroduce the
    # VIP bypass this lookup is intended to prevent.
    if _configured_auth_db_path():
        db_user_id, db_reason = await _get_user_from_auth_db(bounded_token)
        return db_user_id, db_reason

    safe_auth_header = normalize_identifier(auth_header)
    if (
        len(safe_auth_header) > _MAX_AUTH_HEADER_LENGTH
        or "\r" in safe_auth_header
        or "\n" in safe_auth_header
    ):
        safe_auth_header = ""

    # Embyservice's session has the Bot API key as a default header.  An empty
    # override prevents that service credential from being used for this
    # user-authentication request.
    user_request_headers = (
        {
            "X-Emby-Token": "",
            "X-Emby-Authorization": safe_auth_header,
        }
        if safe_auth_header
        else {"X-Emby-Token": bounded_token}
    )

    last_error = ""
    try:
        result = await emby._request(
            "GET",
            "/emby/Users/Me",
            headers=user_request_headers,
        )
    except Exception as e:
        last_error = f"Emby Users/Me lookup error: {type(e).__name__}"
    else:
        if result.success and isinstance(result.data, dict):
            resolved_id = _bounded_identifier(result.data.get("Id"), _MAX_USER_ID_LENGTH)
            if resolved_id:
                return resolved_id, "emby.users.me"
            last_error = "Emby Users/Me returned no user ID"
        elif not result.success:
            last_error = normalize_identifier(getattr(result, "error", "")) or "Emby Users/Me rejected token"
        else:
            last_error = "Invalid Emby Users/Me response"

    return "", (last_error or "Emby user lookup failed")[:300]


def _session_user_for_token(
    sessions: List[Dict[str, Any]], token: str
) -> Tuple[str, Optional[Dict[str, Any]], str]:
    """Resolve a token only from exact ``Sessions.AccessToken`` matches."""
    bounded_token = _bounded_identifier(token, _MAX_TOKEN_LENGTH)
    if not bounded_token:
        return "", None, "missing token"

    matches = [
        session
        for session in sessions
        if _bounded_identifier(session.get("AccessToken"), _MAX_TOKEN_LENGTH)
        == bounded_token
    ]
    if not matches:
        return "", None, "token not present in active Emby sessions"

    user_ids = {
        _bounded_identifier(session.get("UserId"), _MAX_USER_ID_LENGTH)
        for session in matches
    }
    user_ids.discard("")
    if len(user_ids) != 1:
        return "", None, "token maps to no unique Emby user"

    # Prefer the active/playing session when multiple clients share a token.
    matched = next((s for s in matches if s.get("NowPlayingItem")), matches[0])
    return next(iter(user_ids)), matched, "emby.sessions.AccessToken"


def _select_session_for_user(
    sessions: List[Dict[str, Any]],
    *,
    user_id: str,
    token: str,
    device_id: str,
    session_id: str,
    play_session_id: str,
) -> Optional[Dict[str, Any]]:
    """Select a session already authenticated as ``user_id``.

    A token match is preferred.  Device/session IDs are only used to locate a
    session after the token (or ``Users/Me``) has established the user; they
    can never establish an identity by themselves.
    """
    bounded_user = _bounded_identifier(user_id, _MAX_USER_ID_LENGTH)
    bounded_token = _bounded_identifier(token, _MAX_TOKEN_LENGTH)
    bounded_device = _bounded_identifier(device_id)
    bounded_session = _bounded_identifier(session_id)
    bounded_play = _bounded_identifier(play_session_id)
    if not bounded_user:
        return None

    candidates = []
    for session in sessions:
        if _bounded_identifier(session.get("UserId"), _MAX_USER_ID_LENGTH) != bounded_user:
            continue
        access_token = _bounded_identifier(session.get("AccessToken"), _MAX_TOKEN_LENGTH)
        # If a session exposes AccessToken, it must agree with the validated
        # token.  Never use another user's session as a termination target.
        if bounded_token and access_token and access_token != bounded_token:
            continue

        identity_match = False
        if bounded_token and access_token == bounded_token:
            identity_match = True
        if bounded_device and _bounded_identifier(session.get("DeviceId")) == bounded_device:
            identity_match = True
        if bounded_session and _bounded_identifier(session.get("Id")) == bounded_session:
            identity_match = True
        play_state = session.get("PlayState") or {}
        if bounded_play and (
            _bounded_identifier(session.get("PlaySessionId")) == bounded_play
            or _bounded_identifier(play_state.get("PlaySessionId")) == bounded_play
        ):
            identity_match = True
        if identity_match:
            candidates.append(session)

    if not candidates:
        return None
    return next((s for s in candidates if s.get("NowPlayingItem")), candidates[0])


def find_matching_session(
    sessions: List[Dict[str, Any]],
    *,
    user_id: str = "",
    device_id: str = "",
    session_id: str = "",
    play_session_id: str = "",
    token: str = "",
) -> Optional[Dict[str, Any]]:
    """根据多个线索在活跃会话中匹配最可能的会话"""
    normalized_user_id = normalize_identifier(user_id)
    if not normalized_user_id:
        return None
    return _select_session_for_user(
        sessions,
        user_id=normalized_user_id,
        token=token,
        device_id=device_id,
        session_id=session_id,
        play_session_id=play_session_id,
    )

async def resolve_user_context(
    *,
    user_id: str = "",
    device_id: str = "",
    session_id: str = "",
    play_session_id: str = "",
    token: str = "",
    auth_header: str = "",
    original_request_uri: str = "",
    credential_context: Optional[Dict[str, str]] = None,
) -> Tuple[str, Optional[Dict[str, Any]], str]:
    """Resolve an Emby user from a validated per-request credential.

    ``userId`` in a playback URL and ``UserId`` in an authorization header are
    client-controlled claims.  They are deliberately never used as the
    identity source.  A token is authenticated through the optional read-only
    Emby Tokens database or the legacy ``/Users/Me`` endpoint; if neither is
    available, the only fallback is an exact ``Sessions.AccessToken`` match.
    This prevents a normal user from adding a known whitelist user's ID to a
    URL and bypassing the VIP-line check.

    The three-item return value is kept for callers: ``(canonical_user_id,
    matched_session, source_or_failure_reason)``.  An empty user ID means that
    no authenticated identity was established.
    """
    auth_info = parse_emby_authorization(auth_header)
    original_query = parse_original_request_uri(original_request_uri)
    original_auth_info = parse_emby_authorization(
        original_query.get("X-Emby-Authorization", "")
        or original_query.get("Authorization", "")
    )

    # UserId values in URLs and authorization metadata are client-controlled
    # hints.  They are intentionally not used for authentication: clients may
    # send stale IDs, while the validated access token remains authoritative.

    resolved_device_id = _bounded_identifier(
        device_id
        or auth_info.get("DeviceId")
        or original_auth_info.get("DeviceId")
        or original_query.get("X-Emby-Device-Id")
        or original_query.get("DeviceId")
    )
    resolved_session_id = _bounded_identifier(
        session_id or original_query.get("SessionId")
    )
    resolved_play_session_id = _bounded_identifier(
        play_session_id or original_query.get("PlaySessionId")
    )

    # Prefer explicit/header tokens over a query-string api_key.  The latter
    # is a compatibility fallback because some Emby clients put their user
    # token in the URL.  Do not let a forged userId claim replace a validated
    # token.  If two high-confidence token sources disagree, fail closed.
    token_candidates = [
        (token, "request.token"),
        (auth_info.get("Token"), "header.X-Emby-Authorization.Token"),
        (original_auth_info.get("Token"), "header.X-Original-URI.X-Emby-Authorization.Token"),
        (original_query.get("X-Emby-Token"), "header.X-Original-URI.X-Emby-Token"),
        (original_query.get("token"), "header.X-Original-URI.token"),
    ]
    selected_token = ""
    selected_token_source = ""
    seen_tokens = set()
    for value, source in token_candidates:
        bounded = _bounded_identifier(value, _MAX_TOKEN_LENGTH)
        if not bounded:
            continue
        seen_tokens.add(bounded)
        if not selected_token:
            selected_token = bounded
            selected_token_source = source

    # ``api_key`` is the legacy URL spelling used by some Emby clients.  It is
    # considered only when no explicit/header token is available, so an
    # unrelated query parameter cannot override a real client token.
    if not selected_token:
        selected_token = _bounded_identifier(
            original_query.get("api_key"), _MAX_TOKEN_LENGTH
        )
        if selected_token:
            selected_token_source = "header.X-Original-URI.api_key"

    # Conflicting token credentials are a tampering signal.  Do not choose one
    # arbitrarily: a caller must present one coherent Emby credential.
    if len(seen_tokens) > 1:
        return "", None, "conflicting Emby token credentials"
    if not selected_token:
        return "", None, "missing Emby token"

    canonical_user_id, token_error = await _get_user_from_token(
        selected_token,
        auth_header=auth_header
        or original_query.get("X-Emby-Authorization", "")
        or original_query.get("Authorization", ""),
    )
    sessions_ok = False
    sessions: List[Dict[str, Any]] = []
    sessions_error = ""
    matched_session: Optional[Dict[str, Any]] = None

    if canonical_user_id:
        # The token database or Users/Me is authoritative for identity. Query
        # sessions on a best-effort basis to locate the session to terminate.
        sessions_ok, sessions, sessions_error = await _fetch_active_sessions_result()
        if sessions_ok:
            token_session_users = {
                _bounded_identifier(item.get("UserId"), _MAX_USER_ID_LENGTH)
                for item in sessions
                if _bounded_identifier(item.get("AccessToken"), _MAX_TOKEN_LENGTH)
                == selected_token
            }
            token_session_users.discard("")
            if token_session_users and token_session_users != {canonical_user_id}:
                return "", None, "token/session user mismatch"

            matched_session = _select_session_for_user(
                sessions,
                user_id=canonical_user_id,
                token=selected_token,
                device_id=resolved_device_id,
                session_id=resolved_session_id,
                play_session_id=resolved_play_session_id,
            )

        if credential_context is not None:
            credential_context.update(user_id=canonical_user_id, token=selected_token)
        return canonical_user_id, matched_session, f"emby.identity:{token_error or 'emby.users.me'}:{selected_token_source}"

    if _configured_auth_db_path():
        return "", None, f"emby.identity.invalid:{token_error[:240]}"

    # If the configured auth database and Users/Me are unavailable, the only
    # permitted fallback is an exact token match in the active Sessions
    # response; device/session IDs alone are insufficient to establish an
    # identity.
    sessions_ok, sessions, sessions_error = await _fetch_active_sessions_result()
    if not sessions_ok:
        detail = sessions_error or token_error or "Emby identity lookup failed"
        return "", None, f"emby.identity.lookup_failed:{detail[:240]}"

    fallback_user_id, matched_session, fallback_reason = _session_user_for_token(
        sessions, selected_token
    )
    if not fallback_user_id:
        detail = fallback_reason or token_error or "invalid Emby token"
        return "", None, f"emby.identity.invalid:{detail[:240]}"
    if credential_context is not None:
        credential_context.update(user_id=fallback_user_id, token=selected_token)
    return fallback_user_id, matched_session, fallback_reason


async def log_line_violation(
    user_id: Optional[str] = None,
    user_name: Optional[str] = None,
    session_id: Optional[str] = None,
    client_name: Optional[str] = None,
    tg_id: Optional[int] = None,
    user_lv: Optional[str] = None,
    action_taken: Optional[str] = None,
):
    """记录线路权限违规"""
    try:
        lv_display = {'a': '白名单', 'b': '普通用户', 'c': '封禁用户', 'd': '未注册'}.get(user_lv, '未知')

        log_message = (
            f"⚠️ 线路权限违规\n"
            f"━━━━━━━━━━━━━━━\n"
            f"👤 用户: {user_name or 'Unknown'}\n"
            f"🆔 Emby ID: {user_id or 'Unknown'}\n"
            f"📱 TG ID: {f'[{tg_id}](tg://user?id={tg_id})' if tg_id else 'Unknown'}\n"
            f"🏷️ 用户等级: {lv_display}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📺 客户端: {client_name or 'Unknown'}\n"
            f"🔑 会话ID: {session_id or 'Unknown'}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🚨 处理措施: {action_taken or '无'}\n"
            f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        LOGGER.warning(log_message)

        if hasattr(config, "group") and config.group:
            try:
                out = await bot.send_message(chat_id=config.group[0], text=log_message)
                if tg_id:
                    await out.forward(tg_id)
            except Exception as e:
                LOGGER.error(f"发送线路违规通知失败: {str(e)}")

    except Exception as e:
        LOGGER.error(f"记录线路违规失败: {str(e)}")


async def handle_line_violation(
    emby_id: str,
    user_name: str,
    session_id: str,
    client_name: str,
    user_details: Optional[Emby],
) -> dict:
    """
    处理线路权限违规
    :return: 处理结果字典
    """
    action_taken_list = []
    block_success = False
    terminate_success = False

    terminate_session_enabled = getattr(config, "line_filter_terminate_session", True)
    block_user_enabled = getattr(config, "line_filter_block_user", False)

    if terminate_session_enabled and session_id:
        reason = "您使用的线路与您的账户等级不匹配，请使用正确的线路"
        terminate_success = await emby.terminate_session(session_id, reason)
        if terminate_success:
            action_taken_list.append("✅ 已终止会话")
            LOGGER.info(f"成功终止违规会话 {session_id}")
        else:
            action_taken_list.append("❌ 终止会话失败")
            LOGGER.error(f"终止违规会话失败 {session_id}")
    elif terminate_session_enabled:
        action_taken_list.append("❌ 缺少会话 ID，无法终止")

    if block_user_enabled:
        block_success = await emby.emby_change_policy(emby_id=emby_id, disable=True)
        if block_success:
            if user_details:
                sql_update_emby(Emby.tg == user_details.tg, lv="c")
            action_taken_list.append("✅ 已封禁用户")
            LOGGER.info(f"成功封禁违规用户 {emby_id}")
        else:
            action_taken_list.append("❌ 封禁用户失败")
            LOGGER.error(f"封禁违规用户失败 {emby_id}")

    action_taken = " | ".join(action_taken_list) if action_taken_list else "仅记录，未采取行动"

    await log_line_violation(
        user_id=emby_id,
        user_name=user_name,
        session_id=session_id,
        client_name=client_name,
        tg_id=user_details.tg if user_details else None,
        user_lv=user_details.lv if user_details else None,
        action_taken=action_taken,
    )

    return {
        "terminate_success": terminate_success,
        "block_success": block_success,
        "action_taken": action_taken,
    }


@router.get("/line_report")
async def line_report(
    userId: str = "",
    line: str = "",
    host: str = "",
    deviceId: str = "",
    sessionId: str = "",
    playSessionId: str = "",
    token: str = "",
    x_emby_authorization: Optional[str] = Header(default=None, alias="X-Emby-Authorization"),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_emby_token: Optional[str] = Header(default=None, alias="X-Emby-Token"),
    x_original_uri: Optional[str] = Header(default=None, alias="X-Original-URI"),
):
    """
    接收网关转发的线路访问通知。

    网关在代理播放请求时，将 line（线路标识）、host（域名）和 Emby 客户端
    凭据转发到此端点。Bot 据此判断用户是否有权使用该线路。

    :param userId: Emby 用户 ID（从 nginx $arg_userId 获取）
    :param line: 线路标识名称（在 nginx 中通过 set $line_name 定义）
    :param host: 用户访问的域名（从 nginx $server_name 获取）
    :param deviceId: 客户端设备 ID（建议从 nginx $arg_X_Emby_Device_Id 转发）
    :param sessionId: Emby 会话 ID（如能获取建议转发）
    :param playSessionId: Emby 播放会话 ID（如能获取建议转发）
    """
    # Bound all proxy-controlled routing values before normalizing/logging
    # them. A public endpoint must not do unbounded work on attacker input.
    line = _bounded_identifier(line, _MAX_USER_ID_LENGTH)
    host = _bounded_identifier(host, _MAX_IDENTIFIER_LENGTH)
    userId = _bounded_identifier(userId, _MAX_USER_ID_LENGTH)
    deviceId = _bounded_identifier(deviceId)
    sessionId = _bounded_identifier(sessionId)
    playSessionId = _bounded_identifier(playSessionId)
    token = _bounded_identifier(token, _MAX_TOKEN_LENGTH)
    if not line or not host:
        return JSONResponse(
            status_code=403,
            content={"status": "blocked", "message": "Missing line or host"},
        )

    # The gateway must identify both a configured host and a known line
    # label.  Returning 403 here is important because Caddy's forward_auth
    # treats any 2xx response as authorization; unknown/missing values must
    # never fall through to the normal-line allow path.
    line_role = classify_line_request(host, line)
    if line_role is None:
        return JSONResponse(
            status_code=403,
            content={
                "status": "blocked",
                "message": "Unknown or mismatched line/host",
                "line": line,
                "host": host,
            },
        )
    using_whitelist = line_role == "vip"

    redacted_original_request_uri = redact_request_uri(x_original_uri or "")
    request_token = token or (x_emby_token or "")
    request_auth_header = x_emby_authorization or authorization or ""
    hls_binding = None
    if using_whitelist and not has_explicit_credential(request_token, request_auth_header, x_original_uri or ""):
        hls_binding = _hls_access.lookup(host, x_original_uri or "")
        if hls_binding:
            request_token = hls_binding.token
    credential_context: Dict[str, str] = {}
    resolved_user_id, matched_session, resolved_from = await resolve_user_context(
        user_id=userId,
        device_id=deviceId,
        session_id=sessionId,
        play_session_id=playSessionId,
        token=request_token,
        auth_header=request_auth_header,
        original_request_uri=x_original_uri or "",
        credential_context=credential_context,
    )

    if hls_binding and resolved_user_id != hls_binding.user_id:
        _hls_access.discard(host, x_original_uri or "")
        resolved_user_id = ""

    if not resolved_user_id:
        LOGGER.warning(
            "线路上报忽略: 无法识别用户 "
            f"(line={line}, host={host}, reason={resolved_from}, "
            f"deviceId={deviceId}, sessionId={sessionId}, "
            f"playSessionId={'<provided>' if playSessionId else ''}, x_original_uri={redacted_original_request_uri or '<empty>'})"
        )
        if using_whitelist:
            # Caddy's forward_auth treats every 2xx response as authorized;
            # a missing/failed identity therefore has to be non-2xx on VIP.
            status_code = 401 if resolved_from == "missing Emby token" else 403
            return JSONResponse(
                status_code=status_code,
                content={
                    "status": "blocked",
                    "message": "Unable to authenticate Emby user",
                    "line": line,
                    "host": host,
                },
            )
        return {
            "status": "ignored",
            "message": "Missing user identity",
            "line": line,
            "host": host,
            "deviceId": deviceId,
        }

    # host was validated above and is the authoritative gateway endpoint.
    server_address = host

    # 获取用户详情
    # The resolved ID came from Emby token authentication. Look up the local
    # entitlement by the Emby-ID column only; the general bot lookup also
    # matches Telegram IDs and names and is unsafe for authorization.
    try:
        user_details = sql_get_emby_by_embyid(resolved_user_id, raise_on_error=True)
        user_details = effective_line_entitlement(user_details)
    except Exception as exc:
        LOGGER.error(f"Line entitlement lookup unavailable: {type(exc).__name__}")
        # An unknown entitlement during an outage is not a policy violation.
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "message": "Unable to verify line entitlement"},
        )

    # 白名单用户可以用任何线路
    if is_user_whitelisted(user_details):
        if using_whitelist:
            if hls_binding:
                _hls_access.refresh(host, x_original_uri or "", hls_binding)
            elif not _hls_access.register(host, x_original_uri or "", resolved_user_id, credential_context.get('token', '')):
                return JSONResponse(status_code=403, content={"status": "blocked", "message": "Playback identity conflict"})
        LOGGER.debug(f"线路检查通过: 白名单用户 {resolved_user_id} 使用线路 {line}")
        return {"status": "allowed", "message": "Whitelist user"}

    if hls_binding:
        _hls_access.discard(host, x_original_uri or "")
        return JSONResponse(status_code=403, content={"status": "blocked", "message": "Playback entitlement is no longer active"})

    if using_whitelist:
        # 冷却期内的重复上报直接忽略（播放器不响应终止会话时会持续上报）
        if is_in_cooldown(resolved_user_id):
            cooldown_seconds = getattr(config, "line_filter_cooldown_seconds", 60)
            LOGGER.debug(
                f"线路违规冷却中，忽略重复上报: 用户 {resolved_user_id} "
                f"(冷却 {cooldown_seconds}s 内)"
            )
            # Caddy's forward_auth treats every 2xx response as authorized.
            # Keep denying the original playback request while the violation
            # is cooling down, otherwise a client can continue after the
            # first report has been handled.
            return JSONResponse(
                status_code=403,
                content={
                    "status": "cooldown",
                    "message": "Violation already handled, in cooldown",
                    "userId": resolved_user_id,
                },
            )

        update_cooldown(resolved_user_id)

        LOGGER.warning(
            f"线路权限违规(nginx): 用户 {resolved_user_id} 通过 {server_address} 使用白名单线路"
        )

        # 查询活跃会话以获取 session_id 等信息
        session = matched_session
        if not session:
            sessions = await fetch_active_sessions()
            session = _select_session_for_user(
                sessions,
                user_id=resolved_user_id,
                device_id=deviceId,
                session_id=sessionId,
                play_session_id=playSessionId,
                token=token or (x_emby_token or ""),
            )

        session_id = normalize_identifier(session.get("Id")) if session else ""
        client_name = normalize_identifier(session.get("Client")) if session else ""
        user_name = normalize_identifier(session.get("UserName")) if session else ""

        result = await handle_line_violation(
            emby_id=resolved_user_id,
            user_name=user_name or (user_details.name if user_details else ""),
            session_id=session_id,
            client_name=client_name,
            user_details=user_details,
        )

        # This endpoint is also used as Caddy's synchronous forward_auth
        # check. A JSON field alone is not enough: forward_auth only blocks
        # the original request when the auth response is non-2xx.
        return JSONResponse(
            status_code=403,
            content={
                "status": "blocked",
                "message": "Line not allowed",
                "line": line,
                "host": host,
                "userId": resolved_user_id,
                "action_result": result,
            },
        )

    LOGGER.debug(f"线路检查通过: 用户 {resolved_user_id} 使用线路 {line}")
    return {"status": "allowed", "line": line, "host": host, "userId": resolved_user_id}
