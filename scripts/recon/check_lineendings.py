"""检查行尾符：shell / systemd 单元必须是 LF，Python 不应混入 CRLF。

为什么单独做这一步：
    在 Windows 上用 Python 的文本模式写文件（`open(p, "w")`）会把 `\\n`
    自动翻成 `\\r\\n`。对 `.py` 通常无害，但对 `.sh` 是**致命的**——
    bash 会报 `syntax error near unexpected token $'in\\r'`，
    而且报错位置指向毫不相干的 `case ... in`，非常难查。
    这个坑已经踩过两次，所以固化成检查。

用法：
    python scripts/recon/check_lineendings.py
"""
from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

# 必须 LF：在 Linux 上被 shell / systemd 直接执行
MUST_LF = (".sh", ".service", ".timer")
# 期望 LF：跨平台使用，混入 CRLF 虽不致命但不一致
SHOULD_LF = (".py", ".json", ".md", ".txt", ".html", ".js", ".bat")
SKIP_DIRS = {"__pycache__", ".git", "node_modules", "vendor", "apk", "recon", "capture"}


def scan() -> tuple[list[str], list[str]]:
    hard, soft = [], []
    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            p = os.path.join(root, fn)
            ext = os.path.splitext(fn)[1].lower()
            if ext not in MUST_LF and ext not in SHOULD_LF:
                continue
            try:
                data = open(p, "rb").read()
            except Exception:
                continue
            crlf = data.count(b"\r\n")
            if not crlf:
                continue
            rel = os.path.relpath(p, PROJECT_ROOT)
            (hard if ext in MUST_LF else soft).append("%s (%d 处 CRLF)" % (rel, crlf))
    return hard, soft


def main() -> int:
    hard, soft = scan()
    print("=== 必须 LF 的文件（.sh / .service / .timer）===")
    if hard:
        for h in hard:
            print("  FAIL %s" % h)
        print()
        print("  修复： python -c \"import pathlib; p=pathlib.Path('<file>');"
              " p.write_bytes(p.read_bytes().replace(b'\\\\r\\\\n', b'\\\\n'))\"")
    else:
        print("  OK   全部为 LF")

    print()
    print("=== 期望 LF 的文件 ===")
    if soft:
        for s in soft:
            print("  注意 %s" % s)
    else:
        print("  OK   全部为 LF")

    print()
    if hard:
        print("  结论: FAIL（%d 个必须-LF 文件含 CRLF，Linux 上会直接执行失败）" % len(hard))
        return 1
    print("  结论: PASS（必须-LF 文件全部合规%s）"
          % ("；另有 %d 个文件可统一为 LF" % len(soft) if soft else ""))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
