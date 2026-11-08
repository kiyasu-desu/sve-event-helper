"""试探「我的赛事」用的接口/参数。

原 APP 在"我的"里有"我的赛事"入口（bundle 里存在 /pages/my-tournament/index），
但它对应的接口无法从混淆后的字符串表还原，只能实测。
本脚本用**正确的签名方式**（列表接口的 query 参数必须参与签名）逐个试参数。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                "app", "client", "core"))
sys.stdout.reconfigure(encoding="utf-8")
from sveapi import Account, SveClient, SveError  # noqa: E402

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SESSION = os.path.join(PROJECT_ROOT, "capture", "session.json")


def main() -> int:
    s = json.load(open(SESSION, encoding="utf-8-sig"))
    c = SveClient(Account(sessionid=s["sessionid"], deviceid=s["deviceid"]))
    base_uid = c.info().get("id")
    print("  测试账号 uid=%s" % base_uid)

    # 基线：不带地区参数会怎样
    print()
    print("=== 不带 area（看能否返回全部/我的）===")
    for params in ({"_size": 200}, {"_size": 200, "uid": base_uid},
                   {"_size": 200, "user_id": base_uid}):
        try:
            d = c._request("GET", "/urs/tournament/list", params=params)
            items = d.get("items") or []
            mine = [x for x in items
                    if (x.get("zh_user_status") or "") in ("已报名", "已预约")]
            print("  %-38s 返回 %3d 场，已报名/预约 %d"
                  % (json.dumps(params, ensure_ascii=False), len(items), len(mine)))
        except SveError as e:
            print("  %-38s 失败: %s" % (json.dumps(params, ensure_ascii=False), str(e)[:60]))

    # 带地区 + 各种"只看我的"参数
    print()
    print("=== 带 area 的筛选参数试探（广州）===")
    base = {"_size": 200, "area": "广东省-广州市", "is_sv2": 0}
    trials = [
        {}, {"user_status": "已报名"}, {"status": "已报名"},
        {"mine": 1}, {"only_mine": 1}, {"is_mine": 1}, {"joined": 1},
        {"user_only": 1}, {"signup": 1}, {"uid": base_uid},
        {"user_id": base_uid}, {"type": "mine"}, {"filter": "mine"},
    ]
    for extra in trials:
        p = dict(base)
        p.update(extra)
        try:
            d = c._request("GET", "/urs/tournament/list", params=p)
            items = d.get("items") or []
            mine = [x for x in items
                    if (x.get("zh_user_status") or "") in ("已报名", "已预约")]
            flag = "  <== 明显不同" if len(items) != 100 and len(items) != 0 else ""
            print("  %-40s 返回 %3d 场，其中我的 %d%s"
                  % (json.dumps(extra, ensure_ascii=False), len(items), len(mine), flag))
        except SveError as e:
            print("  %-40s 失败: %s" % (json.dumps(extra, ensure_ascii=False), str(e)[:60]))

    # 试探独立端点
    print()
    print("=== 试探可能的独立端点 ===")
    for path in ("/urs/tournament/mine", "/urs/tournament/my",
                 "/urs/tournament/list/mine", "/urs/user/tournament",
                 "/urs/info/tournament", "/urs/tournament/user"):
        try:
            d = c._request("GET", path)
            n = len(d.get("items") or [])
            print("  %-32s OK items=%d" % (path, n))
        except SveError as e:
            print("  %-32s 失败: %s" % (path, str(e)[:50]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
