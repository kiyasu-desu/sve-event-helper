"""自动报名模块（带三重保护）。

保护措施：
  1. 白名单：只对 config 中 allowed_tournament_ids 列出的赛事生效，并校验赛事名一致；
  2. 状态校验：报名前先确认自己“未报名”，避免重复提交；
  3. 审计：每一步都推送通知（开始 / 结果 / 失败原因）；
  另外限制每小时尝试次数，避免异常情况下反复重试。

签名由 scripts/poller/signer.py 本地生成，不需要模拟器或 Frida。
"""
from __future__ import annotations

import json
import os
import sys
import time

import requests

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts", "poller"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from notify import notify_all, any_ok, render_event_html  # noqa: E402
from signer import generate_sign, sign_payload  # noqa: E402
import sveenv  # noqa: E402

# 会话统一由 sveenv.load_session() 提供（单一来源 = identity-v2.json）；
# capture/session.json 仅作迁移兜底，这里不再直接引用。
# 可在运行时替换（测试用），避免把演练写入真实的 state.json
STATE = sveenv.STATE_FILE
# 任务完成标记（终态快照）：存在即表示"报名已稳定确认，脚本不再发任何请求"
COMPLETION = sveenv.COMPLETION_FILE
# 日志文件（可在运行时替换：单元测试必须指到独立文件，
# 否则测试输出会混进生产日志——曾因此误判"生产环境发生了自动报名"）
LOGFILE = sveenv.POLL_LOG
BASE = "https://api-sve.nie.netease.com/sve/api/v1"
TIMEOUT = 20


class GuardError(Exception):
    """未通过保护校验。"""


def log(msg: str) -> None:
    """轻量日志：默认与轮询器写同一份 capture/poll.log。"""
    line = "[%s] [autosignup] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        from runtime_paths import private_append
        private_append(LOGFILE, line + "\n")
    except Exception:
        pass


