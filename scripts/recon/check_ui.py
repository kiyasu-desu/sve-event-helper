"""校验客户端页面的内联 JavaScript 语法。

为什么需要单独做这一步：
    页面的 JS 是内联在 .html 里的，Python 语法检查与 .ps1 检查都覆盖不到。
    曾因为一次编辑残留了一个多余的 `}`，导致整个 <script> 解析失败——
    表现为"页面上所有按钮都点了没反应"（包括登录），而服务端日志里
    什么也看不到，很容易误判成后端问题。

做法：抽出 <script> 内容交给 node 做语法检查，并顺带检查
"脚本引用了但页面未定义的元素 id"。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
UI_DIR = os.path.join(PROJECT_ROOT, "app", "client", "ui")
TMP = os.path.join(PROJECT_ROOT, "vendor", "tmp")


def main() -> int:
    if not os.path.isdir(UI_DIR):
        print("  未找到 UI 目录: %s" % UI_DIR)
        return 1
    os.makedirs(TMP, exist_ok=True)

    pages = sorted(f for f in os.listdir(UI_DIR) if f.endswith(".html"))
    fails = []
    for page in pages:
        path = os.path.join(UI_DIR, page)
        html = open(path, encoding="utf-8").read()
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        js = "\n".join(scripts)

        # 1) node 语法检查
        jsfile = os.path.join(TMP, page.replace(".html", ".js"))
        open(jsfile, "w", encoding="utf-8").write(js)
        r = subprocess.run(["node", "--check", jsfile], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        syn_ok = (r.returncode == 0)
        if not syn_ok:
            msg = (r.stderr or "").strip().split("\n")[:3]
            fails.append("%s: JS 语法错误 -> %s" % (page, " / ".join(msg)))

        # 2) 元素 id 引用一致性
        defined = set(re.findall(r'id="([A-Za-z][\w-]*)"', html))
        used = set(re.findall(r'\$\("([A-Za-z][\w-]*)"\)', js))
        missing = used - defined
        if missing:
            fails.append("%s: 引用了不存在的元素 id %s" % (page, sorted(missing)))

        print("  [%s] %-12s script=%d 字符  id引用=%d  %s"
              % ("OK  " if syn_ok and not missing else "FAIL", page, len(js),
                 len(used), ("缺失:%s" % sorted(missing)) if missing else ""))

    print()
    if fails:
        for f in fails:
            print("  FAIL %s" % f)
        return 1
    print("  ALL CHECKS PASSED（%d 个页面）" % len(pages))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
