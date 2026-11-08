"""回调服务的离线自测：不打开窗口，只验证 HTTP 回调接收与 sessionid 解析。"""
import json
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import os
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts", "client"))

import login_probe as L  # noqa: E402

PORT = 8799
SID = "a" * 64

srv = ThreadingHTTPServer(("127.0.0.1", PORT), L.CallbackHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.5)

# 模拟登录页的投递
body = json.dumps({"session_id": SID}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:%d/login_callback" % PORT,
    data=body,
    headers={"Content-Type": "application/json", "Cookie": "NTES_SESS=fake"},
)
r = urllib.request.urlopen(req, timeout=5)
print("  回调响应: HTTP %s %s" % (r.status, r.read().decode()))

# 再模拟一次 GET（有些实现用 302 跳回）
urllib.request.urlopen("http://127.0.0.1:%d/login_callback" % PORT, timeout=5)

time.sleep(0.3)
print("  收到回调数:", len(L.RESULT.hits))
got = [h["sessionid"] for h in L.RESULT.hits if h["sessionid"]]
print("  解析出的 sessionid:", got[0][:24] + "..." if got else "无")
print("  done 事件是否置位:", L.RESULT.done.is_set())
print()
print("  ✅ 回调服务自测通过" if got and L.RESULT.done.is_set() else "  ❌ 自测失败")

# 一并打印构造出的登录 URL，便于人工核对
print()
print("构造的登录 URL：")
print("  " + L.build_login_url("http://127.0.0.1:8799/login_callback", "5f3a9c1d7e2b4806"))
srv.shutdown()
