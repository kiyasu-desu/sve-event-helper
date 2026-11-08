"""用真实浏览器验证订阅页的折叠/展开与日志行为。

为什么值得这么做：这类前端问题"看代码像是对的"，但只有真实渲染才能确认
DOM 结构、点击展开、日志滚动位置是否真的符合预期。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
CHROME = r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"
PORT = 9931


def cdp(ws_url: str, method: str, params: dict | None = None, _id=[0]):
    import websocket
    _id[0] += 1
    ws = websocket.create_connection(ws_url, timeout=20, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": _id[0], "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == _id[0]:
                return msg
    finally:
        ws.close()


def eval_js(ws: str, expr: str):
    r = cdp(ws, "Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                     "awaitPromise": True})
    return r.get("result", {}).get("result", {}).get("value")


def main() -> int:
    exe = os.path.expandvars(CHROME)
    if not os.path.exists(exe):
        print("  未找到 Chrome")
        return 1
    prof = os.path.join(PROJECT_ROOT, "capture", "cdp-probe")
    args = [exe, "--headless=new", "--no-sandbox", "--disable-gpu",
            "--user-data-dir=" + prof, "--remote-debugging-port=%d" % PORT,
            "http://127.0.0.1:8801/subs"]
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws = None
        for _ in range(30):
            time.sleep(1)
            try:
                d = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % PORT, timeout=3).read().decode())
                pages = [t for t in d if t.get("type") == "page" and "/subs" in (t.get("url") or "")]
                if pages:
                    ws = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
        if not ws:
            print("  未能连上页面调试端口")
            return 1

        time.sleep(5)   # 等首屏 + 第一次 refresh 完成

        print("=== 页面结构 ===")
        print("  账号分组数       :", eval_js(ws, "document.querySelectorAll('.acct-group').length"))
        print("  订阅卡片数       :", eval_js(ws, "document.querySelectorAll('.card').length"))
        print("  最近日志行文本   :",
              eval_js(ws, "(document.querySelector('.latest')||{}).textContent"))

        print()
        print("=== 默认是否折叠（.body 应全部 display:none）===")
        print("  可见的 body 数   :",
              eval_js(ws, "Array.from(document.querySelectorAll('.card .body'))"
                          ".filter(e=>getComputedStyle(e).display!=='none').length"))
        print("  （应为 0）")

        print()
        print("=== 日志区初始滚动位置（应贴底=最新）===")
        print("  scrollTop/scrollHeight:",
              eval_js(ws, "Array.from(document.querySelectorAll('pre.log')).map("
                          "e=>e.scrollTop+'/'+e.scrollHeight).join(' , ')"))

        print()
        print("=== 模拟点击卡片头部展开 ===")
        eval_js(ws, "document.querySelector('.card .head').click(); 1")
        time.sleep(1.2)
        print("  展开后可见 body 数:",
              eval_js(ws, "Array.from(document.querySelectorAll('.card .body'))"
                          ".filter(e=>getComputedStyle(e).display!=='none').length"))
        print("  展开后日志行数    :",
              eval_js(ws, "(document.querySelector('pre.log').textContent.match(/\\n/g)||[]).length+1"))

        print()
        print("=== 日志滚动：滚到中间后等一次刷新，位置是否保持 ===")
        eval_js(ws, "var e=document.querySelector('pre.log'); e.scrollTop=30; e.dispatchEvent(new Event('scroll')); 1")
        before = eval_js(ws, "document.querySelector('pre.log').scrollTop")
        time.sleep(4)   # 跨过 2 秒轮询
        after = eval_js(ws, "document.querySelector('pre.log').scrollTop")
        print("  刷新前 scrollTop=%s  刷新后 scrollTop=%s  %s"
              % (before, after, "OK 未被重置" if after == before else "FAIL 被重置"))

        print()
        print("=== 页面是否包含「当前登录」标签 ===")
        print("  ", eval_js(ws, "document.body.innerHTML.includes('g-mine\">当前登录')"))
    finally:
        p.terminate()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
