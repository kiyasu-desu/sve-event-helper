"""SVE 广州超级大奖赛 —— 名额轮询器。

目标：在报名时段内以人肉刷新的频率拉取赛事详情，一旦从"满员"变为"有空位"，
立即通过 PushPlus + 邮件通知，由人工完成报名。

用法：
    python scripts/poller/poll.py --once          # 只拉一次并打印结果（排查用）
    python scripts/poller/poll.py --test-notify   # 拉一次 + 发一条通道自检通知
    python scripts/poller/poll.py                 # 常驻轮询（建议 16:00 前启动）
    python scripts/poller/poll.py --ignore-window # 忽略时段限制（排查用）

落盘：
    capture/state.json      当前状态（重启不误报）
    capture/samples.jsonl   每次采样（可复盘空位出现时间与存活时长）
    capture/poll.log        运行日志

判定：available <=> player_num < max_player_number
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import random
import sys
import time
from datetime import datetime, time as dtime

import requests

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts", "poller"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from notify import notify_all, any_ok, render_event_html  # noqa: E402
import sveenv  # noqa: E402

SETTINGS = sveenv.SETTINGS_FILE
# 会话统一走 sveenv.load_session()（单一来源 = identity-v2.json；旧 capture/session.json
# 仅作迁移兜底）。SESSION_ACCOUNT 由 --account 设置，None 表示跟随 active 账号。
SESSION_ACCOUNT: str | None = None
STATE = sveenv.STATE_FILE
SAMPLES = sveenv.SAMPLES_FILE
LOGFILE = sveenv.POLL_LOG
# 常驻模式的单实例锁：避免重复启动导致重复请求/重复报名
LOCK = os.path.join(sveenv.CAPTURE_DIR, "poller.pid")

BASE = "https://api-sve.nie.netease.com/sve/api/v1"
TIMEOUT = 20


# ------------------------------------------------------------------ 工具
def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str, mirror: bool = True) -> None:
    line = "[%s] %s" % (now_str(), msg)
    if mirror:
        print(line, flush=True)
    try:
        from runtime_paths import private_append
        private_append(LOGFILE, line + "\n")
    except Exception:
        pass


def load_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log("读取 %s 失败(%s)，使用默认值" % (os.path.basename(path), exc))
        return default


def save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def append_sample(rec: dict) -> None:
    try:
        with open(SAMPLES, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        log("写 samples 失败: %s" % exc)


def parse_hhmm(text: str, fallback: dtime) -> dtime:
    try:
        parts = [int(x) for x in text.split(":")]
        return dtime(parts[0], parts[1], parts[2] if len(parts) > 2 else 0)
    except Exception:
        return fallback


# ------------------------------------------------------------------ 请求
def build_headers(session: dict) -> dict:
    """请求头。

    sign 一律用本地 signer 现算（已从 bundle 还原并验证）：
    这样即使 session.json 里录的 sign 过期，只要 sessionid 仍然有效就能继续用。
    """
    try:
        from signer import generate_sign
        sign = generate_sign({}, udid=session.get("deviceid", "udid"))
    except Exception as exc:  # noqa: BLE001
        log("本地签名失败(%s)，回退到 session 里记录的 sign" % exc)
        sign = session.get("sign", "")
    return {
        "content-type": "application/json",
        "deviceid": session["deviceid"],
        "platform": "android",
        "sign": sign,
        "sessionid": session["sessionid"],
        "User-Agent": session.get("user_agent", "okhttp/4.9.2"),
    }


class SessionInvalid(Exception):
    pass


def fetch_detail(headers: dict, tournament_id: str) -> dict:
    """返回 item 字典；会话失效抛 SessionInvalid；其它异常抛 Exception。"""
    url = "%s/urs/tournament/detail/%s" % (BASE, tournament_id)
    resp = requests.get(url, headers=headers, timeout=TIMEOUT)
    if resp.status_code == 403:
        raise SessionInvalid("HTTP 403 %s" % resp.text[:200])
    if resp.status_code != 200:
        raise RuntimeError("HTTP %s %s" % (resp.status_code, resp.text[:200]))
    body = resp.json()
    if body.get("status") != "success":
        code = body.get("code", "")
        if str(code).startswith("AUTH") or "sign" in str(body.get("detail", "")).lower():
            raise SessionInvalid("业务失败 %s %s" % (code, body.get("detail")))
        raise RuntimeError("业务失败 %s %s" % (code, body.get("msg")))
    item = body.get("item")
    if not item:
        raise RuntimeError("响应缺少 item 字段")
    return item


# ------------------------------------------------------------------ 时段
# 服务端下发的报名时段缓存（每个赛事一份）。
# 优先使用服务端字段，本地配置只作兜底：服务端才是权威，
# 而且它可能随赛事调整而变化。
_SERVER_WINDOWS: dict[str, dict] = {}


def parse_server_hhmm(text: str):
    """把服务端的 'HH:MM' 解析成 dtime；失败返回 None。"""
    try:
        parts = [int(x) for x in str(text).split(":")]
        if len(parts) >= 2:
            return dtime(parts[0], parts[1], parts[2] if len(parts) > 2 else 0)
    except Exception:
        pass
    return None


def cache_server_window(tid: str, item: dict) -> None:
    """从详情响应里提取并缓存报名时段。"""
    start = parse_server_hhmm(item.get("signup_start_time"))
    end = parse_server_hhmm(item.get("signup_end_time"))
    if start or end:
        _SERVER_WINDOWS[str(tid)] = {
            "start": start, "end": end,
            "start_day": item.get("signup_start_day"),
            "end_day": item.get("signup_end_day"),
        }


def active_window(cfg: dict):
    """返回 (start, end, source)：优先服务端字段，其次本地配置。"""
    tid = str(cfg.get("tournament_id"))
    srv = _SERVER_WINDOWS.get(tid) or {}
    if srv.get("start") or srv.get("end"):
        start = srv.get("start") or parse_hhmm(cfg.get("active_start", "16:00"), dtime(16, 0))
        end = srv.get("end") or parse_hhmm(cfg.get("active_end", "23:59:59"), dtime(23, 59, 59))
        return start, end, "server(%s~%s)" % (srv.get("start"), srv.get("end"))
    start = parse_hhmm(cfg.get("active_start", "16:00"), dtime(16, 0))
    end = parse_hhmm(cfg.get("active_end", "23:59:59"), dtime(23, 59, 59))
    return start, end, "config"


def in_active_window(cfg: dict) -> bool:
    start, end, _ = active_window(cfg)
    now = datetime.now().time()
    if start <= end:
        return start <= now <= end
    # 跨零点
    return now >= start or now <= end


def seconds_until_window(cfg: dict) -> int:
    start, _end, _src = active_window(cfg)
    now = datetime.now()
    target = datetime.combine(now.date(), start)
    if target <= now:
        target = target.replace(day=now.day)
        if target <= now:
            from datetime import timedelta
            target = target + timedelta(days=1)
    return max(30, int((target - now).total_seconds()))


def next_delay(cfg: dict) -> float:
    base = float(cfg.get("interval_seconds", 45))
    jitter = float(cfg.get("jitter_ratio", 0.2))
    low = max(float(cfg.get("min_interval_seconds", 20)), base * (1 - jitter))
    high = base * (1 + jitter)
    return random.uniform(low, high)


# ------------------------------------------------------------------ 通知
def send_slot_notification(cfg: dict, item: dict) -> bool:
    player_num = item.get("player_num")
    max_num = item.get("max_player_number")
    ev = {
        "headline": "【空位出现】%s 可以报名了" % cfg.get("tournament_name", "赛事"),
        "name": item.get("name", cfg.get("tournament_name", "-")),
        "tournament_id": cfg.get("tournament_id", "-"),
        "player_num": player_num,
        "max_player_number": max_num,
        "found_at": now_str(),
        "remark": "已报名 %s / 定员 %s（剩余 %s）。请立即打开《口袋影之诗》APP 或微信小程序报名。"
                  % (player_num, max_num, (max_num - player_num) if isinstance(player_num, int) and isinstance(max_num, int) else "?"),
    }
    title = "【SVE广州GP】出现空位！%s/%s" % (player_num, max_num)
    log("发送空位通知: %s" % title)
    return any_ok(notify_all(cfg.get("channels", {}), title, render_event_html(ev)))


def send_problem_notification(cfg: dict, headline: str, detail: str) -> bool:
    ev = {
        "headline": headline,
        "name": cfg.get("tournament_name", "-"),
        "tournament_id": cfg.get("tournament_id", "-"),
        "found_at": now_str(),
        "remark": detail,
    }
    title = "【SVE名额监控】%s" % headline
    log("发送告警: %s | %s" % (headline, detail))
    return any_ok(notify_all(cfg.get("channels", {}), title, render_event_html(ev)))


# ------------------------------------------------------------------ 主逻辑
def poll_once(cfg: dict, session: dict, headers: dict, state: dict, notify: bool) -> dict:
    item = fetch_detail(headers, str(cfg["tournament_id"]))
    player_num = item.get("player_num")
    max_num = item.get("max_player_number")
    available = isinstance(player_num, int) and isinstance(max_num, int) and player_num < max_num

    rec = {
        "ts": now_str(),
        "player_num": player_num,
        "max_player_number": max_num,
        "available": available,
        "zh_user_status": item.get("zh_user_status"),
        "tournament_state": item.get("tournament_state"),
    }
    append_sample(rec)

    # 顺手缓存服务端下发的报名时段（用于时段门控，优先于本地配置）
    try:
        cache_server_window(str(cfg["tournament_id"]), item)
    except Exception:
        pass

    log("采样: %s/%s available=%s 我的状态=%s"
        % (player_num, max_num, available, item.get("zh_user_status")))

    # 触发条件：
    #   自动报名关闭时 = 平台层面有空位（player_num < max）
    #   自动报名开启时 = 有空位【或】自身状态已是「可报名」
    # 后者很重要：名额释放与"自身状态同步为可报名"可能不在同一时刻，
    # 任一先到都该触发，剩下的交给 autosignup 的自旋刷新去补齐。
    auto_cfg = cfg.get("auto_signup", {}) or {}
    eligible = item.get("zh_user_status") == "可报名"
    if auto_cfg.get("enabled") and auto_cfg.get("verify_own_status", True) is False:
        trigger_now = available
    elif auto_cfg.get("enabled"):
        trigger_now = available or eligible
    else:
        trigger_now = available

    was_trigger = state.get("trigger_available")

    if notify:
        became_available = trigger_now and was_trigger is not True
        still_available = trigger_now and was_trigger is True

        if became_available:
            if auto_cfg.get("enabled"):
                log("检测到可报名迹象（平台有空位=%s, 自身状态=%s），触发自动报名"
                    % (available, item.get("zh_user_status")))
                try:
                    from autosignup import auto_signup
                    res = auto_signup(cfg)
                    log("自动报名结果: success=%s reason=%s"
                        % (res.get("success"), res.get("reason")))
                    if res.get("task_completed"):
                        state["task_completed"] = True
                        state["task_completed_at"] = now_str()
                        save_json(STATE, state)
                        log("任务已完成（报名稳定确认），停止所有网络请求并退出")
                        return 0
                except Exception as exc:  # noqa: BLE001
                    log("自动报名异常: %s" % exc)
                    send_slot_notification(cfg, item)
                    state["last_notify_at"] = time.time()
            else:
                if send_slot_notification(cfg, item):
                    state["last_notify_at"] = time.time()

        elif still_available:
            cooldown = float(cfg.get("notify_cooldown_minutes", 10)) * 60
            last = float(state.get("last_notify_at") or 0)
            if time.time() - last >= cooldown:
                log("空位仍在，超过冷却时间，重发一次通知")
                if send_slot_notification(cfg, item):
                    state["last_notify_at"] = time.time()

    state["available"] = available
    state["trigger_available"] = trigger_now
    state["player_num"] = player_num
    state["max_player_number"] = max_num
    state["last_poll_at"] = now_str()
    state["last_ok_at"] = time.time()
    # 便于外部脚本（watchdog）直接读取的本地时间字符串，避免 epoch 换算歧义
    state["last_ok_at_local"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["session_ok"] = True
    save_json(STATE, state)
    return rec


def try_recover_session(attempts: int = 3) -> tuple[bool, str]:
    """会话失效时尝试自动重新登录（从设备里的 APP 恢复 session_id）。

    返回 (是否恢复成功, 说明)。成功后调用方应重新加载 session 并重建 headers。
    """
    last = "未尝试"
    for attempt in range(1, attempts + 1):
        try:
            from session_manager import recover_session
            ok, detail = recover_session()
            last = detail
            log("会话恢复尝试 %d: %s | %s" % (attempt, ok, detail))
            if ok:
                return True, detail
        except Exception as exc:  # noqa: BLE001
            last = "恢复异常: %s" % exc
            log("会话恢复异常: %s" % exc)
        if attempt < attempts:
            time.sleep(20)
    return False, last


def run(cfg: dict, args) -> int:
    # --- 完成标记优先：若任务已完成，只做一次只读校验，绝不再发写请求 ---
    try:
        from autosignup import verify_completed
        comp = verify_completed(cfg=cfg)
        if comp.get("done"):
            log("任务已完成，不再轮询。%s" % comp.get("detail"))
            if comp.get("still_registered") is False:
                log("注意：只读校验显示当前不是「已报名」状态（状态=%s）。"
                    "可能是你已手动交接，或报名被服务端回滚。"
                    "脚本不会自动重新报名——如需重新盯梢，请先删除 capture/completed.json。"
                    % comp.get("status"))
            return 0
    except Exception as exc:  # noqa: BLE001
        log("完成标记检查异常（继续正常流程）: %s" % exc)

    session = sveenv.load_session(SESSION_ACCOUNT)
    if not session or not session.get("sessionid"):
        log("找不到会话凭证（accounts.json 里没有可用账号），无法轮询")
        return 2
    headers = build_headers(session)
    state = load_json(STATE, {}) or {}

    log("=== 轮询器启动 tournament_id=%s 间隔=%ss 时段=%s~%s ==="
        % (cfg.get("tournament_id"), cfg.get("interval_seconds"),
           cfg.get("active_start"), cfg.get("active_end")))
    _s, _e, _src = active_window(cfg)
    log("时段来源: %s（服务端字段优先，未取到时用本地配置）" % _src)

    if args.test_notify:
        ok = send_slot_notification(cfg, {"name": cfg.get("tournament_name"), "player_num": 99,
                                          "max_player_number": 100})
        log("自检通知结果: %s" % ("成功" if ok else "失败"))

    if args.once or args.test_notify:
        try:
            rec = poll_once(cfg, session, headers, state, notify=False)
            print(json.dumps(rec, ensure_ascii=False, indent=2))
            if state.get("task_completed"):
                log("任务已完成（本次采样触发了自动报名且已稳定确认）")
            return 0
        except SessionInvalid as exc:
            log("会话失效: %s" % exc)
            ok, detail = try_recover_session()
            if ok:
                session = sveenv.load_session(SESSION_ACCOUNT) or {}
                headers = build_headers(session)
                log("会话已自动恢复，重试一次采样")
                try:
                    rec = poll_once(cfg, session, headers, state, notify=False)
                    print(json.dumps(rec, ensure_ascii=False, indent=2))
                    return 0
                except Exception as exc2:  # noqa: BLE001
                    log("恢复后重试仍失败: %s" % exc2)
                    return 3
            log("自动恢复失败: %s" % detail)
            return 3
        except Exception as exc:  # noqa: BLE001
            log("轮询失败: %s" % exc)
            return 1

    consecutive_failures = 0
    while True:
        try:
            if not in_active_window(cfg) and not args.ignore_window:
                wait = seconds_until_window(cfg)
                log("非报名时段，休眠 %d 秒（约 %.1f 小时）" % (wait, wait / 3600))
                time.sleep(min(wait, 1800))
                continue

            poll_once(cfg, session, headers, state, notify=True)
            consecutive_failures = 0
            time.sleep(next_delay(cfg))

        except SessionInvalid as exc:
            log("!! 会话失效: %s" % exc)
            # --- 自动重新登录：从设备里的 APP 恢复最新 session_id ---
            ok, detail = try_recover_session()
            if ok:
                fresh = sveenv.load_session(SESSION_ACCOUNT)
                if fresh.get("sessionid"):
                    session = fresh
                    headers = build_headers(session)
                    state["session_ok"] = True
                    save_json(STATE, state)
                    log("会话已自动恢复，继续监控（无需人工介入）")
                    continue

            if cfg.get("notify_on_session_invalid", True) and state.get("session_ok") is not False:
                send_problem_notification(cfg, "会话失效且自动恢复失败",
                                          "登录凭证被服务端拒绝：%s<br>"
                                          "已尝试从模拟器里的 APP 自动恢复会话但失败"
                                          "（可能 APP 已退出登录或模拟器未运行）。<b>在此之前无法发现空位。</b>"
                                          "<br>恢复详情：%s" % (exc, detail))
            state["session_ok"] = False
            save_json(STATE, state)
            time.sleep(300)

        except Exception as exc:  # noqa: BLE001
            consecutive_failures += 1
            backoff = min(300, 5 * (2 ** min(consecutive_failures, 6)))
            log("轮询异常(%d 连败): %s -> 退避 %ds" % (consecutive_failures, exc, backoff))
            time.sleep(backoff)


def _pid_alive(pid: int) -> bool:
    """判断 pid 是否存活（不靠命令行匹配，避免误判成"第二个实例"）。

    跨平台：POSIX 用 os.kill(pid, 0)；Windows 上 os.kill 语义不同，
    因此先试 tasklist。Linux 上不要调用 tasklist（会抛 FileNotFoundError，
    虽然被捕获，但每次都要走一次异常，没必要）。
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import subprocess
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                                 capture_output=True, text=True, timeout=10).stdout
            if str(pid) in out:
                return True
        except Exception:
            pass
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def release_single_instance_lock() -> None:
    try:
        if os.path.exists(LOCK):
            cur = int(open(LOCK, encoding="utf-8").read().strip() or "0")
            if cur == os.getpid():
                os.remove(LOCK)
    except Exception:
        pass


