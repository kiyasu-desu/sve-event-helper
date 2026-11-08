"""Offline, resumable schema migration. Preview never writes, imports no business code."""
from __future__ import annotations

import copy
import hashlib
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from accountguard import AccountConflict, normalized_uid
from identity_store import (DOCUMENT, JOURNAL, LEGACY, FENCE, IdentityStore, WriterLock,
                            empty_state, read_json, validate, write_private, sync_directory)


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if Path(path).exists() else None


def convert(accounts, subscriptions, records):
    state = empty_state()
    if not isinstance(accounts, dict) or type(accounts.get("schema_version", 1)) is not int or accounts.get("schema_version", 1) != 1:
        raise AccountConflict("仅支持预期的旧账号 schema")
    source = accounts.get("accounts", {})
    if not isinstance(source, dict) or not isinstance(subscriptions, dict) or not isinstance(records, dict):
        raise AccountConflict("旧数据结构无效，保留原件并停止迁移")
    aliases, candidates = {}, {}
    def pending(category, original, source_key):
        state["pending"].append({"category": category, "source_key": source_key, "original": copy.deepcopy(original)})
    for old_name, raw in source.items():
        try:
            aid = normalized_uid(raw.get("uid"))
        except (AccountConflict, AttributeError):
            pending("account_missing_uid", raw, old_name)
            continue
        if not isinstance(old_name, str) or not old_name or any(
                not isinstance(raw.get(field, ""), str) for field in ("nickname", "mobile", "sessionid", "deviceid", "user_agent")):
            pending("account_invalid_fields", raw, old_name)
            continue
        aliases[old_name] = aid
        candidates.setdefault(aid, []).append((old_name, raw))
    for aid, rows in candidates.items():
        # Identical sessions may merge deterministically. Differing sessions/metadata
        # have no reliable timestamp: retain all candidates without selecting a winner.
        credentials = {tuple(row.get(field, "") for field in ("sessionid", "deviceid", "nickname", "mobile", "user_agent"))
                       for _, row in rows}
        if len(credentials) != 1 or not all(row.get("sessionid") and row.get("deviceid") for _, row in rows):
            pending("account_session_conflict", rows, aid)
            continue
        names = sorted(name for name, _ in rows)
        raw = copy.deepcopy(sorted(rows, key=lambda pair: pair[0])[0][1])
        raw.setdefault("user_agent", "okhttp/4.9.2")
        raw.setdefault("mobile", "")
        raw.update(account_id=aid, name=raw.get("nickname") or names[0],
                   nickname=raw.get("nickname") or names[0], legacy_names=names,
                   version=1, session_version=1, profile_version=1)
        state["accounts"][aid] = raw
    active = aliases.get(accounts.get("active"))
    state["active_account_id"] = active if active in state["accounts"] else None
    for field, rows in (("subscriptions", subscriptions), ("records", records)):
        for old_key, raw in rows.items():
            if not isinstance(raw, dict):
                pending(field + "_invalid", raw, old_key)
                continue
            row = copy.deepcopy(raw)
            if "enabled" in row and type(row["enabled"]) is not bool:
                pending(field + "_invalid_enabled", row, old_key)
                continue
            legacy_name = row.get("account")
            if legacy_name is not None and not isinstance(legacy_name, str):
                pending(field + "_invalid_name", row, old_key)
                continue
            by_name = aliases.get(legacy_name)
            try:
                by_uid = normalized_uid(row["uid"]) if row.get("uid") is not None else None
            except AccountConflict:
                pending(field + "_invalid_uid", row, old_key)
                continue
            if by_uid and by_name and by_uid != by_name:
                pending(field + "_ownership_conflict", row, old_key)
                continue
            aid = by_uid or by_name
            if aid and aid not in candidates and field == "records":
                state["tombstones"].setdefault(aid, {"version": 1, "nickname": legacy_name or "已删除账号"})
            elif not aid or aid not in state["accounts"]:
                pending(field + "_unresolved", row, old_key)
                continue
            raw_tid = row.get("tournament_id")
            if raw_tid is not None and (isinstance(raw_tid, bool) or not isinstance(raw_tid, (int, str))):
                pending(field + "_invalid_tournament", row, old_key)
                continue
            tid = str(raw_tid or (old_key.rsplit("|", 1)[-1]))
            if not tid.strip():
                pending(field + "_missing_tournament", row, old_key)
                continue
            row.update(account_id=aid, account=aid, tournament_id=tid,
                       uid=state["accounts"][aid]["uid"] if aid in state["accounts"] else row["uid"])
            row.setdefault("migration_sources", []).append(old_key)
            key = aid + "|" + tid
            if key in state[field]:
                existing = state[field][key]
                # Conflicting enablement must not accidentally re-enable a paused task.
                if field == "subscriptions":
                    existing["enabled"] = bool(existing.get("enabled", True) and row.get("enabled", True))
                existing["logs"] = list(dict.fromkeys(existing.get("logs", []) + row.get("logs", [])))
                existing["migration_sources"].append(old_key)
                existing.setdefault("migration_duplicates", []).append(row)
            else:
                state[field][key] = row
    validate(state)
    return state


def preview(root):
    root = Path(root)
    if not root.is_dir():
        raise AccountConflict("数据目录不存在，不生成空迁移报告")
    for name in LEGACY:
        if (root / name).resolve() != root.resolve() / name:
            raise AccountConflict("旧身份文件为符号链接，先核对真实数据目录")
    if (root / DOCUMENT).exists():
        return IdentityStore(root).load()
    values = [read_json(root / name) if (root / name).exists() else ({} if name != "accounts.json" else {"accounts": {}})
              for name in LEGACY]
    return convert(*values)


