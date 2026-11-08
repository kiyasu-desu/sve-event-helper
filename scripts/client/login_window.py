"""自建 WebView 登录 —— 最小验证原型（桌面窗口）。

目的：验证"不依赖模拟器与官方 APP，用自己的 Chromium 窗口完成网易账号登录并拿到 sessionid"
是否走得通。只做一件事：打开登录页 → 你登录 → 显示/打印 sessionid。

机制（依据 2026-10-10 的 hook 实测，见 capture/client_recon.md 第九节）
    网易登录页接收一个"回调契约"：
        #/pages/index/index?url=<回调地址>&method=post&data=<设备信息JSON>
                          &useNative=true&project=yingzhishi
    用户完成登录后，登录页会把结果 POST 到 <回调地址>。
    我们把本地地址当作回调地址，就能直接收到登录结果。

关于窗口实现（重要）
    原计划用 pywebview，但它在 Windows 上唯一后端是 winforms，需要 pythonnet 加载
    System.Windows.Forms；而 clr_loader 硬编码只解析 Microsoft.NETCore.App
    （clr_loader/__init__.py:144），拿不到 WindowsDesktop 框架 —— 该路本机不通。
    因此本原型改用 **Edge 应用窗口**（--app），底层同為 Chromium/WebView2，
    零 .NET 依赖。窗口内是一个本地页面，用 iframe 承载登录页，从而与回调服务同源。
    若要真正的进程内嵌窗口，后续可换 QtWebEngine / 独立 Electron 外壳。

用法:
    <venv-client>\\Scripts\\python.exe scripts\\client\\login_window.py
    <venv-client>\\Scripts\\python.exe scripts\\client\\login_window.py --port 8765
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

LOGIN_PAGE = "https://login-betasystem.nie.netease.com/ma68/#/pages/index/index"
PROJECT = "yingzhishi"
DEFAULT_UDID = "5f3a9c1d7e2b4806"
SESSION_RE = re.compile(r"[0-9a-f]{64}")
HTML = os.path.join(PROJECT_ROOT, "scripts", "client", "login_window.html")

# 登录页是移动端单页应用，UA 必须伪装成移动端。
#
# 关键细节：`Build/AP3A` 这一段不能省。
# 实测（同参数、同启动方式，只改 UA）：
#   "Android 15; YOK-AN10 Build/AP3A" -> 正常渲染（document.title = 登录）
#   "Android 15; YOK-AN10"            -> 始终空白
# 该页面会解析 UA 判定设备，缺少 Build 段就不渲染。
# 这也解释了此前"同参数时好时坏"的假象——根因一直是 UA 字符串不一致。
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 15; YOK-AN10 Build/AP3A) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")


def build_login_url(callback_url: str, udid: str) -> str:
    """按官方 APP 的实测契约构造登录页 URL。"""
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
    return LOGIN_PAGE + "?" + urllib.parse.urlencode(q)


def find_edge() -> str | None:
    for p in (os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
              os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe")):
        if os.path.exists(p):
            return p
    return None


class Result:
    def __init__(self) -> None:
        self.hits: list[dict] = []
        self.sessionid: str | None = None
        self.done = threading.Event()


RESULT = Result()


class Handler(BaseHTTPRequestHandler):
    """本地回调服务。

    登录页在 login-betasystem.nie.netease.com 域下运行，它要把结果 POST 到这里，
    属于**跨域请求**，所以必须处理预检（OPTIONS）并回 CORS 头。
    （第一版用 iframe 承载登录页以保证同源，但登录页禁止被嵌入，窗口是空白的。）
    """

    CORS = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Max-Age": "600",
        # 关键：登录页在公网 HTTPS 域下，要 POST 到 127.0.0.1（私有网络）。
        # Chromium 的 Private Network Access 会对这类跨网段请求做预检，
        # 且要求响应带下面这个头；否则**实际 POST 被丢弃**，
        # 表现为登录页一直停在"正在跳转"（它在等一个永远不会到达的响应）。
        "Access-Control-Allow-Private-Network": "true",
    }

    def log_message(self, *a):
        pass

    def _send(self, body: bytes, code: int = 200, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in self.CORS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(json.dumps(obj).encode(), code)

    def _text(self, text, ctype="text/html; charset=utf-8"):
        self._send(text.encode("utf-8"), 200, ctype)

    def do_OPTIONS(self):
        # 跨域预检
        self._send(b"", 204)

    def do_GET(self):
        if self.path.startswith("/__status"):
            return self._json({"hits": len(RESULT.hits), "sessionid": RESULT.sessionid})
        if self.path.startswith("/login_callback"):
            return self._record("")
        if self.path.startswith(("/", "/index")):
            with open(HTML, encoding="utf-8") as fh:
                return self._text(fh.read())
        self._json({"status": "ok"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        return self._record(body)

    def _record(self, body: str):
        cookie = self.headers.get("Cookie", "") or ""
        set_cookie = self.headers.get("Set-Cookie", "") or ""
        blob = "\n".join([body, cookie, set_cookie])
        sid = SESSION_RE.search(blob)
        entry = {
            "ts": time.strftime("%H:%M:%S"),
            "path": self.path,
            "cookie": cookie[:400],
            "set_cookie": set_cookie[:400],
            "body": body[:1200],
            "sessionid": sid.group(0) if sid else None,
        }
        RESULT.hits.append(entry)
        print("\n===== 收到回调 (%s) =====" % entry["ts"], flush=True)
        print("  路径       : %s" % entry["path"])
        print("  Cookie     : %s" % (entry["cookie"] or "(空)"))
        print("  Set-Cookie : %s" % (entry["set_cookie"] or "(空)"))
        print("  Body       : %s" % (entry["body"] or "(空)"))
        print("  解析 sessionid: %s" % (entry["sessionid"] or "未找到"), flush=True)
        if entry["sessionid"]:
            RESULT.sessionid = entry["sessionid"]
            RESULT.done.set()
        self._json({"status": "success"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--udid", default=DEFAULT_UDID)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--debug-port", type=int, default=9222,
                    help="Chromium 远程调试端口，用于读取窗口内真实 DOM")
    ap.add_argument("--no-open", action="store_true", help="只起服务，不打开窗口")
    args = ap.parse_args()

    callback = "http://127.0.0.1:%d/login_callback" % args.port
    login_url = build_login_url(callback, args.udid)
    local = "http://127.0.0.1:%d/" % args.port

    print("=== 自建 WebView 登录 —— 最小验证原型 ===")
    print("  回调地址 : %s" % callback)
    print("  udid     : %s" % args.udid)
    print()
    print("  窗口将直接加载登录页（不再用 iframe：登录页禁止被嵌入，会显示空白）")
    print("  登录页 URL（与官方 APP 契约一致）：")
    print("  " + login_url[:200] + "...")
    print()

    # 端口预检：若已被占用必须立刻失败。
    # 之前这里静默失败——存在重复实例时，浏览器把回调发给了旧实例，
    # 而新实例一直等不到，导致排查方向完全错误。
    import socket
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", args.port))
    except OSError as e:
        print("  端口 %d 已被占用（%s）" % (args.port, e))
        print("  请先结束占用它的原型进程，或换一个 --port。")
        return 3
    finally:
        probe.close()

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("  本地回调服务已启动（含 CORS 支持）")

    edge = find_edge()
    proc = None
    if edge and not args.no_open:
        # 每次都用全新的 profile 目录。
        # 复用旧 profile 会让渲染结果时好时坏（实测：同一组参数，
        # 复用 profile 时空白、全新 profile 时正常渲染）。
        profile = os.path.join(PROJECT_ROOT, "capture",
                               "edge-profile-%s" % time.strftime("%H%M%S"))
        os.makedirs(profile, exist_ok=True)
        # 窗口内容 = 登录页本身；回调通过跨域 POST 打到本地服务。
        #
        # 实测结论（两组变量分别验证过，不要混在一起判断）：
        #   1) --user-agent 必须伪装移动端。桌面 UA 下登录页 JS 不渲染 → 空白窗口。
        #      这是"空白"的**唯一**原因。
        #   2) --app 应用模式是**可用**的（无地址栏的独立窗口）。
        #      曾经误判它不可用，实际当时同时还是桌面 UA，把两个变量混在一起了。
        #   3) 必须用 cmd start + **字符串**传参：Popen 传列表时，这个 443 字符、
        #      含 & 与 % 的 URL 会被解析错。
        # 实测：下面这两个参数与"页面空白"相关，**不要加**：
        #   --no-default-browser-check
        #   --remote-allow-origins=*
        # 对照组（不加这两个参数）能稳定渲染；加了就空白。
        # CDP 连接改用 WebSocket 客户端的 suppress_origin=True 来绕过 403，
        # 因此不需要 --remote-allow-origins。
        argline = ('--app="%s" --user-data-dir="%s" --window-size=460,880 '
                   '--user-agent="%s" --no-first-run '
                   '--remote-debugging-port=%d'
                   % (login_url, profile, MOBILE_UA, args.debug_port))
        proc = subprocess.Popen('start "" "%s" %s' % (edge, argline), shell=True)
        print("  已打开独立登录窗口（Chromium 内核 + 移动端 UA）")
        print("  调试端口 %d 已开启" % args.debug_port)

        # 自动兜底：实测这个移动端 SPA 有不小概率停在空白（标题为空），
        # 而**重新导航到登录 URL** 常能让它正常启动（比 Page.reload 更有效，
        # 因为 reload 只是重载当前文档，navigate 会重新走 SPA 的启动流程）。
        def ensure_rendered():
            import urllib.request as _u
            base = "http://127.0.0.1:%d" % args.debug_port
            for attempt in range(8):
                time.sleep(4)
                try:
                    lst = json.loads(_u.urlopen(base + "/json/list", timeout=6).read().decode())
                except Exception:
                    continue
                page = None
                for t in lst:
                    if t.get("type") == "page" and "login-betasystem" in (t.get("url") or ""):
                        page = t
                        break
                if page is None:
                    continue
                if (page.get("title") or "").strip():
                    print("  页面已渲染（标题=%r，第 %d 次检查）" % (page["title"], attempt + 1))
                    return
                ws_url = page.get("webSocketDebuggerUrl")
                if not ws_url:
                    continue
                try:
                    import websocket as _ws
                    c = _ws.create_connection(ws_url, timeout=20, suppress_origin=True)
                    c.send(json.dumps({"id": 1, "method": "Page.navigate",
                                       "params": {"url": login_url}}))
                    c.close()
                    print("  第 %d 次兜底：重新 navigate 到登录页" % (attempt + 1))
                except Exception:
                    pass
            print("  兜底结束，页面仍未渲染")

        threading.Thread(target=ensure_rendered, daemon=True).start()
    elif not edge:
        print("  未找到 Edge，请手动打开：%s" % login_url)

    def watchdog():
        if RESULT.done.wait(args.timeout):
            time.sleep(2.0)
            print("\n  已拿到 sessionid，关闭窗口")
        else:
            print("\n  超时，关闭窗口")
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    threading.Thread(target=watchdog, daemon=True).start()

    print("\n  请在弹出的窗口里完成登录...\n")
    started = time.time()
    try:
        while time.time() - started < args.timeout:
            if RESULT.done.is_set():
                break
            time.sleep(1)
        # 注意：这里【不能】用 proc.poll() 判断"用户是否关了窗口"。
        # 我们用 `start` 拉起浏览器，那个中间进程会立刻退出，
        # 若据此认为窗口已关闭就会马上关停本地服务，窗口随即变空白。
    except KeyboardInterrupt:
        pass

    httpd.shutdown()
    print("\n=== 结果 ===")
    for h in RESULT.hits:
        print("  %s %s sessionid=%s" % (h["ts"], h["path"], (h["sessionid"] or "-")[:20]))
    if RESULT.sessionid:
        print("\n  验证通过：自建 Chromium 窗口登录可以拿到 sessionid")
        print("     sessionid = %s" % RESULT.sessionid)
        return 0
    print("\n  未拿到 sessionid")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
