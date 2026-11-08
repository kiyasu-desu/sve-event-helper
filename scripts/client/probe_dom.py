"""通过 Chromium 远程调试端口读取窗口内的真实 DOM。

为什么需要：窗口标题并不能反映渲染结果（Taro 单页应用的 title 由路由决定，
标题是 URL 也不代表页面空白），截图又会枚举到工具提示窗口。
直接读 DOM 才是可靠判断：页面结构在不在、有没有可见文本、有没有报错。

用法:
    <venv-client>\\Scripts\\python.exe scripts\\client\\probe_dom.py [debug_port]
"""
import json
import sys
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9222


def http_json(path: str, timeout: float = 8.0):
    return json.loads(urllib.request.urlopen(
        "http://127.0.0.1:%d%s" % (PORT, path), timeout=timeout).read().decode())


def main() -> int:
    try:
        targets = http_json("/json/list")
    except Exception as e:
        print("  无法连接调试端口 %d: %s" % (PORT, e))
        print("  （说明窗口进程没带 --remote-debugging-port，或还没起来）")
        return 1

    pages = [t for t in targets if t.get("type") == "page"]
    print("  页面目标数:", len(pages))
    for t in pages:
        print("   - %s" % (t.get("url") or "")[:110])
    if not pages:
        print("  没有 page 类型目标")
        return 1

    target = pages[0]
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        print("  该目标没有 webSocketDebuggerUrl（需要 --remote-allow-origins=*）")
        return 1

    # 用 WebSocket 发 Runtime.evaluate
    try:
        import websocket  # websocket-client
    except ImportError:
        print("  缺少 websocket-client，尝试用 HTTP 方式取 title/url")
        print("  title:", target.get("title"))
        print("  url  :", target.get("url"))
        return 0

    ws = websocket.create_connection(ws_url, timeout=25)
    ws.settimeout(25)

    def rpc(mid, method, params=None):
        """发一条 CDP 命令并等待同 id 的回复（忽略事件通知）。"""
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        for _ in range(60):
            msg = json.loads(ws.recv())
            if msg.get("id") == mid:
                return msg
        return {}

    rpc(0, "Runtime.enable")
    expr = """
    (function(){
      var b = document.body;
      var txt = (b ? (b.innerText || '') : '').replace(/\\s+/g,' ').trim();
      return JSON.stringify({
        title: document.title,
        url: location.href,
        readyState: document.readyState,
        bodyChildren: b ? b.children.length : -1,
        htmlLen: document.documentElement.outerHTML.length,
        bodyTextLen: txt.length,
        bodyText: txt.slice(0, 400),
        hasInput: document.querySelectorAll('input').length,
        hasButton: document.querySelectorAll('button').length,
        scripts: document.querySelectorAll('script').length
      });
    })()
    """
    msg = rpc(1, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
    ws.close()

    try:
        val = json.loads(msg["result"]["result"]["value"])
    except Exception:
        print("  解析失败，原始返回:", json.dumps(msg)[:400])
        return 1

    print()
    print("  === 窗口内真实内容 ===")
    for k in ["title", "url", "readyState", "htmlLen", "bodyChildren",
              "bodyTextLen", "hasInput", "hasButton", "scripts"]:
        v = val.get(k)
        if k == "url":
            v = str(v)[:110]
        print("  %-14s %s" % (k, v))
    print()
    print("  body 可见文本:")
    print("   ", val.get("bodyText") or "(空)")
    print()
    if val.get("bodyTextLen", 0) > 0 or val.get("hasInput", 0) > 0:
        print("  结论: 页面有渲染内容")
        return 0
    print("  结论: 页面为空白（body 无文本、无输入框）")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
