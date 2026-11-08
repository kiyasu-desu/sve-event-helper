"""探测登录窗口里的 Cookie 与页面状态，判断"卡在跳转"时登录是否实际已完成。"""
import json
import sys

import websocket

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7727


def main() -> int:
    try:
        d = json.loads(__import__("urllib.request", fromlist=["request"])
                       .urlopen("http://127.0.0.1:%d/json/list" % PORT, timeout=5).read().decode())
    except Exception as e:
        print("  无法连接调试端口 %d: %s" % (PORT, e))
        return 1

    print("=== 浏览器内的页面 ===")
    for t in d:
        if t.get("type") == "page":
            print("  url  : %s" % (t.get("url") or "")[:120])
            print("  title: %r" % t.get("title"))

    pages = [t for t in d if t.get("type") == "page"]
    if not pages:
        print("  没有页面")
        return 1
    ws = websocket.create_connection(pages[0]["webSocketDebuggerUrl"], timeout=20,
                                     suppress_origin=True)

    def call(method, params=None, _id=[0]):
        _id[0] += 1
        ws.send(json.dumps({"id": _id[0], "method": method, "params": params or {}}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == _id[0]:
                return m

    print()
    print("=== 全部 Cookie（按域分组）===")
    r = call("Storage.getCookies")
    cookies = r.get("result", {}).get("cookies", [])
    by_domain: dict[str, list] = {}
    for c in cookies:
        by_domain.setdefault(c.get("domain", "?"), []).append(c.get("name"))
    for dom, names in sorted(by_domain.items()):
        print("  %-28s %s" % (dom, ", ".join(sorted(set(names)))))

    print()
    key = [c for c in cookies if c.get("name") == "NTES_SESS"]
    if key:
        c = key[0]
        v = c.get("value") or ""
        print("  ✅ 找到 NTES_SESS（域=%s，长度=%d，值前12位=%s…）"
              % (c.get("domain"), len(v), v[:12]))
        print("     说明：登录其实已经成功，页面只是卡在跳转；可以不必等跳转。")
    else:
        print("  ❌ 没有 NTES_SESS —— 登录尚未真正完成")

    print()
    print("=== 页面可见文本（判断卡在哪一步）===")
    r = call("Runtime.evaluate", {"expression":
             "document.body ? document.body.innerText.slice(0,300) : '(无 body)'",
             "returnByValue": True})
    txt = r.get("result", {}).get("result", {}).get("value")
    print("  %r" % txt)
    ws.close()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
