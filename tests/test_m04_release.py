"""Offline release rehearsals. Synthetic data only; never run installers/services."""
from __future__ import annotations

from contextlib import ExitStack
import copy
import hashlib
import importlib.util
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
SYNTHETIC_KEY = "synthetic-release-key"
SYNTHETIC_SESSION = "synthetic-release-session"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


release = module("release", ROOT / "deploy/release.py")
diagnostics = module("sve_release_diagnostics_test", ROOT / "deploy/diagnostics.py")


def production_hashes():
    paths = list((ROOT / "app/client/data").rglob("*")) + list((ROOT / "capture").glob("*.log"))
    paths.append(ROOT / "config/settings.json")
    return {str(p): hashlib.sha256(p.read_bytes()).digest() for p in paths if p.is_file() and p.name != ".identity-writer.lock"}


BEFORE = production_hashes()


def tearDownModule():
    if BEFORE != production_hashes():
        raise AssertionError("Production data/log files changed")


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sve-m04-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "installed"
        self.package = self.root / "package"
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        self.guards.enter_context(patch.dict(os.environ, {"SVE_APP_KEY": SYNTHETIC_KEY,
                                                       "SVE_DATA_DIR": str(self.target / "app/client/data")}))
        for obj, name in ((urllib.request, "urlopen"), (socket.socket, "connect")):
            mock = self.guards.enter_context(patch.object(obj, name, side_effect=AssertionError("Unexpected external action")))
            self.addCleanup(mock.assert_not_called)
        original_start = threading.Thread.start
        def thread_start(thread):
            # Windows subprocess pipes need stdlib reader threads; business
            # background threads remain forbidden throughout these rehearsals.
            if getattr(thread._target, "__module__", "") == "subprocess":
                return original_start(thread)
            raise AssertionError("Unexpected business thread")
        self.guards.enter_context(patch.object(threading.Thread, "start", autospec=True, side_effect=thread_start))
        self.store_module = release.core_module(ROOT, "identity_store")
        self.data = self.target / "app/client/data"
        self.data.mkdir(parents=True)
        with self.store_module.WriterLock(self.data):
            self.store = self.store_module.IdentityStore(self.data)
            self.value = self.store.load(initialize=True)
            self.value["accounts"]["10001"] = {
                "account_id": "10001", "uid": 10001, "name": "test-M01", "nickname": "test-M01",
                "sessionid": SYNTHETIC_SESSION, "deviceid": "synthetic-release-device",
                "user_agent": "test-agent", "version": 1, "session_version": 1, "profile_version": 1,
                "legacy_names": []}  # M01 data deliberately has no profile_updated_at.
            self.value["active_account_id"] = "10001"
            row = {"account_id": "10001", "account": "10001", "uid": 10001,
                   "tournament_id": "test-event", "name": "test-event", "enabled": False, "logs": []}
            self.value["subscriptions"]["10001|test-event"] = dict(row)
            self.value["records"]["10001|test-event"] = dict(row, status="已报名")
            self.store.save(self.value)
        self.put(self.target, "config/settings.json", b'{"channels": {}}\n', private=True)
        self.put(self.target, "capture/session.json", b'synthetic-preserved-session', private=True)
        self.put(self.target, "capture/agent_stdout.log", b'synthetic-preserved-log', private=True)
        self.put(self.target, "app/client/server.py", b'# synthetic M01 baseline\n')
        self.put(self.target, "app/client/core/identity_store.py", b'SCHEMA = 2\n')
        self.put(self.target, "app/client/data/areas.json", b'{"test-area": []}\n')
        self.manifest = self.candidate(self.package)

    def put(self, root, name, content, private=False):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if private:
            path.chmod(0o600)

    def candidate(self, destination):
        manifest = {"manifest_version": 1, "release_id": "sve-" + "a" * 12 + "-20261105T000000Z",
                    "source_sha": "a" * 40, "built_at": "2026-11-05T00:00:00+00:00",
                    "schema_version": 2, "compatible_schemas": [2], "files": {}, "excluded_files": []}
        for name in release.FILES:
            data = (ROOT / name).read_bytes()
            if name.endswith((".sh", ".service", ".timer")):
                data = data.replace(b"\r\n", b"\n")
            self.put(destination, name, data)
            manifest["files"][name] = release.digest(data)
        release.write_json(destination / "release.json", manifest)
        return manifest

    def private_hashes(self):
        return {str(p.relative_to(self.target)): release.digest(p.read_bytes())
                for directory in (self.data, self.target / "config", self.target / "capture")
                for p in directory.rglob("*") if p.is_file() and p.name not in {"areas.json", ".identity-writer.lock", "settings.example.json", "secrets.env.example"}}

    def switch(self, **kwargs):
        # Other tests exercise the real isolated candidate import; keep file/lock
        # fault tests independent of interpreter dependencies and subprocess I/O.
        with patch.object(release, "preflight", side_effect=lambda package, *a: release.verify_package(package)):
            return release.apply(self.package, self.target, writers_stopped=True, **kwargs)

    def test_real_preflight_import_is_isolated_and_missing_interpreter_fails(self):
        before = self.private_hashes()
        release.preflight(self.package, self.target)
        self.assertEqual(before, self.private_hashes())
        self.assertFalse(list(self.package.rglob("__pycache__")))
        with self.assertRaises(OSError):
            release.preflight(self.package, self.target, self.root / "missing-python")

    def test_missing_dependency_fails_before_any_target_changes(self):
        before = self.private_hashes()
        with patch.object(subprocess, "run", return_value=Mock(returncode=1)):
            with self.assertRaises(ValueError):
                release.apply(self.package, self.target, writers_stopped=True)
        self.assertEqual(before, self.private_hashes())
        self.assertFalse((self.target / "deployment-state.json").exists())

    def test_repeated_upgrade_preserves_data_and_is_idempotent(self):
        before = self.private_hashes()
        self.assertEqual("complete", self.switch()["stage"])
        self.assertTrue(self.switch()["unchanged"])
        self.assertEqual(before, self.private_hashes())
        self.assertEqual(self.manifest, release.verify_package(self.target, installed=True))
        self.assertEqual(1, len(list((self.target / ".deployment-backups").iterdir())))
        self.assertNotEqual(b'# synthetic M01 baseline\n', (self.target / "app/client/server.py").read_bytes())

    def test_bad_hash_missing_extra_file_and_symlink_fail_before_write(self):
        for defect in ("bad_hash", "missing", "extra"):
            with self.subTest(defect=defect):
                candidate = self.root / defect
                self.candidate(candidate)
                path = candidate / "app/client/server.py"
                if defect == "bad_hash":
                    path.write_bytes(b'# corrupt')
                elif defect == "missing":
                    path.unlink()
                else:
                    self.put(candidate, "config/settings.json", b'{}')
                before = self.private_hashes()
                with self.assertRaises(ValueError if defect != "missing" else FileNotFoundError):
                    release.apply(candidate, self.target, writers_stopped=True)
                self.assertEqual(before, self.private_hashes())
                self.assertFalse((self.target / "deployment-state.json").exists())
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda p: p == self.package / "app"):
            with self.assertRaises(ValueError):
                release.verify_package(self.package)

    def test_writer_lock_and_explicit_maintenance_required(self):
        with self.assertRaises(ValueError):
            release.apply(self.package, self.target)
        with self.store_module.WriterLock(self.data):
            with self.assertRaises(Exception):
                self.switch()
        self.assertFalse((self.target / "deployment-state.json").exists())

    def test_overlap_missing_configuration_and_missing_dataset_refuse_preflight(self):
        for package, data in ((self.target, self.data), (self.package, self.target), (self.package, self.root)):
            with self.assertRaises(ValueError):
                release.apply(package, self.target, writers_stopped=True, data_dir=data)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                release.preflight(self.package, self.target)
        (self.data / "identity-v2.json").unlink()
        with self.assertRaises(ValueError):
            release.preflight(self.package, self.target)
        self.assertFalse((self.target / "deployment-state.json").exists())

    def test_interrupted_upgrade_is_fenced_and_recovers_previous_code(self):
        for stage in ("prepared", "copying"):
            before = self.private_hashes()
            with self.assertRaises(OSError):
                self.switch(fail_after=stage)
            with self.assertRaises(ValueError):
                release.core_module(ROOT, "releaseinfo").snapshot(self.target)
            with self.assertRaises(ValueError):
                self.switch()
            release.rollback(self.target, writers_stopped=True)
            self.assertEqual(b'# synthetic M01 baseline\n', (self.target / "app/client/server.py").read_bytes())
            self.assertFalse((self.target / "release.json").exists())
            self.assertEqual(before, self.private_hashes())

    def test_rollback_keeps_new_business_data_and_can_resume_interruption(self):
        self.switch()
        with self.store_module.WriterLock(self.data):
            value = self.store.load()
            value["pending"].append({"category": "test-new-business-data"})
            value["accounts"]["10001"].update(nickname="test-after-release", profile_updated_at=1800000000)
            value["records"]["10001|test-new-event"] = dict(value["records"]["10001|test-event"], tournament_id="test-new-event")
            self.store.save(value)
        before = self.private_hashes()
        with self.assertRaises(OSError):
            release.rollback(self.target, writers_stopped=True, fail_after="rolling_back")
        self.assertEqual("rolling_back", release.read(self.target / "deployment-state.json")["stage"])
        release.rollback(self.target, writers_stopped=True)
        self.assertTrue(release.rollback(self.target, writers_stopped=True)["unchanged"])
        self.assertEqual(before, self.private_hashes())
        self.assertEqual([{"category": "test-new-business-data"}], self.store.load()["pending"])
        self.assertEqual("test-after-release", self.store.load()["accounts"]["10001"]["nickname"])
        self.assertIn("10001|test-new-event", self.store.load()["records"])

    def assert_installed_entry_blocks_start(self):
        # Execute the file that the existing unit actually invokes, rather than
        # testing releaseinfo.snapshot() (which the legacy fixture never calls).
        spec = importlib.util.spec_from_file_location("sve_rollback_entry_test", self.target / "app/client/server.py")
        entry = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = entry
        # If the synthetic legacy entry leaks through, keep execution isolated.
        sys.path.insert(0, str(ROOT / "app/client/core"))
        for name in ("sveapi", "scheduler", "weblogin", "agentlink", "accountguard"):
            importlib.import_module(name)
        with patch.dict(sys.modules, {"identity_store": self.store_module}):
            try:
                spec.loader.exec_module(entry)
            except SystemExit as error:
                self.assertIn("rollback", str(error).lower())
                return
            with patch.object(entry, "load_all"), patch.object(entry, "log"), \
                    patch.object(entry, "restore_schedulers") as restore, \
                    patch.object(entry.app, "run") as http:
                entry.main()
                self.assertFalse(restore.called or http.called, "Legacy M01 entry bypassed rolling_back and started execution")

    def test_legacy_M01_entry_cannot_start_after_partial_rollback(self):
        legacy = (ROOT / "tests/fixtures/legacy_unfenced_entry.py").read_bytes()
        self.put(self.target, "app/client/server.py", legacy)
        self.switch()
        before = self.private_hashes()
        original_write = release.write
        def interrupted(path, content, *args, **kwargs):
            original_write(path, content, *args, **kwargs)
            # The old algorithm has restored server.py before this core file.
            if path == self.target / "app/client/core/identity_store.py":
                raise OSError("Simulated failure after restoring legacy core")
        with patch.object(release, "write", side_effect=interrupted):
            with self.assertRaises(OSError):
                release.rollback(self.target, writers_stopped=True)
        self.assertEqual("rolling_back", release.read(self.target / "deployment-state.json")["stage"])
        self.assert_installed_entry_blocks_start()
        self.assertEqual(before, self.private_hashes())
        release.rollback(self.target, writers_stopped=True)
        self.assertEqual(legacy, (self.target / "app/client/server.py").read_bytes())

    def test_rollback_entry_stays_fenced_at_restore_and_commit_boundaries(self):
        legacy = (ROOT / "tests/fixtures/legacy_unfenced_entry.py").read_bytes()
        self.put(self.target, "app/client/server.py", legacy)
        for phase in ("rolling_back", "rollback_restored", "rollback_committed"):
            with self.subTest(phase=phase):
                self.switch()
                before = self.private_hashes()
                with self.assertRaises(OSError):
                    release.rollback(self.target, writers_stopped=True, fail_after=phase)
                state = release.read(self.target / "deployment-state.json")
                self.assertTrue(state["entry_pending"])
                self.assertEqual("rolled_back" if phase == "rollback_committed" else "rolling_back", state["stage"])
                self.assert_installed_entry_blocks_start()
                with self.assertRaises(ValueError):
                    release.core_module(ROOT, "releaseinfo").snapshot(self.target)
                with self.assertRaises(ValueError):
                    self.switch()
                release.rollback(self.target, writers_stopped=True)
                self.assertEqual(legacy, (self.target / "app/client/server.py").read_bytes())
                self.assertFalse(release.read(self.target / "deployment-state.json")["entry_pending"])
                self.assertEqual(before, self.private_hashes())

    def test_committed_rollback_rechecks_restored_files_before_releasing_entry(self):
        self.switch()
        with self.assertRaises(OSError):
            release.rollback(self.target, writers_stopped=True, fail_after="rollback_committed")
        core = self.target / "app/client/core/identity_store.py"
        original = core.read_bytes()
        core.write_bytes(b'# synthetic unexpected drift\n')
        with self.assertRaises(ValueError):
            release.rollback(self.target, writers_stopped=True)
        self.assert_installed_entry_blocks_start()
        core.write_bytes(original)
        release.rollback(self.target, writers_stopped=True)
        self.assertEqual(b'# synthetic M01 baseline\n', (self.target / "app/client/server.py").read_bytes())

    def test_interruption_after_entry_publication_leaves_complete_previous_code(self):
        self.switch()
        original_write = release.write
        def after_publication(path, content, *args, **kwargs):
            original_write(path, content, *args, **kwargs)
            if path == self.target / "app/client/server.py" and content != release.ROLLBACK_FENCE:
                raise OSError("Simulated failure after old entry publication")
        with patch.object(release, "write", side_effect=after_publication):
            with self.assertRaises(OSError):
                release.rollback(self.target, writers_stopped=True)
        state = release.read(self.target / "deployment-state.json")
        self.assertEqual("rolled_back", state["stage"])
        release.verify_restored(self.target, state["previous_files"], include_entry=True)
        release.rollback(self.target, writers_stopped=True)
        self.assertFalse(release.read(self.target / "deployment-state.json")["entry_pending"])

    def test_versioned_release_to_release_rollback_restores_exact_previous_manifest(self):
        self.switch()
        before = self.private_hashes()
        next_package = self.root / "package-two"
        next_manifest = self.candidate(next_package)
        next_manifest["release_id"] = "sve-" + "b" * 12 + "-20261105T010000Z"
        next_manifest["source_sha"] = "b" * 40
        release.write_json(next_package / "release.json", next_manifest)
        self.package = next_package
        self.switch()
        self.assertEqual(next_manifest, release.verify_package(self.target, installed=True))
        release.rollback(self.target, writers_stopped=True)
        self.assertEqual(self.manifest, release.verify_package(self.target, installed=True))
        self.assertEqual(before, self.private_hashes())

    def test_incompatible_schema_and_corrupt_backup_refuse_rollback(self):
        self.switch()
        journal = release.read(self.target / "deployment-state.json")
        journal["compatible_schemas"] = [1]
        release.journal_save(self.target / "deployment-state.json", journal)
        before = (self.target / "app/client/server.py").read_bytes()
        with self.assertRaises(ValueError):
            release.rollback(self.target, writers_stopped=True)
        self.assertEqual(before, (self.target / "app/client/server.py").read_bytes())
        journal["compatible_schemas"] = [2]
        release.journal_save(self.target / "deployment-state.json", journal)
        backup = self.target / ".deployment-backups" / journal["backup_id"] / "code/app/client/server.py"
        backup.write_bytes(b'# bad backup')
        with self.assertRaises(ValueError):
            release.rollback(self.target, writers_stopped=True)
        self.assertEqual(before, (self.target / "app/client/server.py").read_bytes())

    def test_default_diagnostic_is_read_only_and_reports_only_safe_summary(self):
        self.switch()
        before = {str(p): p.read_bytes() for p in self.target.rglob("*") if p.is_file()}
        result = diagnostics.inspect(self.target)
        self.assertTrue(result["ok"])
        self.assertEqual({"accounts": 1, "subscriptions": 1, "records": 1}, result["counts"])
        self.assertNotIn("session", json.dumps(result))
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.target.rglob("*") if p.is_file()})
        with self.assertRaises(ValueError):
            diagnostics.inspect(self.target, health_url="https://sve.example.com/api/state")

    def test_running_release_snapshot_is_pinned_when_disk_manifest_changes(self):
        self.switch()
        info = release.core_module(ROOT, "releaseinfo")
        running = info.snapshot(self.target)
        manifest = release.read(self.target / "release.json")
        manifest["release_id"] = "sve-" + "b" * 12 + "-20261105T010000Z"
        manifest["source_sha"] = "b" * 40
        release.write_json(self.target / "release.json", manifest)
        self.assertNotEqual(running, info.snapshot(self.target))
        self.assertEqual(self.manifest["release_id"], running["release_id"])

    def test_external_upgrade_backup_and_rollback_keep_latest_data(self):
        import runtime_migrate
        self.switch()
        external = self.root / "external-private"
        runtime_migrate.relocate(self.target, external, writers_stopped=True)
        self.guards.enter_context(patch.dict(os.environ, {"SVE_DATA_DIR": str(external)}))
        before = (external / "identity-v2.json").read_bytes()
        next_package = self.root / "external-package"
        manifest = self.candidate(next_package)
        manifest.update(release_id="sve-" + "b" * 12 + "-20261105T010000Z", source_sha="b" * 40)
        release.write_json(next_package / "release.json", manifest)
        self.package = next_package
        self.switch(data_dir=external)
        self.assertFalse(any(p.is_file() for p in (self.target / ".deployment-backups").rglob("*")))
        transaction = release.read(self.target / "deployment-state.json")
        backup = external / "deployment-backups" / transaction["backup_id"]
        self.assertTrue((backup / "private/data/identity-v2.json").is_file())
        self.assertFalse((backup / "private/data/deployment-backups").exists())
        release.rollback(self.target, data_dir=external, writers_stopped=True)
        self.assertEqual(before, (external / "identity-v2.json").read_bytes())
        self.assertTrue(diagnostics.inspect(self.target, external)["ok"])
        self.assertTrue(diagnostics.storage_probe(self.target, external)["ok"])
        self.assertFalse(list(external.rglob(".sve-permission-*")))

    def test_m04_manifest_remains_valid_but_not_a_new_candidate(self):
        manifest = dict(self.manifest, files=dict(self.manifest["files"]))
        for name in release.M05_ADDITIONS | release.M06_ADDITIONS:
            del manifest["files"][name]
            (self.package / name).unlink()
        release.write_json(self.package / "release.json", manifest)
        self.assertEqual(manifest, release.verify_package(self.package))
        with self.assertRaises(ValueError):
            release.preflight(self.package, self.target)

    def test_m05_manifest_remains_verifiable_for_rollback(self):
        manifest = dict(self.manifest, files=dict(self.manifest["files"]))
        for name in release.M06_ADDITIONS:
            del manifest["files"][name]
            (self.package / name).unlink()
        release.write_json(self.package / "release.json", manifest)
        self.assertEqual(manifest, release.verify_package(self.package))
        with self.assertRaises(ValueError):
            release.preflight(self.package, self.target)

    def test_manifest_paths_and_schema_cannot_change_contract(self):
        for change in ({"schema_version": 1}, {"compatible_schemas": [1, 2]},
                       {"files": {"../escape": "invalid"}}):
            manifest = dict(self.manifest, **change)
            release.write_json(self.package / "release.json", manifest)
            with self.assertRaises(ValueError):
                release.verify_package(self.package)

    def test_build_uses_commit_not_dirty_tree_and_normalizes_linux_files(self):
        source, output = self.root / "git-source", self.root / "built"
        self.candidate(source)
        # Generated manifest must never be an input to the committed release.
        (source / "release.json").unlink()
        self.put(source, "deploy/verify.sh", b'#!/bin/bash\r\nexit 0\r\n')
        self.put(source, "android/test-source.txt", b'intentionally excluded\n')
        commands = (("init", "-q"), ("add", "."), ("-c", "user.name=SVE Maintainer", "-c", "user.email=maintainer@example.com", "commit", "-qm", "synthetic release fixture"))
        for args in commands:
            subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
        committed = subprocess.check_output(["git", "-C", str(source), "show", "HEAD:app/client/server.py"])
        self.put(source, "app/client/server.py", b'# dirty worktree must not ship\n')
        manifest = release.build(source, "HEAD", output)
        self.assertEqual(committed, (output / "app/client/server.py").read_bytes())
        self.assertNotIn(b'\r', (output / "deploy/verify.sh").read_bytes())
        self.assertIn("android/test-source.txt", manifest["excluded_files"])
        self.assertFalse((output / "android").exists())
        release.verify_package(output)
        with self.assertRaises(ValueError):
            release.build(source, "HEAD", output)


if __name__ == "__main__":
    unittest.main()
