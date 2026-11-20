#!/usr/bin/env python3
"""Scoped bulk-unban recovery. Read the private JSON plan from stdin; default is preview.

Run from the checkout in a separate process, using the Bot container's Python.
Only --apply writes. No messages are sent and no Telegram client is started.
"""
import argparse
import copy
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.audit_unban_state import read_database, read_remote_users


def validate_plan(value):
    if not isinstance(value, dict) or set(value) != {
            "restore_renewal", "disable_expired", "disable_unmanaged"}:
        raise ValueError("Invalid recovery plan")
    plan = copy.deepcopy(value)
    renewal = plan["restore_renewal"]
    if not isinstance(renewal, dict) or set(renewal) != {
            "id", "tg", "used_at", "days", "tier", "expires_at"}:
        raise ValueError("Invalid renewal plan")
    if (type(renewal["tg"]) is not int or renewal["tg"] <= 0
            or type(renewal["days"]) is not int or renewal["days"] != 90
            or renewal["tier"] != "vip"):
        raise ValueError("Unexpected renewal scope")
    for key in ("used_at", "expires_at"):
        if not isinstance(renewal[key], str):
            raise ValueError("Invalid renewal time")
        renewal[key] = datetime.fromisoformat(renewal[key])
        if renewal[key].tzinfo is not None:
            raise ValueError("Naive Asia/Shanghai time required")
    if renewal["expires_at"] != renewal["used_at"] + timedelta(days=renewal["days"]):
        raise ValueError("Renewal deadline differs")
    expired, unmanaged = plan["disable_expired"], plan["disable_unmanaged"]
    if (not isinstance(expired, list) or len(expired) != 22
            or not isinstance(unmanaged, list) or len(unmanaged) != 2):
        raise ValueError("Unexpected recovery scope")
    for item in unmanaged:
        if (not isinstance(item, dict) or set(item) != {"id", "name"}
                or not isinstance(item["name"], str) or not item["name"].strip()):
            raise ValueError("Exact unmanaged account name required")
    ids = [renewal["id"]] + expired + [item["id"] for item in unmanaged]
    if any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{32}", item) for item in ids):
        raise ValueError("Invalid exact Emby ID")
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate or conflicting recovery IDs")
    return plan


def actions(plan):
    return ([('restore_renewal', plan["restore_renewal"])]
            + [('disable_expired', {"id": item}) for item in plan["disable_expired"]]
            + [('disable_unmanaged', item) for item in plan["disable_unmanaged"]])


def checked_remote(user, user_id, name=None):
    if not isinstance(user, dict) or user.get("Id") != user_id:
        raise ValueError("Remote account ID differs")
    policy = user.get("Policy")
    if (not isinstance(policy, dict) or policy.get("IsAdministrator") is not False
            or type(policy.get("IsDisabled")) is not bool):
        raise ValueError("Remote administrator or ambiguous policy")
    if name is not None and user.get("Name") != name:
        raise ValueError("Remote account name differs")
    return user


def preview(plan, dbrows, users, now):
    results = []
    for action, item in actions(plan):
        user_id = item["id"]
        matches = [u for u in users if u.get("Id") == user_id]
        if len(matches) != 1:
            raise ValueError("Missing or duplicate remote account")
        checked_remote(matches[0], user_id, item.get("name"))
        rows = [r for r in dbrows if r.get("id") == user_id]
        if action == "disable_unmanaged":
            if rows:
                raise ValueError("Unmanaged target now has a database record")
        elif (len(rows) != 1 or rows[0].get("source") != "emby"
              or not rows[0].get("managed")):
            raise ValueError("Managed target database record differs")
        if action == "restore_renewal" and rows[0].get("tg") != item["tg"]:
            raise ValueError("Renewal Telegram ID differs")
        results.append({"action": action, "id": user_id, "status": "preview",
                        "database_checks": "rechecked under lock during apply"})
    return {"mode": "preview", "as_of": now.isoformat(),
            "counts": {"restore_renewal": 1, "disable_expired": 22, "disable_unmanaged": 2},
            "actions": results}


