"""用有报名记录的账号，确认「我的赛事」的最佳取数方式。

关键问题：
  1. 不带 area 的 list 是否真的跨地区返回？
  2. 是否有参数能直接筛出"我的赛事"？
  3. 若没有，能否一次取全量再本地过滤（避免逐地区扫描）？
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                                "app", "client", "core"))
sys.stdout.reconfigure(encoding="utf-8")
from sveapi import Account, SveClient, SveError  # noqa: E402

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def main() -> int:
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
    import sveenv
    name = sys.argv[1] if len(sys.argv) > 1 else None
    session = sveenv.load_session(name)
    if not session:
        sys.exit("请提供有效 account_id；未知 ID 不使用其他账号")
    acc = Account.from_dict(session)
    c = SveClient(acc)
    print("  使用账号: %s (uid=%s)" % (name, acc.uid))

    # 该账号已报名的赛事 id 需自行确认（原脚本里写死了几个）
    KNOWN = {"33998", "33933", "33959", "33960", "33968", "34043"}

    print()
    print("=== A) 不带 area ===")
    d = c._request("GET", "/urs/tournament/list", params={"_size": 200, "_page": 1})
    items = d.get("items") or []
    got = {str(x.get("tournament_id")) for x in items}
    areas = Counter((x.get("location_area") or "?") for x in items)
    print("  返回 %d 场，覆盖地区数 %d" % (len(items), len(areas)))
    for a, n in areas.most_common(8):
        print("     %-24s %d" % (a, n))
    hit = KNOWN & got
    print("  命中的已知报名赛事: %d/%d  %s" % (len(hit), len(KNOWN), sorted(hit)))
    print("  各状态分布:", dict(Counter((x.get("zh_user_status") or "?") for x in items)))

    print()
    print("=== B) 分页能否取更多 ===")
    for page in (2, 3):
        d2 = c._request("GET", "/urs/tournament/list",
                        params={"_size": 200, "_page": page})
        it2 = d2.get("items") or []
        print("  _page=%d 返回 %d 场（%s）" % (page, len(it2),
              "与本页有重叠" if {str(x.get('tournament_id')) for x in it2} & got else "全新"))

    print()
    print("=== C) 带 area 时是否也能看到已知报名 ===")
    for area in ("广东省-广州市", "广东省-深圳市"):
        d3 = c._request("GET", "/urs/tournament/list",
                        params={"_size": 200, "area": area, "is_sv2": 0})
        it3 = d3.get("items") or []
        got3 = {str(x.get("tournament_id")) for x in it3}
        print("  %-16s 返回 %3d 场，命中已知 %d" % (area, len(it3), len(KNOWN & got3)))

    print()
    print("=== 结论 ===")
    if len(hit) >= len(KNOWN) - 1:
        print("  不带 area 的 list 可一次拿到全量（含我的赛事），本地过滤即可。")
    else:
        print("  不带 area 拿不全，仍需按地区扫描。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
