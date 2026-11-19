"""Per-user game UID bindings and isolated notification storage (`users-v1.json`)."""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import time

from accountguard import AccountConflict, normalized_uid
from identity_store import read_json, write_private

USER_SCHEMA = 1
USER_DOC = "users-v1.json"


def normalize_user_id(user_id: str) -> str:
    if not isinstance(user_id, str) or not user_id.strip():
        raise AccountConflict("平台用户 ID 无效")
    text = user_id.strip().lower()
    return "owner" if text == "owner" else text


def user_storage_key(user_id: str) -> str:
    uid = normalize_user_id(user_id)
    if uid == "owner":
        return "owner"
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()[:24]


def validate_users_doc(data: dict) -> None:
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != USER_SCHEMA:
        raise AccountConflict("不支持的用户绑定 schema")
    users = data.get("users")
    if not isinstance(users, dict):
        raise AccountConflict("用户绑定数据结构无效")
    for raw_uid, entry in users.items():
        if not isinstance(raw_uid, str) or raw_uid != normalize_user_id(raw_uid):
            raise AccountConflict("用户 ID 格式无效")
        if not isinstance(entry, dict):
            raise AccountConflict("用户记录格式无效")
        if entry.get("role") not in ("admin", "user"):
            raise AccountConflict("用户角色无效")
        bound = entry.get("bound_account_ids")
        if not isinstance(bound, list) or any(not isinstance(aid, str) or normalized_uid(aid) != aid for aid in bound):
            raise AccountConflict("用户绑定账号列表无效")
        if len(set(bound)) != len(bound):
            raise AccountConflict("用户绑定账号列表存在重复")
        active = entry.get("active_account_id")
        if active is not None and (not isinstance(active, str) or active not in bound):
            raise AccountConflict("用户当前选中账号无效")
        history = entry.get("unbound_history")
        if not isinstance(history, dict):
            raise AccountConflict("用户历史解绑记录无效")
        for aid, row in history.items():
            if not isinstance(aid, str) or normalized_uid(aid) != aid or not isinstance(row, dict):
                raise AccountConflict("用户历史解绑条目无效")


