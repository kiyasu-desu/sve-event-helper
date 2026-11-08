"""让登录窗口重新加载，并捕获这一次加载的网络请求与失败。

用途：定位"空白窗口"——判断是页面没请求资源，还是资源加载失败，
或只是首次加载的时序问题（重载即可恢复）。
"""
import json
import sys
import time
import urllib.request

import websocket

PORT = 9222


def pages():
    d = json.loads(urllib.request.urlopen(
        "http://127.0.0.1:%d/json/list" % PORT, timeout=8).read().decode())
    return [t for t in d if t.get("type") == "page"]


def main() -> int:
    ps = [t for t in pages() if "login-betasystem" in (t.get("url") or "")]
    if not ps:
        print("  未找到登录页目标")
        for t in pages():
            print("   -", t.get("title"), (t.get("url") or "")[:80])
        return 1

    t = ps[0]
    print("  目标 title=%r" % t.get("title"))
    ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=25,
                                     suppress_origin=True)
    ws.settimeout(25)

    got = {}

    def rpc(mid, method, params=None):
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        for _ in range(400):
            m = json.loads(ws.recv())
            if m.get("id") == mid:
                return m
            handle(m)
        return {}

    def handle(m):
        meth = m.get("method")
        if meth == "Network.requestWillBeSent":
            u = m["params"]["request"]["url"]
            if any(k in u for k in ("ma68", "gsf", "fansclub", "passport", "urs/", "127.0.0.1")):
                print("  -> %s" % u[:140], flush=True)
        elif meth == "Network.loadingFailed":
            p = m["params"]
            print("  ✗ 加载失败: %s  errorText=%s  canceled=%s"
                  % (p.get("type"), p.get("errorText"), p.get("canceled")), flush=True)
        elif meth == "Network.responseReceived":
            r = m["params"]["response"]
            if any(k in r["url"] for k in ("ma68", "fansclub", "gsf")):
                print("  <- %s %s" % (r["status"], r["url"][:120]), flush=True)

    rpc(1, "Network.enable")
    print("\n  触发重新加载...")
    rpc(2, "Page.enable")
    rpc(3, "Page.reload", {"ignoreCache": True})

    # 收 20 秒事件
    end = time.time() + 20
    while time.time() < end:
        try:
            handle(json.loads(ws.recv()))
        except Exception:
            continue

    # 读重载后的标题与正文
    r = rpc(9, "Runtime.evaluate", {
        "expression": "JSON.stringify({title:document.title, "
                      "bodyLen:(document.body?document.body.innerHTML.length:-1), "
                      "text:(document.body?document.body.innerText.slice(0,150):''), "
                      "inputs:document.querySelectorAll('input').length})",
        "returnByValue": True})
    try:
        v = json.loads(r["result"]["result"]["value"])
        print("\n  重载后: title=%r  bodyLen=%s  inputs=%s" % (v["title"], v["bodyLen"], v["inputs"]))
        print("  文本: %r" % (v["text"] or ""))
    except Exception:
        print("\n  读取重载后状态失败:", json.dumps(r)[:200])

    ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
