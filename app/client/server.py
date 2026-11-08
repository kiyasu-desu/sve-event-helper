"""SVE 客户端：本地 Web 服务（后端 + 界面托管）。

设计取向：核心逻辑全在 core/ 里，本文件只做"薄"的 HTTP 层与状态编排，
因此后续要迁到服务器（或换前端）都不需要动核心。

启动：
    <venv-client>\\Scripts\\python.exe app\\client\\server.py
    然后浏览器打开 http://127.0.0.1:8801

接口一览：
    GET  /                      界面
    GET  /api/state             账号 / 订阅 / 日志总览
    POST /api/login/start       打开登录窗口
    GET  /api/login/status      登录进度
    POST /api/account/select    切换当前账号
    POST /api/account/forget    删除账号
    GET  /api/tournaments       按地区拉赛事列表（含状态归类）
    POST /api/subscribe         订阅赛事（到点自动报名）
    POST /api/unsubscribe       取消订阅
    GET  /api/detail            单场详情
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import copy
import hashlib
import uuid
import math
from html import escape as escape_html
from pathlib import Path
from functools import wraps

from flask import Flask, jsonify, request, send_from_directory, g, has_request_context

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "core"))

REPO_ROOT = os.path.dirname(os.path.dirname(HERE))

from sveapi import (Account, SveClient, SveError, classify_status,  # noqa: E402
                    classify_class, CLASS_ORDER, CLASS_LABEL, HIDDEN_BY_DEFAULT,
                    is_signable_item, is_makeup_status, is_makeup_opportunity,
                    is_already_in_status)
from weblogin import (LoginWindow, LoginWindowClosed, SESSION_COOKIE_NAMES,  # noqa: E402
                      EXCHANGEABLE_COOKIE_NAMES,
                      find_browser)
from scheduler import Scheduler, window_from_detail             # noqa: E402
from agentlink import AgentLink, Tunnel, DEFAULT_LOCAL_PORT, DEFAULT_REMOTE_PORT  # noqa: E402
from accountguard import (AccountConflict, normalized_uid, revision, check_identity,
                          parse_account)  # noqa: E402
from identity_store import (IdentityStore, WriterLock, PublicationUncertain,
                            resolve as resolve_value, empty_state)  # noqa: E402
from releaseinfo import snapshot as release_snapshot  # noqa: E402

RELEASE_AT_START = release_snapshot(REPO_ROOT)
PROCESS_STARTED_AT = time.time()

from runtime_paths import RuntimePaths, private_append
RUNTIME = RuntimePaths(REPO_ROOT)
RUNTIME.prepare()
DATA_DIR = str(RUNTIME.data)
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
SUBS_FILE = os.path.join(DATA_DIR, "subscriptions.json")
RECORDS_FILE = os.path.join(DATA_DIR, "records.json")
AREAS_FILE = str(RUNTIME.areas)
LOG_FILE = str(RUNTIME.client_log)
# 通知凭据：由界面写入这里（而不是仓库里的 config/），配合 SVE_DATA_DIR
# 可以做到"仓库中永不含凭据"——这是结构性保证，不依赖 .gitignore 的自觉。
NOTIFY_FILE = os.path.join(DATA_DIR, "notify.json")
os.makedirs(DATA_DIR, exist_ok=True, mode=0o700)

# 报名成功后，订阅在列表中保留多久再自动移除（秒）
AUTO_REMOVE_DELAY = 5

app = Flask(__name__, static_folder=None)


@app.after_request
def _no_cache(resp):
    """给接口响应禁用缓存。

    实测踩过：切换账号后赛事列表不变——因为两次请求的 URL 完全相同
    （同一个 area），浏览器直接复用了缓存的旧响应，导致列表停留在旧账号的
    报名信息上。后端数据本身是对的，问题出在客户端缓存。
    """
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    if request.path == "/api/login/cookie":
        # Do not leave an old intent attached to a later native-FAB login. A lost
        # response leaves the client's original cookie intact for receipt replay.
        resp.delete_cookie("sve_login_intent")
    return resp

# ------------------------------------------------------------------ 状态
STATE = {
    "active": None,          # default account_id; browser choice is a per-client cookie
    "accounts": {},          # account_id -> Account
    "pending": [], "tombstones": {}, "operations": {}, "workers": {}, "login_intents": {}, "storage_failed": False,
    # 订阅以 "(account_id, 赛事id)" 为键，不依赖可变昵称。
    # 早先用赛事id 作键，导致换账号后订阅被"改绑"，这是错误的。
    "subs": {},              # sub_key -> {account, uid, tournament_id, ..., logs: []}
    "schedulers": {},        # sub_key -> Scheduler
    "scheduler_threads": set(),  # Includes cancelled schedulers until run() returns.
    # 已报名记录：订阅完成后订阅会被自动移出，若不另记一笔，
    # 界面上就再也看不到"这工具帮我报上了哪些比赛"。
    # 记录只是线索，真实状态由 refresh_records() 定期回查服务端确认。
    "records": {},           # sub_key -> {account, uid, tournament_id, name, source, at, status, ...}
    "login": {"running": False, "message": "", "ok": False, "error": ""},
    # 远端执行模式：订阅与调度交给服务器上的代理，本机只做交互。
    # remote = {"enabled": bool, "host","user","key","ssh_port","local_port","remote_port"}
    "remote": {"enabled": False},
    "tunnel": None,          # agentlink.Tunnel 实例（远端模式时存在）
    "log": [],
}

# UID 数据集只有完整校验成功后才允许写入；旧文件不作为运行来源。
ACCOUNTS_LOADED_OK = False
LOCK = threading.RLock()
STORE = None
REMOTE_LOCK = threading.Lock()  # Lock order: REMOTE_LOCK, then LOCK; never the reverse.


def remote_operation(func):
    """Serialize tunnel changes, login snapshots and credential submissions."""
    @wraps(func)
    def guarded(*args, **kwargs):
        if not REMOTE_LOCK.acquire(blocking=False):
            raise AccountConflict("远端连接正在使用或切换，请稍后重试")
        try:
            return func(*args, **kwargs)
        finally:
            REMOTE_LOCK.release()
    return guarded


def scheduler_thread(sched, key):
    """Caller holds LOCK. Keep cancelled workers visible until their work ends."""
    def run():
        try:
            sched.run()
        finally:
            with LOCK:
                # Completed tasks retain their registration until delayed removal.
                # Other exited workers must not turn a resubscribe into a no-op.
                if STATE["schedulers"].get(key) is sched and not sched.completed:
                    STATE["schedulers"].pop(key, None)
                STATE["scheduler_threads"].discard(thread)
                if STATE["workers"].get(key) is thread:
                    STATE["workers"].pop(key, None)
    thread = threading.Thread(target=run, daemon=True, name="sched-%s" % key)
    STATE["scheduler_threads"].add(thread)
    STATE["workers"][key] = thread
    return thread

# 远端模式的配置文件（与账号/订阅同目录，便于整体搬走）
REMOTE_FILE = os.path.join(DATA_DIR, "remote.json")

# 已报名记录的复核间隔（秒）：过旧就在后台回查一次真实状态
RECORD_TTL = 60
RECORDS_REFRESHING = {"flag": False}

# One independent worker, using credential snapshots so profile I/O never holds
# the live Account's credential lock (which is also used by signup).
PROFILE_JOBS = {}
PROFILE_IO_LOCK = threading.Lock()


def profile_interval():
    cfg = load_json(AGENT_SETTINGS, {}) or {}
    try:
        value = float(cfg.get("profile_refresh_interval_seconds", 600))
        return max(10.0, value) if math.isfinite(value) else 600.0
    except (TypeError, ValueError):
        return 600.0


def profile_status(account):
    job = PROFILE_JOBS.get(account.account_id, {})
    if job.get("owner") is not account or job.get("version") != account.version:
        job = {}
    return {"profile_version": account.profile_version,
            "profile_updated_at": account.profile_updated_at,
            "profile_refreshing": job.get("status") in {"queued", "running"},
            "profile_status": job.get("status", "idle"),
            "profile_error": job.get("error"),
            "profile_next_refresh_at": job.get("next_at", account.profile_updated_at + profile_interval() if account.profile_updated_at else 0)}


@app.post("/api/account/profile/refresh")
def api_profile_refresh():
    body = request.get_json(silent=True) or {}
    if "account_id" not in body:
        raise AccountConflict("资料刷新需要明确 account_id")
    aid = request_account(body, allow_default=False)
    with LOCK:
        account = STATE["accounts"].get(aid)
        if account is None or account.__dict__.get("_retired"):
            raise AccountConflict("账号已删除或身份待核对，请重新读取摘要并明确登录")
        if type(body.get("expected_version")) is not int or body["expected_version"] != account.version:
            raise AccountConflict("账号版本已变化，请刷新摘要后重试资料刷新")
        state = profile_status(account)
        if not state["profile_refreshing"]:
            old = PROFILE_JOBS.get(aid, {})
            # Bound repeated manual failures as well as automatic retries.
            if old.get("owner") is account and old.get("version") == account.version and old.get("retry_at", 0) > time.time():
                return jsonify({"ok": False, "code": "profile_backoff", "error": "资料刷新过于频繁，请等待下次刷新时间后重试", **state})
            PROFILE_JOBS[aid] = {"owner": account, "version": account.version,
                                 "status": "queued", "next_at": time.time(),
                                 "failures": old.get("failures", 0) if old.get("owner") is account and old.get("version") == account.version else 0}
    return jsonify({"ok": True, "account_id": aid, "queued": True})


def refresh_profile_once(aid):
    """Claim and publish one profile with CAS. No live locks during upstream I/O."""
    if not PROFILE_IO_LOCK.acquire(blocking=False):
        return False
    try:
        with LOCK:
            if remote_enabled() or STATE["storage_failed"] or not ACCOUNTS_LOADED_OK:
                return False
            account = STATE["accounts"].get(aid)
            if not account or account.__dict__.get("_retired"):
                return False
            status = profile_status(account)
            if status["profile_status"] == "running" or status["profile_next_refresh_at"] > time.time():
                return False
            snapshot = Account.from_dict(account.to_dict())
            previous = PROFILE_JOBS.get(aid, {})
            failures = previous.get("failures", 0) if previous.get("owner") is account and previous.get("version") == account.version else 0
            job = {"owner": account, "version": account.version, "status": "running", "next_at": time.time()}
            PROFILE_JOBS[aid] = job
        error = None
        try:
            info = SveClient(snapshot).info()
            if not isinstance(info, dict) or normalized_uid(info.get("id")) != aid:
                raise AccountConflict("资料 UID 不匹配")
            if not isinstance(info.get("nickname"), str):
                error = "invalid_response"
            nickname = info.get("nickname")
        except AccountConflict:
            error = "identity_mismatch"
        except SveError:
            error = "upstream_error"
        except Exception:
            error = "invalid_response"
        with LOCK:
            # Relogin, delete/restore and connection changes invalidate snapshots.
            if (STATE["accounts"].get(aid) is not account or account.version != snapshot.version
                    or remote_enabled() or STATE["storage_failed"] or account.__dict__.get("_retired")):
                if PROFILE_JOBS.get(aid) is job:
                    PROFILE_JOBS.pop(aid, None)
                return False
            now = time.time()
            if error:
                delay = min(3600, 30 * 2 ** min(failures, 7))
                job.update(status="failed", error=error, failures=failures + 1,
                           next_at=now + delay, retry_at=now + delay)
                return False
            updated = Account.from_dict(account.to_dict())
            updated.nickname = nickname
            updated.name = nickname or "uid%s" % aid
            updated.legacy_names = sorted(set(account.legacy_names + [account.name, updated.name]))
            updated.version += 1
            updated.profile_version += 1
            updated.profile_updated_at = now
            accounts = {key: value.to_dict() for key, value in STATE["accounts"].items()}
            accounts[aid] = updated.to_dict()
            persist_dataset(accounts=accounts)
            # Only display fields change. Keep sessions, Account identity, workers,
            # subscriptions and historical event bodies intact.
            for field in ("nickname", "name", "legacy_names", "version", "profile_version", "profile_updated_at"):
                setattr(account, field, getattr(updated, field))
            job.update(version=account.version, status="idle", error=None, failures=0,
                       next_at=now + profile_interval(), retry_at=now + 5)
            return True
    finally:
        PROFILE_IO_LOCK.release()


def profile_refresh_loop(stop):
    while not stop.is_set():
        try:
            with LOCK:
                # Earliest due first: every valid account gets a turn.
                due = sorted(STATE["accounts"], key=lambda aid: profile_status(STATE["accounts"][aid])["profile_next_refresh_at"])
            for aid in due:
                if stop.is_set():
                    break
                refresh_profile_once(aid)
        except Exception:
            # Never log upstream payloads or credentials. Storage errors already
            # fence execution via persist_dataset.
            log("账号资料刷新暂不可用，保留最近资料")
        stop.wait(1)

# 每个订阅各自保留的日志行数（调度日志分到订阅页，不再混在主页面）
SUB_LOG_LIMIT = 200


def sub_key(account: str, tid) -> str:
    return "%s|%s" % (account, tid)


def sched_params() -> dict:
    """调度器参数，从选定的 settings 文件读取。

    这样大奖赛那种"更密的轮询 + 更长的二次确认"也能靠配置表达，
    不需要为它单独留一个 poll.py 进程——即"统一纳入订阅体系"。
    """
    cfg = load_json(AGENT_SETTINGS, {}) or {}
    out = {}
    m = {"scheduler_poll_interval": "poll_interval",
         "scheduler_hot_window_seconds": "hot_window_seconds",
         "scheduler_hot_interval": "hot_interval",
         "scheduler_near_window_seconds": "near_window_seconds",
         "scheduler_warm_window_seconds": "warm_window_seconds",
         "scheduler_warm_interval": "warm_interval",
         "scheduler_idle_interval": "idle_interval",
         "second_confirm_delay": "second_confirm_delay",
         "confirm_window": "confirm_window",
         "confirm_retry": "confirm_retry"}
    for src, dst in m.items():
        if cfg.get(src) is not None:
            try:
                out[dst] = float(cfg[src])
            except (TypeError, ValueError):
                pass
    if not out:
        # 默认档位（以窗口开放时刻 T 为基准）：
        #   T 前 > 40 分钟  -> 30 分钟一次
        #   T 前 40~6 分钟  -> 5 分钟一次
        #   T 前 6~2 分钟   -> 15 秒一次
        #   T 前后各 2 分钟 -> 0.8 秒
        #   T 后 2 分钟起   -> 15 秒一次
        out = {"idle_interval": 1800.0, "warm_window_seconds": 2400.0,
               "warm_interval": 300.0, "near_window_seconds": 360.0,
               "poll_interval": 15.0, "hot_window_seconds": 120.0,
               "hot_interval": 0.8, "second_confirm_delay": 60.0}
    return out


def split_sub_key(key: str) -> tuple[str, str]:
    a, _, t = (key or "").partition("|")
    return a, t


def log(msg: str) -> None:
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with LOCK:
        STATE["log"].append(line)
        if len(STATE["log"]) > 500:
            STATE["log"] = STATE["log"][-500:]
    try:
        private_append(LOG_FILE, line + "\n")
    except Exception:
        pass


def save_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    # 数据目录里的文件含会话凭据，落盘即收紧权限。
    # 必须在这里做：早先只在安装脚本里 chmod 过一次，结果账号同步重写文件后
    # 权限被打回 umask 默认的 644（verify.sh 第 4 节就是这么抓到的）。
    if os.path.dirname(os.path.abspath(path)) == os.path.abspath(DATA_DIR):
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass          # Windows 上 POSIX 位无意义，忽略即可
    os.replace(tmp, path)


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


@remote_operation
def load_all() -> None:
    global ACCOUNTS_LOADED_OK, STORE
    STORE = IdentityStore(DATA_DIR)
    STORE._require_writer()
    data = STORE.load(initialize=True)
    STATE["active"] = data["active_account_id"]
    STATE["accounts"] = {aid: Account.from_dict(row) for aid, row in data["accounts"].items()}
    STATE["subs"] = data["subscriptions"]
    STATE["records"] = data["records"]
    for field in ("pending", "tombstones", "operations"):
        STATE[field] = data[field]
    ACCOUNTS_LOADED_OK = True
    # 远端执行模式：上次是开着的就自动恢复（含重建隧道），
    # 否则重启客户端后会悄悄退回本地执行，变成两处都可能有调度器。
    saved_remote = load_json(REMOTE_FILE, {}) or {}
    if saved_remote.get("enabled") and saved_remote.get("host") and saved_remote.get("user"):
        STATE["remote"] = saved_remote
        try:
            start_tunnel(saved_remote)
            log("已恢复远端执行模式：%s@%s" % (saved_remote["user"], saved_remote["host"]))
            # 启动只读比较；离线缓存不能恢复/覆盖服务器成员和会话。
            threading.Timer(12.0, refresh_remote_accounts).start()
        except Exception as e:
            log("恢复远端模式失败：%s" % e)
    else:
        STATE["remote"] = dict(saved_remote, enabled=False)
    log("已载入 %d 个账号、%d 个订阅、%d 条记录；%d 项身份待处理"
        % (len(STATE["accounts"]), len(STATE["subs"]), len(STATE["records"]), len(STATE["pending"])))


def _start_scheduler_for(key: str, s: dict, acc) -> bool:
    """为一条订阅启动守护线程。成功返回 True。

    抽出来是为了让两处共用同一段逻辑：
      * `restore_schedulers()`       —— 进程启动时
      * `restore_missing_schedulers()` —— 账号在运行期才出现时
    两处各写一遍必然会分叉（本文件已经因为分叉出过多次缺陷）。

    幂等：`key` 已在 `STATE["schedulers"]` 里就直接返回，不会起第二个线程。

    ⚠️ **调用方不得持有 `LOCK`**：本函数最终会经 `_sub_log()` 再取一次 `LOCK`，
    而 `threading.Lock` 不可重入 → 死锁。这个死锁在写测试时被实测抓到过，
    所以锁的粒度刻意收在"登记调度器"那一小段里。
    """
    if STORE is None:
        raise AccountConflict("未持有身份数据集，禁止恢复执行")
    STORE._require_writer()
    with LOCK:
        if (remote_enabled() or not s.get("enabled", True) or STATE["subs"].get(key) is not s
                or STATE["accounts"].get(acc.account_id) is not acc or acc.__dict__.get("_retired")):
            return False
        if key in STATE["workers"] and key not in STATE["schedulers"]:
            return False
        if key in STATE["schedulers"]:
            s["orphan"] = False      # 已在跑 → 顺手清掉可能的陈旧标记
            return True
        name = s.get("account_id")
        tid = s.get("tournament_id") or split_sub_key(key)[1]
        s["tournament_id"] = tid
    try:
        client = SveClient(acc)
        sched = Scheduler(client, tid,
                          logger=lambda m, k=key: _sub_log(k, m),
                          on_event=lambda kind, data, k=key: _on_event(k, kind, data),
                          # 必须带上参数：否则重启后会静默退回 45 秒默认值，
                          # 与订阅时设定的频率不一致（实测踩过：
                          # 同一条订阅重启前 15 秒、重启后变 45 秒）。
                          **sched_params())
        with LOCK:
            if (remote_enabled() or STATE["subs"].get(key) is not s
                    or STATE["accounts"].get(acc.account_id) is not acc or acc.__dict__.get("_retired")):
                return False
            if key in STATE["schedulers"]:      # 竞态兜底：别起第二个
                s["orphan"] = False
                return True
            STATE["schedulers"][key] = sched
            thread = scheduler_thread(sched, key)
            # 账号存在 → 必须清掉 orphan 标记。
            # 这个字段此前**只被置 True、从不复位**，于是账号删了又加回来之后，
            # 界面会一直显示"该账号已被删除，订阅无法运行"，而账号其实好好的。
            s["orphan"] = False
        thread.start()
        _sub_log(key, "已恢复守护（账号 %s）" % name)     # ← 不持锁
        return True
    except Exception as e:
        log("启动订阅 %s 的守护失败: %s" % (key, e))
        return False


def restore_missing_schedulers(reason: str = "") -> int:
    """给「账号已存在但没有调度器」的订阅补启动守护。返回补启动的条数。

    为什么需要：`restore_schedulers()` 只在进程启动时跑一次。若某账号在启动
    那一刻还不存在，它的订阅会被跳过（只置 `orphan=True`）；之后该账号在
    **运行期**被加回来（手机端登录 / 账号同步）时**没有任何代码去补启动**，
    那个订阅就永远没有调度器——账号在、订阅在，却不会报名，界面还显示
    "该账号已被删除"。这是**静默失效**，已在服务器上实测发生：
    某个仍存在的账号下，两条订阅 running=False。
    （此处不写账号昵称——仓库内不得出现任何账号标识。）

    未确认身份进入 pending，不能通过恢复守护自动删除或改绑。

    ⚠️ 快照在锁内取，**启动动作放在锁外**（见 `_start_scheduler_for` 的说明）。
    """
    if remote_enabled():
        return 0
    with LOCK:
        pending = []
        for key, s in list(STATE["subs"].items()):
            acc = STATE["accounts"].get(s.get("account_id"))
            if key in STATE["schedulers"]:
                s["orphan"] = False   # 已在跑 → 顺手清陈旧标记
                continue
            if acc is not None and s.get("enabled", True):
                pending.append((key, s, acc))

    started = []
    for key, s, acc in pending:
        if _start_scheduler_for(key, s, acc):
            started.append(key)
    if started:
        with LOCK:
            persist_subs()
        log("账号出现后补启动守护（%s）：%d 条 —— %s"
            % (reason or "账号变化", len(started), ", ".join(started)))
    return len(started)


def restore_schedulers() -> None:
    """服务重启后恢复订阅的守护。

    调度器只存在于内存里，所以重启后若不恢复，界面会显示"已订阅"
    但实际没有任何东西在守护——这是很危险的静默失效。
    恢复时**用订阅自己记录的绑定账号**，而不是当前激活账号，
    否则会悄悄把订阅改成另一个账号的。
    """
    if remote_enabled():
        return  # Keep offline local subscriptions intact; execution belongs to the server.
    for key, s in list(STATE["subs"].items()):
        name = s.get("account_id")
        s["tournament_id"] = s.get("tournament_id") or split_sub_key(key)[1]
        acc = STATE["accounts"].get(name)
        if not acc:
            s["orphan"] = True
            log("订阅身份未确认，保留原件并暂停；请核对迁移待处理项")
            continue
        if _start_scheduler_for(key, s, acc):
            log("已恢复订阅 %s（%s），账号 %s" % (s.get("name"), key, name))

    # Unresolved legacy rows live in pending; never purge them as orphans.


def dataset(**overrides):
    value = empty_state()
    value.update(active_account_id=STATE["active"],
                 accounts={aid: acc.to_dict() for aid, acc in STATE["accounts"].items()},
                 subscriptions=STATE["subs"], records=STATE["records"],
                 pending=STATE["pending"], tombstones=STATE["tombstones"], operations=STATE["operations"])
    value.update(overrides)
    return copy.deepcopy(value)


def persist_dataset(**overrides):
    if STATE["storage_failed"]:
        raise AccountConflict("存储切换待确认，拒绝后续写入")
    if STORE is None:
        raise AccountConflict("身份数据集尚未载入，拒绝写入")
    value = dataset(**overrides)
    try:
        STORE.save(value)
    except (PublicationUncertain, OSError, AccountConflict):
        # Never keep executing against stale memory after a possibly committed write.
        with LOCK:
            STATE["storage_failed"] = True
            for account in STATE["accounts"].values():
                account._retired = True
            for scheduler in STATE["schedulers"].values():
                scheduler.stop()
        raise AccountConflict("身份存储提交失败或结果需确认，已暂停执行；请检查存储并重启读取最终数据集") from None
    return value


def persist_accounts() -> None:
    persist_dataset()


def remote_enabled() -> bool:
    return bool((STATE.get("remote") or {}).get("enabled"))


def agent() -> AgentLink:
    """指向服务器的代理连接（经 ssh 隧道）。"""
    lp = int((STATE.get("remote") or {}).get("local_port") or DEFAULT_LOCAL_PORT)
    return AgentLink("http://127.0.0.1:%d" % lp)


def start_tunnel(cfg: dict) -> Tunnel:
    """Caller holds REMOTE_LOCK; drain the old tunnel before replacing it."""
    old = STATE.get("tunnel")
    if old:
        try:
            old.stop()
        except Exception:
            raise AccountConflict("旧隧道尚未确认退出，未建立新隧道；请稍后重试") from None
    t = Tunnel(host=cfg["host"], user=cfg["user"], key=cfg.get("key") or None,
               remote_port=int(cfg.get("remote_port") or DEFAULT_REMOTE_PORT),
               local_port=int(cfg.get("local_port") or DEFAULT_LOCAL_PORT),
               ssh_port=int(cfg.get("ssh_port") or 22),
               logger=lambda m: log("[隧道] %s" % m))
    t.connection_config = dict(cfg)
    try:
        t.start()
    except Exception:
        t.stop()
        raise
    STATE["tunnel"] = t
    return t


def remote_state() -> dict | None:
    """取代理的完整状态；不可达返回 None。"""
    d = agent().state()
    if d.get("ok") is False and d.get("error"):
        return None
    return d


@app.get("/api/notify/config")
def api_notify_config():
    """Return configuration status only, including for older clients."""
    try:
        return jsonify({"ok": True, **notify_config_view()})
    except Exception:
        return jsonify(settings_failure("settings_read_failed", "无法读取通知设置，请检查配置与存储"))


NOTIFY_FIELDS = {
    "pushplus": ("enabled", "token"),
    "email": ("enabled", "smtp_host", "smtp_port", "use_ssl", "username", "authcode", "from_addr", "to_addrs"),
}
REMOTE_FIELDS = ("host", "user", "key", "ssh_port", "local_port", "remote_port")


def settings_json(path, default):
    """An unreadable configuration must never be mistaken for an empty one."""
    try:
        with open(path, encoding="utf-8-sig") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return default
    if not isinstance(value, dict):
        raise ValueError("invalid settings object")
    return value


def settings_failure(code, message, fields=None):
    result = {"ok": False, "code": code, "error": message}
    if fields:
        result["fields"] = fields
    return result


def settings_port(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isascii() or not str(value).isdecimal():
        raise ValueError("invalid port")
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("invalid port")
    return port


def remote_config_view(cfg=None):
    cfg = STATE.get("remote", {}) if cfg is None else cfg
    return {"enabled": bool(cfg.get("enabled")), "fields": {
        field: {"configured": cfg.get(field) not in (None, ""),
                "source": "file" if cfg.get(field) not in (None, "") else "default",
                "editable": True} for field in REMOTE_FIELDS}}


@app.post("/api/notify/config")
def api_notify_save():
    body = request.json or {}
    incoming = body.get("channels", {})
    if not isinstance(incoming, dict) or any(name not in NOTIFY_FIELDS or not isinstance(row, dict) for name, row in incoming.items()):
        return jsonify(settings_failure("invalid_settings", "通知配置格式无效"))
    errors = {}
    try:
        with LOCK:
            cur = copy.deepcopy(settings_json(NOTIFY_FILE, {}))
            for channel, fields in incoming.items():
                for field, val in fields.items():
                    if field not in NOTIFY_FIELDS[channel] or notify_sources().get(channel + "." + field) == "env":
                        continue
                    if val is None or (isinstance(val, str) and not val.strip()):
                        continue
                    try:
                        if field in ("enabled", "use_ssl"):
                            if isinstance(val, str) and val.lower() in ("true", "false", "1", "0", "yes", "no", "on", "off"):
                                val = val.lower() in ("true", "1", "yes", "on")
                            if not isinstance(val, bool):
                                raise ValueError()
                        elif field == "smtp_port":
                            val = settings_port(val.strip() if isinstance(val, str) else val)
                        elif field == "to_addrs":
                            if isinstance(val, str):
                                val = [x.strip() for x in val.replace(";", ",").split(",") if x.strip()]
                            if not isinstance(val, list) or not all(isinstance(x, str) and "@" in x for x in val):
                                raise ValueError()
                        elif not isinstance(val, str):
                            raise ValueError()
                        else:
                            val = val.strip()
                        cur.setdefault(channel, {})[field] = val
                    except (ValueError, TypeError):
                        errors[channel + "." + field] = "字段格式无效"
            if body.get("clear_secrets"):
                for channel, field in SECRET_FIELDS:
                    if channel in cur:
                        cur[channel].pop(field, None)
            effective = read_notify_channels(AGENT_SETTINGS, NOTIFY_FILE,
                lambda path, default: cur if str(path) == str(NOTIFY_FILE) else settings_json(path, default))
            required = {"pushplus": ("token",), "email": ("smtp_host", "username", "authcode", "to_addrs")}
            for channel, fields in required.items():
                row = effective.get(channel, {})
                if not row.get("enabled"):
                    continue
                for field in fields:
                    if body.get("clear_secrets") and (channel, field) in SECRET_FIELDS:
                        continue  # Preserve the existing explicit secret-clear operation.
                    if not row.get(field):
                        errors[channel + "." + field] = "首次配置需填写此字段"
                if channel == "email":
                    try:
                        settings_port(row.get("smtp_port", 465))
                    except (ValueError, TypeError):
                        errors["email.smtp_port"] = "端口须为 1 至 65535 的整数"
            if errors:
                return jsonify(settings_failure("validation_failed", "请检查标记的通知字段", errors))
            save_json(NOTIFY_FILE, cur)
        return jsonify({"ok": True, **notify_config_view()})
    except Exception:
        return jsonify(settings_failure("settings_write_failed", "通知配置保存失败，请检查配置与存储"))


@app.post("/api/notify/test")
def api_notify_test():
    """发一条测试通知，用来验证凭据真的可用（而不是"填了就算配好"）。"""
    ch = notify_channels()
    if not ch:
        return jsonify({"ok": False, "error": "还没有任何通知渠道配置"})
    title = "【SVE】通知测试"
    html = ("<h3>通知通道自检</h3>"
            "<p><b>来源：</b>%s</p>"
            "<p><b>时间：</b>%s</p>"
            "<p>看到这条说明配置可用。</p>"
            % (STATE.get("active") or "自动报名代理",
               time.strftime("%Y-%m-%d %H:%M:%S")))
    results = []
    try:
        import notification_sender as _n
        for name, ok, detail in _n.notify_all(ch, title, html):
            results.append({"channel": name if name in NOTIFY_FIELDS else "unknown", "ok": bool(ok),
                            "detail": "发送成功" if ok else "发送失败，请检查通知配置或网络"})
    except Exception as e:
        return jsonify(settings_failure("notification_failed", "通知发送未确认，请检查配置与网络"))
    ok_any = any(r["ok"] for r in results)
    log("通知测试：%s" % ("成功" if ok_any else "失败"))
    return jsonify({"ok": ok_any, "results": results})


@app.get("/api/remote/status")
def api_remote_status():
    try:
        info = {"ok": True, "config": remote_config_view(),
                "tunnel": {"running": bool(STATE.get("tunnel") and STATE["tunnel"].running)}}
        info["agent_ok"] = bool(remote_enabled() and agent().health()[0])
        return jsonify(info)
    except Exception:
        return jsonify(settings_failure("connection_failed", "无法读取连接状态，请检查连接"))


@app.post("/api/remote/config")
def api_remote_config():
    try:
        return configure_remote()
    except AccountConflict:
        return jsonify(settings_failure("remote_busy", "连接正在使用或切换，请稍后重试"))
    except Exception:
        return jsonify(settings_failure("connection_failed", "连接设置未生效，请检查连接及运行环境"))


@remote_operation
def configure_remote():
    """设置/关闭远端执行模式。

    enabled=True  → 启动隧道，之后所有订阅都发往服务器；
    enabled=False → 无本机缓存订阅时停隧道；有缓存时拒绝未经确认的执行权交接。
    """
    body = request.json or {}
    if not body.get("enabled"):
        with LOCK:
            if remote_enabled() and STATE["subs"]:
                # Stopping the tunnel does not stop remote schedulers. No API yet
                # acknowledges a completed handover; even orphan caches may run
                # after a later login. Keep remote mode and all data intact.
                return jsonify({"ok": False, "code": "execution_handover_required",
                                "error": "本机仍有缓存订阅，无法确认远端执行已停止；"
                                         "暂不支持安全自动交接，请保持远端执行模式"})
        t = STATE.get("tunnel")
        if t:
            try:
                t.stop()
            except Exception:
                raise AccountConflict("隧道尚未确认退出，未关闭远端模式；请稍后重试") from None
        with LOCK:
            disabled = dict(STATE["remote"], enabled=False)
            save_json(REMOTE_FILE, disabled)
            STATE["tunnel"] = None
            STATE["remote"] = disabled
        log("已关闭远端执行模式（订阅回到本机）")
        restore_missing_schedulers("关闭远端执行模式")
        return jsonify({"ok": True, "enabled": False})

    previous = dict(STATE["remote"])
    cfg = dict(previous, enabled=True)
    errors = {}
    for field in REMOTE_FIELDS:
        value = body.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if field in ("host", "user", "key"):
            if not isinstance(value, str):
                errors[field] = "请填写有效文本"
            else:
                cfg[field] = value.strip()
        else:
            try:
                cfg[field] = settings_port(value.strip() if isinstance(value, str) else value)
            except (ValueError, TypeError):
                errors[field] = "端口须为 1 至 65535 的整数"
    for field in ("host", "user"):
        if not isinstance(cfg.get(field), str) or not cfg[field].strip():
            errors[field] = "首次配置需填写此字段"
    if cfg.get("key") is not None and not isinstance(cfg["key"], str):
        errors["key"] = "请填写有效文本"
    for field, default in (("ssh_port", 22), ("local_port", DEFAULT_LOCAL_PORT), ("remote_port", DEFAULT_REMOTE_PORT)):
        try:
            value = cfg.get(field)
            cfg[field] = settings_port(default if value is None or value == "" else value)
        except (ValueError, TypeError):
            errors[field] = "端口须为 1 至 65535 的整数"
    cfg.setdefault("key", "")
    if errors:
        return jsonify(settings_failure("validation_failed", "请检查标记的连接字段", errors))
    with LOCK:
        if STATE["scheduler_threads"] or any(
                not s.completed and not s.stop_flag.is_set() for s in STATE["schedulers"].values()):
            return jsonify({"ok": False, "error": "本机仍有运行中的订阅，请先停止本机执行，再启用远端模式"})
        previous = dict(STATE["remote"])
    old_tunnel = STATE.get("tunnel")
    try:
        start_tunnel(cfg)
        with LOCK:
            # Local registration can complete while the tunnel is starting.
            # Check and commit under the same lock used by scheduler registration.
            if STATE["scheduler_threads"] or any(
                    not s.completed and not s.stop_flag.is_set() for s in STATE["schedulers"].values()):
                raise AccountConflict("本机执行已启动，未启用远端模式；请先停止本机执行")
            save_json(REMOTE_FILE, cfg)
            STATE["remote"] = cfg
    except Exception:
        replacement = STATE.get("tunnel")
        if replacement is not old_tunnel:
            if replacement:
                replacement.stop()
            STATE["tunnel"] = old_tunnel
        if previous.get("enabled") and old_tunnel and not old_tunnel.running:
            try:
                old_tunnel.start()
            except Exception:
                log("原连接暂未恢复，保留原远端执行模式，禁止本机接管")
        raise
    log("已启用远端执行模式（订阅将由服务器代理执行）")
    threading.Timer(8.0, refresh_remote_accounts).start()
    return jsonify({"ok": True, "enabled": True, "config": remote_config_view(cfg)})


def read_account_summary(link, *, subscription_identity=False, summary=None) -> list:
    summary = link.accounts_summary() if summary is None else summary
    if not isinstance(summary, dict) or summary.get("schema_version") != 2 or summary.get("session_write") != 2:
        raise AccountConflict("服务器不支持 UID 协议，请先升级服务器；不会回退到旧身份协议")
    if subscription_identity and summary.get("subscription_identity") != 2:
        raise AccountConflict("服务器不支持 UID 订阅身份校验")
    rows = summary.get("accounts")
    if not isinstance(rows, list):
        raise AccountConflict("服务器账号摘要格式无效")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise AccountConflict("服务器账号摘要行格式无效")
        aid = normalized_uid(row.get("account_id"))
        if aid != row["account_id"] or aid in seen or normalized_uid(row.get("uid")) != aid or type(row.get("version")) is not int or row["version"] < 1:
            raise AccountConflict("服务器账号摘要身份或版本无效")
        seen.add(aid)
    return rows


def compare_remote_accounts() -> dict:
    if not remote_enabled():
        return {"ok": False, "error": "未启用远端模式"}
    rows = read_account_summary(agent())
    remote = {row["account_id"]: row for row in rows}
    with LOCK:
        local = dict(STATE["accounts"])
    return {"ok": True, "readonly": True, "pushed": [], "removed": [],
            "total": len(remote), "local_total": len(local),
            "local_only": sorted(local.keys() - remote.keys()),
            "remote_only": sorted(remote.keys() - local.keys()), "conflicts": [],
            "note": "服务器维护成员集合；本机缓存仅供登录，不自动同步或恢复账号。"}


def identity_accounts():
    if remote_enabled():
        link = agent()
        summary = link.accounts_summary()
        rows = read_account_summary(link, summary=summary)
        g.identity_tombstones = summary.get("tombstones", {})
        return {row["account_id"]: Account.from_dict(row) for row in rows}
    return STATE["accounts"]


def resolve(accounts, selector, *, explicit=False, expected_uid=None):
    tombstones = getattr(g, "identity_tombstones", {}) if has_request_context() and remote_enabled() else STATE["tombstones"]
    if not explicit and expected_uid is None:
        try:
            selector_uid = normalized_uid(selector)
        except AccountConflict:
            selector_uid = None
        if selector_uid in tombstones or (isinstance(selector, str) and any(
                selector in {row.get("nickname"), *row.get("legacy_names", [])} for row in tombstones.values())):
            raise AccountConflict("旧账号参数曾属于已删除身份，需要明确 account_id，不能转用同名账号")
    return resolve_value(accounts, selector, explicit=explicit, expected_uid=expected_uid)


def request_account(body=None, *, allow_default=False):
    if body is None:
        body = request.get_json(silent=True) if request.method != "GET" else request.args
    body = body or {}
    if "expected_uid" in body:
        normalized_uid(body["expected_uid"])
    selected = request.cookies.get("sve_account_id")
    if not allow_default and not selected and not any(field in body for field in ("account_id", "account", "name")):
        raise AccountConflict("请求缺少账号 ID，请刷新并明确选择账号")
    accounts = identity_accounts()
    if "account_id" in body:
        return resolve(accounts, body["account_id"], explicit=True, expected_uid=body.get("expected_uid"))
    if "account" in body or "name" in body:
        return resolve(accounts, body.get("account", body.get("name")), expected_uid=body.get("expected_uid"))
    if selected:
        if allow_default and selected not in accounts:
            return STATE["active"] if STATE["active"] in accounts else next(iter(accounts), None)
        return resolve(accounts, selected, explicit=True)
    if allow_default:
        default = STATE["active"]
        return default if default in accounts else next(iter(accounts), None)
    raise AccountConflict("请求缺少账号 ID，请刷新并明确选择账号")


def check_subscription_identity(body, account):
    if account not in STATE["accounts"] or normalized_uid(account) != STATE["accounts"][account].account_id:
        raise AccountConflict("订阅身份已失效")
    if "expected_uid" in body and normalized_uid(body["expected_uid"]) != account:
        raise AccountConflict("订阅预期 UID 不符")
    if "expected_version" in body and body["expected_version"] != STATE["accounts"][account].version:
        raise AccountConflict("订阅账号版本已变化，请刷新后重试")


@remote_operation
def forward_subscription(tid, account, *, cancel=False):
    if not remote_enabled():
        raise AccountConflict("执行模式已变化")
    ready_login_tunnel()
    rows = read_account_summary(agent(), subscription_identity=True)
    if account not in {row["account_id"] for row in rows}:
        raise AccountConflict("服务器账号 ID 不存在")
    operation = agent().unsubscribe if cancel else agent().subscribe
    return operation(tid, account, expected_uid=account)


def refresh_remote_accounts() -> None:
    if not remote_enabled():
        return
    try:
        result = compare_remote_accounts()
        if result.get("ok"):
            log("远端账号只读比较：本机 %d，服务器 %d，仅本机 %d，仅服务器 %d，身份冲突 %d"
                % (result["local_total"], result["total"], len(result["local_only"]),
                   len(result["remote_only"]), len(result["conflicts"])))
        else:
            log("远端账号只读比较未完成，请检查服务器版本与连接")
    except Exception:
        log("远端账号只读比较失败；保留两端原数据")


@app.post("/api/remote/push_accounts")
def api_remote_push_accounts():
    """Old UI button remains compatible but is now strictly read-only."""
    try:
        return jsonify(compare_remote_accounts())
    except AccountConflict:
        return jsonify(settings_failure("remote_busy", "无法读取账号摘要，请检查连接或稍后重试"))
    except Exception:
        return jsonify({"ok": False, "error": "无法读取账号摘要，请检查服务器连接与版本"})


def auto_push_accounts(reason: str) -> None:
    """Compatibility no-op: stale callbacks must never submit an account table."""
    return


def operation_fingerprint(kind, payload):
    return hashlib.sha256(json.dumps([kind, payload], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def operation_result(operation_id, fingerprint):
    if not isinstance(operation_id, str) or not 8 <= len(operation_id) <= 128:
        raise AccountConflict("缺少有效幂等操作 ID")
    receipt = STATE["operations"].get(operation_id)
    if receipt:
        if receipt["fingerprint"] != fingerprint:
            raise AccountConflict("幂等操作 ID 已用于其他请求")
        if receipt["result"]["account_id"] in STATE["tombstones"] and receipt["kind"] != "delete":
            raise AccountConflict("该操作的账号已删除，拒绝重放")
        return dict(receipt["result"])
    return None


def commit_login(acc: Account, *, expected_revision, select=False, operation_id, fingerprint, restore=False, binding=None):
    global ACCOUNTS_LOADED_OK
    parse_account(acc.to_dict(), Account)
    aid = acc.account_id
    with LOCK:
        if not ACCOUNTS_LOADED_OK:
            raise AccountConflict("身份数据集未成功载入，保留原件并拒绝写入")
        previous = operation_result(operation_id, fingerprint)
        if previous:
            return previous
        current = STATE["accounts"].get(aid)
        tombstone = STATE["tombstones"].get(aid)
        expected = revision(current) if current else (tombstone["version"] if tombstone else 0)
        if expected_revision != expected or (tombstone and not restore):
            raise AccountConflict("账号版本已变化或已删除；刷新后明确重新登录/恢复，拒绝旧缓存覆盖")
        acc.name = acc.nickname or ("uid%s" % aid)
        acc.legacy_names = sorted(set((current.legacy_names if current else (tombstone or {}).get("legacy_names", [])) +
                                     ([current.name] if current else []) + [acc.name]))
        acc.version = expected + 1
        acc.session_version = (current.session_version if current else (tombstone or {}).get("session_version", 0)) + 1
        acc.profile_version = (current.profile_version if current else (tombstone or {}).get("profile_version", 0)) + 1
        acc.profile_updated_at = time.time()  # Login already verified official info.
        result = {"ok": True, "name": acc.name, "nickname": acc.nickname, "uid": acc.uid,
                  "account_id": aid, "version": acc.version}
        accounts = {key: value.to_dict() for key, value in STATE["accounts"].items()}
        accounts[aid] = acc.to_dict()
        tombstones = dict(STATE["tombstones"])
        tombstones.pop(aid, None)
        operations = dict(STATE["operations"])
        operations[operation_id] = {"kind": "session", "fingerprint": fingerprint, "result": result, "binding": binding}
        active = aid if select else (STATE["active"] or aid)
        persist_dataset(accounts=accounts, active_account_id=active, tombstones=tombstones, operations=operations)
        if current:
            with current.credential_lock:
                current.__dict__.update({key: value for key, value in acc.to_dict().items() if key != "account_id"})
                current._retired = False
                current._identity_blocked = False
                current._verified_identity = (current.sessionid, current.deviceid, aid)
                current._verified_official_uid = acc.uid
        else:
            acc._verified_identity = (acc.sessionid, acc.deviceid, aid)
            acc._verified_official_uid = acc.uid
            STATE["accounts"][aid] = acc
        STATE.update(active=active, tombstones=tombstones, operations=operations)
        ACCOUNTS_LOADED_OK = True
        return result


@remote_operation
def save_explicit_login(acc: Account, observed: dict):
    aid = acc.account_id
    acc.name = acc.nickname or ("uid%s" % aid)
    parse_account(acc.to_dict(), Account)
    local = observed["accounts"]
    restore = observed.get("restore_account_id") == aid
    if observed.get("restore_account_id") and not restore:
        raise AccountConflict("本次登录 UID 与明确恢复的账号不一致")
    local_expected = local.get(aid, observed.get("tombstones", {}).get(aid, {}).get("version", 0) if restore else 0)
    with LOCK:
        if STATE["remote"] != observed["remote"]:
            raise AccountConflict("执行配置已变化，请重新登录；本次未提交服务器")
        current = STATE["accounts"].get(aid)
        local_version = revision(current) if current else (STATE["tombstones"].get(aid, {}).get("version", 0) if restore else 0)
        if local_version != local_expected:
            raise AccountConflict("本机身份已变化，请重新登录")
    if observed["remote"].get("enabled"):
        if ready_login_tunnel() is not observed["tunnel"]:
            raise AccountConflict("远端连接已重建，请重新登录")
        old = next((row for row in observed["remote_accounts"] if row["account_id"] == aid), None)
        expected = old["version"] if old else (observed.get("remote_tombstones", {}).get(aid, {}).get("version", 0) if restore else 0)
        result = observed["link"].save_session(acc.to_dict(), expected,
                                              operation_id=observed["operation_id"], restore=restore)
        if not result.get("ok"):
            raise AccountConflict(result.get("error") or "服务器未接受本次登录")
    return commit_login(acc, expected_revision=local_expected, select=not remote_enabled(),
                        operation_id=observed["operation_id"],
                        fingerprint=operation_fingerprint("login", acc.to_dict()), restore=restore,
                        binding=observed.get("cookie_binding"))


def ready_login_tunnel():
    tunnel = STATE.get("tunnel")
    if (tunnel is None or not tunnel.running
            or tunnel.connection_config != STATE["remote"]):
        raise AccountConflict("远端隧道尚未就绪或配置不匹配，请连接稳定后重新登录")
    return tunnel


@remote_operation
def login_snapshot() -> dict:
    """Capture both sides before acquiring a new session; never refresh its CAS token."""
    with LOCK:
        snapshot = {"accounts": {name: revision(acc) for name, acc in STATE["accounts"].items()},
                    "remote": dict(STATE["remote"]), "operation_id": uuid.uuid4().hex,
                    "tombstones": copy.deepcopy(STATE["tombstones"])}
        link = agent() if remote_enabled() else None
    if link is not None:
        snapshot["tunnel"] = ready_login_tunnel()
        try:
            summary = link.accounts_summary()
            snapshot["remote_accounts"] = [dict(row) for row in read_account_summary(link, summary=summary)]
            snapshot["remote_tombstones"] = copy.deepcopy(summary.get("tombstones", {}))
        except AccountConflict:
            raise
        except Exception:
            raise AccountConflict("无法读取登录前的远端账号版本，请检查连接后重试") from None
        snapshot["link"] = link
    return snapshot


def persist_subs() -> None:
    persist_dataset()


def persist_records() -> None:
    persist_dataset()


# ------------------------------------------------------------------ 通知
AGENT_SETTINGS = str(RUNTIME.settings)

from notification_config import ENV_NOTIFY, SECRET_FIELDS, channels as read_notify_channels


def notify_channels() -> dict:
    """解析通知渠道配置，三层优先级：

        环境变量  >  <DATA_DIR>/notify.json（界面写入）  >  RUNTIME.settings

    未迁移时保留旧配置；外部模式只读外部 settings，不回退到代码目录。
    """
    return read_notify_channels(AGENT_SETTINGS, NOTIFY_FILE, load_json)


def notify_sources() -> dict:
    return {"%s.%s" % (channel, field): "env"
            for (channel, field), name in ENV_NOTIFY.items() if os.environ.get(name)}


def notify_config_view() -> dict:
    """No personal configuration values or credential fragments reach the page."""
    cfg = read_notify_channels(AGENT_SETTINGS, NOTIFY_FILE, settings_json)
    src = notify_sources()
    out = {}
    for channel, fields in NOTIFY_FIELDS.items():
        current = cfg.get(channel, {})
        row = {}
        for field in fields:
            source = src.get(channel + "." + field, "file" if field in current else "default")
            row[field + "__source"] = source
            row[field + "__editable"] = source != "env"
            row[field + "__set"] = field in current and current[field] not in (None, "", [])
            if field in ("enabled", "use_ssl"):
                row[field] = bool(current.get(field, field == "use_ssl"))
        out[channel] = row
    return {"channels": out}


def send_notify(title: str, html: str) -> None:
    """发送通知。复用 core/notification_sender.py 的实现（PushPlus + 邮件 + 重试）。"""
    ch = notify_channels()
    if not ch:
        log("未找到通知配置（%s），跳过通知" % AGENT_SETTINGS)
        return
    try:
        import notification_sender as _n
        results = _n.notify_all(ch, title, html)
        for name, ok, detail in results:
            log("通知[%s] %s %s" % (name, "成功" if ok else "失败", detail))
    except Exception as e:
        log("通知发送异常: %s" % e)


def notify_completed(s: dict, already: bool) -> None:
    """报名成功（或发现已是已报名）后的通知。"""
    who = escape_html(account_label(s.get("account_id")))
    html = (
        "<h3>【SVE】自动报名成功</h3>"
        "<p><b>赛事：</b>%s（id=%s）</p>"
        "<p><b>账号：</b>%s</p>"
        "<p><b>时间：</b>%s</p>"
        "<p><b>说明：</b>%s</p>"
        % (s.get("name"), s.get("tournament_id"), who,
           time.strftime("%Y-%m-%d %H:%M:%S"),
           "订阅启动时该账号已是「已报名」，无需重复提交" if already
           else "已提交报名，并经过「立即复核 + 静默二次确认」两段校验")
    )
    send_notify("【SVE】自动报名成功 - %s" % (s.get("name") or s.get("tournament_id")), html)


def notify_time_conflict(s: dict, reason: str, code=None, detail: str = "") -> None:
    """「与已报名赛事时间冲突」导致订阅被自动取消时的通知。

    这种失败不会自行消失（除非用户处理掉那场已报名的赛事），所以我们主动
    取消订阅、停止无谓的轮询，并把决定权交回用户。
    """
    who = escape_html(account_label(s.get("account_id")))
    html = (
        "<h3>【SVE】订阅已自动取消：与已报名赛事时间冲突</h3>"
        "<p><b>赛事：</b>%s（id=%s）</p>"
        "<p><b>账号：</b>%s</p>"
        "<p><b>失败原因：</b>%s</p>"
        "<p><b>服务端错误码：</b>%s</p>"
        "<p><b>时间：</b>%s</p>"
        "<hr>"
        "<p><b>已做的处理：</b>已停止该订阅的守护，并把它从订阅列表移除。</p>"
        "<p><b>为什么这么做：</b>本场与你已报名的另一场赛事开赛时间冲突，"
        "在你处理掉那场已报名的赛事之前，这个冲突不会消失——"
        "继续守护只会不断重复失败（实测曾反复提交 503 次）。</p>"
        "<p><b>需要你决定：</b>要么取消那场已报名的赛事，再重新订阅本场；"
        "要么放弃本场。程序不会替你选。</p>"
        % (s.get("name"), s.get("tournament_id"), who, reason,
           code or "（服务端未提供）", time.strftime("%Y-%m-%d %H:%M:%S"))
    )
    send_notify("【SVE】订阅已取消（时间冲突）- %s"
                % (s.get("name") or s.get("tournament_id")), html)


def notify_account_purged(account: str, dropped: list, *, reason: str = "") -> None:
    """账号被移除、其订阅被自动清理时的通知。

    用户明确要求"删账号要通知"——订阅凭空消失会让人以为是自己点的。
    """
    if not dropped:
        return
    shown = dropped[:8]
    items = "".join(
        "<li>%s（id=%s）</li>" % (d.get("name") or "（未记录赛事名）", d.get("tid"))
        for d in shown
    )
    if len(dropped) > len(shown):
        items += "<li>…以及另外 %d 场</li>" % (len(dropped) - len(shown))
    html = (
        "<h3>【SVE】账号已移除，其订阅已自动清理</h3>"
        "<p><b>账号：</b>%s</p>"
        "<p><b>原因：</b>%s</p>"
        "<p><b>已清理 %d 条订阅</b>（账号不存在了，这些订阅已无法运行）：</p>"
        "<ul>%s</ul>"
        "<p><b>时间：</b>%s</p>"
        "<hr>"
        "<p>这是<b>自动清理</b>，不是订阅自己消失的。</p>"
        "<p>若该账号之后重新加回来，需要<b>重新订阅</b>这些赛事。</p>"
        % (escape_html(account), reason or "账号已被删除", len(dropped), items,
           time.strftime("%Y-%m-%d %H:%M:%S"))
    )
    send_notify("【SVE】账号已移除，%d 条订阅已清理 - %s" % (len(dropped), account), html)


def add_record(account: str, uid, tid, name: str, source: str,
               status: str = "已报名") -> None:
    """记一笔"已报名"。同一 (账号, 赛事) 只保留最新一条。"""
    key = sub_key(account, tid)
    with LOCK:
        old = STATE["records"].get(key) or {}
        STATE["records"][key] = {
            "account_id": account, "account": account, "uid": uid, "tournament_id": str(tid),
            "name": name, "source": source, "status": status,
            "at": old.get("at") or time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "checked_at": 0.0,
            "player_num": old.get("player_num"), "max": old.get("max"),
        }
        for field in ("migration_sources", "migration_duplicates"):
            if field in old:
                STATE["records"][key][field] = copy.deepcopy(old[field])
        persist_records()


def refresh_records(max_age: float = RECORD_TTL, account_id=None) -> None:
    """后台回查每条已报名记录的真实状态（同一时刻只跑一个）。"""
    with LOCK:
        if RECORDS_REFRESHING["flag"]:
            return
        stale = [(k, dict(v)) for k, v in STATE["records"].items()
                 if (account_id is None or v["account_id"] == account_id)
                 and v["account_id"] in STATE["accounts"]
                 and time.time() - float(v.get("checked_at") or 0) > max_age]
        RECORDS_REFRESHING["flag"] = True
    if not stale:
        with LOCK:
            RECORDS_REFRESHING["flag"] = False
        return

    def worker():
        try:
            for key, rec in stale:
                acc = STATE["accounts"].get(rec.get("account_id"))
                if not acc:
                    with LOCK:
                        if key in STATE["records"]:
                            STATE["records"][key]["status"] = "账号已删除"
                            STATE["records"][key]["checked_at"] = time.time()
                    continue
                try:
                    it = SveClient(acc).detail(rec["tournament_id"])
                    with LOCK:
                        r = STATE["records"].get(key)
                        if r:
                            r["status"] = it.get("zh_user_status") or r.get("status")
                            r["player_num"] = it.get("player_num")
                            r["max"] = it.get("max_player_number")
                            r["name"] = it.get("name") or r.get("name")
                            r["checked_at"] = time.time()
                            r["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    with LOCK:
                        if key in STATE["records"]:
                            STATE["records"][key]["checked_at"] = time.time()
                time.sleep(0.3)
            with LOCK:
                persist_records()
        finally:
            with LOCK:
                RECORDS_REFRESHING["flag"] = False

    threading.Thread(target=worker, daemon=True, name="rec-refresh").start()


@app.errorhandler(AccountConflict)
def identity_error(error):
    return jsonify({"ok": False, "code": "identity_conflict", "error": str(error)})


@app.errorhandler(OSError)
def identity_disk_error(error):
    return jsonify({"ok": False, "code": "storage_error", "error": "身份数据保存失败，保留原数据；请检查存储后重试"})


def account_label(aid):
    account = STATE["accounts"].get(aid)
    if account:
        return "%s (UID %s)" % (account.nickname or account.name, aid)
    tombstone = STATE["tombstones"].get(aid, {})
    return "%s (UID %s，已删除)" % (tombstone.get("nickname", "账号"), aid)


def active_client():
    aid = request_account()
    return SveClient(STATE["accounts"][aid])


def require_client():
    return active_client()


# ------------------------------------------------------------------ 页面
@app.get("/settings")
@app.get("/subs")
@app.get("/")
def index():
    return send_from_directory(os.path.join(HERE, "ui"), "index.html")


@app.get("/<path:fn>")
def ui_files(fn):
    resp = send_from_directory(os.path.join(HERE, "ui"), fn)
    # sw.js 必须不被缓存，否则浏览器会一直用旧的 Service Worker，
    # 表现为"改了界面却怎么刷新都不生效"（SW 自身控制着后续所有缓存）。
    # manifest 同理：改了就希望立刻生效。
    if fn.endswith(("sw.js", "manifest.json")):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    if fn.endswith(".js"):
        # 有些环境对 .js 默认给 text/plain，SW 要求 JavaScript MIME
        resp.headers["Content-Type"] = "application/javascript; charset=utf-8"
    return resp


# ------------------------------------------------------------------ API
@app.get("/api/version")
@app.get("/api/health")
def api_release_health():
    # Never re-read disk metadata here: a replaced file is not a running process.
    return jsonify({"ok": ACCOUNTS_LOADED_OK and not STATE["storage_failed"],
                    "release": RELEASE_AT_START, "started_at": PROCESS_STARTED_AT,
                    "pid": os.getpid(), "storage_ready": ACCOUNTS_LOADED_OK and not STATE["storage_failed"]})


def scheduler_status(key, row):
    """Caller holds LOCK. Never infer thread liveness from a scheduler flag."""
    sched = STATE["schedulers"].get(key)
    worker = STATE["workers"].get(key)
    alive = bool(worker is not None and worker.is_alive())
    completed = bool(row.get("done_handled") or (sched and sched.completed))
    if completed:
        status = "completed"
    elif row.get("identity_blocked"):
        status = "identity_blocked"
    elif not row.get("enabled", True):
        status = "paused"
    elif row["account_id"] not in STATE["accounts"] or row.get("orphan"):
        status = "orphan"
    elif sched and sched.stop_flag.is_set():
        status = "cancelled"
    elif not alive or sched is None:
        status = "worker_dead"
    else:
        status = "active"
    progress = sched.progress.snapshot() if sched is not None and hasattr(sched, "progress") else None
    return {"account_id": row["account_id"], "tournament_id": str(row["tournament_id"]),
            "status": status, "thread_alive": alive, "progress": progress}


@app.get("/api/scheduler/health")
def api_scheduler_health():
    # Local watchdog protocol; no nicknames, logs, sessions or upstream requests.
    if request.remote_addr not in {"127.0.0.1", "::1"}:
        return jsonify({"ok": False, "code": "loopback_required"}), 403
    if not LOCK.acquire(timeout=0.2):
        return jsonify({"ok": False, "code": "state_busy"}), 503
    try:
        return jsonify({"health_version": 1, "ok": ACCOUNTS_LOADED_OK and not STATE["storage_failed"],
                        "mode": "remote" if remote_enabled() else "executor",
                        "process_id": "%s:%s" % (os.getpid(), PROCESS_STARTED_AT),
                        "tasks": [scheduler_status(key, row) for key, row in STATE["subs"].items()],
                        "pending_count": len(STATE["pending"])})
    finally:
        LOCK.release()


_LOGIN_CAP: dict = {}


def login_capability() -> dict:
    """本机能否执行「打开浏览器登录」。

    为什么需要这个能力位：登录靠的是**在运行本服务的那台机器上开一个真浏览器**
    读它的 Cookie。服务器是无头环境，没有浏览器，那个按钮点了必然抛
    「未找到 Chrome 或 Edge 浏览器」——对用户毫无意义。界面据此把它换成
    可操作的引导（手机 App 的「新增账号」/ 电脑客户端）。

    结果缓存：find_browser() 会探若干固定路径，没必要每次 /api/state 都做一遍。
    复用 weblogin.find_browser()，不在别处另写一套判断（避免两处名单分叉）。
    """
    if "value" not in _LOGIN_CAP:
        try:
            path, name = find_browser()
        except Exception:
            path, name = None, ""
        _LOGIN_CAP["value"] = {"can_login": bool(path), "browser": name}
    return _LOGIN_CAP["value"]


PROXY_ROUTES = {"/api/state", "/api/tournaments", "/api/my_tournaments", "/api/detail",
                "/api/enroll", "/api/cancel", "/api/batch_enroll", "/api/records",
                "/api/records/scan", "/api/records/forget", "/api/account/forget",
                "/api/account/profile/refresh"}


@remote_operation
def proxy_identity_request():
    ready_login_tunnel()
    body = dict(request.get_json(silent=True) or {})
    params = dict(request.args)
    no_selector = {"/api/records", "/api/records/scan", "/api/batch_enroll"}
    if request.path == "/api/batch_enroll":
        accounts = identity_accounts()
        selectors = body.get("account_ids", body.get("accounts", []))
        if not isinstance(selectors, list):
            raise AccountConflict("账号列表格式无效")
        body["account_ids"] = [resolve(accounts, value, explicit="account_ids" in body) for value in selectors]
        body.pop("accounts", None)
    elif request.path == "/api/account/forget" and (
            type(body.get("expected_version")) is not int or not body.get("operation_id")):
        raise AccountConflict("删除需要明确 ID、版本和幂等操作标识，请刷新界面")
    elif request.path == "/api/records/forget" and "account_id" in body:
        # The agent validates live/tombstone membership, UID and record key.
        # A deleted identity is deliberately absent from the live summary.
        body["account_id"] = normalized_uid(body["account_id"])
    elif request.path not in no_selector:
        aid = request_account(body if request.method != "GET" else params, allow_default=request.path == "/api/state")
        if aid is not None:
            (params if request.method == "GET" else body)["account_id"] = aid
    result = agent().endpoint(request.method, request.path, body if request.method != "GET" else None, params)
    if request.path == "/api/state" and result.get("ok") is not False:
        result["login"] = STATE["login"]
        result["can_login"] = login_capability()["can_login"]
    if request.path == "/api/state":
        result["remote"] = dict(remote_config_view(), agent_ok=result.get("ok") is not False)
        result["log"] = []
        if result.get("ok") is False:
            result["error"] = "无法读取服务器状态，请检查连接"
    return jsonify(result)


@app.before_request
def proxy_data_routes():
    if STATE["storage_failed"] and request.path.startswith("/api/") and request.path != "/api/scheduler/health":
        return jsonify({"ok": False, "code": "storage_unavailable", "error": "身份存储待核对，执行已暂停；检查存储后重启读取，不覆盖数据快照"})
    if request.method == "POST" and request.content_length and not isinstance(request.get_json(silent=True), dict):
        raise AccountConflict("JSON 请求必须是对象；未执行任何账号操作")
    if remote_enabled() and request.path in PROXY_ROUTES:
        return proxy_identity_request()


@app.get("/api/state")
def api_state():
    selected = request_account(allow_default=True)
    with LOCK:
        accs = [{"account_id": aid, "name": a.name, "uid": a.uid, "nickname": a.nickname,
                 "label": account_label(aid), "version": a.version, "active": aid == selected,
                 **profile_status(a)}
                for aid, a in STATE["accounts"].items()]
        grouped = {}
        for key, row in STATE["subs"].items():
            aid = row["account_id"]
            sched = STATE["schedulers"].get(key)
            output = {field: row.get(field) for field in ("name", "uid", "status", "player_num", "max", "enabled", "tournament_id")}
            output.update(key=key, account_id=aid, account=account_label(aid),
                          window=row.get("window_desc"), orphan=False,
                          completed=bool(sched and sched.completed), attempts=sched.attempts if sched else 0,
                          running=bool(STATE["workers"].get(key) is not None and STATE["workers"][key].is_alive()
                                       and sched and not sched.completed and not sched.stop_flag.is_set()),
                          logs=row.get("logs", [])[-60:])
            grouped.setdefault(aid, []).append(output)
        return jsonify({"ok": True, "schema_version": 2, "active_account_id": selected,
                        "active": account_label(selected) if selected else None, "accounts": accs,
                        "subs_grouped": grouped, "subs": [row for rows in grouped.values() for row in rows],
                        "mine": [row["tournament_id"] for row in STATE["subs"].values() if row["account_id"] == selected],
                        "sub_count": len(STATE["subs"]), "pending_count": len(STATE["pending"]),
                        "deleted_accounts": [{"account_id": aid, "version": row["version"], "label": account_label(aid)} for aid, row in STATE["tombstones"].items()],
                        "can_login": login_capability()["can_login"], "login_browser": login_capability()["browser"],
                        "login": STATE["login"], "log": [], "remote": remote_config_view()})


def observed_login():
    token = request.cookies.get("sve_login_intent")
    if token:
        with LOCK:
            intent = STATE["login_intents"].get(token)
            if intent is None or time.monotonic() - intent["created"] > 1800:
                raise AccountConflict("登录意图已过期或服务已重启，请从界面重新发起登录")
            return dict(intent["snapshot"])
    return login_snapshot()


@app.post("/api/login/intent")
def api_login_intent():
    body = request.get_json(silent=True) or {}
    snapshot = login_snapshot()
    if body.get("restore_account_id") is not None:
        aid = normalized_uid(body["restore_account_id"])
        tombstones = snapshot.get("remote_tombstones", {}) if snapshot["remote"].get("enabled") else snapshot["tombstones"]
        if aid not in tombstones or type(body.get("expected_version")) is not int or body["expected_version"] != tombstones[aid]["version"]:
            raise AccountConflict("恢复版本已变化，请刷新后明确恢复指定 UID")
        snapshot["restore_account_id"] = aid
    token = snapshot["operation_id"]
    with LOCK:
        now = time.monotonic()
        STATE["login_intents"] = {key: item for key, item in STATE["login_intents"].items() if now - item["created"] <= 1800}
        if len(STATE["login_intents"]) >= 100:
            raise AccountConflict("登录流程过多，请稍后重试")
        STATE["login_intents"][token] = {"snapshot": snapshot, "created": now}
    response = jsonify({"ok": True})
    response.set_cookie("sve_login_intent", token, httponly=True, samesite="Lax")
    return response


@app.post("/api/login/start")
def api_login_start():
    if STATE["login"].get("running"):
        return jsonify({"ok": False, "error": "登录流程已在进行中"})
    # 无浏览器时提前拦住，给出可操作的指引。
    # 不做这层判断的话，会走到 worker 里抛 "未找到 Chrome 或 Edge 浏览器" ——
    # 那是实现细节，用户既看不懂也不知道下一步该做什么。
    if not login_capability()["can_login"]:
        return jsonify({"ok": False, "error":
                        "这台设备上没有浏览器，无法登录新账号。"
                        "请在手机 App 里用「新增账号」，"
                        "或用新版电脑客户端明确登录后提交单个会话。"})

    try:
        observed = observed_login()
    except AccountConflict as e:
        return jsonify({"ok": False, "error": str(e)})

    def worker():
        STATE["login"] = {"running": True, "message": "正在打开登录窗口…", "ok": False, "error": ""}
        try:
            with LoginWindow(port=9210) as w:
                STATE["login"]["message"] = "请在弹出的窗口中完成登录（移动端页面）"
                log("登录窗口已打开，等待登录…")
                acc = w.wait_for_login(
                    timeout=900,
                    on_status=lambda m: STATE["login"].__setitem__("message", m))
            save_explicit_login(acc, observed)
            STATE["login"] = {"running": False, "ok": True,
                              "message": "登录成功: %s (uid=%s)" % (acc.nickname, acc.uid),
                              "error": ""}
            log("登录成功: %s uid=%s" % (acc.nickname, acc.uid))
            if not remote_enabled():
                restore_missing_schedulers("登录成功")
        except LoginWindowClosed:
            # 用户自己关了窗口，属于正常操作，不算错误；关键是**立刻结束流程**，
            # 否则 running 一直为真，会卡住后续的登录尝试。
            STATE["login"] = {"running": False, "ok": False, "message": "",
                              "error": "", "notice": "登录窗口已关闭，已取消本次登录。可再次点击登录。"}
            log("登录窗口被关闭，本次登录已取消")
        except Exception as e:
            STATE["login"] = {"running": False, "ok": False,
                              "message": "", "error": str(e)}
            log("登录失败: %s" % e)
            log(traceback.format_exc()[-600:])

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/login/status")
def api_login_status():
    return jsonify(STATE["login"])


@app.post("/api/login/cookie")
def api_login_cookie():
    """用**客户端读到的网易 Cookie** 换取 sessionid 并保存账号。

    为什么需要这个接口：读 Cookie 必须发生在「真正拥有该 Cookie 的浏览器」里。
      * 服务器无头 —— 没有浏览器可开
      * 手机上的 PWA —— 读不到 HttpOnly Cookie（JS 无权访问，且跨域）
      * **原生 Android App 的 WebView** —— 可以，Android 的 CookieManager 能读
    所以由 App 把 Cookie 送到这里，**交换会话与入库都在服务器完成**。
    结果与电脑客户端登录完全等价（sessionid 与设备无关）。

    安全约定：
      * 请求体含凭据，**响应里绝不回显 sessionid / 手机号**
      * 日志只记 Cookie 个数与失败原因，不记 Cookie 内容
    """
    body = request.json or {}
    raw = body.get("cookies")
    if not raw:
        return jsonify({"ok": False, "error": "缺少 cookies"})
    if isinstance(raw, dict):
        cookie = "; ".join("%s=%s" % (k, v) for k, v in raw.items())
    else:
        cookie = str(raw)
    cookie = cookie.strip()
    if not cookie:
        return jsonify({"ok": False, "error": "cookies 为空"})

    # 先做一次本地体检：不含会话 Cookie 名就直接拒绝，省掉一次注定失败的请求。
    # 实测坏 Cookie 时上游会返回 200 但**不带 session_id**（不是清晰报错），
    # 所以这里提前挡一道能让失败原因更明确。
    names = [seg.split("=", 1)[0].strip() for seg in cookie.split(";") if "=" in seg]
    # 只认**能换出会话**的那两个（NTES_SESS / NTES_YD_SESS）。
    # 不能把 l_yd_sign 也算进来：它出现得比真正的会话 Cookie 早，会让客户端
    # 过早提交——服务器放行、上游却换不出 session_id（实测踩过）。
    if not any(n in EXCHANGEABLE_COOKIE_NAMES for n in names):
        early = [n for n in names if n in SESSION_COOKIE_NAMES]
        if early:
            hint = ("只拿到辅助 Cookie（%s），真正的会话 Cookie（%s）还没出现。"
                    "请等登录页跳转完成后再试（若页面还在验证码/滑块步骤，请先完成它）。"
                    % (" / ".join(early), " / ".join(EXCHANGEABLE_COOKIE_NAMES)))
        else:
            hint = ("Cookie 里没有会话项（需要 %s 之一）"
                    % " / ".join(EXCHANGEABLE_COOKIE_NAMES))
        log("Cookie 换会话被拒（Cookie 名: %s）" % (" / ".join(names) or "无"))
        return jsonify({"ok": False, "error": hint})

    try:
        observed = observed_login()
    except AccountConflict as e:
        return jsonify({"ok": False, "error": str(e)})
    binding = operation_fingerprint("cookie", [cookie, body.get("udid") or ""])
    operation_id = body.get("operation_id") or observed["operation_id"]
    if not isinstance(operation_id, str) or not 8 <= len(operation_id) <= 128:
        raise AccountConflict("登录操作 ID 无效")
    observed["operation_id"] = operation_id
    observed["cookie_binding"] = binding
    with LOCK:
        previous = STATE["operations"].get(operation_id)
        if previous:
            if previous.get("binding") != binding:
                raise AccountConflict("本次登录操作已用于另一份 Cookie，拒绝更换身份")
            result = operation_result(operation_id, previous["fingerprint"])
            response = jsonify(result)
            response.set_cookie("sve_account_id", result["account_id"], httponly=True, samesite="Lax")
            return response
    udid = (body.get("udid") or "").strip()
    try:
        sid = SveClient.login_with_cookie(cookie, **({"udid": udid} if udid else {}))
    except SveError as e:
        log("Cookie 换会话失败（%d 个 Cookie: %s）: %s" % (len(names), " / ".join(names), str(e)[:120]))
        return jsonify({"ok": False, "error": "换取会话失败：%s" % e})
    except Exception as e:
        log("Cookie 换会话异常: %s" % e)
        return jsonify({"ok": False, "error": "换取会话异常：%s" % e})

    acc = Account(name="", sessionid=sid, user_agent="okhttp/4.9.2")
    if udid:
        acc.deviceid = udid
    try:
        info = SveClient(acc).info()
        # 注意：uid 在响应里的字段名是 "id"，不是 "uid"
        acc.uid = info.get("id")
        acc.nickname = info.get("nickname") or ""
        acc.mobile = info.get("mobile") or ""
    except Exception as e:
        return jsonify({"ok": False,
                        "error": "会话已换取，但读取账号信息失败：%s" % e})

    try:
        save_explicit_login(acc, observed)
    except (AccountConflict, OSError) as e:
        return jsonify({"ok": False, "error": str(e)})
    log("新增账号（手机端 Cookie 登录）：%s uid=%s" % (acc.account_id, acc.uid))
    # 新加的这个账号，可能正是之前被删掉、其订阅还留着的那一个
    # （订阅会被标 orphan 且没有调度器）——补启动，否则它永远不报名。
    if not remote_enabled():
        restore_missing_schedulers("手机端新增账号")
    response = jsonify({"ok": True, "name": acc.name, "uid": acc.uid,
                        "nickname": acc.nickname, "account_id": acc.account_id})
    response.set_cookie("sve_account_id", acc.account_id, httponly=True, samesite="Lax")
    return response


@app.post("/api/account/select")
def api_account_select():
    aid = request_account()
    response = jsonify({"ok": True, "account_id": aid})
    response.set_cookie("sve_account_id", aid, httponly=True, samesite="Lax")
    return response


@app.post("/api/account/forget")
def api_account_forget():
    body = request.get_json(silent=True) or {}
    aid = normalized_uid(body["account_id"]) if "account_id" in body else request_account(body)
    if "expected_uid" in body and normalized_uid(body["expected_uid"]) != aid:
        raise AccountConflict("删除的 UID 与账号 ID 不一致")
    expected = body.get("expected_version")
    opid = body.get("operation_id")
    fingerprint = operation_fingerprint("delete", body)
    with LOCK:
        prior = operation_result(opid, fingerprint)
        if prior:
            return jsonify(prior)
        account = STATE["accounts"].get(aid)
        if account is None or type(expected) is not int or expected != account.version:
            raise AccountConflict("删除版本已变化，请刷新后明确删除")
        # Retire the account under its request lock. Already submitted requests cannot
        # be recalled; no later request can start on a deleted account object.
        with account.credential_lock:
            dropped = [dict(row, key=key, tid=row["tournament_id"])
                       for key, row in STATE["subs"].items() if row["account_id"] == aid]
            accounts = {key: value.to_dict() for key, value in STATE["accounts"].items() if key != aid}
            subs = {key: value for key, value in STATE["subs"].items() if value["account_id"] != aid}
            tombstones = dict(STATE["tombstones"])
            tombstones[aid] = {"version": account.version + 1, "nickname": account.nickname,
                               "legacy_names": sorted(set(account.legacy_names + [account.name])),
                               "session_version": account.session_version, "profile_version": account.profile_version}
            result = {"ok": True, "account_id": aid, "version": account.version + 1}
            operations = dict(STATE["operations"])
            operations[opid] = {"kind": "delete", "fingerprint": fingerprint, "result": result}
            active = STATE["active"] if STATE["active"] != aid else next(iter(accounts), None)
            persist_dataset(accounts=accounts, subscriptions=subs, tombstones=tombstones,
                            operations=operations, active_account_id=active)
            account._retired = True
            for key, row in list(STATE["subs"].items()):
                if row["account_id"] == aid:
                    sched = STATE["schedulers"].pop(key, None)
                    if sched:
                        sched.stop()
            STATE["accounts"].pop(aid)
            STATE.update(subs=subs, tombstones=tombstones, operations=operations, active=active)
    if dropped:
        dispatch_notification(notify_account_purged, account_label(aid), dropped,
                              reason="已明确按 UID 删除账号")
    response = jsonify(result)
    if request.cookies.get("sve_account_id") == aid:
        response.delete_cookie("sve_account_id")
    return response


def dispatch_notification(target, *args, **kwargs):
    threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True).start()


def ts_to_str(ms) -> str:
    """服务端下发的是毫秒时间戳；转成本地可读字符串。"""
    try:
        v = float(ms)
        if v <= 0:
            return ""
        if v > 1e11:      # 毫秒
            v /= 1000.0
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(v))
    except Exception:
        return ""


def enrich_active(client, items, cap: int = 24) -> None:
    """给"活跃"赛事补充准确名额。

    列表接口**不返回 player_num**（见 capture/client_recon.md 第二节），
    rule_config.max_player_number 只是规则模板值，不是本场定员。
    所以只能对真正要看的少数赛事再调 detail；已结束的不补，避免无谓请求。
    """
    # "checkin" 也要补：签到→开赛之间可能存在**补录**机会
    # （官方 App 显示为「可补录」，服务端状态是「签到中-未报名」）。
    # 判定需要 detail 里的 player_num 与时间戳，列表项没有，所以必须补。
    actives = [x for x in items
               if x.get("group") in ("open", "bookable", "mine", "full", "checkin")]
    for x in actives[:cap]:
        try:
            it = client.detail(x["tournament_id"])
            x["player_num"] = it.get("player_num")
            x["max_player_number"] = it.get("max_player_number")
            # 详情里才有的报名时段字段（每日时段型）
            x["signup_start_time"] = it.get("signup_start_time") or ""
            x["signup_end_time"] = it.get("signup_end_time") or ""
            # **窗口是否已开放** —— 必须下发给前端，否则它无从判断。
            #
            # 服务端的 zh_user_status 表达的是"你与这场赛事的关系"，与窗口是否
            # 到点无关：每日时段型（如大奖赛 16:00~23:59）在窗口外同样返回
            # 「可报名」，此时提交必然失败（上游报"非报名时间"）。
            # 而列表接口本身**不返回** signup_start_time / signup_end_time，
            # 所以只能借这次本就要做的 detail 把窗口一并算出来。
            w = window_from_detail(it)
            x["window_kind"] = w.kind
            x["window"] = w.describe()
            # **补录**机会与常规窗口无关：此刻就能补报。若这里仍按 w.is_open()
            # 下发 False，前端会给出「订阅」按钮，用户点不到「立即报名」——
            # 那是"不认这个状态"在前端的表现。
            #
            # 窗口字段本身（window/window_kind）保持不变：它如实描述常规报名时段，
            # 只是"是否可操作"要按补录机会放行。
            makeup = is_makeup_status(it.get("zh_user_status")) or is_makeup_opportunity(it)
            x["is_open"] = w.is_open() or makeup
            if makeup and x.get("group") == "checkin":
                # 状态如实保留（标签仍显示「签到中-未报名」），但**归到可操作组**，
                # 前端据此渲染「立即报名」。分区与按钮都看 group，不看 status。
                x["group"] = "open"
                x["makeup"] = True
            nxt = w.opens_at()
            x["opens_at"] = nxt.strftime("%Y-%m-%d %H:%M") if nxt else ""
            x["contact"] = it.get("contact") or ""
            x["location_detail"] = it.get("location_detail") or x.get("location_detail")
        except Exception:
            pass
        time.sleep(0.08)     # 轻微节流，避免密集请求


@app.post("/api/enroll")
def api_enroll():
    """直接报名（用于「当前可报名」的赛事）。

    与「订阅」的区别：
      * 可报名的赛事 → 当场报名，结果直接告诉你，不进入自动报名列表；
      * 已报满/未开放的赛事 → 只能订阅，交给调度器在开放时抢。
    这里仍然先复核一次服务端状态（必须为「可报名」）再提交，
    避免界面数据陈旧时误提交。
    """
    body = request.json or {}
    tid = str(body.get("tournament_id") or "")
    if not tid:
        return jsonify({"ok": False, "error": "缺少 tournament_id"})
    try:
        c = require_client()
    except SveError as e:
        return jsonify({"ok": False, "error": str(e)})

    acc = c.account
    try:
        it = c.detail(tid)
    except SveError as e:
        return jsonify({"account_id": acc.account_id, "ok": False, "error": str(e)})

    own = it.get("zh_user_status")
    name = it.get("name")
    if is_already_in_status(own):
        # 「已报名」以及已报名账号进入签到阶段后的「签到中」。
        log("直接报名：%s 已是 %r，无需操作" % (name, own))
        return jsonify({"account_id": acc.account_id, "ok": True, "already": True, "status": own,
                        "message": "该账号已是「%s」状态" % own})
    if not is_signable_item(it):
        return jsonify({"account_id": acc.account_id, "ok": False, "status": own,
                        "error": "当前状态为 %r，并非可提交报名/补录的状态，未提交" % own})

    # 窗口前置校验。
    #
    # 服务端的「可报名」只表示"你与这场赛事的关系"，与窗口是否到点无关：
    # 每日时段型（如大奖赛 16:00~23:59）在窗口外**同样**返回「可报名」，
    # 此时直接提交会被上游拒绝，用户看到的是含糊的"提交失败: 非报名时间"。
    # 这里提前拦下并给出可操作的指引——本来就要取 detail，所以零额外成本。
    #
    # ⚠️ 例外：**补录**类状态（赛事处于签到→开赛之间，官方 App 显示为「可补录」）
    #    与常规窗口无关——它本身就意味着此刻服务端允许补报。所以窗口门控
    #    只对非补录状态生效，否则用户点了「立即报名」会被这里误拦。
    w = window_from_detail(it)
    if not w.is_open() and not (is_makeup_status(own) or is_makeup_opportunity(it)):
        nxt = w.opens_at()
        log("直接报名被拒：%s 窗口未开放（%s）" % (name, w.describe()))
        return jsonify({
            "ok": False, "status": own, "window": w.describe(),
            "opens_at": nxt.strftime("%Y-%m-%d %H:%M") if nxt else None,
            "error": "报名窗口尚未开放（%s）。请改用「订阅」，到时由调度器自动报名。"
                     % w.describe()})

    try:
        c.signup(tid)
    except SveError as e:
        log("直接报名失败 %s：%s" % (name, e))
        return jsonify({"account_id": acc.account_id, "ok": False, "error": "提交失败: %s" % e})

    # 立即复核：状态类失败直接判定；网络类错误在窗口内重试
    deadline = time.time() + 20
    last = ""
    status = None
    while time.time() < deadline:
        try:
            after = c.detail(tid)
            status = after.get("zh_user_status")
            # 补录提交成功后，状态往往直接变成「签到中」（已报名、正在签到），
            # 而不是「已报名」——所以两者都算复核通过。
            if is_already_in_status(status):
                add_record(acc.account_id, acc.uid, tid, name, source="手动报名",
                           status=status)
                log("直接报名成功：%s @ %s（复核状态 %r）" % (name, acc.account_id, status))
                return jsonify({"account_id": acc.account_id, "ok": True, "status": status,
                                "message": "报名成功（已复核为「%s」）" % status})
        except Exception as e:
            last = str(e)
        time.sleep(2)

    log("直接报名未确认：%s 状态=%r %s" % (name, status, last))
    return jsonify({"account_id": acc.account_id, "ok": False, "status": status,
                    "error": "已提交，但复核未确认成功（状态=%r）%s" % (status, last)})


@app.post("/api/batch_enroll")
def api_batch_enroll():
    """批量报名：把选中的账号 × 选中的赛事逐个提交。

    ⚠️ 这是本工具**唯一会一次性改变多个账号状态**的接口，因此设计上刻意保守：

      * **逐项复核**：每个账号都新建自己的 client（不复用「当前账号」），
        提交前后各取一次 detail：
            - 已是「已报名」   → skip（幂等，不算失败）
            - 不是「可报名」   → fail（附当前状态）
            - 窗口未开放       → fail（附窗口描述，避免"非报名时间"这种含糊报错）
            - 提交后复核未变   → fail（不谎报成功）
      * **中途失败不中断**：单项异常只记录该项，继续处理其余组合
      * **逐项返回结果**：成功 / 跳过 / 失败+原因，供界面汇总展示

    服务端不在这里做「N×M 确认」——那是界面的职责（已实现二次确认），
    本接口只负责"照单执行 + 如实回报"。
    """
    body = request.json or {}
    selectors = body.get("account_ids", body.get("accounts", []))
    if not isinstance(selectors, list):
        raise AccountConflict("账号列表必须是数组")
    accts = list(dict.fromkeys(resolve(STATE["accounts"], value, explicit="account_ids" in body)
                               for value in selectors))
    tids = [str(x) for x in (body.get("tournaments") or []) if str(x).strip()]
    if not accts or not tids:
        return jsonify({"ok": False, "code": "batch_selection_required",
                        "error": "需要同时提供 accounts 与 tournaments"})
    if len(accts) * len(tids) > 60:
        # 上限只是防手滑（一次 60 次提交已经很多了），不是技术限制
        return jsonify({"ok": False, "code": "batch_limit_exceeded",
                        "error": "一次最多 60 次提交（当前 %d 次），请减少账号或赛事"
                                 % (len(accts) * len(tids))})

    results = []
    for aname in accts:
        acc = STATE["accounts"].get(aname)
        if not acc:
            for tid in tids:
                results.append({"account_id": aname, "account": account_label(aname), "tournament_id": tid,
                                "result": "fail", "reason": "账号不存在"})
            continue
        c = SveClient(acc)
        for tid in tids:
            try:
                it = c.detail(tid)
            except SveError as e:
                results.append({"account_id": aname, "account": account_label(aname), "tournament_id": tid,
                                "result": "fail", "reason": str(e)})
                continue
            own = it.get("zh_user_status")
            nm = it.get("name") or tid
            if is_already_in_status(own):
                # 「已报名」以及已报名账号进入签到阶段后的「签到中」——
                # 两者都表示该账号已在这场赛事里，跳过而非失败。
                results.append({"account_id": aname, "account": account_label(aname), "tournament_id": tid, "name": nm,
                                "result": "skip", "reason": "已在该赛事中（%s）" % own})
                continue
            if not is_signable_item(it):
                results.append({"account_id": aname, "account": account_label(aname), "tournament_id": tid, "name": nm,
                                "result": "fail", "reason": "当前状态为 %r" % own})
                continue
            w = window_from_detail(it)
            # 补录机会与常规窗口无关（赛事处于签到→开赛之间、仍有余位，
            # 官方 App 显示为「可补录」），因此窗口门控只对**非补录**生效。
            if not w.is_open() and not (is_makeup_status(own) or is_makeup_opportunity(it)):
                nxt = w.opens_at()
                results.append({"account_id": aname, "account": account_label(aname), "tournament_id": tid, "name": nm,
                                "result": "fail",
                                "reason": "报名窗口未开放（%s）%s"
                                          % (w.describe(),
                                             ("，下次开放 " + nxt.strftime("%Y-%m-%d %H:%M")) if nxt else "")})
                continue
            try:
                c.signup(tid)
            except SveError as e:
                results.append({"account_id": aname, "account": account_label(aname), "tournament_id": tid, "name": nm,
                                "result": "fail", "reason": str(e)})
                continue
            # 提交后复核：不谎报成功
            ok2, why = False, ""
            try:
                it2 = c.detail(tid)
                # 补录成功后状态常为「签到中」而非「已报名」，两者都算通过。
                ok2 = is_already_in_status(it2.get("zh_user_status"))
                if not ok2:
                    why = "提交后复核状态为 %r" % it2.get("zh_user_status")
            except SveError as e:
                why = "提交后复核失败：%s" % e
            results.append({"account_id": aname, "account": account_label(aname), "tournament_id": tid, "name": nm,
                            "result": "ok" if ok2 else "fail",
                            "reason": "" if ok2 else why})
            if ok2:
                add_record(aname, acc.uid, tid, nm, source="批量报名", status=it2.get("zh_user_status"))
            log("批量报名：%s → %s（%s）" % (aname, nm, "成功" if ok2 else "复核未通过"))

    n_ok = sum(1 for r in results if r["result"] == "ok")
    n_skip = sum(1 for r in results if r["result"] == "skip")
    n_fail = sum(1 for r in results if r["result"] == "fail")
    log("批量报名结束：成功 %d · 跳过 %d · 失败 %d（共 %d 项）"
        % (n_ok, n_skip, n_fail, len(results)))
    return jsonify({"ok": True, "results": results,
                    "summary": {"ok": n_ok, "skip": n_skip, "fail": n_fail,
                                "total": len(results)}})


@app.post("/api/cancel")
def api_cancel():
    """取消报名 / 取消预约（用于「已报名」「已预约」的赛事）。

    与直接报名对称：先复核状态必须是自己的（已报名/已预约）才提交，
    提交后再复核确认已经不在该状态——避免"点了但没生效"。
    """
    body = request.json or {}
    tid = str(body.get("tournament_id") or "")
    if not tid:
        return jsonify({"ok": False, "error": "缺少 tournament_id"})
    try:
        c = require_client()
    except SveError as e:
        return jsonify({"ok": False, "error": str(e)})

    acc = c.account
    try:
        it = c.detail(tid)
    except SveError as e:
        return jsonify({"account_id": acc.account_id, "ok": False, "error": str(e)})

    own = it.get("zh_user_status")
    name = it.get("name")
    if own not in ("已报名", "已预约"):
        return jsonify({"account_id": acc.account_id, "ok": False, "status": own,
                        "error": "当前状态为 %r，无可取消的报名/预约" % own})

    try:
        c.cancel(tid)
    except SveError as e:
        log("取消报名失败 %s：%s" % (name, e))
        return jsonify({"account_id": acc.account_id, "ok": False, "error": "提交失败: %s" % e})

    deadline = time.time() + 20
    status = None
    last = ""
    while time.time() < deadline:
        try:
            after = c.detail(tid)
            status = after.get("zh_user_status")
            if status not in ("已报名", "已预约"):
                log("已取消报名：%s @ %s（现状态 %s）" % (name, acc.account_id, status))
                return jsonify({"account_id": acc.account_id, "ok": True, "status": status,
                                "message": "已取消（复核状态：%s）" % status})
        except Exception as e:
            last = str(e)
        time.sleep(2)

    log("取消报名未确认：%s 状态=%r %s" % (name, status, last))
    return jsonify({"account_id": acc.account_id, "ok": False, "status": status,
                    "error": "已提交，但复核时状态仍为 %r" % status})


@app.get("/api/records")
def api_records():
    selected = None
    if "account_id" in request.args:
        selected = normalized_uid(request.args["account_id"])
        if selected not in STATE["accounts"] and selected not in STATE["tombstones"]:
            raise AccountConflict("记录账号 ID 不存在")
    elif any(field in request.args for field in ("account", "name")):
        selected = request_account(request.args)
    refresh_records(account_id=selected)
    with LOCK:
        grouped = {}
        for key, record in STATE["records"].items():
            aid = record["account_id"]
            if selected is not None and aid != selected:
                continue
            row = {field: record.get(field) for field in ("tournament_id", "name", "uid", "source", "status", "at", "updated", "checked_at", "player_num", "max")}
            row.update(key=key, account_id=aid, account=account_label(aid), deleted=aid not in STATE["accounts"])
            grouped.setdefault(aid, []).append(row)
    return jsonify({"ok": True, "grouped": grouped, "records": [row for rows in grouped.values() for row in rows]})


@app.post("/api/records/scan")
def api_records_scan():
    """扫描某个地区，把各账号"已报名/已预约"的赛事补进记录。

    为什么需要：记录只在"本工具完成报名"时产生，无法反映此前已在 APP 里
    报名/预约的赛事。这个接口回查服务端补齐，让列表反映真实全貌。
    """
    body = request.json or {}
    area = body.get("area") or STATE.get("last_area") or "广东省-广州市"
    added = 0
    scanned = []
    targets = list(STATE["accounts"].items())
    if any(field in body for field in ("account_id", "account", "name")):
        aid = request_account(body)
        targets = [(aid, STATE["accounts"][aid])]
    for name, acc in targets:
        try:
            items = SveClient(acc).tournaments(area=area, size=200)
        except SveError as e:
            scanned.append({"account_id": name, "account": account_label(name), "error": str(e)})
            continue
        mine = [x for x in items
                if (x.get("zh_user_status") or "") in ("已报名", "已预约")]
        for x in mine:
            add_record(name, acc.uid, x.get("tournament_id"), x.get("name"),
                       source="扫描（%s）" % area,
                       status=x.get("zh_user_status"))
            added += 1
        scanned.append({"account_id": name, "account": account_label(name), "found": len(mine)})
    log("扫描 %s：新增/更新 %d 条已报名记录" % (area, added))
    return jsonify({"ok": True, "area": area, "added": added, "detail": scanned,
                    "count": len(STATE["records"])})


@app.post("/api/records/forget")
def api_records_forget():
    """删除一条已报名记录（只删本地记录，不影响服务端报名）。"""
    body = request.json or {}
    if "account_id" in body:
        aid = normalized_uid(body["account_id"])
        if "expected_uid" in body and normalized_uid(body["expected_uid"]) != aid:
            raise AccountConflict("记录的 UID 与账号 ID 不一致")
        if aid not in STATE["accounts"] and aid not in STATE["tombstones"]:
            raise AccountConflict("记录的账号 ID 不存在")
        if body.get("key"):
            key_aid, tid = split_sub_key(body["key"])
            if key_aid != aid:
                raise AccountConflict("记录键与请求账号 ID 不一致")
        else:
            tid = str(body.get("tournament_id") or "")
        key = sub_key(aid, tid)
    elif body.get("key"):
        old_account, tid = split_sub_key(body["key"])
        aid = resolve(STATE["accounts"], old_account, explicit="account_id" in body) if "account_id" not in body else request_account(body)
        key = sub_key(aid, tid)
    else:
        key = sub_key(request_account(body), str(body.get("tournament_id") or ""))
    with LOCK:
        STATE["records"].pop(key, None)
        persist_records()
    return jsonify({"ok": True})


@app.get("/api/my_tournaments")
def api_my_tournaments():
    """当前登录账号的"我的赛事"（原 APP 我的→我的赛事）。

    用服务端 `mine=1` 筛选，一次拿到跨地区的全部参与记录。
    """
    try:
        c = require_client()
        items = c.my_tournaments(size=200)
    except SveError as e:
        return jsonify({"ok": False, "error": str(e)})

    out = []
    for t in items:
        st = t.get("zh_user_status") or ""
        out.append({
            "tournament_id": str(t.get("tournament_id")),
            "name": t.get("name"), "status": st, "group": classify_status(st),
            "org_name": t.get("org_name") or "",
            "type": t.get("type") or "",
            "rule_name": (t.get("rule_config") or {}).get("rule_name") or "",
            "fee": t.get("fee"),
            "location_area": t.get("location_area") or "",
            "location_detail": t.get("location_detail") or "",
            "start_time_ms": t.get("start_time"),
            "start_time": ts_to_str(t.get("start_time")),
            "signup_time": ts_to_str(t.get("signup_time")),
        })
    # 我实际"在册"的排前面，其余按比赛时间倒序
    prio = {"mine": 0, "open": 1, "bookable": 1, "full": 1,
            "checkin": 2, "running": 2, "cancelled": 8, "ended": 9, "other": 5}
    out.sort(key=lambda x: (prio.get(x["group"], 5), -(x.get("start_time_ms") or 0)))
    return jsonify({"ok": True, "account_id": c.account.account_id, "account": account_label(c.account.account_id), "items": out,
                    "count": len(out)})


@app.get("/api/areas")
def api_areas():
    """省市清单。

    来源：APP bundle 字符串表提取（scripts/recon/extract_areas.py），
    没有独立的地区接口。格式与服务端 area 参数一致："<省>-<市>"。
    """
    candidates = (AREAS_FILE, os.path.join(HERE, "data", "areas.json"), os.path.join(DATA_DIR, "areas.json"))
    source = next((path for path in candidates if os.path.isfile(path)), AREAS_FILE)
    d = load_json(source, {"provinces": []})
    return jsonify({"ok": True, **d})


@app.get("/api/tournaments")
def api_tournaments():
    area = request.args.get("area", "广东省-广州市")
    enrich = request.args.get("enrich", "1") != "0"
    STATE["last_area"] = area      # 供"扫描已报名"默认使用
    try:
        c = require_client()
        items = c.tournaments(area=area, size=100)
    except SveError as e:
        return jsonify({"ok": False, "error": str(e)})

    out = []
    for t in items:
        st = t.get("zh_user_status") or ""
        rule = t.get("rule_config") or {}
        rule_name = rule.get("rule_name") or ""
        cls = classify_class(rule_name)
        out.append({
            "tournament_id": str(t.get("tournament_id")),
            "name": t.get("name"),
            "status": st,
            "group": classify_status(st),
            "cls": cls,
            "cls_label": CLASS_LABEL.get(cls, ""),
            "type": t.get("type") or "",
            "org_name": t.get("org_name") or "",
            "rule_name": rule_name,
            "fee": t.get("fee"),
            "location_area": t.get("location_area") or "",
            "location_detail": t.get("location_detail") or "",
            # 时间字段（毫秒时间戳 → 可读字符串）
            "signup_time_ms": t.get("signup_time"),
            "start_time_ms": t.get("start_time"),
            "checkin_time_ms": t.get("checkin_time"),
            "signup_time": ts_to_str(t.get("signup_time")),
            "start_time": ts_to_str(t.get("start_time")),
            "checkin_time": ts_to_str(t.get("checkin_time")),
            "player_num": None,
            "max_player_number": None,
            # 窗口信息：由 enrich_active 填充。未被覆盖时保持未知
            # （None / 空串），前端据此退回原行为，而不是替服务端猜"窗口已开"。
            "window_kind": "",
            "window": "",
            "is_open": None,
            "opens_at": "",
            "signup_start_time": "",
            "signup_end_time": "",
        })
    if enrich:
        enrich_active(c, out)

    # 排序（用户指定）：先按赛事类别
    #     大型赛/中型赛/特别赛 → 积分赛 → 娱乐赛
    # 同类别内先按状态分组（可操作的排前面），再按比赛开始时间。
    order = {"open": 0, "bookable": 1, "mine": 2, "full": 3,
             "checkin": 4, "running": 5, "other": 6, "ended": 7}
    out.sort(key=lambda x: (CLASS_ORDER.get(x["cls"], 9),
                            order.get(x["group"], 9),
                            x.get("start_time_ms") or 0))
    return jsonify({"ok": True, "account_id": c.account.account_id, "area": area, "items": out})


@app.get("/api/detail")
def api_detail():
    tid = request.args.get("tournament_id")
    try:
        c = require_client()
        it = c.detail(tid)
    except SveError as e:
        return jsonify({"ok": False, "error": str(e)})
    w = window_from_detail(it)
    return jsonify({"ok": True, "item": {
        "tournament_id": tid, "name": it.get("name"),
        "player_num": it.get("player_num"), "max": it.get("max_player_number"),
        "status": it.get("zh_user_status"), "group": classify_status(it.get("zh_user_status") or ""),
        "window": w.describe(), "window_kind": w.kind,
        "opens_at": (w.opens_at().strftime("%Y-%m-%d %H:%M:%S") if w.opens_at() else None),
        "is_open": w.is_open(),
        "contact": it.get("contact"),
    }})


def summary_payload():
    with LOCK:
        rows = [{"account_id": aid, "uid": acc.uid, "name": acc.name, "nickname": acc.nickname,
                 "version": acc.version, "revision": acc.version, "session_version": acc.session_version,
                 "legacy_names": list(acc.legacy_names), **profile_status(acc)}
                for aid, acc in STATE["accounts"].items()]
        return {"ok": True, "schema_version": 2, "session_write": 2, "subscription_identity": 2,
                "accounts": rows, "tombstones": STATE["tombstones"]}


@app.get("/api/accounts/summary")
def api_accounts_summary():
    return jsonify(agent().accounts_summary() if remote_enabled() else summary_payload())


@app.post("/api/accounts/sync")
def api_accounts_sync():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("accounts"), dict):
        raise AccountConflict("accounts 必须是对象")
    # No whole-table request can mutate membership, credentials, subscriptions or active.
    for name, raw in body["accounts"].items():
        acc = parse_account(raw, Account)
        if name != acc.name:
            raise AccountConflict("旧账号表名称不一致")
        matches = [key for key, value in STATE["accounts"].items() if value.name == name]
        if matches and (len(matches) != 1 or matches[0] != acc.account_id):
            raise AccountConflict("旧名称有歧义或 UID 不符")
    return jsonify({"ok": True, "readonly": True, "pushed": [], "removed": [], "purged": 0,
                    "total": len(STATE["accounts"]), "subs": len(STATE["subs"])})


@app.post("/api/accounts/session")
def api_accounts_session():
    body = request.get_json(silent=True) or {}
    if remote_enabled():
        raise AccountConflict("会话只能提交到执行服务器")
    if type(body.get("expected_version")) is not int or body["expected_version"] < 0:
        raise AccountConflict("需要 UID 协议的 expected_version；请升级电脑客户端")
    acc = parse_account(body.get("account"), Account)
    if body["account"].get("account_id") != acc.account_id:
        raise AccountConflict("会话提交必须明确指定 account_id")
    fingerprint = operation_fingerprint("session", {key: value for key, value in body.items() if key != "expected_version"})
    with LOCK:
        prior = operation_result(body.get("operation_id"), fingerprint)
        if prior:
            return jsonify(prior)
        current = STATE["accounts"].get(acc.account_id)
        expected = revision(current) if current else STATE["tombstones"].get(acc.account_id, {}).get("version", 0)
        if body["expected_version"] != expected:
            raise AccountConflict("会话前置版本已变化")
    try:
        info = SveClient(acc).info()
    except Exception:
        raise AccountConflict("官方身份校验失败，未写入会话") from None
    if normalized_uid(info.get("id")) != acc.account_id:
        raise AccountConflict("官方 UID 与提交身份不符")
    acc.uid = info["id"]
    acc.nickname = info.get("nickname") or ""
    acc.name = acc.nickname or ("uid%s" % acc.account_id)
    acc.mobile = info.get("mobile") or ""
    result = commit_login(acc, expected_revision=body["expected_version"],
                          operation_id=body.get("operation_id"), fingerprint=fingerprint,
                          restore=body.get("restore") is True)
    restore_missing_schedulers("明确 UID 会话提交")
    return jsonify(result)


@app.post("/api/subscribe")
def api_subscribe():
    body = request.json or {}
    tid = str(body.get("tournament_id") or "")
    if not tid:
        return jsonify({"ok": False, "error": "缺少 tournament_id"})

    # 远端模式：订阅发给服务器代理执行，本机**不建调度器**
    # （唯一执行点原则：同一场赛事只能有一处在跑，否则会重复提交）
    #
    # 必须**显式带上账号**：代理有自己的 active，而账号切换只在本机发生、
    # 不会实时同步过去。不带账号时代理会用它过期的 active，结果订阅被记到
    # 另一个账号名下（实测踩过：想用 A 订阅，却建成 B 的订阅，界面还会因为
    # 代理那边"已存在"而显示"已订阅"）。
    if remote_enabled():
        acct = request_account(body)
        try:
            r = forward_subscription(tid, acct)
        except AccountConflict as e:
            return jsonify({"ok": False, "code": "subscription_conflict", "error": str(e)})
        log("订阅转发到服务器：%s → %s（账号 %s）"
            % (tid, "成功" if r.get("ok") else r.get("error"), acct or "代理默认"))
        return jsonify(r)

    try:
        # 可选：指定用哪个账号订阅。不指定则用当前激活账号。
        # 有这个参数才能在"不想改动全局激活账号"时把赛事订阅给特定账号
        # （例如大奖赛固定用某个账号盯）。
        aid = request_account(body)
        c = SveClient(STATE["accounts"][aid])
        with LOCK:
            check_subscription_identity(body, c.account.account_id)
        it = c.detail(tid)
    except AccountConflict as e:
        return jsonify({"ok": False, "code": "subscription_conflict", "error": str(e)})
    except SveError as e:
        return jsonify({"ok": False, "error": str(e)})

    # 订阅以 (当前账号, 赛事) 为键：
    #   * 同一账号重复订阅 = 幂等（刷新信息），**绝不改绑**
    #   * 不同账号订阅同一场 = 各自独立的订阅与调度器
    acc = c.account
    key = sub_key(acc.account_id, tid)
    w = window_from_detail(it)
    with LOCK:
        if remote_enabled():
            return jsonify({"ok": False, "code": "execution_mode_changed",
                            "error": "执行模式已变化，本机未启动订阅；请刷新后重试"})
        try:
            check_subscription_identity(body, acc.account_id)
            if STATE["accounts"].get(acc.account_id) is not acc:
                raise AccountConflict("订阅身份对象已删除或重建")
        except AccountConflict as e:
            return jsonify({"ok": False, "code": "subscription_conflict", "error": str(e)})
        sched = STATE["schedulers"].get(key)
        if sched is not None and sched.stop_flag.is_set() and not sched.completed:
            return jsonify({"ok": False, "code": "subscription_conflict",
                            "error": "旧订阅线程尚未退出，请稍后重试"})
        if key in STATE["workers"] and key not in STATE["schedulers"]:
            raise AccountConflict("旧订阅线程尚未退出，暂不重建执行点")
        old = STATE["subs"].get(key) or {}
        STATE["subs"][key] = {
            "account_id": acc.account_id, "account": acc.account_id, "uid": acc.uid, "tournament_id": tid,
            "name": it.get("name"), "area": body.get("area", ""),
            "enabled": True, "status": it.get("zh_user_status"),
            "player_num": it.get("player_num"), "max": it.get("max_player_number"),
            "window_desc": w.describe(),
            "logs": old.get("logs", []),
            "orphan": False,
        }
        for field in ("migration_sources", "migration_duplicates"):
            if field in old:
                STATE["subs"][key][field] = copy.deepcopy(old[field])
        if old and key in STATE["schedulers"]:
            # Repeated subscribe is a refresh of the same task instance, including
            # its completion marker/timer; do not manufacture a new generation.
            old.update(STATE["subs"][key])
            STATE["subs"][key] = old
        persist_subs()
        existed = bool(STATE["schedulers"].get(key))
        if not existed:
            # Registration and mode changes share LOCK, including before run starts.
            sched = Scheduler(c, tid,
                              logger=lambda m: _sub_log(key, m),
                              on_event=lambda kind, data: _on_event(key, kind, data),
                              **sched_params())
            STATE["schedulers"][key] = sched
            thread = scheduler_thread(sched, key)

    if existed:
        log("订阅已存在，仅刷新信息：%s（%s）@ %s" % (it.get("name"), tid, acc.account_id))
        return jsonify({"ok": True, "window": w.describe(), "account_id": acc.account_id, "account": account_label(acc.account_id),
                        "uid": acc.uid, "already": True})

    thread.start()
    _sub_log(key, "开始守护（%s）" % w.describe())
    log("新增订阅 %s（%s）绑定账号 %s(uid=%s)" % (it.get("name"), tid, acc.account_id, acc.uid))
    return jsonify({"ok": True, "window": w.describe(),
                    "account_id": acc.account_id, "account": account_label(acc.account_id), "uid": acc.uid})


@app.post("/api/unsubscribe")
def api_unsubscribe():
    body = request.json or {}
    tid = str(body.get("tournament_id") or "")
    account = request_account(body)

    if remote_enabled():
        if not account:
            return jsonify({"ok": False, "code": "account_required",
                            "error": "未选择账号，无法取消远端订阅"})
        # The server has its own active account; never fall back to it.
        try:
            r = forward_subscription(tid, account, cancel=True)
        except AccountConflict as e:
            return jsonify({"ok": False, "code": "subscription_conflict", "error": str(e)})
        log("取消订阅转发到服务器：%s" % tid)
        return jsonify(r)

    key = sub_key(account, tid)
    with LOCK:
        try:
            check_subscription_identity(body, account)
        except AccountConflict as e:
            return jsonify({"ok": False, "code": "subscription_conflict", "error": str(e)})
        sched = STATE["schedulers"].pop(key, None)
        s = STATE["subs"].pop(key, None)
        persist_subs()
    if sched:
        sched.stop()
    if s:
        log("已取消订阅 %s（%s）@ %s" % (s.get("name"), tid, account))
    return jsonify({"ok": True, "account_id": account})


def _sub_log(key: str, msg: str) -> None:
    """写某个订阅自己的日志（订阅页展示，不混进主页面日志）。"""
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    with LOCK:
        s = STATE["subs"].get(key)
        if s is not None:
            s.setdefault("logs", []).append(line)
            if len(s["logs"]) > SUB_LOG_LIMIT:
                s["logs"] = s["logs"][-SUB_LOG_LIMIT:]
    print("  [sub %s] %s" % (key, msg), flush=True)


def _on_event(key: str, kind: str, data: dict) -> None:
    with LOCK:
        s = STATE["subs"].get(key)
        if not s:
            return
        if kind == "identity_blocked":
            account = STATE["accounts"].get(s["account_id"])
            if account is not None and data.get("account_version") is not None and account.version != data["account_version"]:
                return False  # A late failure must not disable a newly verified session.
            if not s.get("identity_blocked"):
                STATE["pending"].append({"category": "runtime_identity_mismatch", "source_key": key,
                                         "original": {"subscription": copy.deepcopy(s),
                                                      "account": account.to_dict() if account else None}})
            s.update(enabled=False, identity_blocked=True, status="身份待核对")
            if account is not None:
                account._identity_blocked = True
                account._retired = True
            persist_subs()
            return True
        if kind == "sample":
            s["status"] = data.get("status")
            s["player_num"] = data.get("player_num")
            s["max"] = data.get("max")
            w = data.get("window")
            if w is not None:
                s["window_desc"] = w.describe() if hasattr(w, "describe") else s.get("window_desc")
        persist_subs()
        name = s.get("name")

    # 与已报名赛事时间冲突 → 这个冲突在用户处理掉那场已报名的赛事之前
    # **永远不会消失**，继续守护只会反复撞同一面墙（实测曾提交 503 次）。
    # 因此取消该订阅并通知，把决定权交回用户。
    #
    # ⚠️ 这是整个文件里**唯一**会"自动取消订阅"的路径，判定收得很窄：
    #    只有 scheduler.looks_like_time_conflict() 为真（调度器才会发 abort）
    #    才会走到这里。其余失败（无空位 / 窗口未开 / 网络抖动 / 会话失效 /
    #    未知错误）都不会发 abort，订阅一律不动。
    if kind == "abort":
        if s.get("abort_handled"):
            return                      # 幂等
        s["abort_handled"] = True
        snap = dict(s)
        reason = data.get("reason") or "与已报名赛事时间冲突"
        code = data.get("code")
        detail = data.get("detail") or ""
        _sub_log(key, "放弃守护并取消订阅：%s" % reason)
        log("订阅已自动取消：%s｜%s｜%s｜code=%s｜%s"
            % (name, key, reason, code, detail))
        # 先摘掉订阅与调度器（stop() 让调度线程退出循环），再异步通知
        with LOCK:
            sched = STATE["schedulers"].pop(key, None)
            STATE["subs"].pop(key, None)
            persist_subs()
        if sched:
            sched.stop()
        threading.Thread(target=notify_time_conflict,
                         args=(snap, reason, code, detail),
                         daemon=True, name="notify-abort-%s" % key).start()
        return

    # 报名成功且已稳定确认 → 任务结束，订阅自动退出列表。
    # 同时兜住 "stop(completed=True)"：调度器在某些路径（如"已是已报名"）可能
    # 只发 stop，若不处理，订阅会一直留在列表里空转。
    done = (kind == "completed") or (kind == "stop" and data.get("completed"))
    if done:
        with LOCK:
            if STATE["subs"].get(key) is not s or s.get("done_handled"):
                return                  # Claim completed/stop once, including concurrent callbacks.
            s["done_handled"] = True
            result = data.get("result")
            confirmed = (result.get("second_status") or result.get("quick_status")) if isinstance(result, dict) else (
                getattr(result, "second_status", None) or getattr(result, "quick_status", None))
            if is_already_in_status(confirmed):
                s["status"] = confirmed
            persist_subs()
        # 记一笔"已报名"，这样订阅被自动移出后仍能在订阅页看到结果
        add_record(s.get("account_id"), s.get("uid"), s.get("tournament_id"),
                   s.get("name"), source="自动报名",
                   status=s.get("status") or "已报名")
        _sub_log(key, "任务完成，%.0f 秒后自动移出订阅列表" % AUTO_REMOVE_DELAY)
        log("订阅已完成：%s（%s）" % (name, key))
        # 通知单独放一个线程：发送要几秒（还带重试），不能阻塞调度线程
        snap = dict(s)
        already = bool(isinstance(data.get("result"), dict) and data["result"].get("already")) \
            or bool(data.get("already"))
        threading.Thread(target=notify_completed, args=(snap, already),
                         daemon=True, name="notify-%s" % key).start()
        threading.Timer(AUTO_REMOVE_DELAY, _auto_remove_sub, args=(key, s)).start()


def _auto_remove_sub(key: str, expected=None) -> None:
    with LOCK:
        if expected is not None and STATE["subs"].get(key) is not expected:
            return
        sched = STATE["schedulers"].pop(key, None)
        s = STATE["subs"].pop(key, None)
        persist_subs()
    if sched:
        sched.stop()
    if s:
        log("已自动移出订阅列表：%s（%s）" % (s.get("name"), key))


# ------------------------------------------------------------------ 启动
def main() -> int:
    port = int(os.environ.get("SVE_CLIENT_PORT", "8801"))
    with WriterLock(DATA_DIR):
        load_all()
        log("=== SVE UID 服务启动 ===")
        restore_schedulers()
        stop_profiles = threading.Event()
        profiles = threading.Thread(target=profile_refresh_loop, args=(stop_profiles,), daemon=True, name="profiles")
        profiles.start()
        try:
            app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)
        finally:
            stop_profiles.set()
            profiles.join(timeout=35)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
