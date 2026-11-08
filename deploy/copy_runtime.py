"""Copy code/static resources only. Never copy, remove or replace private runtime data.

Independent of root/systemd/network so the installer's copy step can be rehearsed safely.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import tempfile

def reject_symlinks(path: Path, root: Path) -> None:
    while True:
        if path.is_symlink():
            raise ValueError("Copy refused: symlink in runtime path")
        if path == root:
            return
        if path.parent == path:
            raise ValueError("Copy refused: path outside root")
        path = path.parent


def copy_runtime(source: Path, target: Path) -> int:
    source = source.absolute()
    target = target.absolute()
    if not (source / "app/client/server.py").is_file() or not (source / "app/client/data/areas.json").is_file():
        raise ValueError("Copy refused: required server/static area file missing")
    if source.resolve() == target.resolve():
        return 0  # In-place upgrade: no cleanup, no data/config writes.
    reject_symlinks(source, source)
    reject_symlinks(target, target)
    import importlib.util
    spec = importlib.util.spec_from_file_location("sve_copy_release", Path(__file__).with_name("release.py"))
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    FILES = release.FILES
    files = {source / name for name in (*FILES, "app/client/data/README.md") if (source / name).is_file()}
    plan = []
    for path in sorted(files):
        reject_symlinks(path, source)
        destination = target / path.relative_to(source)
        reject_symlinks(destination, target)
        if destination.exists() and not destination.is_file():
            raise ValueError("Copy refused: runtime file destination is not a file")
        if not destination.resolve().is_relative_to(target.resolve()):
            raise ValueError("Copy refused: destination escapes target")
        plan.append((path, destination))
    # Preflight every path before the first write; no whole-directory copy or delete.
    for path, destination in plan:
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".sve-copy-", dir=destination.parent)
        os.close(fd)
        try:
            shutil.copy2(path, temp)
            os.replace(temp, destination)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    return len(plan)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    try:
        count = copy_runtime(args.source, args.target)
    except (OSError, ValueError):
        print("Runtime copy failed; existing private data/configuration retained. Check paths and permissions.")
        return 1
    print("Runtime files copied: %d; private data/configuration untouched." % count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