class EmbyPolicy:
    def __init__(self, config):
        import requests

        self.base = config["emby_url"].rstrip("/")
        parsed = urlsplit(self.base)
        token = config["emby_api"]
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not isinstance(token, str) or not token or "\r" in token or "\n" in token):
            raise ValueError("Invalid Emby connection")
        self.headers = {"X-Emby-Token": token}
        self.client = requests.Session()
        self.client.trust_env = False

    def get(self, user_id, name=None):
        response = self.client.get(self.base + "/emby/Users/" + user_id,
                                   headers=self.headers, timeout=(5, 15), allow_redirects=False)
        if response.status_code != 200:
            raise ValueError("Emby account read failed")
        return checked_remote(response.json(), user_id, name)

    def set_disabled(self, user_id, disabled, name=None, user=None):
        user = checked_remote(user, user_id, name) if user is not None else self.get(user_id, name)
        if user["Policy"]["IsDisabled"] == disabled:
            return "already_disabled" if disabled else "already_enabled"
        policy = copy.deepcopy(user["Policy"])
        policy["IsDisabled"] = disabled
        response = self.client.post(self.base + "/emby/Users/" + user_id + "/Policy",
                                    headers=self.headers, json=policy,
                                    timeout=(5, 15), allow_redirects=False)
        if response.status_code not in {200, 204}:
            raise ValueError("Emby policy update failed")
        confirmed = self.get(user_id, name)
        if confirmed["Policy"] != policy:
            raise ValueError("Emby policy verification failed")
        return "disabled" if disabled else "enabled"

    def close(self):
        self.client.close()


def private_backup(config, plan, dbrows, users):
    selected = {item["id"] for _, item in actions(plan)}
    fields = {"id", "tg", "name", "source", "managed", "lv", "ex", "disabled_at",
              "blocked_reason", "periods", "redemptions"}
    rows = [{key: value for key, value in row.items() if key in fields}
            for row in dbrows if row.get("id") in selected]
    data = {"created_at": datetime.now(timezone.utc).isoformat(),
            "database_rows": rows,
            "remote_users": [{"Id": u["Id"], "Name": u.get("Name"), "Policy": u["Policy"]}
                             for u in users if u["Id"] in selected]}
    directory = Path(config["db_backup_dir"]).resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / ("unban-recovery-before-" + uuid.uuid4().hex + ".jsonl")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.chmod(path, 0o600)
        if os.name == "posix" and stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            raise OSError("Private backup permissions unavailable")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(data, handle, ensure_ascii=False,
                      default=lambda value: value.isoformat())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd is not None:
            os.close(fd)
    return str(path)


