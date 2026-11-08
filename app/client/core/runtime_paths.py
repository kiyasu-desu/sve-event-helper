"""Shared path contract. Explicit external storage never reads legacy secrets."""
from __future__ import annotations

import json
import os
from pathlib import Path


class RuntimePaths:
    def __init__(self, root, data_dir=None):
        self.root = Path(root).resolve()
        legacy = self.root / "app/client/data"
        self.data = Path(data_dir or os.environ.get("SVE_DATA_DIR") or legacy).resolve()
        self.external = self.data != legacy
        self.settings = Path(os.environ.get("SVE_SETTINGS_FILE") or
                             (self.data / "settings.json" if self.external else self.root / "config/settings.json")).resolve()
        if self.external and self.settings.is_relative_to(self.root):
            raise ValueError("External storage cannot use settings inside the code tree")
        self.capture = self.data / "capture" if self.external else self.root / "capture"
        self.logs = self.data / "logs" if self.external else self.capture
        self.client_log = self.logs / "client.log" if self.external else self.data / "client.log"
        self.backups = self.data / "deployment-backups" if self.external else self.root / ".deployment-backups"
        self.areas = self.root / "app/client/data/areas.json"

    def assert_ready(self):
        journal = self.root / "runtime-migration.json"
        if not journal.exists() and self.external and any((self.root / "app/client/data" / name).exists() for name in ("identity-v2.json", "identity-migration.json")):
            raise RuntimeError("Legacy authority exists; relocate offline before switching storage")
        if journal.exists():
            value = json.loads(journal.read_text(encoding="utf-8"))
            if value.get("stage") != "complete":
                raise RuntimeError("Runtime relocation incomplete; recover offline before starting")
            expected = Path(value["active_data"]).resolve()
            if self.data != expected:
                raise RuntimeError("Runtime storage moved; use the active data directory")

    def prepare(self):
        self.assert_ready()
        for path in (self.data, self.logs, self.capture):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.external and os.name != "nt" and path.stat().st_mode & 0o077:
                raise RuntimeError("Private directory permissions too broad")


def private_append(path, text):
    """Create logs with 0600 regardless of caller umask."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as output:
        output.write(text)
