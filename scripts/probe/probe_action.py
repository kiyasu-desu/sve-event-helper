"""探测 tournament/action 接口要求的请求体结构。

做法：故意发送不完整/错误的 body，观察服务端的报错。
服务端通常会回 "missing parameter: xxx" 这类信息，比抓包猜参数更直接。

重要：本脚本对 POST 默认使用 --dry-run 模式（只打印将要发送的内容）。
真正发送需要显式加 --send，且应先用 GET 确认目标赛事未满员，
否则会产生真实的报名副作用。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
import sveenv  # noqa: E402
BASE = "https://api-sve.nie.netease.com/sve/api/v1"
TIMEOUT = 20

TEST_TOURNAMENT = "33933"   # 周二积分赛（个人赛，测试用）


def load_session() -> dict:
    return sveenv.load_session()


def headers_for(session, body: str | None = None):
    """按 APP 的抓包结果构造请求头；body 非空时附带 Content-Length 语义。"""
    h = {
        "content-type": "application/json",
        "deviceid": session["deviceid"],
        "platform": "android",
        "sign": session["sign"],
        "sessionid": session["sessionid"],
        "User-Agent": "okhttp/4.9.2",
    }
    if body is not None:
        h["Content-Length"] = str(len(body.encode("utf-8")))
    return h


def probe(session, body_obj, label: str, send: bool):
    body = json.dumps(body_obj, ensure_ascii=False, separators=(",", ":"))
    h = headers_for(session, body)
    print("  %-34s body(%d bytes): %s" % (label, len(body.encode("utf-8")), body))
    if not send:
        print("      (dry-run，未发送)")
        return None
    try:
        r = requests.post("%s/urs/tournament/action" % BASE, headers=h,
                          data=body.encode("utf-8"), timeout=TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        print("      ERROR %s" % exc)
        return None
    print("      HTTP %s  %s" % (r.status_code, r.text[:260].replace("\n", " ")))
    return r


def main() -> int:
    sveenv.assert_legacy_executor_allowed()
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="真正发送 POST（默认只 dry-run）")
    ap.add_argument("--tournament", default=TEST_TOURNAMENT)
    args = ap.parse_args()

    session = load_session()
    tid = args.tournament

    print("目标赛事: %s" % tid)
    print("模式    : %s" % ("真实发送" if args.send else "dry-run（只打印）"))
    print()

    print("步骤 0：先用 GET 确认该赛事未满员")
    r = requests.get("%s/urs/tournament/detail/%s" % (BASE, tid),
                     headers=headers_for(session), timeout=TIMEOUT)
    if r.status_code != 200:
        print("  详情接口 HTTP %s: %s" % (r.status_code, r.text[:200]))
        return 2
    item = r.json().get("item", {})
    print("  %s  player_num=%s / max=%s  my_status=%s"
          % (item.get("name"), item.get("player_num"), item.get("max_player_number"),
             item.get("zh_user_status")))
    print()

    print("步骤 1：用各种不完整 body 探测参数校验（看服务端报错）")
    cases = [
        ("空对象", {}),
        ("只有 tournament_id", {"tournament_id": tid}),
        ("tournament_id 为数字", {"tournament_id": int(tid)}),
        ("加 action=signup", {"tournament_id": tid, "action": "signup"}),
        ("加 action=enroll", {"tournament_id": tid, "action": "enroll"}),
        ("加 type=signup", {"tournament_id": tid, "type": "signup"}),
        ("加 op=signup", {"tournament_id": tid, "op": "signup"}),
        ("action=cancel", {"tournament_id": tid, "action": "cancel"}),
        ("带 player_num", {"tournament_id": tid, "player_num": 1}),
    ]
    for label, body in cases:
        probe(session, body, label, args.send)
        if args.send:
            time.sleep(1.2)

    if not args.send:
        print()
        print("提示：先用 --send 跑一轮，观察服务端报错信息（通常会指出缺哪个参数）。")
        print("      注意：如果某个 case 恰好是正确格式，会真的产生报名/取消副作用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