class UserStore:
    """Manages `DATA_DIR/users-v1.json` and per-user `notify.json` paths."""

    def __init__(self, root) -> None:
        self.root = Path(root).resolve()
        self.path = self.root / USER_DOC

    def _check_safe_path(self, target: Path) -> None:
        try:
            rel = target.relative_to(self.root)
        except ValueError as exc:
            raise AccountConflict("路径越界，拒绝访问") from exc
        current = self.root
        if current.is_symlink():
            raise AccountConflict("数据目录为符号链接，拒绝访问")
        for part in rel.parts:
            current = current / part
            if current.is_symlink():
                raise AccountConflict("用户数据路径经过符号链接，拒绝读取/写入")
        resolved = target.resolve()
        if not resolved.is_relative_to(self.root) or ((target.exists() or target.is_symlink()) and resolved != target):
            raise AccountConflict("用户数据路径解析异常，拒绝读取/写入")

    def load(
        self,
        initial_accounts: list[str] | tuple[str, ...] = (),
        initial_active: str | None = None,
        *,
        persist_if_missing: bool = True,
    ) -> dict:
        self._check_safe_path(self.path)
        if self.path.exists():
            try:
                data = read_json(self.path)
            except (ValueError, OSError) as exc:
                raise AccountConflict("用户绑定文档读取失败或已损坏，保留原件") from exc
            validate_users_doc(data)
            return data

        accounts = list(dict.fromkeys(normalized_uid(aid) for aid in initial_accounts))
        active = normalized_uid(initial_active) if initial_active is not None else None
        if active not in accounts:
            active = accounts[0] if accounts else None
        data = {
            "schema_version": USER_SCHEMA,
            "users": {
                "owner": {
                    "role": "admin",
                    "bound_account_ids": accounts,
                    "active_account_id": active,
                    "unbound_history": {},
                }
            },
        }
        validate_users_doc(data)
        if persist_if_missing:
            self.save(data)
        return data

    def save(self, data: dict) -> None:
        self._check_safe_path(self.path)
        validate_users_doc(data)
        write_private(self.path, copy.deepcopy(data))
        if os.name != "nt":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def ensure_user(self, data: dict, user_id: str, role: str) -> dict:
        uid = normalize_user_id(user_id)
        if role not in ("admin", "user"):
            raise AccountConflict("用户角色无效")
        users = data.setdefault("users", {})
        if uid not in users:
            users[uid] = {
                "role": role,
                "bound_account_ids": [],
                "active_account_id": None,
                "unbound_history": {},
            }
        else:
            users[uid]["role"] = role
        return users[uid]

    def bound_accounts(self, data: dict, user_id: str) -> list[str]:
        uid = normalize_user_id(user_id)
        entry = (data.get("users") or {}).get(uid)
        if not entry:
            return []
        return list(entry.get("bound_account_ids", []))

    def bind_account(
        self,
        data: dict,
        user_id: str,
        role: str,
        account_id: str,
        *,
        select: bool = True,
        label: str = "",
    ) -> None:
        aid = normalized_uid(account_id)
        entry = self.ensure_user(data, user_id, role)
        if aid not in entry["bound_account_ids"]:
            entry["bound_account_ids"].append(aid)
        entry.setdefault("unbound_history", {}).pop(aid, None)
        if select or entry.get("active_account_id") not in entry["bound_account_ids"]:
            entry["active_account_id"] = aid

    def unbind_account(
        self,
        data: dict,
        user_id: str,
        account_id: str,
        *,
        label: str = "",
    ) -> list[str]:
        uid = normalize_user_id(user_id)
        aid = normalized_uid(account_id)
        entry = (data.get("users") or {}).get(uid)
        if entry is not None:
            bound = entry.setdefault("bound_account_ids", [])
            if aid in bound:
                bound.remove(aid)
            history = entry.setdefault("unbound_history", {})
            history[aid] = {
                "account_id": aid,
                "label": str(label or ("UID " + aid)),
                "unbound_at": time.time(),
            }
            if entry.get("active_account_id") == aid:
                entry["active_account_id"] = bound[0] if bound else None
        return self.users_for_account(data, aid)

    def users_for_account(self, data: dict, account_id: str) -> list[str]:
        aid = normalized_uid(account_id)
        return [
            uid
            for uid, entry in (data.get("users") or {}).items()
            if aid in entry.get("bound_account_ids", ())
        ]

    def set_active_account(self, data: dict, user_id: str, account_id: str | None) -> None:
        uid = normalize_user_id(user_id)
        entry = (data.get("users") or {}).get(uid)
        if entry is None:
            raise AccountConflict("用户记录不存在")
        if account_id is None:
            entry["active_account_id"] = None
            return
        aid = normalized_uid(account_id)
        if aid not in entry.get("bound_account_ids", ()):
            raise AccountConflict("账号未绑定给当前用户")
        entry["active_account_id"] = aid

    def get_active_account(self, data: dict, user_id: str) -> str | None:
        uid = normalize_user_id(user_id)
        entry = (data.get("users") or {}).get(uid)
        if not entry:
            return None
        bound = entry.get("bound_account_ids", [])
        active = entry.get("active_account_id")
        if active in bound:
            return active
        return bound[0] if bound else None

    def unbound_history(self, data: dict, user_id: str) -> dict:
        uid = normalize_user_id(user_id)
        entry = (data.get("users") or {}).get(uid)
        if not entry:
            return {}
        return copy.deepcopy(entry.get("unbound_history", {}))

    def notify_file_for(self, user_id: str) -> Path:
        key = user_storage_key(user_id)
        if key == "owner":
            target = self.root / "notify.json"
        else:
            target = self.root / "users" / key / "notify.json"
        self._check_safe_path(target)
        return target
