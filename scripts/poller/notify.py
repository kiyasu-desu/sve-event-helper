"""推送通道：PushPlus（微信）+ SMTP 邮件兜底。

两个通道都返回 (ok, detail)，任一成功即视为送达，方便轮询器记录与重试。

凭证从 config/settings.json 读取，绝不打印、绝不写日志。
"""
from __future__ import annotations

import smtplib
import ssl
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import requests

PUSHPLUS_URL = "https://www.pushplus.plus/send"


def _mask(value: str) -> str:
    if not value:
        return "(未配置)"
    return value[:4] + "***" + value[-4:] if len(value) > 10 else "***"


# ---------------------------------------------------------------- PushPlus
def send_pushplus(channel_cfg: dict, title: str, content: str, timeout: int = 15):
    token = channel_cfg.get("token", "")
    if not token:
        return False, "pushplus: token 未配置"
    payload = {
        "token": token,
        "title": title,
        "content": content,
        "template": "html",
    }
    try:
        resp = requests.post(PUSHPLUS_URL, json=payload, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return False, "pushplus 请求异常: %s" % exc
    if resp.status_code != 200:
        return False, "pushplus HTTP %s" % resp.status_code
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return False, "pushplus 返回非 JSON: %s" % resp.text[:120]
    if body.get("code") == 200:
        return True, "pushplus ok (token %s)" % _mask(token)
    return False, "pushplus 失败: code=%s msg=%s" % (body.get("code"), body.get("msg"))


# ---------------------------------------------------------------- Email
def send_email(channel_cfg: dict, subject: str, content: str, timeout: int = 25):
    host = channel_cfg.get("smtp_host", "")
    port = int(channel_cfg.get("smtp_port", 465))
    user = channel_cfg.get("username", "")
    authcode = channel_cfg.get("authcode", "")
    from_addr = channel_cfg.get("from_addr", user)
    to_addrs = channel_cfg.get("to_addrs") or []
    if not (host and user and authcode and to_addrs):
        return False, "email: 配置不完整"

    msg = MIMEText(content, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr(("SVE名额监控", from_addr))
    msg["To"] = ", ".join(to_addrs)

    try:
        if channel_cfg.get("use_ssl", True):
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx) as server:
                server.login(user, authcode)
                server.sendmail(from_addr, to_addrs, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=timeout) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(user, authcode)
                server.sendmail(from_addr, to_addrs, msg.as_string())
    except Exception as exc:  # noqa: BLE001
        return False, "email 发送失败: %s" % exc
    return True, "email ok (%s -> %s)" % (_mask(user), ", ".join(to_addrs))


# ---------------------------------------------------------------- 组合
def _with_retry(name: str, func, *args, attempts: int = 3, base_delay: float = 3.0, quiet: bool = False):
    """通道级重试。

    实测教训：PushPlus 与 QQ 邮箱在短时间内被连发多条后会分别返回
    code=999 / 550 Too many attempts / Connection unexpectedly closed，
    表现为"看起来发送失败"。这里做指数退避重试，避免关键时刻丢通知。
    """
    last = (False, "%s: 未执行" % name)
    for i in range(attempts):
        ok, detail = func(*args)
        if ok:
            return True, detail
        last = (ok, detail)
        if i < attempts - 1:
            delay = base_delay * (2 ** i)
            if not quiet:
                print("  [notify] %-9s 第 %d 次失败，%.0fs 后重试: %s" % (name, i + 1, delay, detail))
            time.sleep(delay)
    return last


def notify_all(channels: dict, title: str, content: str, *, quiet: bool = False, attempts: int = 3) -> list[tuple[str, bool, str]]:
    """按配置发送全部通道，返回 [(通道名, 是否成功, 说明)]。

    通道之间刻意留出间隔：同时打两个服务更容易触发风控。
    """
    results: list[tuple[str, bool, str]] = []

    pp = channels.get("pushplus") or {}
    if pp.get("enabled"):
        ok, detail = _with_retry("pushplus", send_pushplus, pp, title, content, quiet=quiet, attempts=attempts)
        results.append(("pushplus", ok, detail))
        if (channels.get("email") or {}).get("enabled"):
            time.sleep(2)

    em = channels.get("email") or {}
    if em.get("enabled"):
        ok, detail = _with_retry("email", send_email, em, title, content, quiet=quiet, attempts=attempts)
        results.append(("email", ok, detail))

    if not results:
        results.append(("none", False, "没有任何通道被启用"))

    for name, ok, detail in results:
        if not quiet:
            print("  [notify] %-9s %s  %s" % (name, "OK  " if ok else "FAIL", detail))
    return results


def any_ok(results: list[tuple[str, bool, str]]) -> bool:
    return any(ok for _, ok, _ in results)


def render_event_html(ev: dict) -> str:
    """把一次空位事件渲染成通知正文。"""
    lines = [
        "<h3>%s</h3>" % ev.get("headline", "空位提醒"),
        "<p><b>赛事：</b>%s（id=%s）</p>" % (ev.get("name", "-"), ev.get("tournament_id", "-")),
        "<p><b>当前已报名：</b><b style='color:#c00'>%s</b> / 定员 %s</p>"
        % (ev.get("player_num", "-"), ev.get("max_player_number", "-")),
        "<p><b>发现时间：</b>%s</p>" % ev.get("found_at", "-"),
        "<p><b>详情：</b>%s</p>" % ev.get("remark", "-"),
        "<hr><p style='color:#888'>请尽快在《口袋影之诗》APP / 微信小程序中打开该赛事完成报名。</p>",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    # 自检：python scripts/poller/notify.py
    import json
    import os

    root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    import sys
    sys.path.insert(0, os.path.join(root, "scripts"))
    import sveenv
    cfg_path = sveenv.SETTINGS_FILE
    with open(cfg_path, encoding="utf-8-sig") as fh:
        cfg = json.load(fh)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    res = notify_all(cfg.get("channels", {}),
                     "【SVE名额监控】通道自检",
                     render_event_html({
                         "headline": "通道自检",
                         "name": cfg.get("tournament_name", "-"),
                         "tournament_id": cfg.get("tournament_id", "-"),
                         "found_at": stamp,
                         "remark": "这是一条测试消息，用于确认推送通道可用。",
                     }))
    print("任一通道成功:", any_ok(res))
