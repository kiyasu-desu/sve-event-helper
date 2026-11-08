"""SVE 客户端核心：报名调度器。

职责：在目标赛事的报名窗口打开时自动提交报名，并做两段复核。

沿用主项目已验证的报名状态机（scripts/poller/autosignup.py）：
    提交 → 立即复核（+2s）→ 静默等待 60s → 二次确认 → 才算成功
两段复核的分工：
    立即复核  只看"这一枪打中没有"，没打中就赶紧回去等下一次；
    二次确认  看"座位有没有坐稳"（服务端可能随后回滚）。

调度策略（依据实测的两种报名形态，见 capture/client_recon.md 第四节）：
  * 每日固定时段型（如大奖赛 16:00~23:59）：每天到点开始轮询；
  * 单次开放时刻型（绝大多数）：signup_time 是毫秒时间戳，到点后持续可报。
  两者统一为：**到点前低频、临近到点切高频、到点立即提交**。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from scheduler_health import Progress
from sveapi import (TIMEOUT, SveClient, SveError, Account, is_signable_item,
                    is_makeup_status, is_makeup_opportunity,
                    is_already_in_status)

SESSION_RE = re.compile(r"[0-9a-f]{64}")


# ------------------------------------------------------------------ 报名时段
@dataclass
class SignupWindow:
    """归一化后的报名窗口。

    两种来源：
      daily   —— 每天固定时段（signup_start_time / signup_end_time 形如 "16:00"）
      instant —— 单次开放时刻（signup_time 毫秒时间戳）
    """
    kind: str = "unknown"          # "daily" | "instant" | "unknown"
    daily_start: str = ""          # "16:00"
    daily_end: str = ""            # "23:59"
    start_ts: float | None = None  # epoch 秒
    end_day: str = ""
    raw: dict = field(default_factory=dict)

    def opens_at(self, now: datetime | None = None) -> datetime | None:
        """下一次开放时刻（本地时间）。"""
        now = now or datetime.now()
        if self.kind == "instant" and self.start_ts:
            return datetime.fromtimestamp(self.start_ts)
        if self.kind == "daily" and self.daily_start:
            hh, mm = _parse_hhmm(self.daily_start)
            t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if t <= now:
                t += timedelta(days=1)
            return t
        return None

    def opened_at(self, now: datetime | None = None) -> datetime | None:
        """最近一次"已经发生"的开放时刻（本地时间）。

        与 opens_at() 的区别很关键：opens_at() 返回的是**下一次**开放，
        一旦当前时间越过开放时刻，每日窗口会跳到明天——于是没法回答
        "窗口刚开了多久"，也就无法实现"开放后 2 分钟内保持高频"。
        """
        now = now or datetime.now()
        if self.kind == "instant" and self.start_ts:
            t = datetime.fromtimestamp(self.start_ts)
            return t if t <= now else None
        if self.kind == "daily" and self.daily_start:
            hh, mm = _parse_hhmm(self.daily_start)
            t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if t <= now:
                return t
            return t - timedelta(days=1)
        return None

    def is_open(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        if self.kind == "instant":
            if not self.start_ts:
                return False
            if datetime.fromtimestamp(self.start_ts) > now:
                return False
            # 单次开放型：开放后持续可报（直到赛事开始/签到，由服务端状态反映）
            return True
        if self.kind == "daily":
            sh, sm = _parse_hhmm(self.daily_start or "00:00")
            eh, em = _parse_hhmm(self.daily_end or "23:59")
            cur = now.hour * 60 + now.minute
            s, e = sh * 60 + sm, eh * 60 + em
            if s <= e:
                return s <= cur <= e
            return cur >= s or cur <= e
        # 没有时段字段 → 视为"随时可报"（例如常驻积分赛）
        return True

    def describe(self) -> str:
        if self.kind == "instant" and self.start_ts:
            return "单次开放: %s" % datetime.fromtimestamp(self.start_ts).strftime("%Y-%m-%d %H:%M:%S")
        if self.kind == "daily":
            return "每日 %s~%s" % (self.daily_start, self.daily_end)
        return "无时段限制（常驻可报）"


def _parse_hhmm(s: str) -> tuple[int, int]:
    try:
        parts = [int(x) for x in str(s).split(":")]
        return parts[0], (parts[1] if len(parts) > 1 else 0)
    except Exception:
        return 0, 0


def window_from_detail(item: dict) -> SignupWindow:
    """从 detail 响应里归一化报名窗口。"""
    st = (item.get("signup_start_time") or "").strip()
    en = (item.get("signup_end_time") or "").strip()
    ts = item.get("signup_time")
    if st or en:
        return SignupWindow(kind="daily", daily_start=st or "00:00", daily_end=en or "23:59",
                            end_day=item.get("signup_end_day") or "", raw=item)
    if isinstance(ts, (int, float)) and ts > 0:
        # 服务端给的是毫秒时间戳
        return SignupWindow(kind="instant",
                            start_ts=(ts / 1000.0 if ts > 1e11 else float(ts)),
                            end_day=item.get("signup_end_day") or "", raw=item)
    return SignupWindow(kind="unknown", raw=item)


# ------------------------------------------------------------------ 失败原因判定
# 「与已报名赛事时间冲突」这个文案**来自服务端**——在 Python 代码里原本一次都没
# 出现过，sveapi 只是把它透传上来。所以判定有两种依据：
#
#   1) 错误码（可靠）—— SveError.code，取自服务端响应的 code 字段
#   2) 文案匹配（回退）—— 服务端改文案即失效
#
# 目前 code 还**未知**：这个冲突无法在本地复现（复现就得真的提交一次报名，
# 会改动真实账号状态），所以 TIME_CONFLICT_CODES 暂为空。
# 首次真实遇到时看日志里的 "提交失败 code=XXX"，确认后填进来，
# 此后判定就不再依赖中文文案。
TIME_CONFLICT_CODES: tuple[str, ...] = ()

# 文案回退用。取得窄一点，避免误伤（判定过宽会误删用户的订阅）
_TIME_CONFLICT_HINTS = ("时间冲突",)


def looks_like_time_conflict(code=None, reason: str = "") -> bool:
    """提交失败是否属于「与已报名赛事时间冲突」。

    这种失败在用户处理掉那场已报名的赛事之前**永远不会消失**，
    所以由调度器放弃守护、上层取消订阅；其余任何失败一律不适用。
    """
    if code is not None and str(code) in TIME_CONFLICT_CODES:
        return True
    return any(h in (reason or "") for h in _TIME_CONFLICT_HINTS)


# ------------------------------------------------------------------ 结果与日志
@dataclass
class AttemptResult:
    ok: bool = False
    attempted: bool = False
    reason: str = ""
    quick_status: str | None = None
    second_status: str | None = None
    # 提交失败时服务端返回的错误码。**保留成结构化字段**而不是只拼进 reason：
    # 否则判定只能靠中文文案，服务端一改措辞就失效。
    code: str | None = None
    # 是否要求"放弃守护并终止"（目前只有一种原因：与已报名赛事时间冲突）
    abort: bool = False
    abort_reason: str = ""


class Scheduler:
    """守护某场赛事的报名调度。线程安全地对外汇报状态。"""

    def __init__(self, client: SveClient, tournament_id, *,
                 poll_interval: float = 15.0,
                 hot_window_seconds: float = 120.0,
                 hot_interval: float = 0.8,
                 near_window_seconds: float = 360.0,
                 warm_window_seconds: float = 2400.0,
                 warm_interval: float = 300.0,
                 idle_interval: float = 1800.0,
                 second_confirm_delay: float = 60.0,
                 confirm_window: float = 90.0,
                 confirm_retry: float = 10.0,
                 logger=None, on_event=None):
        """按"距窗口开放时间"分档轮询。

        基准时刻 T = 窗口开放时刻；每日窗口型取**每天的开放时刻**（如每天 16:00）。
        档位（自远及近）：
            > warm_window_seconds (40 分钟)          -> idle_interval  (30 分钟)
            6 ~ 40 分钟 (near ~ warm)                -> warm_interval  (5 分钟)
            2 ~ 6 分钟  (hot ~ near)                 -> poll_interval  (15 秒)
            T 前后 hot_window_seconds (各 2 分钟)     -> hot_interval   (0.8 秒)
            开放 2 分钟后（窗口内）                   -> poll_interval  (15 秒)

        为什么要有这几档：之前只有一个 poll_interval，且公式写成
        `min(poll_interval, remain - hot_window)`，使 poll_interval 变成"最大等待"，
        于是离窗口 15 小时也在 15 秒轮询（实测约 5760 次/天）。
        """
        self.client = client
        self.tid = str(tournament_id)
        self.poll_interval = poll_interval
        self.hot_window_seconds = hot_window_seconds
        self.hot_interval = hot_interval
        self.near_window_seconds = near_window_seconds
        self.warm_window_seconds = warm_window_seconds
        self.warm_interval = warm_interval
        self.idle_interval = idle_interval
        self.second_confirm_delay = second_confirm_delay
        self.confirm_window = confirm_window
        self.confirm_retry = confirm_retry
        self.log = logger or (lambda m: None)
        self.on_event = on_event or (lambda kind, data: None)

        self.stop_flag = threading.Event()
        self.completed = False
        self.attempts = 0
        self.state: dict = {}
        self.progress = Progress()

    def _detail(self, phase):
        # A detail/signup may verify the official identity first (two requests).
        # Credential lock/I/O stuck beyond this budget is a real stall.
        self.progress.enter(phase, 2 * TIMEOUT + 10)
        self.progress.attempted()
        try:
            value = self.client.detail(self.tid)
        except Exception:
            self.progress.result(False)
            raise
        self.progress.result(True)
        return value

    def _wait(self, delay, phase, interruptible=True):
        delay = max(0.0, delay)
        self.progress.enter(phase, delay)
        if interruptible:
            self.stop_flag.wait(delay)
        else:
            time.sleep(delay)

    # ---- 复核 ----
    def wait_before_open(self, remain: float) -> float:
        """窗口未开时，下一次复查的等待秒数（离得越远等得越久）。

        四档（自远及近），以"距开放 T 的剩余秒数 remain"判断：
            remain > warm_window_seconds          -> idle_interval (30 分钟)
            near_window_seconds < remain <= warm  -> warm_interval (5 分钟)
            hot_window_seconds  < remain <= near  -> poll_interval (15 秒)
            remain <= hot_window_seconds          -> hot_interval  (高频)

        低频档会**夹住剩余时间**，保证在进入下一档之前一定醒来，
        不会因为一次睡太久而错过提速时机。
        """
        if remain <= self.hot_window_seconds:
            return self.hot_interval
        if remain <= self.near_window_seconds:
            return self.poll_interval
        if remain <= self.warm_window_seconds:
            return self.warm_interval
        # 越远越慢，但绝不睡过"进入 warm 档"的时刻；同时不短于 warm_interval
        return max(self.warm_interval,
                   min(self.idle_interval, remain - self.warm_window_seconds))

    def wait_in_window(self, win: "SignupWindow") -> float:
        """窗口已开时的等待秒数。

        "开放前后各 2 分钟保持高频"里的**后半段**由这里负责：
        刚开放的那 2 分钟内继续高频（此时正是抢位最激烈的时刻），
        之后回到 poll_interval。
        """
        opened = win.opened_at()
        if opened is not None:
            elapsed = (datetime.now() - opened).total_seconds()
            if 0 <= elapsed <= self.hot_window_seconds:
                return self.hot_interval
        return self.poll_interval

    def confirm(self, label: str, window: float, retry: float) -> tuple[bool, str, str]:
        """复核报名是否生效。

        区分错误类型：网络类错误在窗口内重试（抖动不该判失败），
        状态不对则立即判定失败（座位真的没了）。
        """
        started = time.monotonic()
        last = ""
        interval = max(1.0, retry)
        while True:
            try:
                it = self._detail("confirm_quick" if label == "立即复核" else "confirm_second")
                status = it.get("zh_user_status")
                if is_already_in_status(status):
                    return True, status, "%s通过（状态=%s）" % (label, status)
                return False, status, "%s失败（状态=%r）" % (label, status)
            except Exception as e:
                last = str(e)
                self.log("%s 请求失败: %s" % (label, e))
            if time.monotonic() - started >= window:
                return False, "", "%s在 %.0f 秒内未成功: %s" % (label, window, last)
            self._wait(min(interval, max(1.0, window - (time.monotonic() - started))), "confirm_retry", False)

    # ---- 单次尝试 ----
    def try_once(self) -> AttemptResult:
        res = AttemptResult()
        if self.stop_flag.is_set():
            res.reason = "守护已停止"
            return res
        it = self._detail("attempt_sample")
        if self.stop_flag.is_set():
            res.reason = "守护已停止"
            return res
        self.state = {
            "name": it.get("name"),
            "player_num": it.get("player_num"),
            "max": it.get("max_player_number"),
            "status": it.get("zh_user_status"),
            "window": window_from_detail(it),
        }
        self.on_event("sample", dict(self.state))

        own = it.get("zh_user_status")
        if is_already_in_status(own):
            # 「已报名」，以及**已报名账号进入签到阶段**后的「签到中」。
            # 后者是实测结论（见 sveapi.is_already_in_status 的证据）：
            # 9 个账号里显示「签到中」的 5 个全都在参与记录里。
            res.ok = True
            res.quick_status = own
            res.reason = "已是 %r 状态（已在该赛事中），无需重复提交" % own
            self.completed = True
            # 必须和"报名成功"一样发 completed：
            # 否则订阅不会被自动移出列表，会永远留在那里空转。
            # （实测踩过：账号通过预约/手动报名已进场时，这一路径不触发移除。）
            self.on_event("completed", {"result": res, "already": True})
            return res

        pn, mx = it.get("player_num"), it.get("max_player_number")
        has_slot = isinstance(pn, int) and isinstance(mx, int) and pn < mx
        if not has_slot and not is_signable_item(it):
            res.reason = "当前无空位（%s/%s，状态=%s）" % (pn, mx, own)
            return res

        if not is_signable_item(it):
            # 可提交的两类：常规「可报名」；以及**补录**机会
            # （签到→开赛之间、仍有余位，官方 App 显示为「可补录」）。
            # 见 sveapi.is_signable_item() 的说明。
            res.reason = "自身状态为 %r，并非可提交报名/补录的状态，不提交" % own
            return res

        res.attempted = True
        self.attempts += 1
        self.on_event("attempt", {"n": self.attempts, "before": pn})
        if self.stop_flag.is_set():
            res.attempted = False
            res.reason = "守护已停止"
            return res
        try:
            self.progress.enter("signup", 2 * TIMEOUT + 10)
            self.progress.attempted()
            self.client.signup(self.tid)
            self.progress.result(True, "signup")
            res.reason = "已提交报名"
        except SveError as e:
            self.progress.result(False, "signup")
            res.code = None if e.code is None else str(e.code)
            res.reason = "提交失败: %s" % e
            # 把 code 打进日志。这是判断"时间冲突对应哪个错误码"的**唯一途径**——
            # 该冲突无法在本地复现（复现要真的提交报名），只能等真实遇到时从日志确认。
            self.log("提交失败 code=%s" % (res.code or "（服务端未提供）"))
            if looks_like_time_conflict(res.code, str(e)):
                # 冲突不会自行消失 → 放弃守护，由 server.py 取消订阅并通知。
                # ⚠️ 只有这一种失败会置 abort；其余失败一律继续等待。
                res.abort = True
                res.abort_reason = "与已报名赛事时间冲突"
            return res

        except Exception:
            self.progress.result(False, "signup")
            raise

        ok, status, why = self.confirm("立即复核", 15, 3)
        res.quick_status = status
        if not ok:
            res.reason = why
            self.on_event("attempt_failed", {"reason": why})
            return res

        self.log("立即复核通过，静默 %.0f 秒后二次确认" % self.second_confirm_delay)
        self._wait(self.second_confirm_delay, "confirm_silence", False)
        ok2, status2, why2 = self.confirm("二次确认", self.confirm_window, self.confirm_retry)
        res.second_status = status2
        if not ok2:
            res.reason = why2
            self.on_event("unstable", {"reason": why2})
            return res

        res.ok = True
        res.reason = "报名成功并已稳定确认"
        self.completed = True
        self.on_event("completed", {"result": res})
        return res

    # ---- 主循环 ----
    def run(self):
        """阻塞运行，直到完成或被 stop()。"""
        self.progress.enter("starting", 60)
        self.on_event("start", {"tournament_id": self.tid})
        while not self.stop_flag.is_set() and not self.completed:
            credential_version = getattr(getattr(self.client, "account", None), "version", None)
            try:
                it = self._detail("sampling")
                if self.stop_flag.is_set():
                    break
                win = window_from_detail(it)
                self.state = {"name": it.get("name"), "status": it.get("zh_user_status"),
                              "player_num": it.get("player_num"),
                              "max": it.get("max_player_number"), "window": win,
                              "window_desc": win.describe()}
                self.on_event("sample", dict(self.state))

                # 补录机会（含"状态里直接写了补录"与"签到窗口内仍有余位"两种）。
                # 窗口门控只放行补录，**不能**按 is_signable_item 放行——
                # 普通的「可报名」在窗口外同样会被服务端返回，提交必被以
                # "非报名时间"拒绝，那正是这道门控要防的。
                makeup = (is_makeup_status(it.get("zh_user_status"))
                          or is_makeup_opportunity(it))
                if not win.is_open() and not makeup:
                    nxt = win.opens_at()
                    if nxt:
                        remain = (nxt - datetime.now()).total_seconds()
                        wait = self.wait_before_open(remain)
                        self.log("未到报名时段（%s），距开放 %.0f 秒，%.0f 秒后复查"
                                 % (win.describe(), remain, wait))
                    else:
                        # 拿不到开放时刻（服务端没给字段）：低频待命，
                        # 避免在信息不足时高频空转
                        wait = self.idle_interval
                        self.log("未到报名时段（%s），但无法确定开放时刻，%.0f 秒后复查"
                                 % (win.describe(), wait))
                    self._wait(wait, "waiting_open")
                    continue

                if not win.is_open():
                    # 走到这里说明：报名窗口按时间算已关闭，但存在补录机会
                    # （赛事处于「签到 → 开赛」之间，官方 App 显示为「可补录」）。
                    # 补录意味着**此刻**就能补报，与常规窗口无关，故放行。
                    self.log("报名窗口按时间已关闭，但状态为 %r 且处于签到窗口/仍有余位（补录），"
                             "仍尝试提交" % it.get("zh_user_status"))

                # 已开放：正常尝试
                res = self.try_once()
                if res.ok:
                    self.log("任务完成: %s" % res.reason)
                    break
                if res.abort:
                    # 这类失败不会自行恢复（实测曾反复撞同一面墙 503 次），
                    # 继续守护纯属浪费 → 上报让上层取消订阅并通知用户。
                    self.log("放弃守护：%s（不再重试）｜%s" % (res.abort_reason, res.reason))
                    self.on_event("abort", {"result": res, "reason": res.abort_reason,
                                            "code": res.code, "detail": res.reason})
                    break
                self.log("本轮未成功: %s（继续等待）" % res.reason)
                # 刚开放的那 2 分钟保持高频（抢位最激烈），之后回到 poll_interval
                self._wait(self.wait_in_window(win), "waiting_poll")

            except Exception as e:
                if isinstance(e, SveError) and e.code == "identity_mismatch":
                    handled = self.on_event("identity_blocked", {"reason": str(e), "account_version": credential_version})
                    if handled is False:
                        continue
                    self.stop_flag.set()
                    break
                self.log("调度异常: %s（%.0f 秒后重试）" % (e, self.poll_interval))
                self._wait(self.poll_interval, "upstream_retry")

        self.progress.enter("completed" if self.completed else "stopped", 0)
        self.on_event("stop", {"completed": self.completed})

    def stop(self):
        self.stop_flag.set()


def schedule_in_thread(scheduler: Scheduler) -> threading.Thread:
    t = threading.Thread(target=scheduler.run, daemon=True,
                         name="sve-sched-%s" % scheduler.tid)
    t.start()
    return t
