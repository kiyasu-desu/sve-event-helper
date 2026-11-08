"""SVE 客户端核心：网页登录并换取会话。

固化 2026-10-10 实测成功的路径（详见 capture/client_recon.md 第十二节）：

    1) 用浏览器/WebView 打开网易登录页（**必须伪装移动端 UA，且 UA 里要含
       "Build/..." 段**，否则这个移动端 SPA 不渲染，窗口空白）
    2) 用户在该窗口完成登录 —— 这是标准的网易账号网页登录，不做任何绕过
    3) 从浏览器读取网易账号 Cookie（CDP Storage.getCookies）
    4) 用这些 Cookie 调 POST /urs/login → 得到 SVE 的 session_id
    5) 用 sessionid 调 /urs/info 校验，拿到 uid/nickname

为什么不用"登录页回调"：
    官方 APP 的做法是让登录页把结果 POST 到它自己的 API。我们试过把回调指向本地
    服务，但该页在回调后会等待一个带跳转目标的响应，容易卡在"正在跳转"。
    改为"读 Cookie 换会话"后完全绕开回调，且不需要 CORS / PNA / 回调服务。

关键实现细节（都是踩过的坑）：
  * CDP 用 Storage.getCookies（整个浏览器），Network.getCookies 只给当前页的，
    会漏掉 .netease.com 上的 NTES_SESS。
  * CDP WebSocket 必须 suppress_origin=True，否则被 403 拒绝。
  * 启动浏览器时**不要**加 --no-default-browser-check / --remote-allow-origins，
    实测与"页面空白"相关。
  * 必须锁定登录页 target，多窗口时随便取一个可能读到无关页面。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

from sveapi import Account, SveClient, DEFAULT_UDID

LOGIN_PAGE = "https://login-betasystem.nie.netease.com/ma68/#/pages/index/index"
PROJECT = "yingzhishi"
# 移动端 UA：Build/AP3A 段不能省，否则页面不渲染
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 15; YOK-AN10 Build/AP3A) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")
DOMAIN_HINTS = ("netease.com", "163.com", "easebar.com")

EDGE_CANDIDATES = (
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
)
# 优先用 Chrome：实测 Edge 打开该登录页会**间歇性空白**（约 1/4 概率），
# 而 Chrome 连续多次均正常渲染。两者 Chromium 版本相同，差异来自 Edge
# 自身的首次运行/同步等流程。因此默认首选 Chrome，Edge 仅作回退。
CHROME_CANDIDATES = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
)


def _first_existing(paths) -> str | None:
    for p in paths:
        p = os.path.expandvars(p)
        if os.path.exists(p):
            return p
    return None


def find_browser() -> tuple[str | None, str]:
    """返回 (可执行文件路径, 名称)。优先 Chrome，其次 Edge。"""
    c = _first_existing(CHROME_CANDIDATES)
    if c:
        return c, "chrome"
    e = _first_existing(EDGE_CANDIDATES)
    if e:
        return e, "edge"
    return None, ""


def find_free_port(preferred: int | None = None) -> int:
    """找一个可用的本地端口给 DevTools 用。

    为什么必须动态选：固定端口很容易撞上别的程序。
    实测踩过——9210 被 QQ 占用，Chrome 无法绑定调试端口后直接启动失败，
    表现为"点了登录但没有任何窗口弹出"，且由于该端口上另有服务，
    连 /json/list 探测都会返回 200，极具误导性。
    """
    import socket
    if preferred:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", preferred))
            s.close()
            return preferred
        except OSError:
            s.close()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def find_edge() -> str | None:
    """兼容旧调用：返回任意可用浏览器。"""
    return find_browser()[0]


def build_login_url(udid: str = DEFAULT_UDID, callback: str | None = None) -> str:
    """构造登录页 URL。

    callback 为 None 时使用**官方 API 地址**（与官方 APP 一致）——
    我们不再依赖回调，因此这是最自然的取值。
    """
    import urllib.parse
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
        "url": callback or "https://api-sve.nie.netease.com/sve/api/v1/urs/login",
        "proxy": "",
        "method": "post",
        "data": json.dumps(data, separators=(",", ":")),
        "useNative": "true",
        "project": PROJECT,
    }
    return LOGIN_PAGE + "?" + urllib.parse.urlencode(q)


# ------------------------------------------------------------------ CDP
def _cdp_targets(port: int, timeout: float = 5.0):
    return json.loads(urllib.request.urlopen(
        "http://127.0.0.1:%d/json/list" % port, timeout=timeout).read().decode())


def _pick_login_target(targets):
    """优先取登录页 target；否则退回第一个 page。"""
    pages = [t for t in targets if t.get("type") == "page"]
    for t in pages:
        if "login-betasystem" in (t.get("url") or ""):
            return t
    return pages[0] if pages else None


def read_netease_cookies(port: int, timeout: float = 20.0) -> tuple[dict, str]:
    """读取浏览器中与网易相关的 Cookie。返回 ({name: value}, 页面标题)。"""
    import websocket  # websocket-client
    target = _pick_login_target(_cdp_targets(port))
    if target is None:
        return {}, ""
    ws = websocket.create_connection(target["webSocketDebuggerUrl"],
                                     timeout=timeout, suppress_origin=True)
    ws.settimeout(timeout)
    try:
        ws.send(json.dumps({"id": 1, "method": "Storage.getCookies", "params": {}}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == 1:
                cookies = m.get("result", {}).get("cookies", [])
                break
    finally:
        ws.close()
    rel = {c["name"]: c["value"] for c in cookies
           if any(h in (c.get("domain") or "") for h in DOMAIN_HINTS)}
    return rel, (target.get("title") or "")


class LoginWindowClosed(Exception):
    """登录窗口被用户关闭，本次登录作废。"""


# 登录成功后会出现的网易会话 Cookie 名。
# 不能只认 NTES_SESS：实测「手机号+验证码」这条路登录成功后，浏览器里
# 出现的是 **NTES_YD_SESS**（外加 l_yd_sign、THE_LAST_LOGIN_MOBILE），
# 并不存在 NTES_SESS。此前只检查 NTES_SESS，导致登录明明已经成功却一直等，
# 最后超时失败——用户那边看到的现象正是「点击登录后卡在跳转」。
# 真正能换出 SVE 会话的 Cookie —— /urs/login 依赖它们的**值**。
# 只有这两个之一出现，才说明登录真的完成了。
EXCHANGEABLE_COOKIE_NAMES = ("NTES_SESS", "NTES_YD_SESS")

# 宽松判定：用于「浏览器里是否已进入登录后状态」这类展示性判断，
# 额外包含早期出现的辅助项。
#
# ⚠️ l_yd_sign 是**辅助 Cookie，出现得比真正的会话 Cookie 早**。
#    若拿它判定「登录完成」，会过早提交：服务器校验能通过（名字在名单里），
#    但 /urs/login 换不出 session_id —— 现象就是「响应中没有 session_id」。
#    实测踩过：同一套代码，01:52 成功、18:50 连续 3 次失败，差别只在抓取时机。
#    **换会话前必须校验 EXCHANGEABLE_COOKIE_NAMES。**
SESSION_COOKIE_NAMES = EXCHANGEABLE_COOKIE_NAMES + ("l_yd_sign",)


def has_session_cookie(cookies: dict) -> bool:
    """浏览器里是否已经出现可用于换会话的 Cookie。"""
    return any((cookies or {}).get(n) for n in SESSION_COOKIE_NAMES)


def session_cookie_sig(cookies: dict) -> str:
    """会话相关 Cookie 的指纹，用于判断"是否有变化"。"""
    return "|".join("%s=%s" % (n, (cookies or {}).get(n) or "")
                    for n in SESSION_COOKIE_NAMES)


def port_alive(port: int, timeout: float = 0.6) -> bool:
    """快速判断调试端口是否还在监听（用于检测登录窗口是否被关闭）。

    比直接发 HTTP 请求便宜得多：窗口一关，端口立刻就没了。
    """
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def page_title(port: int) -> str:
    try:
        t = _pick_login_target(_cdp_targets(port, timeout=3))
        return (t or {}).get("title") or ""
    except Exception:
        return ""


# ------------------------------------------------------------------ 登录窗口
class LoginWindow:
    """启动登录窗口，并在用户完成登录后自动换取会话。

    典型用法：
        with LoginWindow(port=9210) as w:
            acct = w.wait_for_login(timeout=600)
    """

    def __init__(self, port: int | None = None, profile_dir: str | None = None,
                 udid: str = DEFAULT_UDID, headless_fallback: bool = False):
        # 端口必须是真正可用的：固定端口撞车会导致浏览器启动失败（见 find_free_port）
        self.port = find_free_port(port)
        self.udid = udid
        self.profile_dir = profile_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "..",
            "capture", "edge-login-%s" % time.strftime("%H%M%S"))
        self.profile_dir = os.path.abspath(self.profile_dir)
        self.proc = None
        self.login_url = build_login_url(udid)

    def open(self) -> None:
        exe, name = find_browser()
        if not exe:
            raise RuntimeError("未找到 Chrome 或 Edge 浏览器")
        self.browser = name
        os.makedirs(self.profile_dir, exist_ok=True)
        # --no-sandbox 必需：从 Python 子进程启动 Chromium 内核时，其沙箱无法初始化，
        # 进程会立刻以 0x80000003 (STATUS_BREAKPOINT) 退出——表现为
        # "点了登录但没有任何窗口弹出"。实测对照：
        #     无 --no-sandbox -> 退出码 2147483651，无页面
        #     加 --no-sandbox -> 进程存活，页面正常渲染
        # （Electron 在本机也是同一个原因、同一个解法。）
        argline = ('--app="%s" --user-data-dir="%s" --window-size=460,880 '
                   '--user-agent="%s" --no-first-run --no-sandbox '
                   '--remote-debugging-port=%d'
                   % (self.login_url, self.profile_dir, MOBILE_UA, self.port))
        # 用 cmd start + 字符串传参：Popen 传列表时这个含 & 与 % 的长 URL 会解析错
        self.proc = subprocess.Popen('start "" "%s" %s' % (exe, argline), shell=True)

    def close(self) -> None:
        # 只关我们这个 profile 的窗口；**绝不** Stop-Process msedge（会误关用户其它窗口）
        try:
            import websocket
            target = _pick_login_target(_cdp_targets(self.port, timeout=3))
            if target:
                ws = websocket.create_connection(target["webSocketDebuggerUrl"],
                                                 timeout=5, suppress_origin=True)
                ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
                ws.close()
        except Exception:
            pass

    def wait_for_login(self, timeout: int = 600, poll: float = 3.0,
                       on_status=None, closed_after_misses: int = 3) -> Account:
        """等待用户完成登录，返回换好会话的 Account。

        判定登录完成的依据：浏览器里出现 NTES_SESS（网易账号会话 cookie）。

        窗口被关闭的处理（实测踩过的坑）：
            用户手动关掉登录窗口后，调试端口就连不上了。若不检测，
            本函数会一直空转到超时——表现为界面提示卡在"页面标题: 登录"，
            且 login.running 一直为真，导致**再也点不开登录窗口**。
            因此连续失败若干次即判定窗口已关闭并立刻退出。
        """
        deadline = time.time() + timeout
        last_title = ""
        misses = 0
        while time.time() < deadline:
            time.sleep(poll)
            # 先用便宜的端口检查判断窗口是否还在；关了就直接结束，
            # 不必等 HTTP 请求超时，也避免把 running 卡住。
            if not port_alive(self.port):
                misses += 1
                if misses >= closed_after_misses:
                    raise LoginWindowClosed("登录窗口已关闭，本次登录已取消")
                continue
            try:
                cookies, title = read_netease_cookies(self.port)
                misses = 0
            except Exception:
                continue
            if title and title != last_title:
                last_title = title
                if on_status:
                    on_status("页面标题: %s" % title)
            if not has_session_cookie(cookies):
                continue
            # 只有会话 Cookie 变化、或距上次尝试已够久，才去换会话——
            # 登录页在"跳转"阶段会反复刷新，避免每次都打一次接口。
            sig = session_cookie_sig(cookies)
            if sig == self._last_sig and (time.time() - self._last_try) < 15:
                continue
            self._last_sig = sig
            self._last_try = time.time()
            if on_status:
                on_status("检测到网易会话 Cookie，正在换取 SVE 会话…")
            try:
                sid = SveClient.login_with_cookie(cookies, udid=self.udid)
            except Exception as e:
                # 换取失败（例如页面还在跳转、会话尚未生效）继续等，不要直接失败
                if on_status:
                    on_status("会话尚未生效，继续等待…（%s）" % str(e)[:60])
                continue
            acc = Account(name="", sessionid=sid, deviceid=self.udid,
                          user_agent="okhttp/4.9.2")
            cli = SveClient(acc)
            info = cli.info()
            acc.uid = info.get("id")
            acc.nickname = info.get("nickname") or ""
            acc.mobile = info.get("mobile") or ""
            if on_status:
                on_status("登录成功: %s (uid=%s)" % (acc.nickname, acc.uid))
            return acc
        raise TimeoutError("等待登录超时（%d 秒）" % timeout)

    _last_sig = ""
    _last_try = 0.0

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def login_interactive(port: int = 9210, timeout: int = 600,
                      on_status=None) -> Account:
    """一步完成：开窗口 → 等登录 → 换会话 → 关窗口。"""
    def say(m):
        if on_status:
            on_status(m)
        else:
            print("  " + m)

    with LoginWindow(port=port) as w:
        say("登录窗口已打开，请完成登录…")
        return w.wait_for_login(timeout=timeout, on_status=say)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print("  登录页 URL:")
    print("   ", build_login_url()[:160])
    print()
    print("  移动端 UA:", MOBILE_UA)
    print("  Edge     :", find_edge())
