"""autosignup 自旋刷新逻辑的单元测试（不触网、不产生副作用）。

验证用户提出的场景：名额已释放，但服务端的"自身状态"还没同步为「可报名」。
自旋刷新应能等到状态翻转，然后继续提交。

用法: python scripts/probe/test_autosignup_spin.py
"""
from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts", "poller"))

import autosignup as A  # noqa: E402

A.STATE = os.path.join(PROJECT_ROOT, "vendor", "tmp", "spin_test_state.json")
A.LOGFILE = os.path.join(PROJECT_ROOT, "vendor", "tmp", "test_poll.log")
if os.path.exists(A.STATE):
    os.remove(A.STATE)

CFG = {
    "tournament_id": "33997",
    "tournament_name": "超级大奖赛2026广州站",
    "channels": {},                       # 不推送
    "auto_signup": {
        "enabled": True,
        "allowed_tournament_ids": ["33997"],
        "expected_name": "超级大奖赛2026广州站",
        "verify_own_status": True,
        "max_attempts_per_hour": 50,
        "allow_status_refresh": True,
        "status_refresh_tries": 5,
        "status_refresh_interval_seconds": 0,   # 测试里不等待
    },
}

SESSION = {"sessionid": "x" * 64, "deviceid": "0000000000000001"}


def scenario(name: str, sequence: list[dict]):
    """sequence: 依次返回的 detail item；最后一项会被重复使用。"""
    calls = {"n": 0, "posted": 0}

    def fake_fetch_detail(session, tid):
        i = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        return dict(sequence[i])

    def fake_post(url, headers=None, data=None, timeout=None):
        calls["posted"] += 1

        class R:
            status_code = 200

            @staticmethod
            def json():
                return {"status": "success"}
        return R()

    orig_fetch, orig_post, orig_uid = A.fetch_detail, A.requests.post, A.resolve_uid
    A.fetch_detail = fake_fetch_detail
    A.requests.post = fake_post
    A.resolve_uid = lambda session: 10001
    if os.path.exists(A.STATE):
        os.remove(A.STATE)
    try:
        res = A.auto_signup(CFG, session=SESSION, dry_run=False)
    finally:
        A.fetch_detail, A.requests.post, A.resolve_uid = orig_fetch, orig_post, orig_uid

    print("场景: %s" % name)
    print("  详情拉取次数 = %d（其中自旋 %d 次）" % (calls["n"], max(0, calls["n"] - 1)))
    print("  attempted=%s success=%s posted=%d" % (res["attempted"], res["success"], calls["posted"]))
    print("  reason: %s" % res["reason"][:140])
    print()
    return res, calls


def main() -> int:
    fails = 0

    # ① 状态第 3 次刷新才翻转 -> 应继续提交
    seq = [
        {"name": "超级大奖赛2026广州站", "player_num": 99, "max_player_number": 100, "zh_user_status": "已报满"},
        {"name": "超级大奖赛2026广州站", "player_num": 99, "max_player_number": 100, "zh_user_status": "已报满"},
        {"name": "超级大奖赛2026广州站", "player_num": 99, "max_player_number": 100, "zh_user_status": "可报名"},
    ]
    res, calls = scenario("状态在第2次自旋后翻转", seq)
    if not (res["attempted"] and calls["posted"] == 1):
        print("  [FAIL] 应在状态翻转后提交\n")
        fails += 1
    else:
        print("  [OK] 自旋等待生效并完成提交\n")

    # ② 刷新期间名额被抢 -> 应放弃
    seq2 = [
        {"name": "超级大奖赛2026广州站", "player_num": 99, "max_player_number": 100, "zh_user_status": "已报满"},
        {"name": "超级大奖赛2026广州站", "player_num": 100, "max_player_number": 100, "zh_user_status": "已报满"},
    ]
    res2, calls2 = scenario("刷新期间名额被抢", seq2)
    if calls2["posted"] != 0:
        print("  [FAIL] 名额被抢后不应提交\n")
        fails += 1
    else:
        print("  [OK] 正确放弃，未提交\n")

    # ③ 状态始终不翻转 -> 自旋耗尽后放弃
    seq3 = [{"name": "超级大奖赛2026广州站", "player_num": 99, "max_player_number": 100, "zh_user_status": "已报满"}]
    res3, calls3 = scenario("状态始终不翻转", seq3)
    if calls3["posted"] != 0 or calls3["n"] < 5:
        print("  [FAIL] 应自旋 5 次后放弃且不提交\n")
        fails += 1
    else:
        print("  [OK] 自旋耗尽后放弃，未提交\n")

    # ④ 已报名 -> 不应重复提交
    seq4 = [{"name": "超级大奖赛2026广州站", "player_num": 99, "max_player_number": 100, "zh_user_status": "已报名"}]
    res4, calls4 = scenario("自身已是已报名", seq4)
    if calls4["posted"] != 0:
        print("  [FAIL] 已报名不应重复提交\n")
        fails += 1
    else:
        print("  [OK] 识别到已报名，未重复提交\n")

    print("结果: %s" % ("全部通过" if fails == 0 else "%d 项失败" % fails))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
