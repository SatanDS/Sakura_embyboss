"""Read-only Emby token identity, shared without importing any HTTP application."""

import os
import sqlite3
import uuid
from typing import Any, List, Tuple
from urllib.parse import quote

_MAX_USER_ID_LENGTH = 255


def _bounded_identifier(value, limit):
    normalized = "" if value is None else str(value).strip()
    return normalized if 0 < len(normalized) <= limit else ""


def configured_auth_db_path(config):
    """Only operator configuration may select the mounted authentication DB."""
    value = getattr(config, "emby_auth_db_path", None) or os.getenv("EMBY_AUTH_DB_PATH", "")
    return _bounded_identifier(value, 4096)


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


def lookup_user_from_auth_db(token: str, db_path: str) -> Tuple[str, str]:
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
            supported_schema = False
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
                supported_schema = True
                token_rows = connection.execute(
                        "SELECT DISTINCT UserId FROM " + table_name + " "
                        "WHERE AccessToken = ? AND IsActive = 1 LIMIT 3",
                        (token,),
                    ).fetchall()
                if len(token_rows) >= 3:
                    return "", "Emby token has ambiguous user bindings"
                rows.extend(token_rows)
            if not supported_schema:
                return "", "Emby authentication database schema is unsupported"
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
