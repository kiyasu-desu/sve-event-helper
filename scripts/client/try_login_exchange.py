"""验证决定性的一环：用浏览器里的网易账号 Cookie 调 /urs/login 换 sessionid。

背景（见 capture/client_recon.md）：
  - 官方 APP 的登录页回调 url 指向 https://api-sve.nie.netease.com/sve/api/v1/urs/login
  - 该接口实测**不校验 sign、不校验 sessionid**，唯一要求是 cookie（网易账号的 NTES_SESS）
  - 所以：只要拿到登录后浏览器持有的 cookie，我们自己调一次该接口，
    就应该能换到 SVE 的 sessionid

本脚本：
  1. 通过 CDP 读取浏览器中与网易相关的 cookie；
  2. 用这些 cookie 调 /urs/login；
  3. 打印响应，并尝试解析 sessionid（响应体或 Set-Cookie）。

用法:
    <venv-client>\\Scripts\\python.exe scripts\\client\\try_login_exchange.py --port 9777
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request

import websocket

API = "https://api-sve.nie.netease.com/sve/api/v1/urs/login"
UDID = "5f3a9c1d7e2b4806"
SESSION_RE = re.compile(r"[0-9a-f]{64}")
DOMAIN_HINTS = ("netease.com", "163.com", "easebar.com", "gsf.netease.com")


def cdp_get_cookies(port: int):
    d = json.loads(urllib.request.urlopen(
        "http://127.0.0.1:%d/json/list" % port, timeout=8).read().decode())
    pages = [t for t in d if t.get("type") == "page"]
    if not pages:
        return None, []
    # 必须锁定登录页目标：多窗口时 pages[0] 可能是别的页面（例如带扩展的空白页），
    # 那样读到的 cookie 与网易无关，会得出"未登录"的错误结论。
    target = None
    for t in pages:
        if "login-betasystem" in (t.get("url") or ""):
            target = t
            break
    if target is None:
        target = pages[0]
    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=25,
                                     suppress_origin=True)
    ws.settimeout(25)

    def rpc(mid, method, params=None):
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            m = json.loads(ws.recv())
            if m.get("id") == mid:
                return m

    # Storage.getCookies 返回整个浏览器的 cookie；Network.getCookies 只返回当前页的。
    m = rpc(1, "Storage.getCookies")
    ws.close()
    return target.get("title"), m.get("result", {}).get("cookies", [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9777)
    args = ap.parse_args()

    title, cookies = cdp_get_cookies(args.port)
    print("  页面: %r" % title)
    print("  浏览器 cookie 总数: %d" % len(cookies))

    rel = [c for c in cookies if any(h in (c.get("domain") or "") for h in DOMAIN_HINTS)]
    print("  与网易相关的 cookie: %d" % len(rel))
    for c in rel[:25]:
        print("    %-28s %-22s len=%s" % ((c.get("domain") or "")[:28],
                                          (c.get("name") or "")[:22],
                                          len(c.get("value") or "")))
    if not rel:
        print("\n  未找到网易相关 cookie —— 说明登录状态不在这个浏览器实例里")
        return 1

    cookie_header = "; ".join("%s=%s" % (c["name"], c["value"]) for c in rel)
    print("\n  组装 Cookie 头长度: %d" % len(cookie_header))

    # 调 /urs/login
    body = json.dumps({"udid": UDID}, separators=(",", ":")).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "okhttp/4.9.2",
        "Cookie": cookie_header,
    })
    print("\n  === 调 /urs/login ===")
    try:
        r = urllib.request.urlopen(req, timeout=25)
        text = r.read().decode("utf-8", "replace")
        print("  HTTP %s" % r.status)
        print("  响应体: %s" % text[:600])
        # 从响应头找 set-cookie
        sc = r.headers.get_all("Set-Cookie") or []
        for s in sc:
            print("  Set-Cookie: %s" % s[:200])
        blob = text + " " + " ".join(sc)
        m = SESSION_RE.search(blob)
        if m:
            print("\n  ✅ 拿到 sessionid = %s" % m.group(0))
            return 0
        print("\n  未在响应中直接看到 sessionid（可能需要换其它字段/再调一次接口读取）")
        return 2
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print("  HTTP %s" % e.code)
        print("  响应体: %s" % body[:400])
        return 1
    except Exception as e:
        print("  请求异常: %s" % e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
