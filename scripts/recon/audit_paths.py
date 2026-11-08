"""扫描工作区内脚本，列出所有可能"越界"（触碰工作区之外路径）的位置。

分类：
  EXTERNAL-WRITE  绝对路径且可能写入 —— 需要重点关注
  EXTERNAL-READ   绝对路径且只读（工具、系统 API）
  DEVICE          通过 adb 操作模拟器（不算宿主机越界）
  PIP             安装依赖（写入 venv，venv 在工作区内）

用法: python scripts/recon/audit_paths.py
"""
from __future__ import annotations

import os
import re
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

# 匹配不落在工作区内的绝对路径
WIN_ABS = re.compile(r"[A-Za-z]:\\\\[^\"'\s\)\];,]{2,}|[A-Za-z]:\\[^\"'\s\)\];,]{2,}")
UNIX_ABS = re.compile(r"(?<![\w.])/(?!sdcard|data/local/tmp|apex|system|proc|dev)[a-zA-Z][\w./\-]{3,}")

# 常见的"工作区内"路径写法，命中则跳过
INSIDE_HINTS = ("$PSScriptRoot", "$ProjectRoot", "$CaptureDir", "$ConfDir", "$ReconDir",
                "$VenvDir", "PROJECT_ROOT", "os.path.join", "$root", "$script",
                "__file__", "$PSScriptRoot\\..", "Join-Path")

WRITE_HINTS = ("WriteAllText", "Set-Content", "Add-Content", "Out-File", "New-Item",
               "open(", "write", "os.replace", "mkdir", "makedirs", "copyfile",
               "shutil", "Remove-Item", "Move-Item", "Copy-Item")


def scan_file(path: str):
    rel = os.path.relpath(path, PROJECT_ROOT)
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except Exception as exc:  # noqa: BLE001
        return [("ERROR", rel, 0, str(exc))]

    findings = []
    for i, line in enumerate(text.splitlines(), 1):
        if any(h in line for h in INSIDE_HINTS):
            continue
        for m in WIN_ABS.finditer(line):
            p = m.group(0)
            if p.lower().startswith(PROJECT_ROOT.lower()[:3]):
                p_norm = p.replace("\\\\", "\\")
                if p_norm.lower().startswith(PROJECT_ROOT.lower()):
                    continue
            is_write = any(w in line for w in WRITE_HINTS)
            kind = "EXTERNAL-WRITE" if is_write else "EXTERNAL-READ"
            # adb / 设备操作不算宿主机越界
            if "$adb" in line.lower() or "adbexe" in line.lower() or "-s $" in line:
                kind = "DEVICE"
            findings.append((kind, rel, i, line.strip()[:150]))
    return findings


def main() -> int:
    targets = []
    for sub in ("scripts",):
        for root, _dirs, files in os.walk(os.path.join(PROJECT_ROOT, sub)):
            if "venv314" in root or "node_modules" in root or "__pycache__" in root:
                continue
            for f in files:
                if f.endswith((".ps1", ".py", ".cmd", ".js")):
                    targets.append(os.path.join(root, f))

    groups: dict[str, list] = {}
    for t in sorted(targets):
        for kind, rel, ln, text in scan_file(t):
            groups.setdefault(kind, []).append((rel, ln, text))

    order = ["EXTERNAL-WRITE", "EXTERNAL-READ", "DEVICE", "ERROR"]
    total = 0
    for kind in order:
        items = groups.get(kind, [])
        total += len(items)
        print("\n===== %s (%d) =====" % (kind, len(items)))
        for rel, ln, text in items:
            print("  %-42s L%-5d %s" % (rel, ln, text))

    print("\n扫描文件数: %d  命中总数: %d" % (len(targets), total))
    if not groups.get("EXTERNAL-WRITE"):
        print("结论: 未发现对工作区之外的写操作。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
