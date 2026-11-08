"""赛事报名 / 取消报名操作（签名在本地生成，不依赖 APP）。

接口：POST /urs/tournament/action
请求体：{"uid": <uid>, "tournament_id": "<id>", "action": "signup" | "cancel"}

签名由 scripts/poller/signer.py 本地计算，因此不需要模拟器、Frida 或抓包。

用法：
    python scripts/poller/action.py --status 33933
    python scripts/poller/action.py --signup 33933
    python scripts/poller/action.py --cancel 33933
    python scripts/poller/action.py --roundtrip 33933   # 报名后立刻取消（自检用）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts", "poller"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from signer import generate_sign, sign_payload  # noqa: E402
import sveenv  # noqa: E402

BASE = "https://api-sve.nie.netease.com/sve/api/v1"
TIMEOUT = 20
ACTION_URL = "%s/urs/tournament/action" % BASE


def load_session() -> dict:
    """会话来自统一入口（UID 数据集的默认 active_account_id）。

    此前这里直接读 capture/session.json——那是第二份副本，同一账号的
    sessionid 存两处既可能不一致，也让"哪些文件必须排除"说不清。
    """
    return sveenv.load_session()


def headers_for(session: dict, body: dict) -> dict:
    return {
        "content-type": "application/json",
        "deviceid": session["deviceid"],
        "platform": "android",
        "sessionid": session["sessionid"],
        "sign": generate_sign(body, udid=session["deviceid"]),
        "User-Agent": "okhttp/4.9.2",
    }


def detail(session: dict, tournament_id: str) -> dict:
    body = {}
    r = requests.get("%s/urs/tournament/detail/%s" % (BASE, tournament_id),
                     headers=headers_for(session, body), timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError("detail HTTP %s: %s" % (r.status_code, r.text[:200]))
    return r.json().get("item", {})


def show(item: dict) -> None:
    if not item:
        print("  (无数据)")
        return
    print("  name          = %s" % item.get("name"))
    print("  player_num    = %s / %s" % (item.get("player_num"), item.get("max_player_number")))
    print("  my_status     = %s" % item.get("zh_user_status"))
    print("  my_signup_time= %s" % item.get("player_signup_time"))


def do_action(session: dict, tournament_id: str, action: str, uid: int | None = None) -> bool:
    if uid is None:
        # /urs/info 的返回里字段名是 id（不是 uid），值与报名 body 里的 uid 相同
        info = requests.get("%s/urs/info" % BASE, headers=headers_for(session, {}), timeout=TIMEOUT)
        item = info.json().get("item", {})
        uid = int(item.get("id") or item.get("uid"))
        print("  已解析 uid=%s (%s)" % (uid, item.get("nickname")))
    body = {"uid": uid, "tournament_id": str(tournament_id), "action": action}
    h = headers_for(session, body)
    print("  action=%s uid=%s tid=%s" % (action, uid, tournament_id))
    print("  payload = %s" % sign_payload(body, udid=session["deviceid"]))
    print("  sign    = %s" % h["sign"])
    r = requests.post(ACTION_URL, headers=h,
                      data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
                      timeout=TIMEOUT)
    ok = r.status_code == 200 and r.json().get("status") == "success"
    print("  HTTP %s -> %s  %s" % (r.status_code, "成功" if ok else "失败", r.text[:200]))
    return ok


def main() -> int:
    sveenv.assert_legacy_executor_allowed()
    ap = argparse.ArgumentParser()
    ap.add_argument("tournament_id", nargs="?", default="33933")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--signup", action="store_true")
    ap.add_argument("--cancel", action="store_true")
    ap.add_argument("--roundtrip", action="store_true", help="报名后立刻取消")
    args = ap.parse_args()

    session = load_session()
    tid = args.tournament_id

    print("=== 操作前 ===")
    show(detail(session, tid))

    if args.roundtrip:
        print("\n=== 报名 ===")
        if not do_action(session, tid, "signup"):
            return 1
        time.sleep(3)
        print("\n=== 报名后状态 ===")
        show(detail(session, tid))
        print("\n=== 取消 ===")
        if not do_action(session, tid, "cancel"):
            return 1
        time.sleep(3)
        print("\n=== 取消后状态 ===")
        show(detail(session, tid))
        return 0

    if args.signup:
        print("\n=== 报名 ===")
        if not do_action(session, tid, "signup"):
            return 1
        time.sleep(3)
        print("\n=== 之后状态 ===")
        show(detail(session, tid))
    elif args.cancel:
        print("\n=== 取消 ===")
        if not do_action(session, tid, "cancel"):
            return 1
        time.sleep(3)
        print("\n=== 之后状态 ===")
        show(detail(session, tid))
    else:
        print("\n（仅查看状态；用 --signup / --cancel / --roundtrip 执行动作）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
