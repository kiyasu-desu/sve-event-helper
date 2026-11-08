"""确认是否有服务端"只看我的"筛选，以及全量分页规模。"""
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
    # 该账号已报名的赛事 id 需自行确认
    KNOWN: set = set()

    print("=== 试探可能的服务端筛选（不带 area）===")
    for extra in ({"mine": 1}, {"only_mine": 1}, {"user_status": "已报名"},
                  {"status": "已报名"}, {"is_mine": 1}, {"user_only": 1}):
        p = {"_size": 200, "_page": 1}
        p.update(extra)
        try:
            d = c._request("GET", "/urs/tournament/list", params=p)
            items = d.get("items") or []
            st = Counter((x.get("zh_user_status") or "?") for x in items)
            got = {str(x.get("tournament_id")) for x in items}
            print("  %-34s %3d 场  状态=%-40s 命中已知=%d"
                  % (json.dumps(extra, ensure_ascii=False), len(items),
                     json.dumps(dict(st), ensure_ascii=False)[:40], len(KNOWN & got)))
        except SveError as e:
            print("  %-34s 失败 %s" % (json.dumps(extra, ensure_ascii=False), str(e)[:50]))

    print()
    print("=== 全量分页规模（不带 area，最多翻 8 页）===")
    seen = set()
    my = []
    for page in range(1, 9):
        try:
            d = c._request("GET", "/urs/tournament/list",
                           params={"_size": 200, "_page": page})
        except SveError as e:
            print("  page %d 失败: %s" % (page, e)); break
        items = d.get("items") or []
        if not items:
            print("  page %d 为空，结束" % page); break
        ids = {str(x.get("tournament_id")) for x in items}
        new = ids - seen
        seen |= ids
        for x in items:
            if (x.get("zh_user_status") or "") in ("已报名", "已预约"):
                my.append(x)
        print("  page %d: %3d 场（新增 %3d，累计 %4d）"
              % (page, len(items), len(new), len(seen)))
        if not new:
            print("  无新增，结束"); break

    print()
    print("=== 翻页后收集到的我的赛事 ===")
    for x in my:
        print("   %-14s %-30s %s" % (x.get("tournament_id"), (x.get("name") or "")[:30],
                                     x.get("zh_user_status")))
    print("  合计 %d 场（已知 6 场）" % len(my))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
