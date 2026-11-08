"""Schema v2: one atomic document for identity, subscriptions, records and tombstones.

No network, notifications or automatic legacy migration. The service owns the writer lock.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import tempfile

from accountguard import AccountConflict, normalized_uid

SCHEMA = 2
DOCUMENT = "identity-v2.json"
JOURNAL = "identity-migration.json"
LEGACY = ("accounts.json", "subscriptions.json", "records.json")
_WRITERS = set()
FENCE = {"schema_version": 2, "active": "requires-schema-v2",
         "accounts": ["requires-schema-v2"], "requires": DOCUMENT}


class PublicationUncertain(OSError):
    """Rename succeeded but directory durability could not be confirmed."""


def empty_state() -> dict:
    return {"schema_version": SCHEMA, "active_account_id": None, "accounts": {},
            "subscriptions": {}, "records": {}, "pending": [], "tombstones": {},
            "operations": {}, "generation": 0}


def write_private(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(prefix=".identity-", dir=path.parent)
    published = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, path)
        published = True
        sync_directory(path.parent)
    except OSError:
        if published:
            raise PublicationUncertain("身份文档已切换，但持久性确认失败；停止执行并重新读取") from None
        raise
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def sync_directory(path):
    if os.name != "nt":
        directory = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def read_json(path: Path):
    with path.open(encoding="utf-8-sig") as source:
        return json.load(source)


class WriterLock:
    def __init__(self, root):
        self.root = Path(root)
        self.file = None

    def __enter__(self):
        self.root = self.root.resolve()
        if (os.getpid(), str(self.root)) in _WRITERS:
            raise AccountConflict("同一数据目录已存在写入者")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.root / ".identity-writer.lock", os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        self.file = os.fdopen(fd, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                if not self.file.read(1):
                    self.file.write(b"0")
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise AccountConflict("数据目录已有执行者或迁移进程，拒绝第二写入点") from None
        _WRITERS.add((os.getpid(), str(self.root)))
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()
            self.file = None
            _WRITERS.discard((os.getpid(), str(self.root)))


def validate(data: dict) -> None:
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != SCHEMA:
        raise AccountConflict("不支持的数据 schema；请使用对应版本和迁移工具")
    for field in ("accounts", "subscriptions", "records", "tombstones", "operations"):
        if not isinstance(data.get(field), dict):
            raise AccountConflict("身份数据结构无效，停止执行并保留原件")
    if not isinstance(data.get("pending"), list):
        raise AccountConflict("待处理身份数据结构无效")
    if type(data.get("generation")) is not int or data["generation"] < 0:
        raise AccountConflict("数据集代数无效")
    for aid, account in data["accounts"].items():
        if not isinstance(account, dict):
            raise AccountConflict("账号结构无效")
        if normalized_uid(aid) != aid or normalized_uid(account.get("uid")) != aid or account.get("account_id") != aid:
            raise AccountConflict("账号 ID 与 UID 不一致")
        for field in ("sessionid", "deviceid", "user_agent"):
            if not isinstance(account.get(field), str) or not account[field].strip():
                raise AccountConflict("账号缺少有效会话字段")
        if any(not isinstance(account.get(field, ""), str) for field in ("name", "nickname", "mobile")):
            raise AccountConflict("账号资料字段格式无效")
        aliases = account.get("legacy_names", [])
        if not isinstance(aliases, list) or not all(isinstance(value, str) for value in aliases):
            raise AccountConflict("旧名称映射无效")
        for field in ("version", "session_version", "profile_version"):
            value = account.get(field)
            if type(value) is not int or value < 1:
                raise AccountConflict("账号版本无效")
        updated = account.get("profile_updated_at", 0)
        if type(updated) not in (int, float) or not math.isfinite(updated) or updated < 0:
            raise AccountConflict("资料刷新时间无效")
        if aid in data["tombstones"]:
            raise AccountConflict("已删除身份与账号同时存在")
    active = data.get("active_account_id")
    if active is not None and active not in data["accounts"]:
        raise AccountConflict("active_account_id 不存在")
    for field in ("subscriptions", "records"):
        for key, row in data[field].items():
            if not isinstance(row, dict):
                raise AccountConflict("订阅或记录结构无效")
            aid = row.get("account_id")
            if normalized_uid(aid) != aid or key != aid + "|" + str(row.get("tournament_id")):
                raise AccountConflict("订阅或记录的身份键无效")
            if field == "subscriptions" and aid not in data["accounts"]:
                raise AccountConflict("未确认身份不能进入调度集合")
            if "uid" in row and normalized_uid(row["uid"]) != aid:
                raise AccountConflict("记录/订阅的 UID 与 account_id 不一致")
            if "enabled" in row and type(row["enabled"]) is not bool:
                raise AccountConflict("订阅启用标记无效")
    for aid, tombstone in data["tombstones"].items():
        if normalized_uid(aid) != aid or type(tombstone.get("version")) is not int or tombstone["version"] < 1:
            raise AccountConflict("删除版本无效")
        if not isinstance(tombstone.get("nickname", ""), str) or not isinstance(tombstone.get("legacy_names", []), list):
            raise AccountConflict("删除身份的旧名称映射无效")
        if not all(isinstance(value, str) for value in tombstone.get("legacy_names", [])):
            raise AccountConflict("删除身份的旧名称格式无效")


class IdentityStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / DOCUMENT
        self.generation = None

    def load(self, *, initialize=False):
        for name in (DOCUMENT, JOURNAL, *LEGACY):
            if (self.root / name).resolve() != self.root / name:
                raise AccountConflict("身份文件路径经过符号链接，拒绝读取/切换")
        journal = self.root / JOURNAL
        if journal.exists() and read_json(journal).get("stage") not in {"complete", "rolled_back"}:
            raise AccountConflict("身份迁移未完成；先运行离线 recover，不启动调度")
        if self.path.exists():
            if any(not (self.root / name).exists() or read_json(self.root / name) != FENCE for name in LEGACY):
                raise AccountConflict("旧数据封锁标记被改变；可能存在旧写入者，拒绝启用新数据集")
            data = read_json(self.path)
        elif any((self.root / name).exists() for name in LEGACY):
            raise AccountConflict("检测到旧身份数据；先离线预览并应用 M01 迁移")
        else:
            data = empty_state()
            if initialize:
                self._require_writer()
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Fence old readers even for fresh v2 installs.
                for name in LEGACY:
                    write_private(self.root / name, FENCE)
                write_private(self.path, data)
        validate(data)
        self.generation = data["generation"]
        return data

    def save(self, data):
        self._require_writer()
        validate(data)
        if any(not (self.root / name).exists() or read_json(self.root / name) != FENCE for name in LEGACY):
            raise AccountConflict("旧读取器封锁标记已变化，拒绝覆盖数据")
        current = read_json(self.path) if self.path.exists() else None
        if current is not None and current.get("generation") != self.generation:
            raise AccountConflict("数据集已由其他写入者更新，停止覆盖")
        candidate = copy.deepcopy(data)
        candidate["generation"] = (self.generation or 0) + 1
        write_private(self.path, candidate)
        self.generation = candidate["generation"]
        data["generation"] = self.generation

    def _require_writer(self):
        if (os.getpid(), str(self.root)) not in _WRITERS:
            raise AccountConflict("只有持有数据目录执行锁的服务/迁移进程才能写入")


def resolve(accounts, selector, *, explicit=False, expected_uid=None):
    if selector is None or selector == "":
        raise AccountConflict("请明确选择账号 ID")
    if explicit:
        aid = normalized_uid(selector)
        if aid not in accounts:
            raise AccountConflict("账号 ID 不存在或已删除")
    else:
        if not isinstance(selector, str):
            raise AccountConflict("旧名称必须是字符串，请使用明确 account_id")
        matches = {aid for aid, account in accounts.items()
                   if selector in {account.name, account.nickname, *account.legacy_names}}
        # Old pages send the prefix of a v2 subscription key as `account`.
        # Consider both meanings, without preferring a UID over a legacy name.
        try:
            selector_uid = normalized_uid(selector)
        except AccountConflict:
            selector_uid = None
        if selector_uid in accounts:
            matches.add(selector_uid)
        if len(matches) != 1:
            raise AccountConflict("旧账号参数缺失或 UID/名称有歧义，请刷新页面并使用明确 account_id")
        aid = matches.pop()
    if expected_uid is not None and normalized_uid(expected_uid) != aid:
        raise AccountConflict("请求的账号 ID 与预期 UID 不一致")
    return aid
