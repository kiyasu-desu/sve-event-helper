"""受控测试：报名成功后订阅是否会自动移出列表。

不触网、不产生真实报名：直接调用 server 的事件回调，模拟 "completed" 事件，
然后在 AUTO_REMOVE_DELAY 之后检查订阅是否被移除。
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))

os.environ["SVE_CLIENT_PORT"] = "8899"   # 不占用正式端口

spec = importlib.util.spec_from_file_location("srv", os.path.join(HERE, "server.py"))
srv = importlib.util.module_from_spec(spec)
sys.modules["srv"] = srv
spec.loader.exec_module(srv)             # 只定义，不会 app.run

TID = "TEST-1"
with srv.LOCK:
    srv.STATE["subs"][TID] = {"name": "测试赛事", "account": "测试账号", "uid": 1,
                              "status": "已报满", "window_desc": "测试窗口"}
    srv.STATE["schedulers"][TID] = None

print("  事件前: 订阅数=%d  含测试项=%s" % (len(srv.STATE["subs"]), TID in srv.STATE["subs"]))
print("  AUTO_REMOVE_DELAY = %s 秒" % srv.AUTO_REMOVE_DELAY)

# 触发完成事件（等价于调度器上报 completed）
srv._on_event(TID, "completed", {})

# 立刻检查：应该还在（留时间给界面渲染"已完成"）
time.sleep(1)
still = TID in srv.STATE["subs"]
print("  1 秒后: 仍在校（应为 True）= %s" % still)

# 等待自动移除
time.sleep(srv.AUTO_REMOVE_DELAY + 2)
gone = TID not in srv.STATE["subs"]
print("  %d 秒后: 已移出（应为 True）= %s" % (srv.AUTO_REMOVE_DELAY + 3, gone))

ok = still and gone
print()
print("  PASS: 完成后会自动移出订阅列表" if ok else "  FAIL")
sys.exit(0 if ok else 1)
