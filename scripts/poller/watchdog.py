"""名额轮询器看护（Python 版，Windows / Linux 通用）。

为什么需要：
    轮询器一旦静默死掉，你会以为"一直没空位" —— 这是最危险的失败模式。
    本脚本定期检查轮询器是否还在成功采样，超时就发告警。

与 watchdog.ps1 的关系：
    这是它的 Python 移植版，逻辑一一对应，但**不再依赖 PowerShell**，
    因此可以在 Linux 服务器上跑（systemd timer 调用）。
    --agent 使用进度协议和持久化去重；旧轮询器检查只作兼容入口。

用法：
    python watchdog.py                 # 常驻，按 --check-seconds 周期检查
    python watchdog.py --once          # 只检查一次（给 systemd timer 用）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import poll as P  # noqa: E402

COMPLETION = P.sveenv.COMPLETION_FILE
WATCHDOG_LOG = str(P.sveenv.PATHS.logs / "watchdog.log")


def say(msg: str) -> None:
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        from runtime_paths import private_append
        private_append(WATCHDOG_LOG, line + "\n")
    except Exception:
        pass


def poller_pid() -> int | None:
    """读单实例锁里的 pid（没有锁文件就说明轮询器没在常驻运行）。"""
    try:
        with open(P.LOCK, encoding="utf-8") as fh:
            return int((fh.read() or "").strip().split()[0])
    except Exception:
        return None


def run_check(stale_minutes: int) -> bool:
    """执行一次检查。返回 True 表示"需要告警"（采样过期/进程缺失）。"""
    cfg = P.load_json(P.SETTINGS, {}) or {}

    # 任务已完成（报名稳定确认）时轮询器会正常退出，此时【不应】告警，
    # 否则会和"任务完成"的通知同时到达，破坏告警可信度。
    if os.path.exists(COMPLETION):
        c = P.load_json(COMPLETION, {}) or {}
        pid = poller_pid()
        alive = P._pid_alive(pid) if pid else False
        say("任务已完成（%s，赛事 %s，二次确认=%s）。轮询器运行中=%s -> 不告警"
            % (c.get("finished_at"), c.get("tournament_name"),
               c.get("second_confirm_status"), alive))
        return False

    if not os.path.exists(P.STATE):
        say("state.json 不存在: %s" % P.STATE)
        return False
    s = P.load_json(P.STATE, {}) or {}

    stamp = s.get("last_ok_at_local")
    if not stamp:
        say("state.json 缺少 last_ok_at_local（轮询器可能是旧版本）")
        return False
    try:
        last = datetime.strptime(str(stamp), "%Y-%m-%d %H:%M:%S")
    except Exception:
        say("时间解析失败: %r" % stamp)
        return False

    age_min = round((datetime.now() - last).total_seconds() / 60.0, 1)
    pid = poller_pid()
    alive = P._pid_alive(pid) if pid else False

    # 关键：轮询器在非报名时段会按设计休眠（state 不再更新），这不是故障。
    # 若不判断时段，每天从 24:00 到次日 16:00 会一直误报"监控已中断"。
    in_window = P.in_active_window(cfg)

    if not in_window:
        if not alive:
            say("非报名时段且轮询器进程不在（pid=%s），如需持续值守请重启轮询器" % pid)
            P.send_problem_notification(
                cfg, "轮询器已停止（非报名时段）",
                "当前不在报名时段（%s~%s），但轮询器进程不存在。"
                "若这是你有意关闭的可忽略；否则请在开窗时间前启动，以免错过名额。"
                % (cfg.get("active_start"), cfg.get("active_end")))
            return True
        say("非报名时段，轮询器静默属正常（最近采样 %s 分钟前，进程存活=True）" % age_min)
        return False

    if age_min > stale_minutes:
        say("!! 已 %s 分钟没有成功采样（阈值 %s），进程存活=%s" % (age_min, stale_minutes, alive))
        P.send_problem_notification(
            cfg, "监控可能已中断",
            "已 %s 分钟没有成功采样，监控进程存活=%s。请检查轮询器、网络与会话是否失效。"
            % (age_min, alive))
        return True

    say("正常：最近一次成功采样 %s 分钟前，player_num=%s/%s available=%s session_ok=%s"
        % (age_min, s.get("player_num"), s.get("max_player_number"),
           s.get("available"), s.get("session_ok")))
    return False


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
    import notify
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
    cfg = P.load_json(P.SETTINGS, {}) or {}
    location = Path(P.sveenv.DATA_DIR) / "watchdog"
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
                accepted = (send or send_events)(events, channels(P.SETTINGS, Path(P.sveenv.DATA_DIR) / "notify.json"))
            except Exception:
                accepted = False
            if accepted:
                monitor.accepted(events)
                write_private(state_file, monitor.value)
            say("看护状态变化 %d 项；通知渠道接受=%s" % (len(events), bool(accepted)))
        return bool(monitor.value["faults"])


def main() -> int:
    ap = argparse.ArgumentParser(description="名额轮询器 / 自动报名代理 看护")
    ap.add_argument("--check-seconds", type=int, default=60, help="常驻模式的检查周期（秒）")
    ap.add_argument("--stale-minutes", type=int, default=10, help="多久没采样判定为中断（分钟）")
    ap.add_argument("--once", action="store_true", help="只检查一次（systemd timer 用）")
    ap.add_argument("--agent", action="store_true",
                    help="改为检查自动报名代理（sve-agent）而不是轮询器")
    args = ap.parse_args()

    check = run_agent_check if args.agent else run_check

    if args.once:
        try:
            check() if args.agent else check(args.stale_minutes)
            return 0
        except Exception:
            say("看护检查未完成；保留状态，检查私有配置、权限和写锁")
            return 1

    say("=== 看护进程启动（%s，周期 %ds）==="
        % ("代理模式" if args.agent else "轮询器模式", args.check_seconds))
    alerted = False
    while True:
        try:
            stale = check() if args.agent else check(args.stale_minutes)
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
