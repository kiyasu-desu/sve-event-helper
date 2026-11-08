"""会话管理：从模拟器里的 APP 恢复最新会话（自动重新登录）。

背景（已从 bundle 与设备实测确认）：
  * APP 把登录数据存在 AsyncStorage 的 `loginRn` 键里，内容包含 `session_id`；
  * `loginRn` 存在 /data/data/com.shadowversecard/databases/RKStorage（SQLite）；
  * 真正的账号登录需要 NetEase 账号凭证（nt_login_md5 + 网页登录流程），
    APP 并不持久化账号密码 —— 所以"用密码自动重登"在架构上不成立。

因此自动重新登录的实现方式为：
    检测到会话失效 → 从设备读取 APP 当前保存的 session_id → 校验 → 写回 session.json
即"让脚本始终跟随 APP 的登录态"。只要 APP 还处于登录状态（哪怕它自己刷新过 session），
脚本就能自动恢复，无需人工介入。

如果设备/APP 不可用，则回退到 Frida RPC（需 frida-server 在跑）。
若两条路都失败，向上报错，由调用方告警。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

import requests

# 路径与工具统一从 scripts/sveenv.py 读取（adb 路径此前在本文件里硬编码过）
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))
import sveenv  # noqa: E402

PROJECT_ROOT = sveenv.PROJECT_ROOT
SNAPSHOT = os.path.join(sveenv.CAPTURE_DIR, "RKStorage.db")

ADB = sveenv.ADB
SERIAL = sveenv.DEFAULT_SERIAL
PKG = sveenv.PKG
RK_PATH = sveenv.RK_STORAGE
BASE = "https://api-sve.nie.netease.com/sve/api/v1"
TIMEOUT = 20


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


def ensure_device() -> None:
    """确保 adb 可用且设备在线（幂等）。"""
    if not os.path.exists(ADB):
        raise RuntimeError("adb 不存在: %s" % ADB)
    out = _run([ADB, "-s", SERIAL, "shell", "echo ok"])
    if "ok" not in (out.stdout or ""):
        raise RuntimeError("设备不可用: %s" % SERIAL)
    # 读 /data/data 需要 root
    who = _run([ADB, "-s", SERIAL, "shell", "id"]).stdout or ""
    if "uid=0" not in who:
        _run([ADB, "-s", SERIAL, "root"])
        import time
        time.sleep(3)


def read_app_session_from_storage() -> str:
    """从设备的 AsyncStorage 读取 APP 当前保存的 session_id。"""
    ensure_device()
    # 拉到工作区再解析，避免 adb shell 的引号地狱
    target = SNAPSHOT
    res = _run([ADB, "-s", SERIAL, "pull", RK_PATH, target])
    if not os.path.exists(target):
        raise RuntimeError("拉取 RKStorage 失败: %s" % (res.stderr or res.stdout))

    tmp = target + ".copy"
    shutil.copyfile(target, tmp)          # 避开 -journal 锁
    try:
        con = sqlite3.connect(tmp)
        cur = con.cursor()
        cur.execute("SELECT value FROM catalystLocalStorage WHERE key='loginRn'")
        row = cur.fetchone()
        con.close()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    if not row:
        raise RuntimeError("AsyncStorage 中没有 loginRn 键（APP 可能未登录）")
    data = json.loads(row[0])
    sid = data.get("session_id") or ""
    if not sid:
        raise RuntimeError("loginRn 中没有 session_id")
    return sid


def read_app_session_from_frida(addr: str = "127.0.0.1:27043") -> str:
    """备选路径：通过 Frida RPC 让 APP 自己读出 loginRn（需要 frida-server 在跑）。"""
    try:
        import frida
    except ImportError:
        raise RuntimeError("未安装 frida")

    bundle = os.path.join(sveenv.CAPTURE_DIR, "netcap.bundle.js")
    if not os.path.exists(bundle):
        raise RuntimeError("缺少 capture/netcap.bundle.js")

    out = _run([ADB, "-s", SERIAL, "shell", "pidof", PKG])
    pid_text = (out.stdout or "").strip().split()
    if not pid_text:
        raise RuntimeError("APP 未运行")
    pid = int(pid_text[0])

    device = frida.get_device_manager().add_remote_device(addr)
    session = device.attach(pid)
    try:
        with open(bundle, encoding="utf-8") as fh:
            script = session.create_script(fh.read())
        script.load()
        import time
        time.sleep(3)
        sid = script.exports_sync.read_storage("loginRn")
        if not sid:
            raise RuntimeError("Frida 读取 loginRn 为空")
        data = json.loads(sid) if isinstance(sid, str) and sid.startswith("{") else {"session_id": sid}
        return data.get("session_id", "")
    finally:
        try:
            session.detach()
        except Exception:
            pass


def validate_session(session: dict) -> dict | None:
    """用会话请求 /urs/info，成功则返回服务端用户信息，失败返回 None。"""
    from signer import generate_sign  # 惰性导入，避免循环依赖
    headers = {
        "content-type": "application/json",
        "deviceid": session.get("deviceid", ""),
        "platform": "android",
        "sign": generate_sign({}, udid=session.get("deviceid", "udid")),
        "sessionid": session["sessionid"],
        "User-Agent": "okhttp/4.9.2",
    }
    try:
        r = requests.get("%s/urs/info" % BASE, headers=headers, timeout=TIMEOUT)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    body = r.json()
    if body.get("status") != "success":
        return None
    return body.get("item") or {}


def load_local_session() -> dict:
    """本地会话来自统一入口（UID 数据集的默认 active_account_id）。

    历史上这里读 capture/session.json；现在收敛到单一来源，
    旧数据必须离线迁移，不能在 UID 数据集中回退旧会话。
    """
    return sveenv.load_session()


def save_session(session: dict) -> None:
    """写回统一来源（accounts.json），而不是另存一份 session.json。"""
    sveenv.save_session(session)


def recover_session() -> tuple[bool, str]:
    """尝试恢复会话。返回 (是否成功, 说明)。成功时写回统一来源 accounts.json。"""
    local = load_local_session()
    if not local.get("deviceid"):
        return False, "取不到会话（UID 数据集里没有可用账号），无法构造请求头"

    # 先确认本地会话是不是真的还能用（避免误判导致无谓恢复）
    if local.get("sessionid") and validate_session(local):
        return True, "本地会话仍然有效，无需恢复"

    # 没有 adb（典型情况：Linux 服务器上没有 MuMu 模拟器）时，
    # 下面的设备读取必然失败。直接给出可操作的说明，而不是抛一堆
    # "adb 不存在" 的异常——后者会让人误以为是代码坏了。
    if not getattr(sveenv, "HAS_ADB", False):
        return False, (
            "本机没有 adb / 模拟器，无法从设备恢复会话。"
            "请通过 UID 服务明确重新登录提交单个会话；"
            "禁止把本机账号表或导出的会话文件覆盖服务器身份数据集。")

    errors = []
    for label, reader in (("AsyncStorage", read_app_session_from_storage),
                          ("Frida", read_app_session_from_frida)):
        try:
            sid = reader()
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s" % (label, exc))
            continue
        if not sid:
            errors.append("%s: 读到空 session_id" % label)
            continue
        candidate = dict(local)
        candidate["sessionid"] = sid
        info = validate_session(candidate)
        if info:
            save_session(candidate)
            return True, ("已从 %s 恢复会话（用户 %s / id=%s）"
                          % (label, info.get("nickname"), info.get("id")))
        errors.append("%s: 读到的 session_id 已被服务端拒绝" % label)

    return False, "恢复失败 -> " + " | ".join(errors)


if __name__ == "__main__":
    import argparse

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        description="会话管理：验证/恢复会话，或导出以迁移到其它机器")
    ap.add_argument("--export", action="store_true",
                    help="把本地 session.json 原样打印到 stdout（用于迁移到 Linux 服务器）")
    ap.add_argument("--recover", action="store_true",
                    help="尝试从设备（模拟器）恢复会话。默认行为。")
    args = ap.parse_args()

    if args.export:
        # 用途：在 Windows 上执行并把输出重定向到服务器上的 capture/session.json
        #   ssh user@server 'cat > /opt/sve-monitor/capture/session.json' < <(python -m ... --export)
        # 注意：输出含 sessionid（等同密码），不要落到日志或聊天记录里。
        print(json.dumps(load_local_session(), ensure_ascii=False, indent=2))
        raise SystemExit(0)

    ok, detail = recover_session()
    print(("[OK] " if ok else "[FAIL] ") + detail)
    local = load_local_session()
    print("当前本地会话 sessionid: %s..." % (local.get("sessionid", "")[:12] or "(空)"))
    raise SystemExit(0 if ok else 1)
