"""通过 CDP 读取页面已发出的网络请求与 JS 错误（含历史请求）。

用途：页面卡在"正在跳转"时，网络监听只能看到"之后的"新请求；
用 performance.getEntriesByType('resource') 可以**回读已经发生过的**请求，
从而判断回调 POST 是否真的发出、被阻塞在哪一步——不需要用户再登录一次。
"""
import json
import sys
import urllib.request

import websocket

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9666


def main() -> int:
    d = json.loads(urllib.request.urlopen(
        "http://127.0.0.1:%d/json/list" % PORT, timeout=8).read().decode())
    pages = [t for t in d if t.get("type") == "page" and "login-betasystem" in (t.get("url") or "")]
    if not pages:
        print("  未找到登录页")
        return 1
    t = pages[0]
    ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=25,
                                     suppress_origin=True)
    ws.settimeout(25)

    def ev(expr):
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": expr, "returnByValue": True}}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == 1:
                return m

    # 1) 基本信息
    r = ev("JSON.stringify({title:document.title, url:location.href.slice(0,120), "
           "ready:document.readyState, "
           "text:(document.body?document.body.innerText.replace(/\\s+/g,' ').slice(0,200):''), "
           "inputs:document.querySelectorAll('input').length})")
    try:
        v = json.loads(r["result"]["result"]["value"])
        print("  title=%r  ready=%s  inputs=%s" % (v["title"], v["ready"], v["inputs"]))
        print("  页面文本: %r" % (v["text"] or ""))
    except Exception:
        print("  基本信息读取失败")

    # 2) 回读已发生的请求
    r2 = ev("JSON.stringify(performance.getEntriesByType('resource')"
            ".map(function(e){return {n:e.name.slice(0,150), t:e.initiatorType, "
            "d:Math.round(e.duration), s:e.responseStatus||0, size:e.transferSize}})"
            ".filter(function(e){return e.n.indexOf('127.0.0.1')>=0 "
            "|| e.n.indexOf('urs')>=0 || e.n.indexOf('login')>=0 || e.n.indexOf('passport')>=0}))")
    try:
        arr = json.loads(r2["result"]["result"]["value"])
        print("\n  === 已发生的相关请求 (%d 条) ===" % len(arr))
        for e in arr:
            print("   [%s] status=%s dur=%sms size=%s" % (e["t"], e["s"], e["d"], e["size"]))
            print("      %s" % e["n"])
    except Exception:
        print("  请求列表读取失败:", json.dumps(r2)[:300])

    # 3) 是否有 JS 错误记录在 window 上
    r3 = ev("JSON.stringify({hasErr: !!window.__sveErr, "
            "online: navigator.onLine, "
            "vis: document.visibilityState})")
    try:
        print("\n  其他:", r3["result"]["result"]["value"])
    except Exception:
        pass

    ws.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
