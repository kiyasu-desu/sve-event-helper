"""离线自测：验证本地回调服务的 CORS 预检与实际投递。

登录页在 login-betasystem.nie.netease.com 域下运行，它 POST 到 127.0.0.1 属于跨域，
因此必须能正确处理预检（OPTIONS）并返回 CORS 头，否则浏览器会拦掉这次投递。
"""
import json
import sys
import threading
import time
import urllib.request

import os
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts", "client"))

import login_window as L  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

PORT = 8767
ORIGIN = "https://login-betasystem.nie.netease.com"

httpd = ThreadingHTTPServer(("127.0.0.1", PORT), L.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.4)

base = "http://127.0.0.1:%d" % PORT

# 1) 预检
req = urllib.request.Request(
    base + "/login_callback", method="OPTIONS",
    headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST",
             "Access-Control-Request-Headers": "content-type"})
r = urllib.request.urlopen(req, timeout=5)
print("  OPTIONS -> HTTP %s  ACAO=%s  ACAM=%s"
      % (r.status, r.headers.get("Access-Control-Allow-Origin"),
         r.headers.get("Access-Control-Allow-Methods")))

# 2) 实际投递（模拟登录页 POST）
sid = "b" * 64
body = json.dumps({"session_id": sid}).encode()
req2 = urllib.request.Request(
    base + "/login_callback", data=body,
    headers={"Content-Type": "application/json", "Origin": ORIGIN,
             "Cookie": "NTES_SESS=fake-cookie-value"})
r2 = urllib.request.urlopen(req2, timeout=5)
print("  POST    -> HTTP %s  body=%s  ACAO=%s"
      % (r2.status, r2.read().decode(), r2.headers.get("Access-Control-Allow-Origin")))

# 3) 状态查询
st = json.loads(urllib.request.urlopen(base + "/__status", timeout=5).read().decode())
print("  status  -> %s" % st)

ok = (st.get("sessionid") == sid)
print()
print("  PASS: 回调可用，sessionid 解析成功" if ok else "  FAIL: 未解析到 sessionid")
httpd.shutdown()
sys.exit(0 if ok else 1)
