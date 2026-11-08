"""挂载 WebView 回调探针（scripts/capture/wvhook.js）。

与 netcap_run.py 的区别：
  - netcap 用 esbuild 打包成 bundle；wvhook.js 是独立脚本，直接读源码即可
  - 本脚本把探针消息既打到 stdout，也写进日志文件

用法:
    python scripts/capture/wvhook_run.py --serial 127.0.0.1:16448 --addr 127.0.0.1:27044
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import frida

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
import sveenv  # noqa: E402

PACKAGE = sveenv.PKG
# 必须加载打包后的 bundle：Frida 17 无内置 Java bridge，且 payload 侧没有 require，
# 所以 wvhook.js 里的 import 只能靠 esbuild 内联（见 build_payload.js）。
SCRIPT = os.path.join(PROJECT_ROOT, "capture", "wvhook.bundle.js")


def adb_pidof(adb: str, serial: str) -> int:
    if not adb or not os.path.exists(adb):
        return 0
    try:
        out = subprocess.run([adb, "-s", serial, "shell", "pidof", PACKAGE],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return 0
    parts = out.strip().split()
    return int(parts[0]) if parts else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default="127.0.0.1:27044")
    ap.add_argument("--serial", default=sveenv.SVE2_SERIAL)
    ap.add_argument("--log", default=os.path.join(sveenv.CAPTURE_DIR, "wvhook.log"))
    ap.add_argument("--seconds", type=int, default=1800)
    args = ap.parse_args()

    logf = open(args.log, "a", encoding="utf-8")

    def emit(line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        text = "[%s] %s" % (stamp, line)
        print(text, flush=True)
        logf.write(text + "\n")
        logf.flush()

    pid = adb_pidof(sveenv.ADB, args.serial)
    if not pid:
        emit("未找到 APP 进程（%s 上 %s 没在运行？）" % (args.serial, PACKAGE))
        return 2
    emit("APP pid=%d，连接 frida %s" % (pid, args.addr))

    device = frida.get_device_manager().add_remote_device(args.addr)
    session = device.attach(pid)

    with open(SCRIPT, encoding="utf-8") as fh:
        source = fh.read()

    def on_message(message, data):
        if message.get("type") == "send":
            p = message.get("payload")
            if isinstance(p, dict) and p.get("kind") == "wvhook":
                emit(p.get("msg"))
            else:
                emit("send: %s" % p)
        elif message.get("type") == "error":
            emit("ERROR: %s" % message.get("description"))

    script = session.create_script(source)
    script.on("message", on_message)
    script.load()
    emit("探针已加载，等待 WebView 活动（最多 %d 秒）" % args.seconds)

    deadline = time.time() + args.seconds
    try:
        while time.time() < deadline:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    emit("探针退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
