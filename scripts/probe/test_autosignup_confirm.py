"""autosignup 的两次复核流程单元测试（不触网、不产生副作用）。

覆盖用户确认的设计：
  * 立即复核 + 静默等待 + 二次确认 两段都保留
  * 网络类错误在窗口内重试，不当成"没坐稳"
  * 状态不对才算失败
  * 二次确认通过 → 写完成标记 + 通知 + task_completed
  * 失败 → 不写完成标记，由调用方继续等待

用法: python scripts/probe/test_autosignup_confirm.py
"""
from __future__ import annotations

import json
import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts", "poller"))

import autosignup as A  # noqa: E402

A.STATE = os.path.join(PROJECT_ROOT, "vendor", "tmp", "confirm_state.json")
A.COMPLETION = os.path.join(PROJECT_ROOT, "vendor", "tmp", "confirm_completed.json")
A.LOGFILE = os.path.join(PROJECT_ROOT, "vendor", "tmp", "test_poll.log")

CFG = {
    "tournament_id": "33997",
    "tournament_name": "超级大奖赛2026广州站",
    "channels": {},
    "auto_signup": {
        "enabled": True,
        "allowed_tournament_ids": ["33997"],
        "expected_name": "超级大奖赛2026广州站",
        "verify_own_status": True,
        "max_attempts_per_hour": 50,
        "allow_status_refresh": False,
        "second_confirm_delay_seconds": 0,     # 测试不等待
        "second_confirm_window_seconds": 2,    # 窗口短一点，避免测试里长时间重试
        "confirm_retry_interval_seconds": 1,   # 下限 1 秒（实现里强制）
    },
}

SESSION = {"sessionid": "x" * 64, "deviceid": "0000000000000001"}
OPEN_ITEM = {"name": "超级大奖赛2026广州站", "player_num": 99,
             "max_player_number": 100, "zh_user_status": "可报名"}
REGISTERED = dict(OPEN_ITEM, player_num=100, zh_user_status="已报名")


def scenario(name, detail_sequence, post_ok=True, post_exc=None, expect_complete=None):
    """detail_sequence: fetch_detail 依次返回；用 Exception 实例表示网络错误。"""
    calls = {"detail": 0, "post": 0}

    def fake_fetch_detail(session, tid):
        i = min(calls["detail"], len(detail_sequence) - 1)
        calls["detail"] += 1
        v = detail_sequence[i]
        if isinstance(v, Exception):
            raise v
        return dict(v)

    def fake_post(*a, **kw):
        calls["post"] += 1
        if post_exc:
            raise post_exc

        class R:
            status_code = 200 if post_ok else 500
            text = '{"status": "success"}' if post_ok else '{"status":"failed"}'

            @staticmethod
            def json():
                return {"status": "success"} if post_ok else {"status": "failed"}
        return R()

    orig = (A.fetch_detail, A.requests.post, A.resolve_uid)
    A.fetch_detail, A.requests.post, A.resolve_uid = fake_fetch_detail, fake_post, lambda s: 10001
    for p in (A.STATE, A.COMPLETION):
        if os.path.exists(p):
            os.remove(p)
    try:
        res = A.auto_signup(CFG, session=SESSION, dry_run=False)
    finally:
        A.fetch_detail, A.requests.post, A.resolve_uid = orig

    done = res.get("task_completed") is True
    marker = os.path.exists(A.COMPLETION)
    verdict = "OK" if (expect_complete is None or expect_complete == done) else "FAIL"
    print("%s  %s" % ("[%s]" % verdict, name))
    print("     detail拉取=%d post=%d  task_completed=%s  完成标记=%s"
          % (calls["detail"], calls["post"], done, marker))
    print("     reason: %s" % res["reason"][:120])
    if marker:
        snap = json.load(open(A.COMPLETION, encoding="utf-8"))
        print("     快照: 立即复核=%s 二次确认=%s"
              % (snap.get("quick_confirm_status"), snap.get("second_confirm_status")))
    print()
    return verdict == "OK"


def main() -> int:
    import requests

    ok = True
    seq_ok = [OPEN_ITEM, REGISTERED, REGISTERED]
    ok &= scenario("立即复核通过 + 二次确认通过", seq_ok, expect_complete=True)

    seq_quick_fail = [OPEN_ITEM, OPEN_ITEM, OPEN_ITEM]
    ok &= scenario("立即复核未通过（被抢）", seq_quick_fail, expect_complete=False)

    seq_net_then_ok = [OPEN_ITEM, REGISTERED,
                       requests.exceptions.ConnectionError("模拟抖动"), REGISTERED]
    ok &= scenario("二次确认遇网络抖动 → 重试后成功", seq_net_then_ok, expect_complete=True)

    seq_net_always = [OPEN_ITEM, REGISTERED,
                      requests.exceptions.Timeout("超时"), requests.exceptions.Timeout("超时"),
                      requests.exceptions.Timeout("超时"), requests.exceptions.Timeout("超时")]
    ok &= scenario("二次确认持续网络失败 → 告警不完成", seq_net_always, expect_complete=False)

    seq_rolled_back = [OPEN_ITEM, REGISTERED, OPEN_ITEM]
    ok &= scenario("二次确认发现被回滚（状态不对）", seq_rolled_back, expect_complete=False)

    ok &= scenario("报名 POST 直接失败", [OPEN_ITEM, OPEN_ITEM], post_ok=False, expect_complete=False)

    print("结果:", "全部通过" if ok else "存在 FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
