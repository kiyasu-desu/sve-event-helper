"""对 scripts/ 下所有 Python 文件做分层验证。

为什么不是只做 ast.parse：
    ast.parse 只检查"文本能否解析成语法树"，抓不到 global 声明顺序、
    名称未定义、导入错误这类问题——曾经因此漏掉一个 netcap_run.py 的真实错误
    （"name 'OUT_FILE' is used prior to global declaration" 只有导入时才暴露）。

验证层次：
    1. py_compile          所有文件都要通过（编译期检查）
    2. 真实导入            能安全导入的就导入（暴露 global/名称/依赖问题）
    3. --help              对 CLI 脚本验证入口可运行（不触发真实动作）

用法: python scripts/recon/check_scripts.py
"""
from __future__ import annotations

import importlib.util
import os
import py_compile
import subprocess
import sys
import tempfile

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
SCRIPTS = os.path.join(PROJECT_ROOT, "scripts")

# 不做真实导入的文件：
#   - 导入即连接设备/网络的
#   - recon/ 与 probe/ 下的一次性分析片段（模块级就执行、依赖 sys.argv），
#     这些是"用完即弃"的脚本，用 py_compile 保证可解析即可
SKIP_IMPORT = {
    "netcap_run.py",     # 导入即尝试连接 frida 设备
    "frida_run.py",
    "probe_device.js",
}

SKIP_IMPORT_DIRS = ("recon", "probe")

# 测试类模块也不要"导入验证"：它们模块级就跑完整场景（有副作用），
# 曾被 check_scripts 间接执行、把测试日志写进生产 poll.log，看起来
# 像是生产环境发生了自动报名。测试的正确执行方式是显式运行它们自己。
SKIP_IMPORT_PREFIX = ("test_",)

# 这些模块定义了 main()/argparse，用 --help 验证入口
# 注意：
#   - 不要把 check_scripts.py 自己列进来（会递归调用自己直到超时）
#   - sve_capture.py 属于已废弃的 HTTP 代理方案，依赖 mitmproxy，不在必需范围
CLI_HELP = {
    "poll.py", "action.py", "session_manager.py", "signer.py",
    "netcap_run.py", "audit_paths.py", "list_sign.py",
    "test_autosignup_spin.py", "test_autosignup_confirm.py",
}


def all_py_files() -> list[str]:
    out = []
    for root, dirs, files in os.walk(SCRIPTS):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "node_modules")]
        for f in sorted(files):
            if f.endswith(".py"):
                out.append(os.path.join(root, f))
    return out


def main() -> int:
    files = all_py_files()
    print("发现 %d 个 .py 文件\n" % len(files))
    failures = []

    # --- 1) 编译检查 ---------------------------------------------------------
    print("== 1) py_compile ==")
    for p in files:
        rel = os.path.relpath(p, PROJECT_ROOT)
        try:
            with tempfile.TemporaryDirectory() as td:
                py_compile.compile(p, cfile=os.path.join(td, "x.pyc"), doraise=True)
        except py_compile.PyCompileError as e:
            failures.append("%s: 编译失败 %s" % (rel, e))
    print("   %s" % ("全部通过" if not failures else "有失败"))

    # --- 2) 真实导入 ---------------------------------------------------------
    print("\n== 2) 真实导入（暴露 global/名称/依赖问题）==")
    import_fail = []
    for p in files:
        name = os.path.basename(p)
        rel = os.path.relpath(p, PROJECT_ROOT)
        parts = rel.replace("\\", "/").split("/")
        if name in SKIP_IMPORT or any(d in SKIP_IMPORT_DIRS for d in parts):
            print("   skip  %s" % rel)
            continue
        if name.startswith(SKIP_IMPORT_PREFIX):
            print("   skip  %s  (测试模块，导入即执行)" % rel)
            continue
        mod_name = "chk_" + name[:-3]
        spec = importlib.util.spec_from_file_location(mod_name, p)
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except SystemExit:
            pass
        except Exception as e:  # noqa: BLE001
            import_fail.append("%s: %s: %s" % (rel, type(e).__name__, e))
    for f in import_fail:
        failures.append(f)
    print("   %d 个导入失败" % len(import_fail) if import_fail else "   全部通过")

    # --- 3) CLI --help ------------------------------------------------------
    print("\n== 3) CLI --help（验证入口可运行）==")
    py = sys.executable
    help_fail = []
    for p in files:
        if os.path.basename(p) not in CLI_HELP:
            continue
        rel = os.path.relpath(p, PROJECT_ROOT)
        try:
            r = subprocess.run([py, p, "--help"], capture_output=True, text=True,
                               timeout=60, cwd=PROJECT_ROOT,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0 and "usage" not in (r.stdout + r.stderr).lower():
                help_fail.append("%s: exit=%s %s" % (rel, r.returncode,
                                                     (r.stderr or "").strip()[:120]))
        except Exception as e:  # noqa: BLE001
            help_fail.append("%s: %s" % (rel, e))
    for f in help_fail:
        failures.append(f)
    print("   %d 个入口异常" % len(help_fail) if help_fail else "   全部通过")

    # --- 汇总 ---------------------------------------------------------------
    print("\n===== 结果 =====")
    if failures:
        for f in failures:
            print("  FAIL %s" % f)
        return 1
    print("  ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
