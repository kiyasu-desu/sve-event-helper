"""Spawn 《口袋影之诗》 under Frida and keep the unpinning payload attached.

Why spawn instead of attach: the hooks must exist before the app's first HTTPS
request, otherwise the early calls still fail against our proxy certificate.

Usage:
    python scripts/capture/frida_run.py [--addr 127.0.0.1:27043] [--pid PID] [--seconds 900]

Behaviour:
    default  : spawn com.shadowversecard, load scripts/capture/unpin.js, resume, stream logs
    --pid N  : attach to an already running process instead of spawning
    The process stays attached until you press Ctrl+C or --seconds elapses.

Everything it prints is prefixed so the log can be grepped for 'unpin:' lines.
"""
import argparse
import os
import sys
import time

try:
    import frida
except ImportError:
    sys.exit("frida is not installed in this interpreter (pip install frida)")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
# Must be the BUNDLED payload: Frida 17 no longer injects the Java bridge, and
# the raw unpin.js/payload.js sources rely on that bridge being present.
DEFAULT_PAYLOAD = os.path.join(PROJECT_ROOT, "capture", "payload.bundle.js")
PACKAGE = "com.shadowversecard"


def on_message(message, data):
    if message.get("type") == "send":
        print("[payload] %s" % message.get("payload"), flush=True)
    elif message.get("type") == "error":
        print("[payload:error] %s" % (message.get("stack") or message.get("description")), flush=True)
    else:
        print("[payload:?] %s" % message, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default="127.0.0.1:27043",
                    help="host address of the adb-forwarded frida-server port")
    ap.add_argument("--package", default=PACKAGE)
    ap.add_argument("--payload", default=DEFAULT_PAYLOAD)
    ap.add_argument("--pid", type=int, default=0, help="attach to this pid instead of spawning")
    ap.add_argument("--seconds", type=int, default=1800, help="give up after this long")
    args = ap.parse_args()

    with open(args.payload, encoding="utf-8") as fh:
        source = fh.read()

    print("[frida] connecting to %s" % args.addr)
    device = frida.get_device_manager().add_remote_device(args.addr)

    if args.pid:
        print("[frida] attaching to pid %d" % args.pid)
        session = device.attach(args.pid)
        script = session.create_script(source)
        script.on("message", on_message)
        script.load()
        print("[frida] attached; press Ctrl+C to detach")
    else:
        print("[frida] spawning %s" % args.package)
        pid = device.spawn([args.package])
        print("[frida] spawned pid %d" % pid)
        session = device.attach(pid)
        script = session.create_script(source)
        script.on("message", on_message)
        script.load()
        device.resume(pid)
        print("[frida] resumed; app should now start")

    deadline = time.time() + args.seconds
    try:
        while time.time() < deadline:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[frida] interrupted")
    finally:
        try:
            session.detach()
        except Exception:
            pass
        print("[frida] detached")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
