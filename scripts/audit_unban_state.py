#!/usr/bin/env python3
"""Read current Bot/Emby state after bulk unban; never restore or update users.

From the checkout, using the running container's installed dependencies:
docker compose exec -T embyboss python - < scripts/audit_unban_state.py
"""
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit


def audit(dbrows, remoteusers, now):
    """Return only explicit status fields, without copying credentials or policies."""
    by_id = defaultdict(list)
    for row in dbrows:
        if row.get("id"):
            by_id[str(row["id"])].append(row)
    summary = Counter({"db_accounts": len(dbrows), "remote_users": len(remoteusers),
                       "remote_administrators": 0, "remote_enabled": 0,
                       "emby": 0, "emby2": 0, "unknown": 0, "managed": 0,
                       "unmanaged": 0, "projection_ledger_mismatches": 0,
                       "redeemed_renewals_requiring_review": 0,
                       "active_vip": 0, "active_normal": 0,
                       "needs_review": 0})
    rows = []
    for user in remoteusers:
        policy = user.get("Policy") or {}
        if policy.get("IsAdministrator") is True:
            summary["remote_administrators"] += 1
            continue
        disabled = policy.get("IsDisabled")
        disabled = disabled if isinstance(disabled, bool) else None
        if disabled is False:
            summary["remote_enabled"] += 1
        matches = by_id.get(str(user["Id"]), [])
        row = matches[0] if len(matches) == 1 else {}
        source = row.get("source", "unknown" if not matches else "ambiguous")
        summary[source] += 1
        issues, reason, blocked, tier = [], None, None, None
        mismatch = False
        periods = [p for p in row.get("periods", [])
                   if p["kind"] != "adjustment" and p["ends_at"] > p["starts_at"]]
        last = max(periods, key=lambda p: (p["ends_at"], p["id"]), default={})
        if not row:
            issues.append("no_database_record" if not matches else "duplicate_database_id")
        elif row.get("managed"):
            summary["managed"] += 1
            active = [p for p in periods if p["starts_at"] <= now < p["ends_at"]]
            reason = row.get("blocked_reason")
            if row["lv"] == "c" and reason is None:
                reason = "legacy_unknown"
            if reason == "expiry" and active:
                reason = None
            if not active and reason is None:
                reason = "expiry"
            if len(active) > 1:
                issues.append("overlapping_periods")
            tier = active[0]["tier"] if len(active) == 1 else None
            blocked = bool(reason or len(active) != 1 or tier not in {"vip", "normal"})
            if ((reason == "expiry" or row.get("blocked_reason") == "expiry")
                    and row.get("ex") and row["ex"] > now
                    and (not last or row["ex"] > last["ends_at"])):
                mismatch = True
                summary["projection_ledger_mismatches"] += 1
                issues.append("projection_ledger_mismatch")
        else:
            summary["unmanaged"] += 1
            expiry = row.get("ex")
            if row["lv"] in {"c", "d"} or row.get("expired") == 1:
                reason = "legacy_disabled"
                blocked = True
            elif isinstance(expiry, datetime) and expiry <= now:
                reason = "expiry"
                blocked = True
            elif row["lv"] in {"a", "b"} and isinstance(expiry, datetime):
                blocked = False
                tier = "vip" if row["lv"] == "a" else "normal"
            else:
                issues.append("legacy_status_requires_review")
        latest = {}
        for record in sorted(row.get("redemptions", []),
                             key=lambda r: r.get("usedtime") or datetime.min, reverse=True):
            latest.setdefault(record["kind"], record)
        renewal, whitelist = latest.get("renew", {}), latest.get("whitelist", {})
        renewal_days, renewed_at = renewal.get("us"), renewal.get("usedtime")
        redeemed_review = bool(reason == "expiry" and last and type(renewal_days) is int
                               and renewal_days > 0 and isinstance(renewed_at, datetime)
                               and renewed_at > last["ends_at"]
                               and (now - renewed_at).days < renewal_days)
        if redeemed_review:
            summary["redeemed_renewals_requiring_review"] += 1
            issues.append("redeemed_renewal_requires_review")
        if blocked is True:
            if disabled is False and not mismatch and not redeemed_review:
                issues.append("expired_but_enabled" if reason == "expiry" else "blocked_but_enabled")
        elif blocked is False:
            summary["active_vip" if tier == "vip" else "active_normal"] += 1
        if disabled is None:
            issues.append("remote_disabled_state_missing")
        if issues:
            summary["needs_review"] += 1
            if len(rows) < 100:
                rows.append({"id": str(user["Id"]), "name": user.get("Name"),
                             "tg": row.get("tg"),
                             "source": source, "managed": bool(row.get("managed")),
                             "lv": row.get("lv"), "ex": row["ex"].isoformat() if row.get("ex") else None,
                             "blocked_reason": row.get("blocked_reason"),
                             "effective_block_reason": reason, "remote_is_disabled": disabled,
                             "requires_review": True,
                             "last_period_tier": last.get("tier"),
                             "last_period_end": last["ends_at"].isoformat() if last else None,
                             "last_renewal_days": renewal.get("us"),
                             "last_renewal_at": renewal["usedtime"].isoformat() if renewal.get("usedtime") else None,
                             "last_whitelist_at": whitelist["usedtime"].isoformat() if whitelist.get("usedtime") else None,
                             "issues": issues})
    summary["rows_omitted"] = summary["needs_review"] - len(rows)
    return {"as_of": now.isoformat(), "timezone": "Asia/Shanghai",
            "notice": "当前状态不能证明操作前状态；原禁用状态需操作前日志或快照，原个性策略需策略快照。"
                      "projection_ledger_mismatch 需核实已兑换续期，不代表应禁用。"
                      "兑换证据仅覆盖最近1000条已用续期/白名单码；缺少记录不能证明未兑换。此脚本只读。",
            "summary": dict(summary), "rows": rows}


