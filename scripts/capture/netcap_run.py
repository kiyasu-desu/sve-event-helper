"""Attach the in-app network capture payload to 《口袋影之诗》 and record traffic.

No proxy is used: the payload hooks OkHttp/HttpURLConnection inside the app and
sends each request/response to this script, which appends them to
capture/net_capture.jsonl.

Usage:
    python scripts/capture/netcap_run.py --attach        # attach to the running app
    python scripts/capture/netcap_run.py --watch         # wait for the app, then attach
    python scripts/capture/netcap_run.py --spawn         # start the app under frida
    python scripts/capture/netcap_run.py --attach --seconds 3600
"""
import argparse
import json
import os
import subprocess
import sys
import time

try:
    import frida
except ImportError:
    sys.exit("frida is not installed in this interpreter")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
# 路径与工具统一来源（adb 路径此前在本文件里硬编码过）
sys.path.insert(0, os.path.abspath(os.path.join(PROJECT_ROOT, "scripts")))
import sveenv  # noqa: E402

PAYLOAD = os.path.join(PROJECT_ROOT, "capture", "netcap.bundle.js")
# 多实例支持：不同模拟器用不同输出文件，避免相互覆盖（--out 可覆盖）
OUT_FILE = os.path.join(PROJECT_ROOT, "capture", "net_capture.jsonl")
PACKAGE = "com.shadowversecard"

def append_jsonl(rec: dict) -> None:
    with open(OUT_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def make_handler():
    def on_message(message, data):
        if message.get("type") == "send":
            payload = message.get("payload")
            if isinstance(payload, str) and payload.startswith("{"):
                try:
                    rec = json.loads(payload)
                except Exception:
                    print("[raw] %s" % payload, flush=True)
                    return
                rec["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
                append_jsonl(rec)
                print("[net] %s %s -> %s"
                      % (rec.get("method"), rec.get("url"), rec.get("status")), flush=True)
            else:
                print("[payload] %s" % payload, flush=True)
        elif message.get("type") == "error":
            print("[payload:error] %s" % (message.get("stack") or message.get("description")), flush=True)
        else:
            print("[payload:?] %s" % message, flush=True)
    return on_message


def load_payload(device, pid, source, on_message):
    session = device.attach(pid)
    script = session.create_script(source)
    script.on("message", on_message)
    script.load()
    return session


def find_pid(device):
    """Resolve the app pid.

    Frida's enumerate_processes() does not always list app processes on MuMu, so
    adb shell pidof is the primary source and frida is only the fallback.
    """
    pid = adb_pidof()
    if pid:
        return pid
    for proc in device.enumerate_processes():
        if proc.name == PACKAGE:
            return proc.pid
    return 0


def adb_pidof() -> int:
    # adb 与默认序列号统一来自 scripts/sveenv.py（--serial 可覆盖）
    adb = sveenv.ADB
    serial = os.environ.get("SVE_SERIAL", sveenv.DEFAULT_SERIAL)
    if not adb or not os.path.exists(adb):
        return 0
    try:
        out = subprocess.run([adb, "-s", serial, "shell", "pidof", PACKAGE],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return 0
    out = out.strip().split()
    if not out:
        return 0
    try:
        return int(out[0])
    except ValueError:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    # global 必须出现在任何对 OUT_FILE 的引用之前，否则导入时就报
    # "name 'OUT_FILE' is used prior to global declaration"
    global OUT_FILE

    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default="127.0.0.1:27043")
    ap.add_argument("--payload", default=PAYLOAD)
    ap.add_argument("--out", default=OUT_FILE, help="输出 jsonl 路径（多实例时区分）")
    ap.add_argument("--serial", default=os.environ.get("SVE_SERIAL", sveenv.DEFAULT_SERIAL),
                    help="用于 adb pidof 的设备序列号")
    ap.add_argument("--seconds", type=int, default=3600)
    ap.add_argument("--attach", action="store_true", help="attach to the running app")
    ap.add_argument("--watch", action="store_true", help="wait until the app appears, then attach")
    ap.add_argument("--spawn", action="store_true", help="start the app under frida")
    ap.add_argument("--pid", type=int, default=0)
    args = ap.parse_args()

    OUT_FILE = args.out
    os.environ["SVE_SERIAL"] = args.serial

    with open(args.payload, encoding="utf-8") as fh:
        source = fh.read()

    print("[netcap] connecting to %s" % args.addr, flush=True)
    device = frida.get_device_manager().add_remote_device(args.addr)
    on_message = make_handler()
    session = None
    pid = args.pid

    if args.spawn:
        pid = device.spawn([PACKAGE])
        print("[netcap] spawned pid %d" % pid, flush=True)
        session = load_payload(device, pid, source, on_message)
        device.resume(pid)
        print("[netcap] resumed; app starting under capture", flush=True)
    elif args.watch:
        print("[netcap] waiting for %s to start ..." % PACKAGE, flush=True)
        deadline = time.time() + args.seconds
        while time.time() < deadline:
            pid = find_pid(device)
            if pid:
                break
            time.sleep(1)
        if not pid:
            print("[netcap] app never appeared", flush=True)
            return 1
        print("[netcap] found pid %d, attaching" % pid, flush=True)
        session = load_payload(device, pid, source, on_message)
    else:
        if not pid:
            pid = find_pid(device)
        if not pid:
            print("[netcap] app not running (use --watch or --spawn)", flush=True)
            return 1
        print("[netcap] attaching to pid %d" % pid, flush=True)
        session = load_payload(device, pid, source, on_message)

    print("[netcap] capturing to %s" % OUT_FILE, flush=True)
    deadline = time.time() + args.seconds
    try:
        while time.time() < deadline:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[netcap] interrupted", flush=True)
    finally:
        try:
            session.detach()
        except Exception:
            pass
        print("[netcap] detached", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
