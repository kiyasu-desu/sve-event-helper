"""诊断订阅页渲染：抓取控制台错误与页面内部状态。"""
import json
import os
import subprocess
import sys
import time
import urllib.request

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
CHROME = r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"
PORT = 9933


def conn(ws):
    import websocket
    return websocket.create_connection(ws, timeout=25, suppress_origin=True)


def call(ws, method, params=None, _id=[0]):
    _id[0] += 1
    ws.send(json.dumps({"id": _id[0], "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == _id[0]:
            return msg
        if msg.get("method") in ("Runtime.exceptionThrown", "Runtime.consoleAPICalled"):
            yield_msg = msg
            print("   [事件] %s: %s" % (msg["method"], json.dumps(msg["params"], ensure_ascii=False)[:300]))


def ev(ws, expr):
    ws.send(json.dumps({"id": 999, "method": "Runtime.evaluate",
                        "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
    while True:
        m = json.loads(ws.recv())
        if m.get("id") == 999:
            res = m.get("result", {})
            if res.get("exceptionDetails"):
                return "EXCEPTION: " + json.dumps(res["exceptionDetails"], ensure_ascii=False)[:300]
            return res.get("result", {}).get("value")


def main():
    exe = os.path.expandvars(CHROME)
    prof = os.path.join(PROJECT_ROOT, "capture", "cdp-dbg")
    p = subprocess.Popen([exe, "--headless=new", "--no-sandbox", "--disable-gpu",
                          "--user-data-dir=" + prof, "--remote-debugging-port=%d" % PORT,
                          "http://127.0.0.1:8801/subs"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = None
        for _ in range(30):
            time.sleep(1)
            try:
                d = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % PORT, timeout=3).read().decode())
                pages = [t for t in d if t.get("type") == "page" and "/subs" in (t.get("url") or "")]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]; break
            except Exception:
                pass
        if not ws_url:
            print("  无法连接"); return 1
        import websocket
        ws = websocket.create_connection(ws_url, timeout=25, suppress_origin=True)
        call(ws, "Runtime.enable")
        call(ws, "Log.enable")
        time.sleep(5)

        print("=== 页面内 API 数据 ===")
        print("  subs_grouped 键:", ev(ws, "fetch('/api/state').then(r=>r.json()).then(d=>Object.keys(d.subs_grouped).join(' | '))"))
        print("  订阅 key 列表  :", ev(ws, "fetch('/api/state').then(r=>r.json()).then(d=>Object.values(d.subs_grouped).flat().map(x=>x.key).join(' | '))"))
        print()
        print("=== 页面内部状态 ===")
        print("  LAST_LAYOUT    :", ev(ws, "typeof LAST_LAYOUT!=='undefined'?LAST_LAYOUT:'(未定义)'"))
        print("  CARDS 键       :", ev(ws, "Object.keys(CARDS).join(' | ')"))
        print("  EXPANDED       :", ev(ws, "Array.from(EXPANDED).join(' | ')"))
        print("  分组数         :", ev(ws, "document.querySelectorAll('.acct-group').length"))
        print("  卡片数         :", ev(ws, "document.querySelectorAll('.card').length"))
        print()
        print("=== 手动重跑一次 refresh 看是否报错 ===")
        print("  结果:", ev(ws, "refresh().then(()=>'OK').catch(e=>'ERR: '+e.message)"))
        time.sleep(1)
        print("  重跑后 分组数  :", ev(ws, "document.querySelectorAll('.acct-group').length"))
        print("  重跑后 卡片数  :", ev(ws, "document.querySelectorAll('.card').length"))
        print("  重跑后 latest  :", ev(ws, "(document.querySelector('.latest')||{}).textContent"))
        print("  重跑后 body显示:", ev(ws, "Array.from(document.querySelectorAll('.card .body')).map(e=>getComputedStyle(e).display).join(' | ')"))
        ws.close()
    finally:
        p.terminate()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
