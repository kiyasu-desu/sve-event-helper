"""Read-only release identity, captured once by the process at startup."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def snapshot(root):
    root = Path(root).resolve()
    journal = root / "deployment-state.json"
    if journal.exists():
        transaction = read_json(journal)
        if transaction.get("stage") not in {"complete", "rolled_back"} or transaction.get("entry_pending"):
            raise ValueError("Deployment incomplete; offline recover/rollback required")
    path = root / "release.json"
    if not path.exists():
        return {"release_id": "legacy-unversioned", "source_sha": None, "built_at": None, "schema_version": 2}
    value = read_json(path)
    if (not re.fullmatch(r"sve-[0-9a-f]{12}-[0-9TZ]+", value.get("release_id", ""))
            or not re.fullmatch(r"[0-9a-f]{40}", value.get("source_sha", ""))
            or value.get("schema_version") != 2 or value.get("compatible_schemas") != [2]
            or not isinstance(value.get("files"), dict)):
        raise ValueError("Invalid release identity")
    if not value["release_id"].startswith("sve-" + value["source_sha"][:12] + "-"):
        raise ValueError("Release ID/source mismatch")
    for name, digest in value["files"].items():
        relative = Path(name)
        target = root / relative
        if relative.is_absolute() or ".." in relative.parts or not target.resolve().is_relative_to(root):
            raise ValueError("Release path escapes root")
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError("Release file differs from manifest")
    return {field: value[field] for field in ("release_id", "source_sha", "built_at", "schema_version")}
