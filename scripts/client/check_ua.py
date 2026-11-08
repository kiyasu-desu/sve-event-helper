"""通过 CDP 检查窗口内的 UA、页面内容与 JS 状态。

目的：确定"空白"到底是
  (a) --user-agent 没生效（页面按桌面 UA 走了不渲染的分支），还是
  (b) UA 生效但页面报错/仍在初始化。
"""
import json
import sys
import time
import urllib.request

import websocket

PORT = 9222


def targets():
    return json.loads(urllib.request.urlopen(
        "http://127.0.0.1:%d/json/list" % PORT, timeout=8).read().decode())


def main() -> int:
    pages = [t for t in targets() if t.get("type") == "page"]
    if not pages:
        print("  没有 page 目标")
        return 1
    t = pages[0]
    print("  目标 url=%s" % (t.get("url") or "")[:120])
    ws_url = t.get("webSocketDebuggerUrl")
    if not ws_url:
        print("  无 webSocketDebuggerUrl")
        return 1

    ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
    ws.settimeout(30)

    def rpc(mid, method, params=None):
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            raw = ws.recv()
            m = json.loads(raw)
            if m.get("id") == mid:
                return m

    r = rpc(1, "Runtime.evaluate", {
        "expression": "JSON.stringify({ua: navigator.userAgent, "
                      "title: document.title, "
                      "ready: document.readyState, "
                      "bodyLen: (document.body ? document.body.innerHTML.length : -1), "
                      "text: (document.body ? document.body.innerText.slice(0,200) : ''), "
                      "inputs: document.querySelectorAll('input').length})",
        "returnByValue": True})
    try:
        val = json.loads(r["result"]["result"]["value"])
    except Exception:
        print("  读取失败:", json.dumps(r)[:400])
        ws.close()
        return 1

    print()
    print("  === 窗口内实况 ===")
    print("  navigator.userAgent = %s" % val.get("ua"))
    print("  是否移动端           = %s" % ("Mobile" in (val.get("ua") or "")))
    print("  document.title       = %r" % val.get("title"))
    print("  readyState           = %s" % val.get("ready"))
    print("  body.innerHTML 长度  = %s" % val.get("bodyLen"))
    print("  input 元素数         = %s" % val.get("inputs"))
    print("  body 文本            = %r" % (val.get("text") or ""))

    # 顺便看有没有 JS 报错
    try:
        r2 = rpc(2, "Runtime.evaluate", {
            "expression": "window.__err || '(未捕获到全局错误)'", "returnByValue": True})
        print("  全局错误             = %s" % str(r2["result"]["result"].get("value"))[:200])
    except Exception:
        pass

    ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
