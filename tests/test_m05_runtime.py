"""Synthetic offline relocation/recovery and external runtime persistence."""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
sys.path.insert(0, str(ROOT / "app/client/core"))
import release
import runtime_migrate as migration
from runtime_paths import RuntimePaths


SYNTHETIC_KEY = "synthetic-private-key"
SYNTHETIC_PUSH_VALUE = "synthetic-token"
SYNTHETIC_BACKUP_VALUE = "synthetic-backup-token"
SYNTHETIC_SESSION = "synthetic-session"
SYNTHETIC_OLD_SESSION = "synthetic-legacy-session"
SYNTHETIC_LOGIN = "synthetic-login-updated"
SYNTHETIC_ENV_KEY = "synthetic-env-key"


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sve-m05-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.code = self.base / "code"
        self.data = self.code / "app/client/data"
        self.external = self.base / "private"
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        self.guards.enter_context(patch.dict(os.environ, {}, clear=False))
        for key in ("SVE_SETTINGS_FILE", "SVE_DATA_DIR", "SVE_APP_KEY"):
            os.environ.pop(key, None)
        for obj, name in ((socket.socket, "connect"), (urllib.request, "urlopen"),
                          (subprocess, "Popen"), (threading.Thread, "start")):
            guard = self.guards.enter_context(patch.object(obj, name, side_effect=AssertionError("Unexpected external action")))
            self.addCleanup(guard.assert_not_called)
        self.storemod = release.core_module(ROOT, "identity_store")
        self.data.mkdir(parents=True, mode=0o700)
        with self.storemod.WriterLock(self.data):
            store = self.storemod.IdentityStore(self.data)
            value = store.load(initialize=True)
            value["accounts"]["10001"] = dict(account_id="10001", uid=10001, nickname="test-account",
                sessionid=SYNTHETIC_SESSION, deviceid="synthetic-device", user_agent="test-agent",
                version=1, session_version=1, profile_version=1)
            value["active_account_id"] = "10001"
            row = dict(account_id="10001", uid=10001, tournament_id="test-event", enabled=False)
            value["subscriptions"]["10001|test-event"] = dict(row)
            value["records"]["10001|test-event"] = dict(row)
            store.save(value)
        for name, content in {
            "config/settings.json": {"app_key": SYNTHETIC_KEY, "channels": {"pushplus": {"token": SYNTHETIC_PUSH_VALUE}}},
            "app/client/data/notify.json": {"pushplus": {"enabled": True}},
            "capture/session.json": {"sessionid": SYNTHETIC_OLD_SESSION},
            ".deployment-backups/test-backup/private/data/notify.json": {"token": SYNTHETIC_BACKUP_VALUE},
            "app/client/data/areas.json": {"provinces": ["test-area"]},
        }.items():
            release.make_directory((self.code / name).parent, mode=0o700)
            release.write_json(self.code / name, content)
        release.write_json(self.data / "identity-migration.json", {"stage": "complete", "synthetic": True})
        release.write(self.data / "client.log", b"synthetic-log\n", 0o600)
        release.write(self.code / "capture/watchdog.log", b"synthetic-watchdog\n", 0o600)
        self.maintenance = {
            "session.json.bak": b"synthetic-session-backup\n",
            "nested/settings.json.bak": b"synthetic-signing-backup\n",
            "nested/capture.dump": b"synthetic-capture-backup\n",
            "README.md": b"synthetic-private-recovery-notes\n",
        }
        for name, content in self.maintenance.items():
            path = self.code / ".maintenance" / name
            release.make_directory(path.parent, mode=0o700)
            release.write(path, content, 0o600)

    def move(self, **kwargs):
        return migration.relocate(self.code, self.external, writers_stopped=True, **kwargs)

    def test_preview_readonly_move_latest_reverse_and_repeat(self):
        pairs = migration.mapping(self.code, self.external)
        for name in self.maintenance:
            self.assertIn((self.code / ".maintenance" / name,
                           self.external / "maintenance-backups" / name), pairs)
        before = {str(p): p.read_bytes() for p in self.code.rglob("*") if p.is_file()}
        self.assertEqual(1, migration.preview(self.code, self.external)["counts"]["accounts"])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.code.rglob("*") if p.is_file()})
        self.assertFalse(self.external.exists())
        with self.assertRaises(RuntimeError):
            RuntimePaths(self.code, self.external).assert_ready()
        self.move()
        for name, content in self.maintenance.items():
            self.assertFalse((self.code / ".maintenance" / name).exists())
            self.assertEqual(content, (self.external / "maintenance-backups" / name).read_bytes())
        self.assertTrue(self.move()["unchanged"])
        self.assertEqual({"stage": "complete", "synthetic": True}, release.read(self.external / "identity-migration.json"))
        self.assertFalse((self.code / "config/settings.json").exists())
        self.assertFalse((self.data / "identity-v2.json").exists())
        with self.assertRaises(Exception):
            self.storemod.IdentityStore(self.data).load(initialize=True)
        with self.assertRaises(RuntimeError):
            RuntimePaths(self.code, self.data).assert_ready()
        RuntimePaths(self.code, self.external).assert_ready()
        with self.storemod.WriterLock(self.external):
            store = self.storemod.IdentityStore(self.external)
            value = store.load()
            value["accounts"]["10001"]["sessionid"] = "synthetic-new-session"
            value["accounts"]["10001"]["nickname"] = "test-new-name"
            store.save(value)
        self.move(reverse=True)
        for name, content in self.maintenance.items():
            self.assertEqual(content, (self.code / ".maintenance" / name).read_bytes())
            self.assertFalse((self.external / "maintenance-backups" / name).exists())
        value = self.storemod.IdentityStore(self.data).load()
        self.assertEqual("synthetic-new-session", value["accounts"]["10001"]["sessionid"])
        self.assertEqual("test-new-name", value["accounts"]["10001"]["nickname"])
        self.move()  # Re-externalizing a previously fenced target is supported.
        self.assertEqual(value, self.storemod.IdentityStore(self.external).load())

    def test_interrupted_forward_and_reverse_are_fenced_and_recoverable(self):
        for direction in (False, True):
            if direction:
                self.move()
            for stage in ("copying", "fenced", "retired"):
                with self.subTest(direction=direction, stage=stage):
                    with self.assertRaises(OSError):
                        self.move(reverse=direction, fail_after=stage)
                    for data in (self.data, self.external):
                        with self.assertRaises(RuntimeError):
                            RuntimePaths(self.code, data).assert_ready()
                    self.move(reverse=direction, recover=True)
                    value = self.storemod.IdentityStore(self.data if direction else self.external).load()
                    self.assertEqual(1, len(value["accounts"]))
                    self.move(reverse=not direction)

    def test_maintenance_copy_corruption_preserves_source_and_recovers(self):
        with self.assertRaises(OSError):
            self.move(fail_after="fenced")
        path = self.external / "maintenance-backups/session.json.bak"
        release.write(path, b"synthetic-corruption\n", 0o600)
        with self.assertRaises(ValueError):
            self.move(recover=True)
        for name, content in self.maintenance.items():
            self.assertEqual(content, (self.code / ".maintenance" / name).read_bytes())
        release.write(path, self.maintenance["session.json.bak"], 0o600)
        self.move(recover=True)
        self.move(reverse=True)
        for name, content in self.maintenance.items():
            self.assertEqual(content, (self.code / ".maintenance" / name).read_bytes())

    def test_old_journals_cannot_silently_skip_maintenance_files(self):
        for name in self.maintenance:
            (self.code / ".maintenance" / name).unlink()
        with self.assertRaises(OSError):
            self.move(fail_after="copying")
        path = self.code / ".maintenance/session.json.bak"
        content = self.maintenance["session.json.bak"]
        release.write(path, content, 0o600)
        journal = (self.code / "runtime-migration.json").read_bytes()
        with self.assertRaises(ValueError):
            self.move(recover=True)
        self.assertEqual(content, path.read_bytes())
        self.assertEqual(journal, (self.code / "runtime-migration.json").read_bytes())
        # Simulate a separate protected offline archive before resuming.
        archived = self.base / "protected-archive/session.json.bak"
        release.make_directory(archived.parent, mode=0o700)
        release.write(archived, path.read_bytes(), 0o600)
        self.assertEqual(content, archived.read_bytes())
        path.unlink()
        self.move(recover=True)
        release.write(path, content, 0o600)
        with self.assertRaises(ValueError):
            self.move()
        self.assertEqual(content, path.read_bytes())

    def test_overlap_conflict_writer_and_bad_copy_are_refused(self):
        with self.assertRaises(ValueError):
            migration.preview(self.code, self.code / "private")
        with self.storemod.WriterLock(self.data):
            with self.assertRaises(Exception):
                self.move()
        self.external.mkdir(mode=0o700, exist_ok=True)
        release.write_json(self.external / "notify.json", {})
        with self.assertRaises(ValueError):
            self.move()
        (self.external / "notify.json").unlink()
        with self.assertRaises(OSError):
            self.move(fail_after="copying")
        with self.assertRaises(Exception):
            self.storemod.IdentityStore(self.data).load(initialize=True)
        release.write_json(self.external / "notify.json", {"corrupt": True})
        # Copying recovery re-copies from the still intact source.
        self.move(recover=True)
        self.assertTrue(release.read(self.external / "notify.json")["pushplus"]["enabled"])

    def test_corrupt_journal_and_destination_do_not_remove_source(self):
        with self.assertRaises(OSError):
            self.move(fail_after="fenced")
        journal_path = self.code / "runtime-migration.json"
        journal = release.read(journal_path)
        original = json.loads(json.dumps(journal))
        journal["files"][0]["source"] = str(self.code / "app/client/server.py")
        release.write_json(journal_path, journal)
        with self.assertRaises(ValueError):
            self.move(recover=True)
        self.assertTrue((self.data / "identity-v2.json").exists())
        release.write_json(journal_path, original)
        path = self.external / "notify.json"
        content = path.read_bytes()
        release.write_json(path, {"corrupt": True})
        with self.assertRaises(ValueError):
            self.move(recover=True)
        self.assertTrue((self.data / "identity-v2.json").exists())
        release.write(path, content, 0o600)
        self.move(recover=True)

    def test_external_paths_key_notifications_areas_and_persistence(self):
        self.move()
        os.environ["SVE_DATA_DIR"] = str(self.external)
        paths = RuntimePaths(self.code)
        self.assertEqual(self.external / "settings.json", paths.settings)
        self.assertEqual(self.external / "logs/client.log", paths.client_log)
        self.assertEqual(self.external / "deployment-backups", paths.backups)
        # Import the real service against external fake storage. No business
        # threads or requests are allowed; load_all only occurs under a writer.
        spec = importlib.util.spec_from_file_location("sve_m05_service", ROOT / "app/client/server.py")
        srv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(srv)
        import sveapi
        with patch.object(sveapi, "_CONFIG_FILE", str(paths.settings)), patch.object(sveapi, "_APP_KEY_CACHE", []):
            self.assertEqual(SYNTHETIC_KEY, sveapi.app_key())
            with patch.dict(os.environ, {"SVE_APP_KEY": SYNTHETIC_ENV_KEY}), patch.object(sveapi, "_APP_KEY_CACHE", []):
                self.assertEqual(SYNTHETIC_ENV_KEY, sveapi.app_key())
        self.assertEqual(SYNTHETIC_PUSH_VALUE, srv.notify_channels()["pushplus"]["token"])
        self.assertTrue(srv.notify_channels()["pushplus"]["enabled"])
        with srv.app.test_client() as client:
            response = client.get("/api/areas")
            self.assertEqual(200, response.status_code)
            self.assertTrue(response.get_json()["provinces"])
        with srv.WriterLock(self.external):
            srv.load_all()
            with srv.LOCK:
                account = srv.STATE["accounts"]["10001"]
                account.sessionid = SYNTHETIC_LOGIN
                srv.STATE["subs"]["10001|test-second"] = dict(account_id="10001", uid=10001,
                    tournament_id="test-second", enabled=False)
                srv.persist_accounts()
                srv.persist_subs()
            srv.load_all()  # Simulate restart, using actual paths and persistence.
            self.assertEqual(SYNTHETIC_LOGIN, srv.STATE["accounts"]["10001"].sessionid)
            self.assertIn("10001|test-second", srv.STATE["subs"])
        srv.save_json(srv.NOTIFY_FILE, {"pushplus": {"enabled": False}})
        self.assertFalse(srv.notify_channels()["pushplus"]["enabled"])
        self.assertEqual(2, len(self.storemod.IdentityStore(self.external).load()["subscriptions"]))
        self.assertFalse((self.external / "areas.json").exists())

    def test_settings_never_fall_back_and_permissions(self):
        self.move()
        self.assertEqual(self.external / "settings.json", release.paths(self.code, self.external).settings)
        release.check_configuration(self.code, self.external)
        if os.name != "nt":
            for path in self.external.rglob("*"):
                self.assertEqual(0, path.stat().st_mode & 0o077, str(path))
        (self.external / "settings.json").unlink()
        with self.assertRaises(ValueError):
            release.check_configuration(self.code, self.external)

    def test_ownership_handoff_on_both_directions_and_recovery(self):
        release.write(self.code / "config/settings.example.json", b"{}\n")
        with patch.object(migration, "service_identity", return_value=(10001, 10001)), \
                patch.object(migration.os, "chown", create=True) as chown:
            for reverse in (False, True):
                for stage in ("copying", "fenced", "retired"):
                    with self.subTest(reverse=reverse, stage=stage):
                        with self.assertRaises(OSError):
                            self.move(reverse=reverse, fail_after=stage, service_user="test-service")
                        paths = {call.args[0] for call in chown.call_args_list}
                        self.assertIn(self.code / "runtime-migration.json", paths)
                        self.assertIn(self.external / ".identity-writer.lock", paths)
                        self.assertIn(self.code / ".maintenance", paths)
                        if reverse:
                            self.assertIn(self.code / ".maintenance/session.json.bak", paths)
                        else:
                            self.assertIn(self.external / "maintenance-backups/session.json.bak", paths)
                        self.assertNotIn(self.code, paths)
                        self.assertNotIn(self.data / "areas.json", paths)
                        self.assertNotIn(self.code / "config/settings.example.json", paths)
                        self.move(reverse=reverse, recover=True, service_user="test-service")
                        self.move(reverse=not reverse, service_user="test-service")
                        chown.reset_mock()
                self.move(reverse=reverse, service_user="test-service")

    def test_completed_handoff_failure_can_be_retried_without_copying(self):
        with patch.object(migration, "service_identity", return_value=(10001, 10001)):
            with patch.object(migration.os, "chown", create=True, side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    self.move(service_user="test-service")
            self.assertEqual("complete", release.read(self.code / "runtime-migration.json")["stage"])
            before = (self.external / "identity-v2.json").read_bytes()
            with patch.object(migration.os, "chown", create=True) as chown:
                self.assertTrue(self.move(service_user="test-service")["unchanged"])
                self.assertIn(self.code / "runtime-migration.json", {c.args[0] for c in chown.call_args_list})
            self.assertEqual(before, (self.external / "identity-v2.json").read_bytes())

    @unittest.skipUnless(os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0,
                         "Requires an isolated POSIX root runner")
    def test_root_owned_code_nonroot_service_can_read_and_write_runtime(self):
        import pwd
        account = pwd.getpwnam("nobody")
        self.base.chmod(0o755)
        self.code.chmod(0o755)

        def check_as_service(active, complete):
            pid = os.fork()
            if pid == 0:
                try:
                    os.setgroups([])
                    os.setgid(account.pw_gid)
                    os.setuid(account.pw_uid)
                    release.read(self.code / "runtime-migration.json")
                    if complete:
                        RuntimePaths(self.code, active).assert_ready()
                        with self.storemod.WriterLock(active):
                            self.storemod.IdentityStore(active).load()
                        probe = active / "test-permission-probe"
                        probe.write_bytes(b"synthetic")
                        probe.unlink()
                    else:
                        try:
                            RuntimePaths(self.code, active).assert_ready()
                        except RuntimeError:
                            pass
                        else:
                            raise AssertionError("Incomplete operation did not fence startup")
                    if os.access(self.code, os.W_OK):
                        raise AssertionError("Code root became writable")
                    os._exit(0)
                except BaseException:
                    os._exit(1)
            self.assertEqual(0, os.waitpid(pid, 0)[1])

        for reverse in (False, True):
            active = self.data if reverse else self.external
            for stage in ("copying", "fenced", "retired"):
                with self.assertRaises(OSError):
                    self.move(reverse=reverse, service_user="nobody", fail_after=stage)
                check_as_service(active, False)
                self.move(reverse=reverse, service_user="nobody", recover=True)
                check_as_service(active, True)
                self.assertEqual(0, self.code.stat().st_uid)
                self.assertEqual(account.pw_uid, (active / "identity-v2.json").stat().st_uid)
                self.move(reverse=not reverse, service_user="nobody")
            self.move(reverse=reverse, service_user="nobody")


if __name__ == "__main__":
    unittest.main()
