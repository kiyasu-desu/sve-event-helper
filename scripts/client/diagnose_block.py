"""在登录页上下文里注入一个到本地回调地址的请求，读出被拦截的确切原因。

为什么这样做：页面卡在"正在跳转"，performance 显示对 http://127.0.0.1:8765 的
请求 status=0（被拦截），但服务端连预检都没收到 —— 说明请求在浏览器内部就被挡了。
本脚本用 CDP 在**同一个页面的上下文**里复现这次请求，并抓取
Network.loadingFailed 的 blockedReason（mixed-content / csp / private-network-access 等），
从而确定到底是哪种策略在拦截。
"""
import json
import sys
import time
import urllib.request

import websocket

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9666
TARGET = "http://127.0.0.1:8765/login_callback"


def main() -> int:
    d = json.loads(urllib.request.urlopen(
        "http://127.0.0.1:%d/json/list" % PORT, timeout=8).read().decode())
    pages = [t for t in d if t.get("type") == "page" and "login-betasystem" in (t.get("url") or "")]
    if not pages:
        print("  未找到登录页")
        return 1
    ws = websocket.create_connection(pages[0]["webSocketDebuggerUrl"], timeout=30,
                                     suppress_origin=True)
    ws.settimeout(30)

    failures = []
    console = []

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            try:
                m = json.loads(ws.recv())
            except Exception:
                continue
            meth = m.get("method")
            if meth == "Network.loadingFailed":
                p = m["params"]
                failures.append((p.get("type"), p.get("errorText"), p.get("blockedReason")))
            elif meth == "Log.entryAdded":
                console.append(m["params"]["entry"].get("text", "")[:200])

    def rpc(mid, method, params=None):
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        for _ in range(200):
            m = json.loads(ws.recv())
            if m.get("id") == mid:
                return m
            meth = m.get("method")
            if meth == "Network.loadingFailed":
                p = m["params"]
                failures.append((p.get("type"), p.get("errorText"), p.get("blockedReason")))
        return {}

    rpc(1, "Network.enable")
    rpc(2, "Log.enable")

    expr = (
        "fetch(%r, {method:'POST', mode:'cors', "
        "headers:{'Content-Type':'application/json'}, body:'{\"probe\":1}'})"
        ".then(function(r){return r.text().then(function(t){return 'OK '+r.status+' '+t;});})"
        ".catch(function(e){return 'ERR '+e.name+': '+e.message;});" % TARGET
    )
    r = rpc(3, "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": True})
    pump(4)

    try:
        print("  注入请求结果: %s" % r["result"]["result"]["value"])
    except Exception:
        print("  结果读取失败:", json.dumps(r)[:300])

    print("\n  === 被拦截/失败的请求 ===")
    if not failures:
        print("   （无）")
    for t, err, blocked in failures:
        print("   类型=%s  错误=%s  **blockedReason=%s**" % (t, err, blocked))

    if console:
        print("\n  === 控制台日志 ===")
        for c in console[-8:]:
            print("   ", c)
        print("\n  （上面若有 mixed-content / CORS 提示，即为拦截原因）")

    ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
