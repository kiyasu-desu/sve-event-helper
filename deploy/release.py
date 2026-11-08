"""Commit-based release directories and offline, recoverable code-only switches.

No service control, package installation, upstream requests or data rollback.
All reports suppress private file contents and exception details.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
import uuid

sys.dont_write_bytecode = True

CORE = ("accountguard", "agentlink", "identity_migration", "identity_store", "notification_config", "releaseinfo", "scheduler_health", "watchdog_state", "runtime_paths", "scheduler", "sveapi", "weblogin")
FILES = (
    "README.md", "config/secrets.env.example", "config/settings.example.json", "app/client/server.py", "app/client/data/areas.json",
    *("app/client/core/%s.py" % name for name in CORE),
    *("app/client/ui/" + name for name in ("index.html", "subs.html", "sw.js", "manifest.json", "apple-touch-icon.png", "icon-192.png", "icon-512.png", "icon-maskable-512.png")),
    "scripts/sveenv.py", "scripts/identity_migrate.py",
    *("scripts/poller/%s.py" % name for name in ("action", "autosignup", "notify", "poll", "session_manager", "signer", "watchdog")),
    *("deploy/" + name for name in ("release.py", "runtime_migrate.py", "diagnostics.py", "requirements.txt", "install.sh", "upgrade.sh", "verify.sh", "copy_runtime.py", "data_hashes.py", "README.md")),
    *("deploy/" + name for name in ("M00-data-protection.md", "M01-account-identity.md", "M02-profile-consistency.md", "M03-signup-confirmation.md", "M04-traceable-deployment.md", "M05-external-runtime.md", "M06-scheduler-watchdog.md", "PWA-SSH-Tunnel.md", "PWA-Tailscale.md", "Android-App.md")),
    *("deploy/systemd/" + name for name in ("sve-agent.service", "sve-watchdog.service", "sve-watchdog.timer")),
)
# M04 manifests remain valid for baseline verification and code rollback.
M05_ADDITIONS = {"app/client/core/runtime_paths.py", "config/secrets.env.example",
                 "deploy/runtime_migrate.py", "deploy/M05-external-runtime.md"}
M06_ADDITIONS = {"app/client/core/notification_config.py", "app/client/core/scheduler_health.py",
                 "app/client/core/watchdog_state.py", "deploy/M06-scheduler-watchdog.md"}
M06_FILES = set(FILES)
M05_FILES = M06_FILES - M06_ADDITIONS
LEGACY_FILES = M05_FILES - M05_ADDITIONS
RETIRED_FILES = tuple("scripts/poller/%s.py" % name for name in
                      ("poll", "action", "autosignup", "session_manager", "signer"))
FILES = tuple(name for name in FILES if name not in RETIRED_FILES) + (
    "app/client/core/notification_sender.py", "scripts/runtime/watchdog.py",
    "deploy/M20-retire-legacy-poller.md",
)
# Only these fixed code paths may be removed/restored. Runtime data is never swept.
SWITCH_FILES = tuple(dict.fromkeys((*FILES, *RETIRED_FILES)))



def paths(target, data_dir=None):
    return core_module(Path(__file__).resolve().parents[1], "runtime_paths").RuntimePaths(target, data_dir)


TERMINAL = {"complete", "rolled_back"}
ENTRY = "app/client/server.py"
# The existing unit invokes this path directly, including on legacy M01. The
# fence must need no imports, dependency files, environment variables or unit edits.
ROLLBACK_FENCE = b"raise SystemExit('Deployment rollback incomplete; run offline recover before starting the service')\n"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sync_directory(path):
    if os.name != "nt":
        directory = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def make_directory(path, mode=0o755):
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    for directory in reversed(missing):
        if mode == 0o700:
            directory.chmod(mode)
        sync_directory(directory.parent)


def safe_path(root, name):
    root = Path(root).absolute()
    path = root / name
    if Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError("Invalid relative path")
    current = path
    while True:
        if current.is_symlink():
            raise ValueError("Symlinks refused")
        if current == root:
            break
        if current == current.parent:
            raise ValueError("Outside root")
        current = current.parent
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Outside root")
    return path


def write(path, data, mode=0o644):
    make_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".sve-release-", dir=path.parent)
    try:
        os.chmod(temporary, mode)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    write(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode(), 0o600)


def journal_save(path, value):
    # Public code hashes/stage only; the unprivileged service must read its fence.
    write(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode(), 0o644)


def build(source, ref, output):
    source, output = Path(source).resolve(), Path(output).absolute()
    sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"], stderr=subprocess.DEVNULL).decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Explicit commit required")
    names = subprocess.check_output(["git", "-C", str(source), "ls-tree", "-rz", sha], stderr=subprocess.DEVNULL).split(b"\0")
    tree = {}
    for row in names:
        if row:
            meta, name = row.split(b"\t", 1)
            tree[name.decode()] = meta.split()
    if not set(FILES) <= tree.keys() or any(tree[name][0] != b"100644" and tree[name][0] != b"100755" for name in FILES):
        raise ValueError("Required committed runtime files missing or nonregular")
    if output.exists() or output.is_symlink():
        raise ValueError("Output must be new")
    now = datetime.now(timezone.utc)
    manifest = {"manifest_version": 1, "release_id": "sve-%s-%s" % (sha[:12], now.strftime("%Y%m%dT%H%M%S%fZ")),
                "source_sha": sha, "built_at": now.isoformat(), "schema_version": 2, "compatible_schemas": [2],
                "files": {}, "excluded_files": sorted(tree.keys() - set(FILES))}
    # A failed build remains an invalid directory, never a valid partial release.
    output.mkdir(parents=True)
    for name in FILES:
        data = subprocess.check_output(["git", "-C", str(source), "cat-file", "blob", tree[name][2].decode()], stderr=subprocess.DEVNULL)
        if name.endswith((".sh", ".service", ".timer")):
            data = data.replace(b"\r\n", b"\n")
            if b"\r" in data:
                raise ValueError("Invalid Linux line endings")
        write(safe_path(output, name), data)
        manifest["files"][name] = digest(data)
    write(output / "release.json", (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode())
    verify_package(output)
    return manifest


def verify_package(root, installed=False):
    root = Path(root).absolute()
    manifest = read(safe_path(root, "release.json"))
    if (manifest.get("manifest_version") != 1 or manifest.get("schema_version") != 2
            or manifest.get("compatible_schemas") != [2]
            or not re.fullmatch(r"[0-9a-f]{40}", manifest.get("source_sha", ""))
            or not re.fullmatch(r"sve-[0-9a-f]{12}-[0-9TZ]+", manifest.get("release_id", ""))
            or not isinstance(manifest.get("built_at"), str)
            or not isinstance(manifest.get("files"), dict) or set(manifest["files"]) not in (set(FILES), M06_FILES, M05_FILES, LEGACY_FILES)):
        raise ValueError("Invalid manifest contract")
    if not manifest["release_id"].startswith("sve-" + manifest["source_sha"][:12] + "-"):
        raise ValueError("Release ID/source mismatch")
    datetime.fromisoformat(manifest["built_at"])
    for name, expected in manifest["files"].items():
        data = safe_path(root, name).read_bytes()
        if digest(data) != expected or (name.endswith((".sh", ".service", ".timer")) and b"\r" in data):
            raise ValueError("Release hash mismatch")
        if name.endswith(".py"):
            source = data.decode("utf-8-sig")
            ast.parse(source, filename=name, feature_version=(3, 10))
            compile(source, name, "exec")
    if set(manifest["files"]) == set(FILES):
        for name in RETIRED_FILES:
            if safe_path(root, name).exists():
                raise ValueError("Retired executable remains in current release")
    if not installed:
        actual = set()
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ValueError("Package symlink refused")
            if path.is_file():
                actual.add(path.relative_to(root).as_posix())
        if actual != set(manifest["files"]) | {"release.json"}:
            raise ValueError("Unexpected package file")
    return manifest


def core_module(root, name):
    sys.path.insert(0, str(Path(root) / "app/client/core"))
    key = "sve_release_" + name + "_" + digest(str(Path(root).resolve()).encode())[:12]
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, Path(root) / "app/client/core" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def check_configuration(target, data_dir=None):
    target = Path(target)
    runtime = paths(target, data_dir)
    runtime.assert_ready()
    settings = runtime.settings
    cfg = read(settings) if settings.exists() else {}
    if not isinstance(cfg, dict):
        raise ValueError("Invalid settings object")
    channels = cfg.get("channels", {})
    if not isinstance(channels, dict) or any(not isinstance(row, dict) for row in channels.values()):
        raise ValueError("Invalid notification configuration")
    key = cfg.get("app_key")
    if isinstance(key, dict):
        key = key.get("official")
    resolved_key = os.environ.get("SVE_APP_KEY", "").strip() or (key.strip() if isinstance(key, str) else "")
    if not resolved_key or resolved_key.startswith("<"):
        raise ValueError("Missing private signing configuration")
    data = paths(target, data_dir).data
    if not data.is_dir() or not os.access(data, os.W_OK | os.X_OK):
        raise ValueError("Runtime directory unavailable to checking user")
    if paths(target, data_dir).external and os.name != "nt" and data.stat().st_mode & 0o077:
        raise ValueError("Private directory permissions too broad")
    for path in (settings, data / "identity-v2.json", data / "notify.json", data / "remote.json"):
        if path.exists() and os.name != "nt" and path.stat().st_mode & 0o077:
            raise ValueError("Private file permissions too broad")
    for path in (data / "notify.json", data / "remote.json"):
        if path.exists() and not isinstance(read(path), dict):
            raise ValueError("Invalid runtime configuration object")
    # Read/validate only; never initialize identity data in an inspection.
    module = core_module(Path(__file__).resolve().parents[1], "identity_store")
    value = module.IdentityStore(data).load()
    return value


def preflight(package, target, python=sys.executable, data_dir=None):
    manifest = verify_package(package)
    if set(manifest["files"]) != set(FILES):
        raise ValueError("Use matching release tools for older candidates; rollback remains supported")
    target = Path(target)
    data = paths(target, data_dir).data
    if (target / "app/client/server.py").exists() and not (data / "identity-v2.json").is_file():
        raise ValueError("Existing install missing authoritative dataset")
    check_configuration(target, data_dir)
    # Only staging may create temporary files. Block every business side effect
    # before importing the candidate server, and never copy private settings.
    code = '''import sys,os,tempfile,socket,subprocess,threading,urllib.request,importlib.util
from unittest.mock import patch
assert sys.version_info >= (3,10)
import flask,requests,importlib.metadata
for name,minimum,maximum in [('flask',(3,0),4),('requests',(2,31),3)]:
 version=tuple(int(p) for p in importlib.metadata.version(name).split('.')[:2]); assert version>=minimum and version[0]<maximum
with tempfile.TemporaryDirectory(prefix='sve-preflight-') as directory:
 os.environ['SVE_DATA_DIR']=directory
 os.environ['SVE_APP_KEY']='synthetic-preflight-key'
 os.environ['SVE_SETTINGS_FILE']=os.path.join(directory,'settings.json')
 def blocked(*a,**k): raise RuntimeError('Unexpected staging side effect')
 with patch.object(socket.socket,'connect',blocked),patch.object(subprocess,'Popen',blocked),patch.object(threading.Thread,'start',blocked),patch.object(urllib.request,'urlopen',blocked):
  spec=importlib.util.spec_from_file_location('sve_candidate',sys.argv[1]); module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
  assert not module.STATE['schedulers']
  sys.path.insert(0,os.path.join(os.path.dirname(sys.argv[1]),'../../scripts/runtime'))
  import watchdog,notification_sender
  assert not any(name in sys.modules for name in ('poll','autosignup','session_manager'))
  # Settings shares the existing packaged shell; older rollback candidates
  # without this panel retain their original route contract.
  with open(os.path.join(os.path.dirname(sys.argv[1]),'ui','index.html'),'rb') as source:
   shell=source.read()
  if b'id="settingsPage"' in shell:
   with module.app.test_client() as client:
    for route in ('/','/subs','/settings'):
     with client.get(route) as response:
      assert response.status_code == 200 and response.data == shell
'''
    result = subprocess.run([str(python), "-B", "-c", code, str(Path(package).resolve() / "app/client/server.py")], capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError("Candidate import/dependency check failed")
    return manifest


def private_snapshot(target, backup, data):
    settings = paths(target, data).settings
    if settings.exists() and not settings.is_relative_to(data) and not settings.is_relative_to(target / "config"):
        make_directory(backup / "private/settings", mode=0o700)
        write(backup / "private/settings/settings.json", settings.read_bytes(), 0o600)
    for label, location in (("data", data), ("config", target / "config"), ("capture", target / "capture")):
        if location.exists():
            if location.is_symlink():
                raise ValueError("Private snapshot symlink refused")
            destination = backup / "private" / label
            for path in location.rglob("*"):
                if location == data and path.relative_to(data).parts[0] == "deployment-backups":
                    continue
                if path.is_symlink():
                    raise ValueError("Private snapshot symlink refused")
                if path.is_file() and path.name != ".identity-writer.lock":
                    make_directory((destination / path.relative_to(location)).parent, mode=0o700)
                    write(destination / path.relative_to(location), path.read_bytes(), 0o600)


def apply(package, target, *, writers_stopped=False, python=sys.executable, data_dir=None, fail_after=None):
    if not writers_stopped:
        raise ValueError("Explicit stopped writers required")
    package, target = Path(package).resolve(), Path(target).absolute()
    if package == target.resolve() or target.resolve().is_relative_to(package) or package.is_relative_to(target.resolve()):
        raise ValueError("Source/target overlap")
    for name in (*SWITCH_FILES, "release.json", "deployment-state.json"):
        safe_path(target, name)
    data = paths(target, data_dir).data
    if target.resolve().is_relative_to(data.resolve()) or package.is_relative_to(data.resolve()):
        raise ValueError("Data/code paths overlap")
    safe_path(data, "identity-v2.json")
    manifest = preflight(package, target, python, data_dir)
    if not data.exists():
        raise ValueError("Initialize a separate protected data directory before install")
    module = core_module(Path(__file__).resolve().parents[1], "identity_store")
    with module.WriterLock(data):
        current_data = check_configuration(target, data)
        if current_data["schema_version"] not in manifest["compatible_schemas"]:
            raise ValueError("Incompatible data schema")
        journal_path = safe_path(target, "deployment-state.json")
        if journal_path.exists():
            transaction = read(journal_path)
            if transaction.get("stage") not in TERMINAL or transaction.get("entry_pending"):
                raise ValueError("Previous switch incomplete; rollback first")
        if (target / "app/client/server.py").exists():
            if (target / "release.json").exists():
                verify_package(target, installed=True)
            else:
                tree = ast.parse((target / "app/client/core/identity_store.py").read_text(encoding="utf-8-sig"))
                if not any(isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SCHEMA" for t in node.targets)
                           and isinstance(node.value, ast.Constant) and node.value.value == 2 for node in tree.body):
                    raise ValueError("Legacy baseline is not schema 2 compatible")
        for name in (*SWITCH_FILES, "release.json"):
            path = safe_path(target, name)
            if path.exists() and not path.is_file():
                raise ValueError("Nonregular destination")
        if (target / "release.json").exists() and read(target / "release.json") == manifest:
            verify_package(target, installed=True)
            return {"stage": "complete", "unchanged": True, "release_id": manifest["release_id"]}
        backup_id = uuid.uuid4().hex
        backup = safe_path(paths(target, data).backups, backup_id)
        make_directory(backup, mode=0o700)
        previous = {}
        for name in (*SWITCH_FILES, "release.json"):
            path = target / name
            if path.exists():
                content = path.read_bytes()
                previous[name] = digest(content)
                write(backup / "code" / name, content, 0o600)
        private_snapshot(target, backup, data)
        journal = {"stage": "prepared", "backup_id": backup_id, "previous_files": previous,
                   "compatible_schemas": [2], "new_release_id": manifest["release_id"]}
        journal_save(journal_path, journal)
        if fail_after == "prepared":
            raise OSError("Simulated interruption")
        journal["stage"] = "copying"
        journal_save(journal_path, journal)
        for index, name in enumerate(FILES):
            content = safe_path(package, name).read_bytes()
            if digest(content) != manifest["files"][name]:
                raise ValueError("Staged package changed during switch")
            write(safe_path(target, name), content)
            if fail_after == "copying" and index == 0:
                raise OSError("Simulated interruption")
        for name in RETIRED_FILES:
            path = safe_path(target, name)
            if path.exists():
                path.unlink()
                sync_directory(path.parent)
        if fail_after == "retired":
            raise OSError("Simulated interruption after retirement")
        write(safe_path(target, "release.json"), (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode(), 0o644)
        verify_package(target, installed=True)
        journal["stage"] = "complete"
        journal_save(journal_path, journal)
        return {"stage": "complete", "release_id": manifest["release_id"]}


def verify_restored(target, previous, *, include_entry=False):
    """Check all previous public bytes/absences before releasing the old entry."""
    for name in (*SWITCH_FILES, "release.json"):
        if name == ENTRY and not include_entry:
            continue
        path = safe_path(target, name)
        if name in previous:
            if digest(path.read_bytes()) != previous[name]:
                raise ValueError("Restored code differs from backup")
        elif path.exists():
            raise ValueError("Unexpected restored code file")


def restore_entry(target, backup, previous):
    path = safe_path(target, ENTRY)
    if ENTRY in previous:
        write(path, safe_path(backup, "code/" + ENTRY).read_bytes())
    elif path.exists():
        path.unlink()
        sync_directory(path.parent)


def rollback(target, *, writers_stopped=False, data_dir=None, fail_after=None):
    if not writers_stopped:
        raise ValueError("Explicit stopped writers required")
    target = Path(target).absolute()
    for name in (*SWITCH_FILES, "release.json", "deployment-state.json"):
        safe_path(target, name)
    data = paths(target, data_dir).data
    if target.resolve().is_relative_to(data.resolve()):
        raise ValueError("Data/code paths overlap")
    safe_path(data, "identity-v2.json")
    module = core_module(Path(__file__).resolve().parents[1], "identity_store")
    with module.WriterLock(data):
        journal_path = safe_path(target, "deployment-state.json")
        journal = read(journal_path)
        if journal.get("stage") == "rolled_back" and not journal.get("entry_pending"):
            return {"stage": "rolled_back", "unchanged": True}
        if not re.fullmatch(r"[0-9a-f]{32}", journal.get("backup_id", "")):
            raise ValueError("Invalid backup identity")
        value = check_configuration(target, data)
        if value["schema_version"] not in journal.get("compatible_schemas", []):
            raise ValueError("Rollback data incompatible; preserve latest data")
        previous = journal["previous_files"]
        if not isinstance(previous, dict) or not set(previous) <= set(SWITCH_FILES) | {"release.json"}:
            raise ValueError("Invalid rollback file list")
        if paths(target, data).external and "app/client/core/runtime_paths.py" not in previous:
            raise ValueError("Reverse runtime relocation before rolling back to pre-M05 code")
        backup = safe_path(paths(target, data).backups, journal["backup_id"])
        # Verify every backup before the first restoration write.
        for name, expected in previous.items():
            if digest(safe_path(backup, "code/" + name).read_bytes()) != expected:
                raise ValueError("Backup hash mismatch")
        for name in (*SWITCH_FILES, "release.json"):
            safe_path(target, name)
        if journal.get("stage") == "rolled_back":
            # A crash between committing the restored set and releasing its entry
            # must remain recoverable. Never assume a terminal marker means that
            # the old entry has already been published.
            verify_restored(target, previous)
            entry = target / ENTRY
            if entry.is_file() and entry.read_bytes() == ROLLBACK_FENCE:
                restore_entry(target, backup, previous)
            verify_restored(target, previous, include_entry=True)
            journal["entry_pending"] = False
            journal_save(journal_path, journal)
            return {"stage": "rolled_back"}
        journal["stage"] = "rolling_back"
        journal["entry_pending"] = True
        journal_save(journal_path, journal)
        write(safe_path(target, ENTRY), ROLLBACK_FENCE)
        for index, name in enumerate(name for name in (*SWITCH_FILES, "release.json") if name != ENTRY):
            path = safe_path(target, name)
            if name in previous:
                write(path, safe_path(backup, "code/" + name).read_bytes())
            elif path.exists():
                path.unlink()  # Only fixed public code paths; never private directories.
                sync_directory(path.parent)
            if fail_after == "rolling_back" and index == 0:
                raise OSError("Simulated interruption")
        verify_restored(target, previous)
        if fail_after == "rollback_restored":
            raise OSError("Simulated interruption")
        # Commit a fully checked code set before publishing a legacy entry that
        # cannot read the journal. Until then, even direct server.py is fenced.
        journal["stage"] = "rolled_back"
        journal_save(journal_path, journal)
        if fail_after == "rollback_committed":
            raise OSError("Simulated interruption")
        restore_entry(target, backup, previous)
        verify_restored(target, previous, include_entry=True)
        journal["entry_pending"] = False
        journal_save(journal_path, journal)
        return {"stage": "rolled_back"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("build", "verify", "preflight", "apply", "rollback", "recover"))
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--ref")
    parser.add_argument("--package", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "build":
            if not args.ref or args.output is None:
                parser.error("build requires --ref and --output")
            value = build(args.source, args.ref, args.output)
        elif args.action == "verify":
            value = verify_package(args.package)
        elif args.action == "preflight":
            value = preflight(args.package, args.target, args.python, args.data_dir)
        elif args.action == "apply":
            value = apply(args.package, args.target, writers_stopped=args.writers_stopped, python=args.python, data_dir=args.data_dir)
        else:
            # Recovery deliberately restores previous code, never an old dataset.
            value = rollback(args.target, writers_stopped=args.writers_stopped, data_dir=args.data_dir)
        print(json.dumps({k: v for k, v in value.items() if k in {"release_id", "source_sha", "built_at", "schema_version", "stage", "unchanged"}}, sort_keys=True))
        return 0
    except Exception:
        print("Release operation failed; details suppressed. Preserve data and inspect the protected transaction state.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
