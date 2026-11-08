"""M01 offline migration: preview by default, explicit apply/recover only with writers stopped."""
import sys
sys.dont_write_bytecode = True

import argparse
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/client/core"))
from identity_migration import apply, preview, recover, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--action", choices=["preview", "apply", "recover", "rollback"], default="preview")
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "preview":
            result = report(preview(args.data_dir))
        else:
            if not args.writers_stopped:
                parser.error("apply/recover/rollback require --writers-stopped after stopping all writers")
            if args.action == "apply":
                result = apply(args.data_dir)
            else:
                recover(args.data_dir, rollback=args.action == "rollback")
                result = {"ok": True, "action": args.action}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception:
        print("迁移未完成；原始资料与事务状态已保留。检查备份/停止写入后使用 recover。详情不回显私人数据。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
