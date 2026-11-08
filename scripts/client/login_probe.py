"""自建 WebView 登录 —— 最小验证原型。

目的：验证"不依赖模拟器与官方 APP，用自己的 WebView 完成网易账号登录并拿到 sessionid"
这条路是否走得通。只做一件事：打开登录页 → 你登录 → 打印/显示拿到的 sessionid。

机制（依据 2026-10-10 的 hook 实测，见 capture/client_recon.md 第九节）：
    网易登录页接收一个"回调契约"：
        #/pages/index/index?url=<回调地址>&method=post&data=<设备信息JSON>
                          &useNative=true&project=yingzhishi
    用户在该页面完成登录后，登录页会把结果 POST 到 <回调地址>。
    我们把自己的本地地址当作 <回调地址>，就能直接收到登录结果。

用法:
    <venv>\\python.exe scripts\\client\\login_probe.py            # 默认用 sve2 的 udid
    <venv>\\python.exe scripts\\client\\login_probe.py --port 8765
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# pywebview 在 Windows 上通过 pythonnet 调 .NET；而 pythonnet 默认选 .NET Framework
# 加载器，在本机（Python 3.14 + pythonnet 3.2.1）会失败：
#   Failed to resolve Python.Runtime.Loader.Initialize
# 本机已装 .NET 6/10 运行时，显式切到 coreclr 即可正常加载。
# 必须【在任何 webview/clr 导入之前】设置。
os.environ.setdefault("PYTHONNET_RUNTIME", "coreclr")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
import sveenv  # noqa: E402

LOGIN_PAGE = "https://login-betasystem.nie.netease.com/ma68/#/pages/index/index"
API_BASE = "https://api-sve.nie.netease.com/sve/api/v1"
PROJECT = "yingzhishi"

# 设备信息模板：字段名与官方 APP 完全一致（实测自 hook 抓到的 data 参数）。
# udid 是 16 位 hex，由客户端生成，与账号无关，因此可以自行指定。
DEFAULT_UDID = "5f3a9c1d7e2b4806"


def build_login_url(callback_url: str, udid: str) -> str:
    data = {
        "app_ver": "1.2.0",
        "device_height": 800,
        "device_width": 450,
        "model_name": "YOK-AN10",
        "network_state": "wifi",
        "os_version": "android 35",
        "platform": "android",
        "udid": udid,
    }
    q = {
        "url": callback_url,
        "proxy": "",
        "method": "post",
        "data": json.dumps(data, separators=(",", ":")),
        "useNative": "true",
        "project": PROJECT,
    }
    import urllib.parse
    return LOGIN_PAGE + "?" + urllib.parse.urlencode(q)


# ------------------------------------------------------------------ 回调接收
class Result:
    def __init__(self) -> None:
        self.hits: list[dict] = []
        self.done = threading.Event()


RESULT = Result()
SESSION_RE = re.compile(r"[0-9a-f]{64}")


def extract_session(text: str) -> str | None:
    """从回调内容里找 64 位 hex（SVE 的 sessionid 就是这个形态）。"""
    for m in SESSION_RE.finditer(text or ""):
        return m.group(0)
    return None


class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默，避免刷屏
        pass

    def _record(self, body: str, headers) -> None:
        cookie_hdr = headers.get("Cookie", "") or ""
        set_cookie = headers.get("Set-Cookie", "") or ""
        blob = "\n".join([body, cookie_hdr, set_cookie])
        sid = extract_session(blob)

        entry = {
            "ts": time.strftime("%H:%M:%S"),
            "path": self.path,
            "cookie": cookie_hdr[:300],
            "set_cookie": set_cookie[:300],
            "body": body[:800],
            "sessionid": sid,
        }
        RESULT.hits.append(entry)
        print("\n===== 收到回调 =====", flush=True)
        print("  路径      : %s" % entry["path"])
        print("  Cookie    : %s" % (entry["cookie"] or "(空)"))
        print("  Set-Cookie: %s" % (entry["set_cookie"] or "(空)"))
        print("  Body      : %s" % (entry["body"] or "(空)"))
        print("  解析出的 sessionid: %s" % (sid or "未找到"))
        print("=" * 40, flush=True)
        if sid:
            RESULT.done.set()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        self._record(body, self.headers)
        # 回一个成功响应，避免登录页认为投递失败
        payload = b'{"status":"success"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._record("", self.headers)
        payload = b'{"status":"success"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--udid", default=DEFAULT_UDID)
    ap.add_argument("--timeout", type=int, default=600, help="窗口最长存活秒数")
    args = ap.parse_args()

    callback = "http://127.0.0.1:%d/login_callback" % args.port
    url = build_login_url(callback, args.udid)

    print("=== 自建 WebView 登录 —— 最小验证原型 ===")
    print("  回调地址 : %s   （登录页会把结果 POST 到这里）" % callback)
    print("  udid     : %s" % args.udid)
    print("  登录页   : %s" % LOGIN_PAGE)
    print()
    print("  即将打开的完整 URL：")
    print("  " + url[:300] + ("..." if len(url) > 300 else ""))
    print()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), CallbackHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print("  本地回调服务已启动（127.0.0.1:%d）" % args.port)

    try:
        import webview
    except ImportError:
        print("  ❌ 未安装 pywebview")
        return 2

    window = webview.create_window("SVE 登录验证原型", url, width=460, height=860)

    def watchdog():
        # 拿到 sessionid 或超时后关闭窗口
        if RESULT.done.wait(args.timeout):
            time.sleep(1.5)
            print("\n  已拿到 sessionid，关闭窗口")
        else:
            print("\n  超时，关闭窗口")
        try:
            window.destroy()
        except Exception:
            pass

    threading.Thread(target=watchdog, daemon=True).start()

    print("  打开窗口，请在其中完成登录...\n")
    webview.start()

    server.shutdown()
    print("\n=== 结果 ===")
    if RESULT.hits:
        for h in RESULT.hits:
            print("  %s  %s  sessionid=%s" % (h["ts"], h["path"], h["sessionid"] or "-"))
    else:
        print("  没有收到任何回调")
    print()
    if any(h.get("sessionid") for h in RESULT.hits):
        print("  ✅ 验证通过：自建 WebView 登录可以拿到 sessionid")
        return 0
    print("  ❌ 未拿到 sessionid —— 需要换回调方案（例如改由客户端再调一次 /urs/login）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
