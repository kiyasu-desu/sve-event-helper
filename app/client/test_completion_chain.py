"""验证「报名完成 → 通知 → 记入记录 → 自动移出订阅」这条链路。

用桩替换真正的发送器，所以不会真的发通知；目的是确认事件接线正确，
而不是重复测试 notify.py 本身（那部分在部署时已实测发送成功）。
"""
import importlib.util
import os
import sys
import time

HERE = os.path.abspath(os.path.dirname(os.path.abspath(__file__)))
PROJECT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(PROJECT, "app", "client"))
sys.path.insert(0, os.path.join(PROJECT, "app", "client", "core"))
sys.stdout.reconfigure(encoding="utf-8")

os.environ["SVE_CLIENT_PORT"] = "8897"

spec = importlib.util.spec_from_file_location(
    "srv", os.path.join(PROJECT, "app", "client", "server.py"))
srv = importlib.util.module_from_spec(spec)
sys.modules["srv"] = srv
spec.loader.exec_module(srv)

sent = []
srv.send_notify = lambda title, html: sent.append((title, html))     # 桩

def run_case(label, event_kind, data, key="测试账号|88888"):
    sent.clear()
    with srv.LOCK:
        srv.STATE["subs"][key] = {
            "account": "测试账号", "uid": 999, "tournament_id": "88888",
            "name": "测试赛事", "status": "已报名", "logs": [], "done_handled": False,
        }
        srv.STATE["schedulers"][key] = None
        srv.STATE["records"].pop(key, None)

    srv._on_event(key, event_kind, data)
    time.sleep(0.6)

    rec = (srv.STATE["records"] or {}).get(key)
    print("  [%s]" % label)
    print("     通知已发送 : %s%s" % (bool(sent), ("  -> " + sent[0][0]) if sent else ""))
    print("     通知含账号 : %s" % (any("测试账号" in h for _, h in sent)))
    print("     已写入记录 : %s" % (bool(rec) and rec.get("status")))
    print("     记录来源   : %s" % (rec or {}).get("source"))

    time.sleep(srv.AUTO_REMOVE_DELAY + 2)
    gone = key not in srv.STATE["subs"]
    print("     %d 秒后自动移出: %s" % (srv.AUTO_REMOVE_DELAY + 3, gone))
    return bool(sent) and bool(rec) and gone


print("=== 完成链路验证（通知为桩，不会真发）===")
ok1 = run_case("真实报名成功（两段确认通过）", "completed",
               {"result": type("R", (), {"ok": True})()})
print()
ok2 = run_case("启动时已是「已报名」", "completed", {"already": True})
print()
ok3 = run_case("仅收到 stop(completed=True) 的兜底路径", "stop", {"completed": True})

print()
print("  结论:", "PASS 三条路径都完成「通知 + 记录 + 自动移出」"
      if (ok1 and ok2 and ok3) else "FAIL")
sys.exit(0 if (ok1 and ok2 and ok3) else 1)
