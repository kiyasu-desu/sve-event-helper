"""Python 侧的共享配置：路径与工具的单一来源。

对应 PowerShell 侧的 scripts/config.ps1。此前 adb 路径在 4 个文件里各写一遍，
这里收敛为一处；同时统一支持环境变量覆盖，便于迁移到其它机器/服务器。

环境变量（都可选）：
    SVE_ADB       adb 可执行文件路径
    SVE_SERIAL    默认设备序列号（默认 sve 实例）
    SVE_SVE_SERIAL / SVE_SVE2_SERIAL / SVE_PJSK_SERIAL
                  覆盖各实例的 adb host:port
    SVE_DATA_DIR  运行时数据目录（账号/订阅等）。把它指向仓库外，
                  同时派生 settings、capture、logs 和部署备份路径。
    SVE_SESSION_FILE
                  显式指定会话文件（调试/迁移用），优先级最高
"""
from __future__ import annotations

import json
import os
import shutil

# 本文件位于 scripts/ 下，项目根目录是它的上一级
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# ------------------------------------------------------------------ 模拟器实例
#   sve  -> 16384  监控用（大奖赛报名监控跑在这个实例上）
#   sve2 -> 16448  第二账号：验证登录流程，不影响 sve 的会话
#   pjsk -> 16416  其它用途，本项目不要碰
SVE_SERIAL = os.environ.get("SVE_SVE_SERIAL", "127.0.0.1:16384")
SVE2_SERIAL = os.environ.get("SVE_SVE2_SERIAL", "127.0.0.1:16448")
PJSK_SERIAL = os.environ.get("SVE_PJSK_SERIAL", "127.0.0.1:16416")
DEFAULT_SERIAL = os.environ.get("SVE_SERIAL", SVE_SERIAL)

_ADB_CANDIDATES = [
    r"E:\MuMuPlayer\nx_main\adb.exe",
    r"C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe",
]


def find_adb() -> str:
    """按 环境变量 → 候选路径 → PATH 的顺序解析 adb。

    返回**字符串**（找不到时返回空串，而不是 None）：
    调用方会用 os.path.exists(ADB) 判断，若返回 None 会抛 TypeError。
    在 Linux 上没有 MuMu，ADB 通常为空——这是允许的，
    因为常驻监控本身不需要 adb（只有"从模拟器恢复会话"才需要）。
    """
    env = os.environ.get("SVE_ADB")
    if env and os.path.exists(env):
        return env
    for cand in _ADB_CANDIDATES:
        if os.path.exists(cand):
            return cand
    found = shutil.which("adb.exe") or shutil.which("adb")
    return found or ""


ADB = find_adb()
HAS_ADB = bool(ADB)
# 会话恢复是否可用（需要 adb + 一台登录着 APP 的设备）。
# Linux 服务器上通常为 False，此时改用"从本机迁移 session.json"的方式。
DEVICE_RECOVERY_AVAILABLE = HAS_ADB

# ------------------------------------------------------------------ 目录
import sys
sys.path.insert(0, os.path.join(PROJECT_ROOT, "app", "client", "core"))
from runtime_paths import RuntimePaths
PATHS = RuntimePaths(PROJECT_ROOT)
PATHS.assert_ready()
CAPTURE_DIR = str(PATHS.capture)
RECON_DIR = os.path.join(PROJECT_ROOT, "recon")
CONFIG_DIR = str(PATHS.settings.parent)
DATA_DIR = str(PATHS.data)
IDENTITY_FILE = os.path.join(DATA_DIR, "identity-v2.json")
ACCOUNTS_FILE = IDENTITY_FILE
LEGACY_SESSION_FILE = os.path.join(CAPTURE_DIR, "session.json")
SESSION_FILE = IDENTITY_FILE
STATE_FILE = os.path.join(CAPTURE_DIR, "state.json")
SAMPLES_FILE = os.path.join(CAPTURE_DIR, "samples.jsonl")
POLL_LOG = str(PATHS.logs / "poll.log")
COMPLETION_FILE = os.path.join(CAPTURE_DIR, "completed.json")
SETTINGS_FILE = str(PATHS.settings)

# APP 内部路径
PKG = "com.shadowversecard"
RK_STORAGE = "/data/data/%s/databases/RKStorage" % PKG


# ------------------------------------------------------------------ 账号 / 会话
# 权威来源为 DATA_DIR/identity-v2.json，按 account_id 读取。
# 旧 accounts.json/subscriptions.json/records.json 仅保留 schema 封锁标记。
# 退役脚本只读；写入必须经过服务端版本/身份校验。
def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def _v2_store():
    from pathlib import Path
    import sys
    core = os.path.join(PROJECT_ROOT, "app", "client", "core")
    if core not in sys.path:
        sys.path.insert(0, core)
    from identity_store import IdentityStore
    return IdentityStore(Path(DATA_DIR))


def load_accounts() -> dict:
    """Read schema v2 only. Never silently fall back to another dataset/schema."""
    return _v2_store().load()


def load_session(account: str | None = None) -> dict:
    """Read by canonical account_id. Explicit unknown IDs never use active/legacy fallback."""
    from pathlib import Path
    if (Path(DATA_DIR) / "identity-v2.json").exists():
        data = load_accounts()
        aid = account if account is not None else data.get("active_account_id")
        if aid is None:
            return {}
        from accountguard import normalized_uid, AccountConflict
        try:
            aid = normalized_uid(aid)
        except AccountConflict:
            return {}
        if aid not in data["accounts"]:
            return {}
        return dict(data["accounts"][aid])
    if any((Path(DATA_DIR) / name).exists() for name in ("accounts.json", "identity-migration.json")):
        raise RuntimeError("旧身份数据或未完成迁移；先预览 M01 迁移，不使用旧会话兜底")
    if account is not None:
        return {}
    env = os.environ.get("SVE_SESSION_FILE")
    return _read_json(env) if env else {}


def save_session(session: dict, account: str | None = None) -> None:
    """Retired scripts must not write the authoritative v2 dataset or legacy files."""
    raise RuntimeError("会话写入已收敛到 UID 服务；请通过明确登录提交，禁止旧脚本绕过版本和身份校验")


def assert_legacy_executor_allowed() -> None:
    from pathlib import Path
    if any((Path(DATA_DIR) / name).exists() for name in ("identity-v2.json", "identity-migration.json")):
        raise RuntimeError("已启用/正在迁移 UID 数据集，旧轮询器和直连报名脚本不得成为第二执行点；请使用 UID 服务")


def describe() -> dict:
    return {
        "project_root": PROJECT_ROOT,
        "data_dir": DATA_DIR,
        "accounts_file": ACCOUNTS_FILE,
        "adb": ADB,
        "sve": SVE_SERIAL,
        "sve2": SVE2_SERIAL,
        "pjsk": PJSK_SERIAL,
    }


if __name__ == "__main__":
    for k, v in describe().items():
        print("  %-14s %s" % (k, v))
