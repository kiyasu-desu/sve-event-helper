"""mitmproxy addon: 抓取《口袋影之诗》(com.shadowversecard) 的赛事接口。

用法(推荐用封装脚本, 它会自动带上工作区内的 confdir):
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\\capture\\mitm_ctl.ps1 -Start
或手动:
    <工作区>\\vendor\\venv314\\Scripts\\mitmdump.exe --set confdir="<工作区>\\capture\\confdir" ^
        -p 8080 -s scripts\\capture\\sve_capture.py -w capture\\capture.flow

产物(均在工作区内, 不写宿主其它位置):
    capture/capture.flow        完整原始流量, 你自己留存
    capture/sve_requests.jsonl  脱敏后的请求, 用来分析

环境变量:
    SVE_CAPTURE_DIR   输出目录, 默认 "capture"
    SVE_CAPTURE_ALL   置 1 时连非游戏域名的请求也记录(排查用), 默认只记游戏后端

脱敏规则: 请求头里 token / cookie / 设备指纹类字段一律替换为 <REDACTED>,
JSON 体里同名敏感 key 也替换掉. 其余内容(URL, 参数名, 参数结构, 响应 JSON)保留,
因为判定名额字段只需要这些.
"""
import json
import os
import re
import time

from mitmproxy import ctx, http

OUT_DIR = os.environ.get("SVE_CAPTURE_DIR", "capture")
OUT_FILE = os.path.join(OUT_DIR, "sve_requests.jsonl")

# 只关心游戏后端; 其余 CDN / 静态资源 / 第三方 SDK 不落盘, 免得刷屏
HOST_RE = re.compile(
    r"(api-sve\.nie\.netease\.com|shadowversecardserver|micsrv|shadowverse)",
    re.I,
)
# 赛事相关路径关键词, 用于给落盘记录打标签
PATH_HINTS = (
    "tournament", "server_list", "urs/", "login", "match", "event",
    "signup", "player", "activity", "news",
)

REDACT_HEADERS = {
    "authorization", "cookie", "set-cookie", "token", "access-token",
    "x-token", "x-auth-token", "x-access-token", "proxy-authorization",
    "deviceid", "device-id", "x-device-id", "x-udid", "udid", "sign",
    "x-sign", "signature", "x-signature", "appkey", "app-key", "x-app-key",
    "nonce", "x-nonce", "timestamp", "x-timestamp", "user-agent",
}
REDACT_KEYS = {
    "token", "access_token", "accessToken", "refresh_token", "refreshToken",
    "cookie", "session", "sessionid", "session_id", "sign", "signature",
    "nonce", "timestamp", "ts", "deviceid", "device_id", "deviceId",
    "udid", "appkey", "app_key", "appKey", "secret", "password", "passwd",
    "mobile", "phone", "id_card", "idcard", "real_name", "realname",
    "email", "account", "username", "user_name",
}

MAX_BODY = 60000
_seq = 0


def _redact_headers(headers) -> dict:
    out = {}
    for k, v in headers.items():
        out[k] = "<REDACTED>" if k.lower() in REDACT_HEADERS else v
    return out


def _redact_json(obj, depth=0):
    if depth > 8:
        return obj
    if isinstance(obj, dict):
        return {
            k: ("<REDACTED>" if k in REDACT_KEYS else _redact_json(v, depth + 1))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_json(v, depth + 1) for v in obj[:50]]
    return obj


def _maybe_json(text: str):
    if not text:
        return None
    stripped = text.lstrip()
    if not stripped[:1] in "{[":
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _shorten(text: str) -> str:
    if text is None:
        return ""
    if len(text) <= MAX_BODY:
        return text
    return text[:MAX_BODY] + "\n...<TRUNCATED %d bytes>" % (len(text) - MAX_BODY)


def _body_text(message) -> str:
    try:
        return message.get_text(strict=False) or ""
    except Exception:
        return "<binary %d bytes>" % len(message.raw_content or b"")


def load(loader):
    os.makedirs(OUT_DIR, exist_ok=True)
    ctx.log.info("[sve_capture] addon loaded; out=%s" % os.path.abspath(OUT_FILE))


def response(flow: http.HTTPFlow) -> None:
    global _seq
    host = flow.request.pretty_host
    path = flow.request.path
    is_target = bool(HOST_RE.search(host) or HOST_RE.search(flow.request.pretty_url))
    if not is_target and os.environ.get("SVE_CAPTURE_ALL", "0") != "1":
        return

    _seq += 1
    req_text = _body_text(flow.request)
    resp_text = _body_text(flow.response) if flow.response else ""

    rec = {
        "seq": _seq,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "client": flow.client_conn.peername[0] if flow.client_conn.peername else None,
        "client_port": flow.client_conn.peername[1] if flow.client_conn.peername else None,
        "method": flow.request.method,
        "scheme": flow.request.scheme,
        "host": host,
        "path": path.split("?")[0],
        "query": dict(flow.request.query),
        "url": flow.request.pretty_url.split("?")[0],
        "status": flow.response.status_code if flow.response else None,
        "content_type": (flow.response.headers.get("content-type", "") if flow.response else ""),
        "tags": [h for h in PATH_HINTS if h in (path + host).lower()],
        "req_headers": _redact_headers(flow.request.headers),
        "req_body_json": _redact_json(_maybe_json(req_text)),
        "req_body_raw": "<REDACTED-BODY>" if _maybe_json(req_text) is not None else _shorten(req_text),
        "resp_headers": dict(flow.response.headers) if flow.response else {},
        "resp_body_json": _maybe_json(resp_text),
        "resp_body_raw": "<see resp_body_json>" if _maybe_json(resp_text) is not None else _shorten(resp_text),
        "req_size": len(flow.request.raw_content or b""),
        "resp_size": len(flow.response.raw_content or b"") if flow.response else 0,
    }

    with open(OUT_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    ctx.log.info(
        "[sve_capture] #%d %s %s%s -> %s (%d bytes)"
        % (_seq, rec["method"], host, rec["path"], rec["status"], rec["resp_size"])
    )
