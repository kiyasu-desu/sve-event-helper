"""反推 generateSign 的精确拼接方式。

已验证的算法框架（来自 recon/bundle_device.bundle）：
    generateSign(data, extra, o):
        l = {...data, ...extra};
        l[o] = getSecretKey();               # o 是 udid 的【值】，被当作 key 名
        s = Object.keys(l).sort().map(k => k + '=' + v).join('');
        return SHA256(s).hexdigest();

关键不确定点：
  1. 第三参数 o 是 udid 值（作 key 名）还是 key 名（"udid"）？
  2. 参与签名的 data 里到底有哪些字段？
  3. 拼接串是否带分隔符（& / 空串）？

本脚本枚举这些变体，与抓包样本比对，找出唯一匹配。
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import sys

# appKey 不随仓库分发：从环境变量 SVE_APP_KEY 或 config/settings.json 读取。
# （这两个脚本原先硬编码了密钥，等于把它写进了仓库。）
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "poller"))
from signer import load_app_key  # noqa: E402
from private_samples import load_sample

PRIVATE_SAMPLE = load_sample("bruteforce_sign")

SECRET = load_app_key()
DEVICEID = PRIVATE_SAMPLE["DEVICEID"]

SAMPLES = PRIVATE_SAMPLE["SAMPLES"]


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render(v) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, separators=(",", ":"), ensure_ascii=False)
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return "null"
    return str(v)


def build(merged: dict, sep: str, lower_keys: bool = False) -> str:
    keys = sorted(merged.keys())
    parts = []
    for k in keys:
        kk = k.lower() if lower_keys else k
        parts.append("%s=%s" % (kk, render(merged[k])))
    return sep.join(parts)


def main() -> int:
    wins = []

    # 变体维度
    sd_variants = {
        "o=udid值(实际变量)": DEVICEID,
        "o='udid'字面量": "udid",
        "o=secretKey": SECRET,
    }
    seps = ["", "&"]
    extra_fields = [
        ("纯body", {}),
        ("+sessionid", {"sessionid": None}),
        ("+deviceid", {"deviceid": DEVICEID}),
        ("+platform", {"platform": "android"}),
    ]

    print("枚举变体 ...")
    for sd_label, sd_value in sd_variants.items():
        for sep in seps:
            for ef_label, ef in extra_fields:
                ok = 0
                for s in SAMPLES:
                    merged = dict(s["body"])
                    merged.update(ef)
                    merged[sd_value] = SECRET
                    payload = build(merged, sep)
                    if sha256(payload) == s["sign"]:
                        ok += 1
                if ok == len(SAMPLES):
                    wins.append((sd_label, repr(sep), ef_label))

    if wins:
        print("\n*** 完全匹配的变体 ***")
        for w in wins:
            print("  udid 用法=%s  分隔符=%s  额外字段=%s" % w)
        return 0

    print("\n没有完全匹配的变体。逐样本打印候选分值，帮助缩小范围：")
    for s in SAMPLES:
        print("\n--- action=%s ---" % s["body"]["action"])
        results = []
        for sd_label, sd_value in sd_variants.items():
            for sep in seps:
                merged = dict(s["body"])
                merged[sd_value] = SECRET
                payload = build(merged, sep)
                h = sha256(payload)
                results.append((h == s["sign"], sd_label, repr(sep), payload))
        for match, label, sep, payload in results:
            print("  %s %-20s sep=%-4s %s" % ("[OK]" if match else "[  ]", label, sep, "<hidden>"))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
