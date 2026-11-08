"""通过 Chromium 调试端口观察登录页的真实网络行为。

为什么需要：登录页是移动端 SPA，靠标题/截图都无法判断它到底做了什么。
这个工具连上调试端口，开启 Network 域，把页面发出的请求打出来——
从而确认"登录动作到底请求了哪里、有没有回调到我们的本地服务"。

用法:
    <venv-client>\\Scripts\\python.exe scripts\\client\\watch_login.py --port 9333 --seconds 180
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

import websocket


def targets(port: int):
    return json.loads(urllib.request.urlopen(
        "http://127.0.0.1:%d/json/list" % port, timeout=8).read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9333, help="Chromium 调试端口")
    ap.add_argument("--seconds", type=int, default=180)
    args = ap.parse_args()

    try:
        ts = targets(args.port)
    except Exception as e:
        print("  无法连接调试端口 %d: %s" % (args.port, e))
        return 1

    pages = [t for t in ts if t.get("type") == "page"]
    print("  页面目标 %d 个" % len(pages))
    for t in pages:
        print("   - title=%r" % t.get("title"))
        print("     url  =%s" % (t.get("url") or "")[:120])
    if not pages:
        return 1

    t = pages[0]
    ws_url = t.get("webSocketDebuggerUrl")
    if not ws_url:
        print("  无 webSocketDebuggerUrl")
        return 1

    # suppress_origin=True 很关键：否则 Chromium 会以 403 拒绝 WebSocket 握手
    # （"Rejected an incoming WebSocket connection from the http://127.0.0.1:<port> origin"）。
    # 抑制 Origin 后就不需要在启动 Edge 时加 --remote-allow-origins，
    # 而实测那个参数与渲染失败相关，能不加就不加。
    ws = websocket.create_connection(ws_url, timeout=20, suppress_origin=True)
    ws.settimeout(20)

    def send(mid, method, params=None):
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))

    send(1, "Network.enable")
    send(2, "Runtime.enable")
    print("\n  已开启网络监听。请在浏览器里执行登录（若已登录，先退出再登录）。\n")

    reqs = {}
    deadline = time.time() + args.seconds
    while time.time() < deadline:
        try:
            msg = json.loads(ws.recv())
        except Exception:
            continue
        m = msg.get("method")
        if m == "Network.requestWillBeSent":
            p = msg["params"]
            rid = p["requestId"]
            reqs[rid] = p
            url = p["request"]["url"]
            # 只报告有意义的请求，过滤静态资源噪音
            if any(k in url for k in ("urs/", "login", "callback", "passport",
                                      "account", "nickname", "token", "127.0.0.1")):
                print("  -> %s %s" % (p["request"]["method"], url[:150]), flush=True)
                pd = p["request"].get("postData")
                if pd:
                    print("     POST: %s" % pd[:300], flush=True)
        elif m == "Network.responseReceived":
            p = msg["params"]
            rid = p["requestId"]
            url = p["response"]["url"]
            if rid in reqs and any(k in url for k in ("urs/", "login", "callback", "127.0.0.1")):
                print("  <- %s %s" % (p["response"]["status"], url[:150]), flush=True)

    ws.close()
    print("\n  监听结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