def append_backup(path, record):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        if os.name == "posix" and stat.S_IMODE(os.fstat(handle.fileno()).st_mode) != 0o600:
            raise OSError("Private backup permissions changed")
        handle.write(json.dumps(record, ensure_ascii=False, default=lambda value: value.isoformat()) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_runtime():
    # Importing sql_helper otherwise runs schema migrations before recovery.
    previous = os.environ.get("SAKURA_RUNNING_MIGRATIONS")
    os.environ["SAKURA_RUNNING_MIGRATIONS"] = "1"
    try:
        from bot.sql_helper import Session
        from bot.sql_helper.sql_emby import Emby
        from bot.sql_helper.sql_emby2 import Emby2
        from bot.sql_helper.sql_code import Code
        from bot.payments import entitlements
        return SimpleNamespace(Session=Session, Emby=Emby, Emby2=Emby2, Code=Code, ent=entitlements)
    finally:
        if previous is None:
            os.environ.pop("SAKURA_RUNNING_MIGRATIONS", None)
        else:
            os.environ["SAKURA_RUNNING_MIGRATIONS"] = previous


def locked_account(session, runtime, user_id, unmanaged=False):
    users = (session.query(runtime.Emby).filter_by(embyid=user_id).with_for_update().all())
    other = (session.query(runtime.Emby2).filter_by(embyid=user_id).with_for_update().all())
    if unmanaged:
        if users or other:
            raise ValueError("Unmanaged target now has a database record")
        return None
    if len(users) != 1 or other:
        raise ValueError("Missing or multiple managed database matches")
    return users[0]


def database_before(session, runtime, item, unmanaged):
    user = locked_account(session, runtime, item["id"], unmanaged)
    if user is None:
        return None
    ent = runtime.ent
    state = (session.query(ent.AccountEntitlement).filter_by(tg=user.tg)
             .with_for_update().one_or_none())
    periods = session.query(ent.AccountPeriod).filter_by(tg=user.tg).with_for_update().all()
    codes = session.query(runtime.Code).filter_by(used=user.tg).with_for_update().all()
    period_rows = []
    for period in periods:
        values = {column.name: getattr(period, column.name) for column in ent.AccountPeriod.__table__.columns
                  if column.name != "source_key"}
        values["source_sha256"] = hashlib.sha256(period.source_key.encode()).hexdigest()
        period_rows.append(values)
    return {"emby": {key: getattr(user, key) for key in ("tg", "embyid", "name", "lv", "ex", "disabled_at")},
            "entitlement": {key: getattr(state, key) for key in ("tg", "blocked_reason", "revision", "active_tier")}
                           if state is not None else None,
            "periods": period_rows,
            "redemptions": [{"code_sha256": hashlib.sha256(code.code.encode()).hexdigest(),
                             **{key: getattr(code, key) for key in ("tg", "us", "used", "usedtime")}}
                            for code in codes]}


def restore_renewal(session, runtime, item, now):
    ent = runtime.ent
    user = locked_account(session, runtime, item["id"])
    if user.tg != item["tg"]:
        raise ValueError("Renewal Telegram ID differs")
    state = (session.query(ent.AccountEntitlement).filter_by(tg=user.tg)
             .with_for_update().one_or_none())
    if state is None or state.blocked_reason != "expiry":
        raise ValueError("Renewal requires an expiry block")
    codes = (session.query(runtime.Code).filter(
        runtime.Code.used == user.tg, runtime.Code.us == item["days"],
        runtime.Code.usedtime == item["used_at"], runtime.Code.code.like("%Renew%"))
        .with_for_update().all())
    if len(codes) != 1:
        raise ValueError("Consumed renewal proof differs")
    source = "legacy-code:" + codes[0].code
    previous = session.query(ent.AccountPeriod).filter_by(source_key=source).with_for_update().one_or_none()
    if previous is not None and (previous.tg != user.tg or previous.kind != "legacy"
            or previous.starts_at != item["used_at"] or previous.ends_at != item["expires_at"]
            or previous.tier != item["tier"] or previous.adjustment_days != item["days"]):
        raise ValueError("Existing recovery period differs")
    periods = (session.query(ent.AccountPeriod).filter_by(tg=user.tg).with_for_update().all())
    original = [p for p in periods if p.kind != "adjustment" and p.ends_at > p.starts_at
                and p.source_key != source]
    if not original or any(p.ends_at > item["used_at"] for p in original):
        raise ValueError("Original periods do not prove an expired renewal")
    last = max(original, key=lambda p: (p.ends_at, p.id))
    if last.tier != item["tier"]:
        whitelist = (session.query(runtime.Code).filter(
            runtime.Code.used == user.tg, runtime.Code.code.like("%Whitelist%"),
            runtime.Code.usedtime >= item["used_at"], runtime.Code.usedtime <= now)
            .with_for_update().first())
        if whitelist is None:
            raise ValueError("VIP tier proof missing")
    if previous is None and (user.lv != "c" or user.ex is None or user.ex > item["used_at"]):
        raise ValueError("Account changed after the audit")
    if not item["used_at"] <= now < item["expires_at"]:
        raise ValueError("Renewal is outside its active time")
    period = ent.append_days(session, user, item["days"], source,
                             now=item["used_at"], tier=item["tier"])
    if period.starts_at != item["used_at"] or period.ends_at != item["expires_at"]:
        raise ValueError("Recovered period deadline differs")
    result = ent.sync_projection(session, user, now)
    if not result.allowed or result.current_tier != item["tier"] or result.final_expiry != item["expires_at"]:
        raise ValueError("Recovered entitlement differs")
    return "already_restored" if previous is not None else "restored"


def expired_status(session, runtime, item, now):
    ent = runtime.ent
    user = locked_account(session, runtime, item["id"])
    state = (session.query(ent.AccountEntitlement).filter_by(tg=user.tg)
             .with_for_update().one_or_none())
    if state is None or state.blocked_reason != "expiry":
        return "skipped_block_reason_changed"
    periods = (session.query(ent.AccountPeriod).filter_by(tg=user.tg).with_for_update().all())
    authoritative = [p for p in periods if p.kind != "adjustment" and p.ends_at > p.starts_at]
    if not authoritative:
        raise ValueError("Authoritative expiry proof missing")
    end = max(p.ends_at for p in authoritative)
    if end > now or (user.ex is not None and user.ex > now) or user.lv != "c":
        return "skipped_account_changed"
    renewals = (session.query(runtime.Code).filter(
        runtime.Code.used == user.tg, runtime.Code.us > 0, runtime.Code.usedtime > end,
        runtime.Code.code.like("%Renew%")).with_for_update().all())
    sources = {p.source_key for p in periods}
    if any("legacy-code:" + code.code not in sources for code in renewals):
        return "skipped_redemption_requires_review"
    return None


def apply_plan(config, plan, dbrows, users, now):
    preview(plan, dbrows, users, now)
    remote = EmbyPolicy(config)
    try:
        fresh = [remote.get(item["id"], item.get("name")) for _, item in actions(plan)]
        backup = private_backup(config, plan, dbrows, fresh)
        print(json.dumps({"backup_path": backup, "status": "backup_ready"}), flush=True)
        runtime = load_runtime()
        results = []
        for action, item in actions(plan):
            result = {"action": action, "id": item["id"]}
            stage = "lock_snapshot"
            try:
                with runtime.Session.begin() as session:
                    before = database_before(session, runtime, item, action == "disable_unmanaged")
                    stage = "read_policy"
                    user = remote.get(item["id"], item.get("name"))
                    stage = "backup"
                    append_backup(backup, {"action": action, "id": item["id"],
                                           "database": before,
                                           "remote_user": {key: user.get(key) for key in ("Id", "Name", "Policy")},
                                           "captured_at": datetime.now(timezone.utc).isoformat()})
                    if action == "restore_renewal":
                        stage = "restore_entitlement"
                        result["status"] = restore_renewal(session, runtime, item, now)
                        stage = "set_disabled"
                        result["remote_status"] = remote.set_disabled(item["id"], False, user=user)
                    elif action == "disable_expired":
                        stage = "check_expired"
                        status = expired_status(session, runtime, item, now)
                        stage = "set_disabled"
                        result["status"] = status or remote.set_disabled(item["id"], True, user=user)
                    else:
                        stage = "check_unmanaged"
                        locked_account(session, runtime, item["id"], unmanaged=True)
                        stage = "set_disabled"
                        result["status"] = remote.set_disabled(item["id"], True, item["name"], user=user)
                    stage = "commit"
            except Exception as exc:
                result.update(status="failed", stage=stage, error_type=type(exc).__name__)
            results.append(result)
            print(json.dumps(result), flush=True)
        return {"mode": "apply", "backup_path": backup,
                "failed": sum(r["status"] == "failed" for r in results), "actions": results}
    finally:
        remote.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the exact private plan after backup")
    args = parser.parse_args()
    try:
        plan = validate_plan(json.load(sys.stdin))
        config = json.loads(Path("config.json").read_text(encoding="utf-8"))
        dbrows, users = read_database(config), read_remote_users(config)
        now = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
        report = (apply_plan(config, plan, dbrows, users, now) if args.apply
                  else preview(plan, dbrows, users, now))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report.get("failed") else 0
    except Exception as exc:
        print(json.dumps({"error": "RECOVERY_FAILED", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
