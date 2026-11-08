"""在 Hermes 字节码里定位签名相关函数的字符串常量。

Hermes 的 StringTable 与函数体是分离的，但函数体里出现的字符串 ID 会形成聚类：
如果函数 A 里同时用了 "sign" / "uid" / "tournament_id" / "HmacSHA256"，
它很可能就是签名函数。本脚本按"字符串 ID 邻近度"把这些聚类找出来。

用法: python scripts/recon/find_sign.py
"""
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

BUNDLE = paths.ensure_bundle()

KEYWORDS = [
    "sign", "Sign", "SIGN", "hmac", "Hmac", "HMAC", "sha256", "SHA256",
    "secret", "Secret", "appkey", "appKey", "AppKey", "app_key",
    "timestamp", "nonce", "uid", "tournament_id", "action", "sessionid",
    "deviceid", "crypto-js", "encrypt", "md5",
]


def load_bundle() -> bytes:
    with open(BUNDLE, "rb") as fh:
        return fh.read()


def guess_string_ids(data: bytes, needle: bytes) -> list[int]:
    """在字节码里按 UTF-16/UTF-8 两种方式找字符串，返回附近的 4 字节小端候选 ID。

    这里不做完整 Hermes 解析：目标是缩小范围，由人工/AI 判读候选。
    """
    hits = []
    for m in re.finditer(re.escape(needle), data):
        near = data[max(0, m.start() - 24): m.start() + 24]
        for off in range(0, len(near) - 4, 2):
            val = int.from_bytes(near[off:off + 4], "little")
            if 0 < val < 200000:
                hits.append(val)
    return hits


def main() -> int:
    data = load_bundle()
    print("bundle: %s (%d bytes)" % (BUNDLE, len(data)))

    # 1) 有哪些关键字真的出现在字节码里
    print("\n=== 关键字在字节码中的出现次数 ===")
    present = {}
    for kw in KEYWORDS:
        for enc, label in ((kw.encode("utf-8"), "utf8"), (kw.encode("utf-16-le"), "utf16")):
            n = data.count(enc)
            if n:
                present.setdefault(kw, 0)
                present[kw] += n
    for kw, n in sorted(present.items(), key=lambda kv: -kv[1]):
        print("  %-16s %d" % (kw, n))

    # 2) 围绕 "sign" 找候选字符串 ID 聚类
    print("\n=== 围绕 sign / HmacSHA256 的候选 ID 聚类 ===")
    for needle in (b"sign", b"Sign"):
        ids = guess_string_ids(data, needle)
        if not ids:
            continue
        cnt = Counter(ids)
        top = cnt.most_common(12)
        print("  %-8s -> %s" % (needle.decode(), ", ".join("0x%x(%d)" % (i, c) for i, c in top)))

    # 3) 直接找可能的拼接模板：形如 "xxx=" 或 "&" 连接的短 ASCII 串
    print("\n=== 可能的签名模板（含 = 或 & 的短串）===")
    patterns = set()
    for m in re.finditer(rb"[\x20-\x7e]{6,80}", data):
        s = m.group(0).decode("ascii", "ignore")
        if ("=" in s or "&" in s) and re.search(r"(sign|key|time|uid|nonce|token)", s, re.I):
            patterns.add(s)
    for s in sorted(patterns)[:40]:
        print("  " + s)

    print("\n提示：完整还原需要 Hermes 反汇编（hbctool/hbctool 类工具）。")
    print("      若静态路线受阻，可改用 Frida hook 运行时签名函数（见 README §5）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