def load_json(path: str, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def headers_for(session: dict, body: dict) -> dict:
    return {
        "content-type": "application/json",
        "deviceid": session["deviceid"],
        "platform": "android",
        "sessionid": session["sessionid"],
        "sign": generate_sign(body, udid=session["deviceid"]),
        "User-Agent": "okhttp/4.9.2",
    }


def fetch_detail(session: dict, tournament_id: str) -> dict:
    r = requests.get("%s/urs/tournament/detail/%s" % (BASE, tournament_id),
                     headers=headers_for(session, {}), timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError("detail HTTP %s: %s" % (r.status_code, r.text[:200]))
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError("detail 业务失败: %s" % body)
    return body.get("item", {})


def resolve_uid(session: dict) -> int:
    r = requests.get("%s/urs/info" % BASE, headers=headers_for(session, {}), timeout=TIMEOUT)
    item = r.json().get("item", {})
    uid = item.get("id") or item.get("uid")
    if not uid:
        raise RuntimeError("无法解析 uid")
    return int(uid)


def classify_fetch_error(exc: Exception) -> str:
    """区分"网络类错误"与"业务类错误"。

    网络抖动（超时/连接被断）不应该被当成"报名没坐稳"，
    否则会误报并把已经到手的座位再抢一次。
    """
    net_types = (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                 requests.exceptions.ChunkedEncodingError)
    if isinstance(exc, net_types):
        return "network"
    if isinstance(exc, requests.exceptions.RequestException):
        return "network"
    return "other"


def confirm_signup(session: dict, tid: str, deadline_seconds: int = 90,
                   retry_interval: int = 10, label: str = "确认") -> dict:
    """复核报名是否生效。

    错误分类处理：
      * 网络类错误 → 在 deadline 窗口内按 retry_interval 重试（抖动不该判失败）
      * 状态不对   → 立即判定失败（座位真的没了）
      * 其它错误   → 也按网络类处理，窗口内重试

    返回 {"ok": bool, "kind": "ok"/"state"/"error", "status": str|None,
          "detail": str, "attempts": int}
    """
    started = time.time()
    attempts = 0
    last_detail = ""
    last_status = None
    # 重试间隔强制下限 1 秒：配置成 0 或负数会在确认窗口内形成请求轰炸
    # （单元测试里曾出现 30 秒内重试 6 万次的情况）。
    interval = max(1.0, float(retry_interval or 1))

    while True:
        attempts += 1
        try:
            item = fetch_detail(session, tid)
            status = item.get("zh_user_status")
            last_status = status
            if status == "已报名":
                return {"ok": True, "kind": "ok", "status": status,
                        "detail": "第 %d 次确认，状态=已报名" % attempts, "attempts": attempts}
            # 状态类失败：不需要等待重试，直接判定
            return {"ok": False, "kind": "state", "status": status,
                    "detail": "第 %d 次确认，状态=%r（非「已报名」）" % (attempts, status),
                    "attempts": attempts}
        except Exception as exc:  # noqa: BLE001
            kind = classify_fetch_error(exc)
            last_detail = "%s（%s）" % (exc, kind)
            log("%s 第 %d 次请求失败[%s]: %s" % (label, attempts, kind, exc))

        remaining = deadline_seconds - (time.time() - started)
        if remaining <= 0:
            return {"ok": False, "kind": "error", "status": last_status,
                    "detail": "在 %d 秒窗口内重试 %d 次仍未成功: %s"
                              % (deadline_seconds, attempts, last_detail),
                    "attempts": attempts}
        time.sleep(min(interval, max(1.0, remaining)))


def push(cfg: dict, headline: str, detail: str, force: bool = True) -> bool:
    """发送审计通知。

    一次报名事件只发一条：PushPlus 与 QQ 邮箱都有频率限制
    （实测发多了会分别返回 code=999 与 550 Too many attempts），
    因此所有 push 调用点都必须是"值得占用一次配额"的终态信息。
    """
    ev = {
        "headline": headline,
        "name": cfg.get("tournament_name", "-"),
        "tournament_id": cfg.get("tournament_id", "-"),
        "found_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "remark": detail,
    }
    return any_ok(notify_all(cfg.get("channels", {}), "【SVE自动报名】%s" % headline,
                             render_event_html(ev)))


def check_rate_limit(state: dict, max_per_hour: int) -> None:
    now = time.time()
    stamps = [t for t in state.get("signup_attempts", []) if now - t < 3600]
    state["signup_attempts"] = stamps
    if len(stamps) >= max_per_hour:
        raise GuardError("过去一小时内已尝试 %d 次，达到上限 %d，本次跳过" % (len(stamps), max_per_hour))


def auto_signup(cfg: dict, session: dict | None = None, dry_run: bool = False) -> dict:
    """执行一次自动报名，返回结果字典。所有分支都会发审计通知。

    dry_run=True 时走完全部保护校验并构造签名，但不发送 POST —— 用于验证链路。
    """
    sveenv.assert_legacy_executor_allowed()
    auto = cfg.get("auto_signup", {})
    tid = str(cfg.get("tournament_id"))
    result = {"attempted": False, "success": False, "reason": "", "uid": None}

    if not auto.get("enabled"):
        result["reason"] = "auto_signup 未启用"
        return result

    # --- 保护 1：白名单 ---
    allowed = [str(x) for x in (auto.get("allowed_tournament_ids") or [])]
    if allowed and tid not in allowed:
        result["reason"] = "赛事 %s 不在白名单内，拒绝自动报名" % tid
        push(cfg, "已跳过（非白名单赛事）", result["reason"])
        return result

    if session is None:
        session = sveenv.load_session()
    if not session or not session.get("sessionid"):
        result["reason"] = "缺少会话凭证"
        return result

    state = load_json(STATE, {}) or {}

    # --- 限流 ---
    try:
        check_rate_limit(state, int(auto.get("max_attempts_per_hour", 6)))
    except GuardError as exc:
        result["reason"] = str(exc)
        save_json(STATE, state)
        push(cfg, "已跳过（触发限流）", result["reason"])
        return result

    # --- 保护 2 前置：拉详情，校验赛事名与自身状态 ---
    try:
        item = fetch_detail(session, tid)
    except Exception as exc:  # noqa: BLE001
        result["reason"] = "拉取赛事详情失败: %s" % exc
        push(cfg, "自动报名中止", result["reason"], force=True)
        return result

    expected = auto.get("expected_name")
    name = item.get("name")
    if expected and name != expected:
        result["reason"] = "赛事名不匹配：期望 %r，实际 %r，拒绝报名" % (expected, name)
        push(cfg, "自动报名中止（赛事名不符）", result["reason"], force=True)
        return result

    own = item.get("zh_user_status")
    player_num = item.get("player_num")
    max_num = item.get("max_player_number")

    if own == "已报名":
        result["reason"] = "自己已是「已报名」状态，无需重复报名"
        result["success"] = True
        push(cfg, "无需报名（已报名）", result["reason"])
        return result

    # 名额刚释放时，服务端的"自身状态"可能还没同步为「可报名」。
    # 这里模仿 APP 详情页右上角的刷新按钮：自旋重拉详情，给状态一点翻转时间。
    refresh_tries = int(auto.get("status_refresh_tries", 5)) if auto.get("allow_status_refresh", True) else 0
    if refresh_tries > 0 and own != "可报名" and isinstance(player_num, int) \
            and isinstance(max_num, int) and player_num < max_num:
        interval = float(auto.get("status_refresh_interval_seconds", 1.5))
        for spin in range(1, refresh_tries + 1):
            time.sleep(interval)
            try:
                fresh = fetch_detail(session, tid)
            except Exception as exc:  # noqa: BLE001
                log("刷新详情失败(%d/%d): %s" % (spin, refresh_tries, exc))
                continue
            new_own = fresh.get("zh_user_status")
            new_player = fresh.get("player_num")
            new_max = fresh.get("max_player_number")
            log("刷新详情 %d/%d: 状态 %s -> %s，人数 %s -> %s"
                % (spin, refresh_tries, own, new_own, player_num, new_player))
            item, own, player_num, max_num = fresh, new_own, new_player, new_max
            if own == "可报名":
                push(cfg, "刷新后状态变为可报名", "自旋 %d 次后服务端状态同步为「可报名」，继续提交" % spin)
                break
            if own == "已报名":
                result["reason"] = "刷新后发现已报名，无需重复提交"
                result["success"] = True
                push(cfg, "无需报名（刷新后已报名）", result["reason"])
                return result
            if not (isinstance(player_num, int) and isinstance(max_num, int) and player_num < max_num):
                result["reason"] = "刷新后名额已被占满（%s/%s），放弃本次" % (player_num, max_num)
                push(cfg, "自动报名中止（名额已被抢）", result["reason"])
                return result

    # 只有服务端认为你「可报名」时才提交。
    # "已报满" 表示名额虽可能释放、但当前你并不具备报名资格（例如已被限制），
    # 此时提交必然失败，不如不提交。
    if auto.get("verify_own_status", True) and own != "可报名":
        result["reason"] = ("自身状态为 %r，并非「可报名」，不提交"
                            "（已自旋刷新 %d 次仍未翻转）" % (own, refresh_tries))
        push(cfg, "自动报名中止（自身状态非可报名）", result["reason"])
        return result

    if not (isinstance(player_num, int) and isinstance(max_num, int) and player_num < max_num):
        result["reason"] = "当前并非有空位（%s/%s），不提交" % (player_num, max_num)
        push(cfg, "自动报名中止（无名额）", result["reason"])
        return result

    # --- 执行报名 ---
    result["attempted"] = True
    try:
        uid = resolve_uid(session)
        result["uid"] = uid
        body = {"uid": uid, "tournament_id": tid, "action": "signup"}
        h = headers_for(session, body)
        # 注意：这里刻意【不】单独推送"开始报名"。
        # PushPlus 与 QQ 邮箱都有频率限制，一次事件只发一条通知才能保证送达。
        state.setdefault("signup_attempts", []).append(time.time())
        save_json(STATE, state)

        if dry_run:
            result["success"] = False
            result["reason"] = ("[dry-run] 已通过全部校验并构造签名，未发送 POST。"
                                " body=%s sign=%s" % (json.dumps(body, ensure_ascii=False), h["sign"]))
            return result

        r = requests.post("%s/urs/tournament/action" % BASE, headers=h,
                          data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
                          timeout=TIMEOUT)
        ok = r.status_code == 200 and r.json().get("status") == "success"
        result["success"] = ok
        result["reason"] = "HTTP %s %s" % (r.status_code, r.text[:200])
    except Exception as exc:  # noqa: BLE001
        result["reason"] = "报名请求异常: %s" % exc
        ok = False

    # --- 第一次复核（立即复核）：快速判断这一枪有没有打中 ---
    verified = None
    if result["success"]:
        quick = confirm_signup(session, tid, deadline_seconds=15,
                               retry_interval=3, label="立即复核")
        verified = quick.get("status")
        result["after_status"] = verified
        result["quick_confirm"] = quick
        if not quick.get("ok"):
            result["success"] = False
            result["reason"] += "；立即复核未通过（%s: %s）" % (quick.get("kind"), quick.get("detail"))
            # 静默期之前就失败：推一条"没抢到"，然后回到找空位
            push(cfg, "自动报名未成功（立即复核未通过）",
                 "赛事：%s（%s）<br>尝试前 %s/%s，我的状态 %s<br>报名响应：%s<br>立即复核：%s"
                 % (name, tid, player_num, max_num, own, result["reason"],
                    quick.get("detail")))
            return result

    # --- 静默等待 + 第二次确认 ---
    # 这 60 秒内【不发任何通知】：座位已经被占住，等它坐稳再宣布，
    # 这样"收到通知"就等于"任务已完成"，你可以立刻开始手动交接。
    if result["success"]:
        delay = int(auto.get("second_confirm_delay_seconds", 60))
        log("立即复核通过（%s），静默等待 %d 秒后做二次确认" % (verified, delay))
        time.sleep(max(0, delay))

        second = confirm_signup(session, tid,
                                deadline_seconds=int(auto.get("second_confirm_window_seconds", 90)),
                                retry_interval=int(auto.get("confirm_retry_interval_seconds", 10)),
                                label="二次确认")
        result["second_confirm"] = second
        result["second_status"] = second.get("status")
        if not second.get("ok"):
            result["success"] = False
            result["reason"] += "；二次确认未通过（%s: %s）" % (second.get("kind"), second.get("detail"))
            push(cfg, "自动报名未坐稳（二次确认未通过）",
                 "赛事：%s（%s）<br>报名响应成功、立即复核也通过，但 %d 秒后二次确认失败。<br>"
                 "详情：%s<br><b>脚本已恢复轮询，继续等待下一次空位。</b>"
                 % (name, tid, delay, second.get("detail")))
            return result

    # --- 成功：写完成标记（终态快照） + 唯一一条成功通知 ---
    if result["success"]:
        finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
        snapshot = {
            "finished_at": finished_at,
            "tournament_id": tid,
            "tournament_name": name,
            "uid": result.get("uid"),
            "player_num_before": player_num,
            "max_player_number": max_num,
            "own_status_before": own,
            "quick_confirm_status": result.get("after_status"),
            "second_confirm_status": result.get("second_status"),
            "second_confirm_delay_seconds": int(auto.get("second_confirm_delay_seconds", 60)),
            "post_response": result.get("reason"),
        }
        try:
            tmp = COMPLETION + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, COMPLETION)
            log("已写入完成标记: %s" % COMPLETION)
        except Exception as exc:  # noqa: BLE001
            log("写完成标记失败: %s" % exc)

        push(cfg, "任务完成（已稳定确认）",
             "赛事：%s（%s）<br>报名前 %s/%s，我的状态 %s<br>"
             "立即复核：%s<br>%d 秒后二次确认：%s<br>"
             "<b>座位已坐稳，脚本即将停止并退出。现在可以开始你的手动交接了。</b>"
             % (name, tid, player_num, max_num, own,
                result.get("after_status"),
                int(auto.get("second_confirm_delay_seconds", 60)),
                result.get("second_status")))
        result["task_completed"] = True
        return result

    # --- 走到这里说明报名 POST 本身就失败了 ---
    push(cfg, "自动报名失败（报名请求未成功）",
         "赛事：%s（%s）<br>尝试前 %s/%s，我的状态 %s<br>结果：%s<br><b>脚本继续等待下一次空位。</b>"
         % (name, tid, player_num, max_num, own, result["reason"]))
    return result


def load_completion() -> dict | None:
    """读取完成标记；不存在返回 None。"""
    if not os.path.exists(COMPLETION):
        return None
    try:
        with open(COMPLETION, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception as exc:  # noqa: BLE001
        log("读取完成标记失败: %s" % exc)
        return None


def verify_completed(session: dict | None = None, cfg: dict | None = None) -> dict:
    """重启后的处理：允许【一次】只读校验，然后退出，绝不重复报名。

    返回 {"done": bool, "still_registered": bool, "detail": str}
    """
    snap = load_completion()
    if not snap:
        return {"done": False, "still_registered": False, "detail": "无完成标记"}

    if cfg is None:
        cfg = load_json(sveenv.SETTINGS_FILE) or {}
    tid = str(snap.get("tournament_id") or cfg.get("tournament_id"))
    if session is None:
        session = sveenv.load_session() or {}
    if not session.get("sessionid"):
        return {"done": True, "still_registered": None,
                "detail": "有完成标记，但缺少会话，无法做只读校验（不做任何写操作）"}

    log("检测到完成标记（%s 完成），做一次只读校验" % snap.get("finished_at"))
    try:
        item = fetch_detail(session, tid)
    except Exception as exc:  # noqa: BLE001
        return {"done": True, "still_registered": None,
                "detail": "只读校验失败（%s），不做任何写操作" % exc}

    status = item.get("zh_user_status")
    still = status == "已报名"
    detail = "只读校验：状态=%r，人数=%s/%s" % (status, item.get("player_num"), item.get("max_player_number"))
    log(detail)
    return {"done": True, "still_registered": still, "detail": detail,
            "status": status, "snapshot": snap}


if __name__ == "__main__":
    cfg = load_json(sveenv.SETTINGS_FILE)
    print(json.dumps(auto_signup(cfg), ensure_ascii=False, indent=2))
