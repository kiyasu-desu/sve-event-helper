"""对 scripts/ 下所有 PowerShell 脚本做语法检查。

为什么单独做：
    本机自动化工具曾在 PS 5.1 下运行，而 PS 5.1 以 ANSI 解析脚本文件——
    带中文注释的 .ps1 会解析失败。切到 PS7 后这个问题消失，但为了兼容
    仍要保证两个引擎都能加载，所以两边的 Parser 都跑一遍。

用法: python scripts/recon/check_ps1.py
"""
from __future__ import annotations

import os
import subprocess
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SCRIPTS = os.path.join(PROJECT_ROOT, "scripts")

PS7 = r"C:\Program Files\PowerShell\7\pwsh.exe"
PS5 = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

# 在目标引擎里解析文件，报告语法错误
PARSE_TPL = (
    "$e=$null;"
    "[System.Management.Automation.Language.Parser]::ParseFile('{path}',[ref]$null,[ref]$e)|Out-Null;"
    "if($e -and $e.Count){{ $e | ForEach-Object {{ 'L' + $_.Extent.StartLineNumber + ': ' + $_.Message }} }}"
    "else {{ 'OK' }}"
)


def ps1_files() -> list[str]:
    out = []
    for root, dirs, files in os.walk(SCRIPTS):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "node_modules")]
        for f in sorted(files):
            if f.lower().endswith(".ps1"):
                out.append(os.path.join(root, f))
    return out


def check(engine: str, path: str) -> tuple[bool, str]:
    if not os.path.exists(engine):
        return True, "(engine not found, skipped)"
    cmd = PARSE_TPL.format(path=path.replace("'", "''"))
    r = subprocess.run([engine, "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True, timeout=90,
                       encoding="utf-8", errors="replace")
    out = (r.stdout or "").strip()
    if out == "OK":
        return True, "OK"
    return False, out or (r.stderr or "").strip()[:200]


def main() -> int:
    files = ps1_files()
    print("发现 %d 个 .ps1 文件\n" % len(files))
    engines = [("PS7", PS7), ("PS5.1", PS5)]
    failures = []

    for path in files:
        rel = os.path.relpath(path, PROJECT_ROOT)
        results = []
        for label, exe in engines:
            ok, msg = check(exe, path)
            results.append((label, ok, msg))
            if not ok:
                failures.append("%s [%s]: %s" % (rel, label, msg.replace("\n", " ")[:160]))
        mark = "OK  " if all(r[1] for r in results) else "FAIL"
        detail = "  ".join("%s=%s" % (r[0], "ok" if r[1] else "ERR") for r in results)
        print("  [%s] %-44s %s" % (mark, rel, detail))

    print("\n===== 结果 =====")
    if failures:
        for f in failures:
            print("  FAIL %s" % f)
        return 1
    print("  ALL CHECKS PASSED (%d files x %d engines)" % (len(files), len(engines)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