def report(state):
    categories = {}
    for item in state["pending"]:
        category = item["category"]
        categories[category] = categories.get(category, 0) + 1
    names = {}
    for row in state["accounts"].values():
        names.setdefault(row["nickname"], 0)
        names[row["nickname"]] += 1
    return {"schema_version": 2, "accounts": len(state["accounts"]),
            "subscriptions": len(state["subscriptions"]), "records": len(state["records"]),
            "same_uid_alias_groups": sum(len(row["legacy_names"]) > 1 for row in state["accounts"].values()),
            "same_nickname_groups": sum(count > 1 for count in names.values()), "pending": categories}


def apply(root, *, fail_after=None):
    root = Path(root)
    with WriterLock(root):
        if (root / DOCUMENT).exists():
            return report(IdentityStore(root).load())
        journal_path = root / JOURNAL
        if journal_path.exists() and read_json(journal_path).get("stage") != "rolled_back":
            raise AccountConflict("存在迁移事务，请先 recover")
        digests = {name: file_digest(root / name) for name in LEGACY}
        state = preview(root)
        backup = root / "identity-backups" / uuid.uuid4().hex
        if backup.parent.resolve() != root.resolve() / "identity-backups":
            raise AccountConflict("备份目录经过符号链接，拒绝复制凭据")
        backup.mkdir(parents=True, mode=0o700)
        for name in LEGACY:
            if digests[name] is not None:
                shutil.copyfile(root / name, backup / name)
                (backup / name).chmod(0o600)
                with (backup / name).open("r+b") as output:
                    output.flush()
                    os.fsync(output.fileno())
        if digests != {name: file_digest(root / name) for name in LEGACY}:
            raise AccountConflict("迁移期间旧数据变化，请停止所有写入后重试")
        write_private(backup / "prepared.json", state)
        sync_directory(backup.parent)
        journal = {"stage": "prepared", "backup": backup.relative_to(root).as_posix(),
                   "legacy_hashes": digests, "initial_hash": file_digest(backup / "prepared.json")}
        write_private(journal_path, journal)
        if fail_after == "prepared":
            raise OSError("synthetic migration interruption")
        _finish(root, journal, fail_after=fail_after)
        return report(state)


def _finish(root, journal, *, fail_after=None):
    backup = root / journal["backup"]
    state = read_json(backup / "prepared.json")
    validate(state)
    if file_digest(backup / "prepared.json") != journal["initial_hash"]:
        raise AccountConflict("迁移候选文件哈希不符，保留原件")
    _check_legacy_inputs(root, journal)
    journal["stage"] = "fencing"
    write_private(root / JOURNAL, journal)
    for i, name in enumerate(LEGACY):
        write_private(root / name, FENCE)
        if fail_after == "fence-%d" % i:
            raise OSError("synthetic migration interruption")
    write_private(root / DOCUMENT, state)
    if fail_after == "activated":
        raise OSError("synthetic migration interruption")
    journal["stage"] = "complete"
    write_private(root / JOURNAL, journal)


def _check_legacy_inputs(root, journal):
    for name in LEGACY:
        path = root / name
        if file_digest(path) == journal["legacy_hashes"][name]:
            continue
        if path.exists() and read_json(path) == FENCE:
            continue
        raise AccountConflict("迁移事务期间出现新的旧格式写入，拒绝覆盖；请先核对最新数据")


def recover(root, *, rollback=False, fail_after=None):
    root = Path(root)
    with WriterLock(root):
        journal = read_json(root / JOURNAL)
        if journal["stage"] == "rolled_back":
            return
        if journal["stage"] == "rolling_back" and not rollback:
            raise AccountConflict("回退事务未完成，只能继续 rollback")
        if journal["stage"] == "complete" and not (root / DOCUMENT).exists():
            raise AccountConflict("已启用的数据集缺失；拒绝重放初始快照，请核对最新 v2 备份")
        backup = root / journal["backup"]
        if not backup.resolve().is_relative_to(root.resolve()):
            raise AccountConflict("备份路径无效")
        if (root / DOCUMENT).exists() and file_digest(root / DOCUMENT) != journal["initial_hash"]:
            raise AccountConflict("新数据集已变化，禁止用旧快照覆盖；保留 v2 并另行制定回滚")
        if rollback:
            _check_legacy_inputs(root, journal)
            for name in LEGACY:
                original = backup / name
                if file_digest(original) != journal["legacy_hashes"][name]:
                    raise AccountConflict("备份校验失败")
            journal["stage"] = "rolling_back"
            write_private(root / JOURNAL, journal)
            for index, name in enumerate(LEGACY):
                original = backup / name
                if original.exists():
                    # Restore exact bytes, not just equivalent JSON serialization.
                    fd, target = tempfile.mkstemp(prefix=".identity-restore-", dir=root)
                    with os.fdopen(fd, "wb") as output:
                        output.write(original.read_bytes())
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(target, root / name)
                elif (root / name).exists():
                    (root / name).unlink()
                if fail_after == "rollback-%d" % index:
                    raise OSError("synthetic rollback interruption")
            if (root / DOCUMENT).exists():
                (root / DOCUMENT).unlink()
            if fail_after == "rollback-removed":
                raise OSError("synthetic rollback interruption")
            journal["stage"] = "rolled_back"
            write_private(root / JOURNAL, journal)
        else:
            _finish(root, journal)
