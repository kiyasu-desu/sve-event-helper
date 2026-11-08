"""Shared notification resolution for service and standalone watchdog. No sending."""
from __future__ import annotations
import json
import os

# 环境变量覆盖表：(渠道, 字段) -> 环境变量名。
# 环境变量优先级最高，便于运维轮换密钥而不必碰界面/文件。
# 注意：systemd 的 Environment= 会出现在 `systemctl show` 输出里，
# 因此凭据建议放在 EnvironmentFile=（其内容不会被 show 出来）。
ENV_NOTIFY = {
    ("pushplus", "enabled"): "SVE_PUSHPLUS_ENABLED",
    ("pushplus", "token"): "SVE_PUSHPLUS_TOKEN",
    ("email", "enabled"): "SVE_EMAIL_ENABLED",
    ("email", "smtp_host"): "SVE_SMTP_HOST",
    ("email", "smtp_port"): "SVE_SMTP_PORT",
    ("email", "use_ssl"): "SVE_SMTP_SSL",
    ("email", "username"): "SVE_SMTP_USER",
    ("email", "authcode"): "SVE_SMTP_AUTHCODE",
    ("email", "from_addr"): "SVE_SMTP_FROM",
    ("email", "to_addrs"): "SVE_NOTIFY_TO",
}
SECRET_FIELDS = {("pushplus", "token"), ("email", "authcode")}


def _coerce(channel: str, field: str, raw: str):
    """把环境变量的字符串转成配置里应有的类型。"""
    if field in ("enabled", "use_ssl"):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if field == "smtp_port":
        try:
            return int(raw)
        except ValueError:
            return raw
    if field == "to_addrs":
        return [x.strip() for x in str(raw).replace(";", ",").split(",") if x.strip()]
    return raw


def read_json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default


def channels(settings_file, notify_file, loader=read_json):
    base = (loader(settings_file, {}) or {}).get("channels") or {}
    ui = loader(notify_file, {}) or {}
    merged = {}
    for name in set(base) | set(ui):
        if isinstance(base.get(name), dict) or isinstance(ui.get(name), dict):
            merged[name] = dict(base.get(name)) if isinstance(base.get(name), dict) else {}
            if isinstance(ui.get(name), dict):
                merged[name].update(ui[name])
    for (channel, field), env_name in ENV_NOTIFY.items():
        raw = os.environ.get(env_name)
        if raw is not None and raw != "":
            merged.setdefault(channel, {})[field] = _coerce(channel, field, raw)
    return merged
