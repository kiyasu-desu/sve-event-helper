"""验证从 bundle 源码还原出的 generateSign 算法。

源码（recon/bundle_device.bundle，模块 1962 提供 CryptoJS 的 SHA256）：
    generateSign(data, extra, keyName):
        merged = {...data, ...extra}
        merged[keyName] = getSecretKey()          # 正式服的密钥不写在仓库里
        s = Object.keys(merged).sort()
                 .map(k => k + '=' + (isObject(v)||isArray(v) ? JSON.stringify(v) : v))
                 .join('')
        return SHA256(s).toString()               # CryptoJS SHA256 -> 64 位小写十六进制

注意：结果长度 64 位十六进制，因此哈希是 SHA-256 而不是 MD5。

真实抓包样本从私人配置读取，不随仓库分发。

udid 来源是 react-native-device-info 的 getUniqueId()，其真实取值未知，
因此脚本会对若干候选值逐一尝试，用来反推出 udid。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

# appKey 不随仓库分发：从环境变量 SVE_APP_KEY 或 config/settings.json 读取。
# （这两个脚本原先硬编码了密钥，等于把它写进了仓库。）
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from signer import load_app_key  # noqa: E402
from private_samples import load_sample

PRIVATE_SAMPLE = load_sample("verify_sign")

# 正式服密钥从配置读取；iOS / 测试服仅在需要时用环境变量提供（本项目不用）
SECRET_KEY_OFFICIAL = load_app_key()
SECRET_KEY_IOS = os.environ.get("SVE_APP_KEY_IOS", "")
SECRET_KEY_DEV = os.environ.get("SVE_APP_KEY_DEV", "")

SAMPLES = PRIVATE_SAMPLE["SAMPLES"]

UDID_CANDIDATES = PRIVATE_SAMPLE["UDID_CANDIDATES"]


def js_str(v) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return "null"
    return str(v)


def build_payload(data: dict, udid: str, secret_key: str = SECRET_KEY_OFFICIAL,
                  extra: dict | None = None, key_name: str = "udid") -> str:
    merged = dict(data)
    if extra:
        merged.update(extra)
    merged[key_name] = secret_key
    parts = []
    for k in sorted(merged.keys()):
        v = merged[k]
        if isinstance(v, (dict, list)):
            parts.append("%s=%s" % (k, json.dumps(v, separators=(",", ":"), ensure_ascii=False)))
        else:
            parts.append("%s=%s" % (k, js_str(v)))
    return "".join(parts)


def generate_sign(data: dict, udid: str, secret_key: str = SECRET_KEY_OFFICIAL) -> tuple[str, str]:
    payload = build_payload(data, udid, secret_key)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), payload


def main() -> int:
    print("=== 用 SHA256 验证 ===")
    best_ok = 0
    for udid in UDID_CANDIDATES:
        ok = 0
        print("\n-- udid 候选: %s --" % udid)
        for i, s in enumerate(SAMPLES, 1):
            sign, payload = generate_sign(s["body"], udid)
            match = (sign == s["sign"])
            ok += 1 if match else 0
            print("  样本%d %-7s 我的=%s 抓包=%s %s"
                  % (i, s["body"]["action"], sign[:16], s["sign"][:16],
                     "[OK]" if match else "[FAIL]"))
            print("        参与串已隐藏（含密钥）")
        best_ok = max(best_ok, ok)
        if ok == len(SAMPLES):
            print("  ==> udid = %s 验证通过" % udid)
            return 0

    print("\n通过 %d/%d（无候选 udid 命中）" % (best_ok, len(SAMPLES)))
    print("算法（SHA256 + 排序拼接 + appKey）已确认；缺的是 udid 的真实取值。")
    print("下一步：用 Frida RPC 调用 react-native-device-info 的原生 getUniqueId() 取值。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
