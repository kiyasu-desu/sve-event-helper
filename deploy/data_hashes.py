"""Offline hash inventory of private files, for before/after upgrade comparisons.

Read-only except an explicitly requested output file. Run with writers stopped.
Reports may contain private filenames and must stay in protected local storage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def inventory(root: Path, data_dir: Path | None = None) -> dict:
    root = root.resolve()
    if not root.is_dir() or (data_dir is not None and not data_dir.is_dir()):
        raise ValueError("Hash inventory root/data directory does not exist")
    locations = [("config", root / "config"), ("capture", root / "capture"),
                 ("data", data_dir or root / "app/client/data")]
    result = {}
    for label, directory in locations:
        if directory.is_symlink():
            raise ValueError("Hash inventory refuses symlink directories")
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError("Hash inventory refuses symlinks")
            if not path.is_file():
                continue
            relative = path.relative_to(directory).as_posix()
            if (label == "data" and relative in {"areas.json", "README.md"}) or (label == "config" and relative.endswith(".example.json")):
                continue
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[label + "/" + relative] = digest.hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        text = json.dumps(inventory(args.root, args.data_dir), sort_keys=True, indent=2) + "\n"
        if args.output:
            # Never overwrite a runtime file or a previous baseline by accident.
            fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(text)
        else:
            print(text, end="")
    except (OSError, ValueError):
        print("Hash inventory failed; check paths/permissions. No existing files were overwritten.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
