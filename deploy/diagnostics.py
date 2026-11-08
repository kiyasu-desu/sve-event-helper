"""Read-only default verification; upstream reads and notification tests are opt-in."""
from __future__ import annotations

import argparse
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.parse
import urllib.request

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release


def storage_probe(root, data_dir=None):
    """Explicit local write test; execute as the service user in its sandbox."""
    release.check_configuration(root, data_dir)
    runtime = release.paths(root, data_dir)
    runtime.prepare()
    for directory in dict.fromkeys((runtime.data, runtime.logs, runtime.capture, runtime.backups)):
        release.make_directory(directory, mode=0o700)
        fd, name = tempfile.mkstemp(prefix=".sve-permission-", dir=directory)
        source, destination = Path(name), Path(name + ".replaced")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(b"synthetic-storage-probe\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(source, destination)
            release.sync_directory(directory)
            if destination.read_bytes() != b"synthetic-storage-probe\n":
                raise ValueError("Storage probe verification failed")
        finally:
            for path in (source, destination):
                if path.exists():
                    path.unlink()
            release.sync_directory(directory)
    return {"ok": True, "operation": "local_storage_probe"}


def inspect(root, data_dir=None, health_url=None):
    if sys.version_info < (3, 10):
        raise ValueError("Python 3.10 required")
    root = Path(root).resolve()
    release.verify_package(root, installed=True)
    # An incomplete switch must not pass verification even if bytes already match.
    info = release.core_module(root, "releaseinfo").snapshot(root)
    for module, minimum, maximum in (("flask", (3, 0), 4), ("requests", (2, 31), 3)):
        version = tuple(int(part) for part in importlib.metadata.version(module).split(".")[:2])
        if not minimum <= version or version[0] >= maximum:
            raise ValueError("Unsupported dependency")
    value = release.check_configuration(root, data_dir)
    if health_url:
        parsed = urllib.parse.urlsplit(health_url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.path != "/api/health" or parsed.username or parsed.password:
            raise ValueError("Only loopback health endpoint allowed")
        with urllib.request.urlopen(health_url, timeout=8) as response:
            running = json.load(response)
        if not running.get("ok") or running.get("release") != info:
            raise ValueError("Running process does not match installed release")
    return {"ok": True, "release": info, "counts": {key: len(value[key]) for key in ("accounts", "subscriptions", "records")}}


def upstream(root, data_dir, account_id):
    if not account_id:
        raise ValueError("Explicit account ID required")
    value = release.check_configuration(root, data_dir)
    sys.path.insert(0, str(Path(root) / "app/client/core"))
    from sveapi import Account, SveClient
    from accountguard import normalized_uid
    aid = normalized_uid(account_id)
    account = Account.from_dict(value["accounts"][aid])
    info = SveClient(account).info()  # Read only; never update cache/files or signup.
    return {"ok": normalized_uid(info.get("id")) == aid, "operation": "upstream_identity_read"}


def notify_test(root, data_dir):
    # This opt-in sends messages. Import creates no schedulers; existing data dir
    # is required so a typo cannot initialize a new production dataset.
    release.check_configuration(root, data_dir)
    data = release.paths(root, data_dir).data
    os.environ["SVE_DATA_DIR"] = str(data)
    spec = importlib.util.spec_from_file_location("sve_notify_diagnostic", Path(root) / "app/client/server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.path.insert(0, str(Path(root) / "scripts/poller"))
    import notify
    results = notify.notify_all(module.notify_channels(), "SVE explicit notification test", "<p>Explicit operator notification test</p>")
    return {"ok": any(ok for _, ok, _ in results), "operation": "notification_test",
            "channels": [{"channel": name, "accepted": bool(ok)} for name, ok, _ in results]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--health-url")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--validate-upstream", action="store_true")
    group.add_argument("--test-notify", action="store_true")
    group.add_argument("--probe-storage", action="store_true")
    parser.add_argument("--account-id")
    args = parser.parse_args()
    try:
        if args.validate_upstream:
            value = upstream(args.root, args.data_dir, args.account_id)
        elif args.test_notify:
            value = notify_test(args.root, args.data_dir)
        elif args.probe_storage:
            value = storage_probe(args.root, args.data_dir)
        else:
            value = inspect(args.root, args.data_dir, args.health_url)
        print(json.dumps(value, sort_keys=True))
        return 0 if value["ok"] else 1
    except Exception:
        print('{"ok": false, "error": "diagnostic failed; private details suppressed"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