def acquire_single_instance_lock(force: bool = False) -> bool:
    """常驻模式的单实例锁。

    为什么需要：曾靠"命令行里是否含 poll.py"来判断实例数，
    结果既把子进程误当成第二个实例，又误杀了真正在跑的那个。
    让程序自己用 pid 文件声明"我在跑"，远比外部猜测可靠。

    锁文件存在但 pid 已死 → 视为陈旧锁，自动接管。
    """
    if force:
        return True
    if os.path.exists(LOCK):
        try:
            old = int(open(LOCK, encoding="utf-8").read().strip() or "0")
        except Exception:
            old = 0
        if old and old != os.getpid() and _pid_alive(old):
            log("已有轮询器在运行（pid=%d），本次退出以免重复请求。"
                "如需强制再起一个，请加 --force" % old)
            return False
        log("发现陈旧锁（pid=%s，进程已不存在），接管" % (old or "?"))

    tmp = LOCK + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))
    os.replace(tmp, LOCK)
    atexit.register(release_single_instance_lock)
    return True


def main() -> int:
    sveenv.assert_legacy_executor_allowed()
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只拉一次并打印")
    ap.add_argument("--test-notify", action="store_true", help="拉一次并发一条自检通知")
    ap.add_argument("--ignore-window", action="store_true", help="忽略报名时段限制")
    ap.add_argument("--force", action="store_true", help="忽略单实例锁，强制启动")
    ap.add_argument("--account", default=None,
                    help="账号参数为稳定 account_id；启用 UID 数据集后旧轮询器拒绝运行。"
                         "显式指定可避免「轮询器跟着客户端切账号」这种隐性行为。")
    args = ap.parse_args()

    # 让本进程内所有 sveenv.load_session() 都解析到同一个账号。
    # 会话统一到 accounts.json 后，默认会跟随 active 账号——而 active 是
    # 客户端在切的。轮询器若因此换了账号，会变成"用另一个号去报名"，
    # 所以提供显式开关，并在启动时把它讲清楚。
    global SESSION_ACCOUNT
    SESSION_ACCOUNT = (args.account or "").strip() or None
    if SESSION_ACCOUNT:
        log("使用指定账号: %s" % SESSION_ACCOUNT)
    else:
        log("使用 UID 数据集的默认 active_account_id")

    cfg = load_json(SETTINGS)
    if not cfg:
        log("读取 config/settings.json 失败")
        return 2

    # 单实例锁只对常驻模式生效：--once 属于一次性排查，允许并发。
    if not (args.once or args.test_notify):
        if not acquire_single_instance_lock(force=args.force):
            return 4

    log("当前时间 %s | 在报名时段内: %s" % (now_str(), in_active_window(cfg)))
    return run(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
