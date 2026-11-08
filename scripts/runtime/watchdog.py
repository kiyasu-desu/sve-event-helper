"""Agent progress watchdog. Shares persistent state and lock with the legacy forwarding path."""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app/client/core"))
from runtime_paths import RuntimePaths
PATHS = RuntimePaths(ROOT)
SETTINGS = str(PATHS.settings)
DATA_DIR = str(PATHS.data)
WATCHDOG_LOG = str(PATHS.logs / "watchdog.log")


def load_settings():
    import json
    PATHS.assert_ready()
    path = Path(SETTINGS)
    value = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if not isinstance(value, dict):
        raise ValueError("Invalid settings object")
    return value


def say(msg: str) -> None:
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        from runtime_paths import private_append
        private_append(WATCHDOG_LOG, line + "\n")
    except Exception:
        pass


AGENT_URL = os.environ.get("SVE_AGENT_URL", "http://127.0.0.1:8901")


def fetch_progress():
    """Local protocol only; no redirects or environment proxy routing."""
    import json
    import urllib.parse
    import urllib.request
    url = urllib.parse.urlsplit(AGENT_URL)
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}):
        raise ValueError("Watchdog requires a loopback agent URL")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(AGENT_URL.rstrip("/") + "/api/scheduler/health", timeout=8) as response:
        content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("Progress response too large")
        return json.loads(content.decode("utf-8"))


def boot_marker():
    from pathlib import Path
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        # On other platforms a monotonic clock reset is detected by Monitor.
        return "monotonic-clock"


def send_events(events, channels):
    from html import escape
    import notification_sender as notify
    labels = {"worker_dead": "调度线程已退出或缺失", "stalled": "调度阶段超过推进期限",
              "upstream_unavailable": "上游请求持续失败（线程仍在推进）", "orphan": "订阅账号缺失",
              "identity_blocked": "账号身份待核对", "agent_unreachable": "代理接口不可达",
              "protocol_unsupported": "服务端尚不支持调度进度协议", "protocol_invalid": "调度健康响应无效",
              "storage_unavailable": "代理存储未就绪", "not_executor": "看护目标不是执行端"}
    rows = []
    for event in events[:30]:
        detail = labels[event["kind"]]
        if "account_id" in event:
            detail += "；账号 %s，赛事 %s" % (event["account_id"], event["tournament_id"])
        rows.append("<li>%s：%s</li>" % ("已解除或任务已结束" if event["recovered"] else "异常", escape(detail)))
    if len(events) > 30:
        rows.append("<li>其余 %d 项已聚合</li>" % (len(events) - 30))
    accepted = notify.notify_all(channels, "SVE 看护：%d 项状态变化" % len(events),
                                 "<ul>" + "".join(rows) + "</ul>", quiet=True, attempts=1)
    return notify.any_ok(accepted)


def run_agent_check(*, fetch=None, send=None, now=None, boot=None) -> bool:
    """Observe only. Persist confirmation/backoff before any notification send."""
    import json
    from pathlib import Path
    import urllib.error
    from identity_store import WriterLock, write_private
    from notification_config import channels
    from watchdog_state import Monitor, faults
    cfg = load_settings()
    location = Path(DATA_DIR) / "watchdog"
    state_file = location / "watchdog-state.json"
    if location.is_symlink() or state_file.is_symlink():
        raise ValueError("Watchdog storage symlink refused")
    with WriterLock(location):
        if os.name != "nt" and location.stat().st_mode & 0o077:
            raise ValueError("Watchdog directory must be private")
        value = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else None
        monitor = Monitor(value, cfg.get("watchdog") or {})
        error, snapshot = None, None
        try:
            snapshot = (fetch or fetch_progress)()
            faults(snapshot, monitor.config)  # Validate before changing stored observations.
        except urllib.error.HTTPError as exc:
            error = "protocol_unsupported" if exc.code == 404 else "agent_unreachable"
            exc.close()
        except (ValueError, TypeError, KeyError, AttributeError):
            error = "protocol_invalid"
        except Exception:
            error = "agent_unreachable"
        events = monitor.observe(snapshot, now=time.monotonic() if now is None else now,
                                 boot=boot_marker() if boot is None else boot, error=error)
        write_private(state_file, monitor.value)  # Reserve retries even if send crashes.
        if events:
            try:
                accepted = (send or send_events)(events, channels(SETTINGS, Path(DATA_DIR) / "notify.json"))
            except Exception:
                accepted = False
            if accepted:
                monitor.accepted(events)
                write_private(state_file, monitor.value)
            say("看护状态变化 %d 项；通知渠道接受=%s" % (len(events), bool(accepted)))
        return bool(monitor.value["faults"])


def main() -> int:
    ap = argparse.ArgumentParser(description="自动报名代理看护")
    ap.add_argument("--check-seconds", type=int, default=60, help="常驻模式的检查周期（秒）")
    ap.add_argument("--once", action="store_true", help="只检查一次（systemd timer 用）")
    ap.add_argument("--agent", action="store_true",
                    help="兼容旧 unit 参数；始终检查 sve-agent")
    args = ap.parse_args()
    if args.check_seconds < 1:
        ap.error("--check-seconds must be positive")

    check = run_agent_check

    if args.once:
        try:
            check()
            return 0
        except Exception:
            say("看护检查未完成；保留状态，检查私有配置、权限和写锁")
            return 1

    say("=== 看护进程启动（%s，周期 %ds）==="
        % ("代理模式", args.check_seconds))
    alerted = False
    while True:
        try:
            stale = check()
        except Exception as e:
            say("看护检查未完成；保留状态，检查私有配置、权限和写锁")
            stale = False
        if stale and not alerted:
            alerted = True
        elif not stale and alerted:
            say("已恢复")
            alerted = False
        time.sleep(args.check_seconds)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
