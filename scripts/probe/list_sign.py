"""反推 list 接口的签名内容。

已知：detail 等接口的签名 = SHA256(按 key 排序的 "k=v" 拼接)，其中插入一项
      "<udid>=<appKey>"。而 list 用同样的空 body 会 403，说明它把 query
      参数也算进了签名内容。

本脚本枚举若干候选，与抓包得到的真实 sign 比对，找出唯一匹配。

真实抓包样本从私人配置读取，不随仓库分发。

用法: python scripts/probe/list_sign.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from urllib.parse import parse_qsl, quote, urlencode

# appKey 不随仓库分发：从环境变量 SVE_APP_KEY 或 config/settings.json 读取。
# （这两个脚本原先硬编码了密钥，等于把它写进了仓库。）
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from signer import load_app_key  # noqa: E402
from private_samples import load_sample

PRIVATE_SAMPLE = load_sample("list_sign")

SECRET = load_app_key()
UDID = PRIVATE_SAMPLE["UDID"]
TARGET = PRIVATE_SAMPLE["TARGET"]

# 抓包里的原始 query（顺序、编码都保持原样）
RAW_QUERY = PRIVATE_SAMPLE["RAW_QUERY"]
PATH = "/urs/tournament/list"
FULL_PATH = "/sve/api/v1" + PATH


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def variant(name: str, text: str):
    h = sha(text)
    mark = "  <<< 命中" if h == TARGET else ""
    print("  %-46s %s%s" % (name, h[:20], mark))
    if h == TARGET:
        print("      参与串已隐藏（含密钥）")
        return True
    return False


def main() -> int:
    print("目标 sign: %s" % TARGET[:20])
    print()

    wins = []

    # A 组：把 query 原样拼进去的几种形式
    cands = [
        ("A1 raw_query 原样", RAW_QUERY),
        ("A2 <udid>=secret&raw_query", "%s=%s&%s" % (UDID, SECRET, RAW_QUERY)),
        ("A3 raw_query&<udid>=secret", "%s&%s=%s" % (RAW_QUERY, UDID, SECRET)),
        ("A4 path?raw_query + udid 项", "%s?%s%s=%s" % (PATH, RAW_QUERY, UDID, SECRET)),
        ("A5 fullpath?raw_query + udid 项", "%s?%s%s=%s" % (FULL_PATH, RAW_QUERY, UDID, SECRET)),
        ("A6 udid 项 + fullpath?raw_query", "%s=%s%s?%s" % (UDID, SECRET, FULL_PATH, RAW_QUERY)),
        ("A7 raw_query + udid 项", "%s%s=%s" % (RAW_QUERY, UDID, SECRET)),
        ("A8 udid 项 + raw_query", "%s=%s%s" % (UDID, SECRET, RAW_QUERY)),
    ]

    # B 组：把 query 解析成 k=v 后按 key 排序，再和 udid 项合并
    q = dict(parse_qsl(RAW_QUERY, keep_blank_values=True))
    merged = dict(q)
    merged[UDID] = SECRET
    sorted_join = "".join("%s=%s" % (k, merged[k]) for k in sorted(merged))
    cands.append(("B1 query+udid 按 key 排序拼接", sorted_join))

    merged2 = dict(q)
    merged2["udid"] = SECRET
    cands.append(("B2 query+udid(字面量) 排序", "".join("%s=%s" % (k, merged2[k]) for k in sorted(merged2))))

    # C 组：URL 编码形态差异
    enc_join = urlencode(q) + "%s=%s" % (UDID, SECRET)
    cands.append(("C1 urlencode(query)+udid 项", enc_join))
    cands.append(("C2 quote(area) 排序拼接",
                  "".join("%s=%s" % (k, (quote(merged[k]) if k == "area" else merged[k]))
                          for k in sorted(merged))))

    for name, text in cands:
        if variant(name, text):
            wins.append(name)

    print()
    if wins:
        print("命中: %s" % ", ".join(wins))
        return 0
    print("未命中。需要抓一次 APP 内部的签名计算过程，或改签名内容继续枚举。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