def read_database(config):
    import pymysql

    connection = pymysql.connect(host=config["db_host"], port=int(config.get("db_port", 3306)),
                                 user=config["db_user"], password=config["db_pwd"],
                                 database=config["db_name"], charset="utf8mb4", autocommit=False,
                                 cursorclass=pymysql.cursors.DictCursor,
                                 connect_timeout=5, read_timeout=15, write_timeout=5)
    try:
        with connection.cursor() as cursor:
            cursor.execute("START TRANSACTION READ ONLY")
            cursor.execute("SELECT TABLE_NAME AS name FROM information_schema.tables "
                           "WHERE TABLE_SCHEMA = DATABASE()")
            tables = {row["name"] for row in cursor.fetchall()}
            ledger = {"payment_account_entitlements", "payment_account_periods"}
            if tables & ledger and not ledger <= tables:
                raise ValueError("Incomplete entitlement schema")
            if ledger <= tables:
                cursor.execute("SELECT e.embyid AS id,e.name,e.tg,e.lv,e.ex,e.disabled_at,"
                               "s.tg AS entitlement_tg,s.blocked_reason FROM emby e "
                               "LEFT JOIN payment_account_entitlements s ON s.tg=e.tg")
            else:
                cursor.execute("SELECT embyid AS id,name,tg,lv,ex,disabled_at FROM emby")
            rows = list(cursor.fetchall())
            periods = defaultdict(list)
            if ledger <= tables:
                cursor.execute("SELECT id,tg,starts_at,ends_at,tier,kind FROM payment_account_periods")
                for period in cursor.fetchall():
                    periods[period["tg"]].append(period)
            redemptions = defaultdict(dict)
            if "Rcode" in tables:
                cursor.execute("SELECT used AS tg,us,usedtime,CASE WHEN code LIKE '%Whitelist%' "
                               "THEN 'whitelist' ELSE 'renew' END AS kind FROM Rcode "
                               "WHERE used IS NOT NULL AND (code LIKE '%Renew%' OR code LIKE '%Whitelist%') "
                               "ORDER BY usedtime DESC LIMIT 1000")
                for record in cursor.fetchall():
                    redemptions[record["tg"]].setdefault(record["kind"], record)
            for row in rows:
                row.update(source="emby", managed=row.get("entitlement_tg") is not None,
                           periods=periods[row["tg"]], redemptions=list(redemptions[row["tg"]].values()))
            if "emby2" in tables:
                cursor.execute("SELECT embyid AS id,name,lv,ex,expired FROM emby2")
                for row in cursor.fetchall():
                    rows.append({**row, "source": "emby2", "managed": False})
            return rows
    finally:
        connection.rollback()
        connection.close()


def read_remote_users(config):
    import requests

    base = config["emby_url"].rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Invalid Emby origin")
    token = config["emby_api"]
    if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
        raise ValueError("Invalid Emby token")
    with requests.Session() as client:
        client.trust_env = False
        response = client.get(base + "/emby/Users", headers={"X-Emby-Token": token},
                              timeout=(5, 15), allow_redirects=False)
        if response.status_code != 200:
            raise ValueError("Emby user listing failed")
        users = response.json()
    if (not isinstance(users, list) or any(not isinstance(u, dict) or not u.get("Id")
            or not isinstance(u.get("Policy", {}), dict) for u in users)):
        raise ValueError("Invalid Emby user listing")
    return users


def main():
    try:
        config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        dbrows = read_database(config)
        users = read_remote_users(config)
        now = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
        print(json.dumps(audit(dbrows, users, now), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"error": "AUDIT_FAILED", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
