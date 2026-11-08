"""Isolated M00 API and installer regression tests. Never start the application."""
from __future__ import annotations

from contextlib import ExitStack
import copy
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import uuid
import unittest
from unittest.mock import Mock, patch
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
SANDBOX = tempfile.TemporaryDirectory(prefix="sve-m00-")
TEST_ROOT = Path(SANDBOX.name)
GUARDS = ExitStack()
SYNTHETIC_KEY = "synthetic-key-for-tests"
GUARDS.enter_context(patch.dict(os.environ, {"SVE_DATA_DIR": str(TEST_ROOT / "import"),
                                           "SVE_APP_KEY": SYNTHETIC_KEY}))
NETWORK = GUARDS.enter_context(patch.object(urllib.request, "urlopen", side_effect=AssertionError("Unexpected network")))
SOCKET = GUARDS.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("Unexpected socket")))
PROCESS = GUARDS.enter_context(patch.object(subprocess, "Popen", side_effect=AssertionError("Unexpected process")))
REAL_THREAD_START = threading.Thread.start
THREAD = GUARDS.enter_context(patch("threading.Thread.start", side_effect=AssertionError("Unexpected thread")))


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def protected_snapshot():
    # Only hashes are kept in memory; no production contents are printed or copied.
    paths = list((ROOT / "app/client/data").rglob("*"))
    paths += [ROOT / "config/settings.json"]
    paths += [ROOT / "capture" / name for name in ("session.json", "agent_stdout.log", "poll.log", "watchdog.log")]
    return {str(path): hashlib.sha256(path.read_bytes()).digest() for path in paths
            if path.is_file() and path.name != ".identity-writer.lock"}


PROTECTED_BEFORE = protected_snapshot()
# SVE_DATA_DIR and network/process guards are installed before importing business code.
srv = load_module("sve_m00_server", "app/client/server.py")
REAL_ACTION = srv.SveClient.action
RESTORE_MISSING = srv.restore_missing_schedulers
copier = load_module("sve_m00_copy", "deploy/copy_runtime.py")
hashes = load_module("sve_m00_hashes", "deploy/data_hashes.py")


def tearDownModule():
    try:
        NETWORK.assert_not_called()
        SOCKET.assert_not_called()
        PROCESS.assert_not_called()
        THREAD.assert_not_called()
        if PROTECTED_BEFORE != protected_snapshot():
            raise AssertionError("Protected production data/log files changed")
    finally:
        GUARDS.close()
        SANDBOX.cleanup()


def fake_account(uid=10001, name=None, session=None):
    name = name or str(uid)
    return srv.Account(name=name, uid=uid, nickname=name,
                       sessionid=session or "synthetic-session-%s" % uid,
                       deviceid="synthetic-device")


class AccountProtectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        for field, filename in {"ACCOUNTS_FILE": "accounts.json", "SUBS_FILE": "subscriptions.json",
                                "RECORDS_FILE": "records.json", "REMOTE_FILE": "remote.json",
                                "LOG_FILE": "client.log", "NOTIFY_FILE": "notify.json",
                                "AREAS_FILE": "areas.json",
                                "AGENT_SETTINGS": "settings.json"}.items():
            self.patches.enter_context(patch.object(srv, field, str(self.data / filename)))
        self.patches.enter_context(patch.object(srv, "DATA_DIR", str(self.data)))
        self.patches.enter_context(srv.WriterLock(self.data))
        self.patches.enter_context(patch.object(srv, "ACCOUNTS_LOADED_OK", True))
        self.patches.enter_context(patch.object(srv, "log"))
        self.restore = self.patches.enter_context(patch.object(srv, "restore_missing_schedulers"))
        self.notify = self.patches.enter_context(patch.object(srv, "send_notify", side_effect=AssertionError("Unexpected notification")))
        self.addCleanup(self.notify.assert_not_called)
        self.dispatch = self.patches.enter_context(patch.object(srv, "dispatch_notification"))
        self.info = self.patches.enter_context(patch.object(srv.SveClient, "info"))
        action = self.patches.enter_context(patch.object(srv.SveClient, "action", side_effect=AssertionError("Unexpected signup")))
        self.addCleanup(action.assert_not_called)
        srv.STATE.update(accounts={a.account_id: a for a in (fake_account(10001 + i) for i in range(14))},
                         active="10001", remote={"enabled": False}, tunnel=None,
                         subs={"10001|event": {"account_id": "10001", "account": "10001", "tournament_id": "event", "uid": 10001}},
                         records={"10001|event": {"account_id": "10001", "account": "10001", "tournament_id": "event", "status": "已报名"}}, schedulers={},
                         scheduler_threads=set(), workers={}, pending=[], tombstones={}, operations={}, login_intents={}, storage_failed=False, log=[])
        store = srv.IdentityStore(self.data)
        store.load(initialize=True)
        self.patches.enter_context(patch.object(srv, "STORE", store))
        srv.persist_accounts()
        srv.persist_subs()
        srv.persist_records()
        self.client = srv.app.test_client()
        srv.PROFILE_JOBS.clear()
        self.client.set_cookie("sve_account_id", "10001")

    def snapshot(self):
        return ({p.name: p.read_bytes() for p in self.data.iterdir() if p.is_file() and p.name != ".identity-writer.lock"},
                {k: a.to_dict() for k, a in srv.STATE["accounts"].items()},
                copy.deepcopy(srv.STATE["subs"]), copy.deepcopy(srv.STATE["records"]), srv.STATE["active"])

    def post(self, path, body):
        response = self.client.post(path, json=body)
        self.assertEqual(200, response.status_code)
        self.assertIn("no-store", response.headers["Cache-Control"])
        return response.get_json()

    def test_settings_and_existing_pages_share_packaged_shell_without_writes(self):
        before = self.snapshot()
        for route in ("/", "/subs", "/settings"):
            with self.client.get(route) as response:
                self.assertEqual(200, response.status_code)
                self.assertIn(b'id="settingsPage"', response.data)
                self.assertIn(b'href="./settings"', response.data)
        self.assertEqual(before, self.snapshot())
        self.restore.assert_not_called()

    def m19_notifications(self):
        self.patches.enter_context(patch.dict(os.environ, {name: "" for name in srv.ENV_NOTIFY.values()}))
        self.patches.enter_context(patch.object(srv, "AGENT_SETTINGS", str(self.data / "synthetic-settings.json")))
        self.credential_value = "synthetic-notification-credential"
        self.mail_value = "synthetic-mail-credential"
        config = {"pushplus": {"enabled": True, "token": self.credential_value}, "email": {
            "enabled": True, "smtp_host": "smtp.example.com", "username": "sender@example.com",
            "authcode": self.mail_value, "to_addrs": ["recipient@example.com"], "use_ssl": True, "smtp_port": 465}}
        srv.save_json(srv.NOTIFY_FILE, config)
        return config

    def test_m19_settings_responses_never_include_config_values_or_error_details(self):
        self.m19_notifications()
        cfg = {"enabled": False, "host": "node.example.com", "user": "synthetic-ssh-user",
               "key": "/srv/synthetic-key", "ssh_port": 8022}
        srv.STATE["remote"] = cfg
        srv.STATE["log"] = ["synthetic-ssh-user node.example.com /srv/synthetic-key"]
        private = [cfg["host"], cfg["user"], cfg["key"], self.credential_value, self.mail_value,
                   "smtp.example.com", "sender@example.com", "recipient@example.com", srv.NOTIFY_FILE]
        for route in ("/api/state", "/api/remote/status", "/api/notify/config"):
            result = self.client.get(route).get_json()
            self.assertTrue(result["ok"])
            for marker in private:
                self.assertNotIn(marker, json.dumps(result))
        view = self.client.get("/api/notify/config").get_json()["channels"]
        self.assertTrue(view["pushplus"]["token__set"])
        self.assertNotIn("token", view["pushplus"])
        self.assertNotIn("smtp_port", view["email"])
        self.assertTrue(view["email"]["use_ssl"])
        srv.STATE["remote"]["enabled"] = True
        with patch.object(srv, "agent", return_value=Mock(health=Mock(return_value=(False, cfg["key"])))):
            status = self.client.get("/api/remote/status").get_json()
        self.assertTrue(status["ok"])
        self.assertFalse(status["agent_ok"])
        self.assertNotIn(cfg["key"], json.dumps(status))

    def test_m19_remote_blank_save_and_one_field_update_keep_credentials_and_ports(self):
        cfg = {"enabled": False, "host": "node.example.com", "user": "synthetic-user",
               "key": "/srv/synthetic-key", "ssh_port": 8022, "local_port": 8902, "remote_port": 8903}
        srv.STATE["remote"] = dict(cfg)
        with patch.object(srv, "start_tunnel") as start, patch.object(srv.threading, "Timer"):
            result = self.post("/api/remote/config", {"enabled": True, "host": " ", "user": "", "key": "", "ssh_port": ""})
            self.assertTrue(result["ok"])
            expected = dict(cfg, enabled=True)
            self.assertEqual(expected, start.call_args.args[0])
            self.assertEqual(expected, srv.STATE["remote"])
            self.assertTrue(self.post("/api/remote/config", {"enabled": True, "local_port": "8904"})["ok"])
            self.assertEqual(dict(expected, local_port=8904), srv.STATE["remote"])
        self.assertNotIn(cfg["key"], json.dumps(result))

    def test_m19_remote_initial_fields_ports_and_failure_are_atomic(self):
        before = self.snapshot()
        with patch.object(srv, "start_tunnel") as start, patch.object(srv.threading, "Timer"):
            result = self.post("/api/remote/config", {"enabled": True})
            self.assertEqual({"host", "user"}, set(result["fields"]))
            for port in (0, 65536, True, "1.5", "test-invalid"):
                result = self.post("/api/remote/config", {"enabled": True, "host": "node.example.com",
                                   "user": "synthetic-user", "ssh_port": port})
                self.assertFalse(result["ok"])
                self.assertIn("ssh_port", result["fields"])
                self.assertEqual(before, self.snapshot())
            start.assert_not_called()
            start.side_effect = RuntimeError("synthetic-private-diagnostic")
            result = self.post("/api/remote/config", {"enabled": True, "host": "node.example.com", "user": "synthetic-user"})
            self.assertEqual("connection_failed", result["code"])
            self.assertNotIn("synthetic-private-diagnostic", json.dumps(result))
            self.assertEqual(before, self.snapshot())
            start.side_effect = None
            self.assertTrue(self.post("/api/remote/config", {"enabled": True, "host": "node.example.com", "user": "synthetic-user"})["ok"])
            self.assertEqual(22, srv.STATE["remote"]["ssh_port"])
            self.assertEqual(srv.DEFAULT_LOCAL_PORT, srv.STATE["remote"]["local_port"])

    def test_m19_failed_tunnel_replacement_restores_old_executor_and_config(self):
        self.remote()
        old = srv.STATE["tunnel"]
        old.stop.side_effect = lambda: setattr(old, "running", False)
        old.start.side_effect = lambda: setattr(old, "running", True)
        cfg = dict(srv.STATE["remote"])
        srv.save_json(srv.REMOTE_FILE, cfg)
        before = self.snapshot()
        replacement = Mock()
        replacement.start.side_effect = RuntimeError("synthetic-private-diagnostic")
        with patch.object(srv, "Tunnel", return_value=replacement):
            result = self.post("/api/remote/config", {"enabled": True, "host": "other.example.com"})
        self.assertFalse(result["ok"])
        self.assertIs(srv.STATE["tunnel"], old)
        self.assertTrue(old.running)
        old.start.assert_called_once_with()
        replacement.stop.assert_called_once_with()
        self.assertEqual(cfg, srv.STATE["remote"])
        self.assertEqual(before, self.snapshot())

    def test_m19_notification_blank_partial_environment_and_switch_updates(self):
        config = self.m19_notifications()
        before = srv.notify_channels()
        self.assertTrue(self.post("/api/notify/config", {"channels": {"email": {"username": "", "smtp_port": ""}}})["ok"])
        self.assertEqual(before, srv.notify_channels())
        with patch.dict(os.environ, {"SVE_SMTP_HOST": "environment.example.com", "SVE_PUSHPLUS_TOKEN": self.credential_value}):
            result = self.post("/api/notify/config", {"channels": {"email": {"smtp_host": "ignored.example.com", "use_ssl": False},
                               "pushplus": {"token": self.mail_value, "enabled": False}}})
            self.assertTrue(result["ok"])
            self.assertEqual("environment.example.com", srv.notify_channels()["email"]["smtp_host"])
            stored = json.loads(Path(srv.NOTIFY_FILE).read_text())
            self.assertEqual(config["email"]["smtp_host"], stored["email"]["smtp_host"])
            self.assertEqual(self.credential_value, stored["pushplus"]["token"])
            self.assertFalse(result["channels"]["email"]["use_ssl"])
            self.assertFalse(result["channels"]["email"]["smtp_host__editable"])
            self.assertEqual("env", result["channels"]["email"]["smtp_host__source"])
        self.assertFalse(srv.notify_channels()["pushplus"]["enabled"])

    def test_m19_notification_first_setup_validation_clear_and_sender_keep_full_values(self):
        config = self.m19_notifications()
        srv.save_json(srv.NOTIFY_FILE, {})
        before = Path(srv.NOTIFY_FILE).read_bytes()
        result = self.post("/api/notify/config", {"channels": {"email": {"enabled": True}}})
        self.assertIn("email.authcode", result["fields"])
        self.assertEqual(before, Path(srv.NOTIFY_FILE).read_bytes())
        self.assertTrue(self.post("/api/notify/config", {"channels": config})["ok"])
        sender = Mock()
        sender.notify_all.return_value = [("email", False, self.mail_value + " recipient@example.com")]
        with patch.dict(sys.modules, {"notification_sender": sender}):
            result = self.post("/api/notify/test", {})
            self.assertEqual(self.mail_value, sender.notify_all.call_args.args[0]["email"]["authcode"])
            self.assertNotIn(self.mail_value, json.dumps(result))
            self.assertNotIn("recipient@example.com", json.dumps(result))
            sender.notify_all.side_effect = RuntimeError(self.mail_value)
            self.assertNotIn(self.mail_value, json.dumps(self.post("/api/notify/test", {})))
        self.assertTrue(self.post("/api/notify/config", {"clear_secrets": True})["ok"])
        self.assertNotIn("token", json.loads(Path(srv.NOTIFY_FILE).read_text())["pushplus"])

    def test_m19_invalid_or_unreadable_notification_config_is_not_overwritten(self):
        self.m19_notifications()
        before = Path(srv.NOTIFY_FILE).read_bytes()
        for value in (True, "1.2", "65536", "test-invalid"):
            result = self.post("/api/notify/config", {"channels": {"email": {"smtp_port": value}}})
            self.assertEqual("validation_failed", result["code"])
            self.assertEqual(before, Path(srv.NOTIFY_FILE).read_bytes())
        Path(srv.NOTIFY_FILE).write_text("synthetic-broken-configuration", encoding="utf-8")
        self.assertFalse(self.client.get("/api/notify/config").get_json()["ok"])
        self.assertFalse(self.post("/api/notify/config", {"channels": {}})["ok"])
        self.assertEqual("synthetic-broken-configuration", Path(srv.NOTIFY_FILE).read_text())

    def test_m19_partial_notification_save_merges_base_config_and_default_port(self):
        config = self.m19_notifications()
        config["email"].pop("smtp_port")
        srv.save_json(srv.AGENT_SETTINGS, {"channels": config})
        srv.save_json(srv.NOTIFY_FILE, {})
        result = self.post("/api/notify/config", {"channels": {"email": {"use_ssl": False, "smtp_port": ""}}})
        self.assertTrue(result["ok"])
        effective = srv.notify_channels()
        self.assertEqual(self.mail_value, effective["email"]["authcode"])
        self.assertEqual(465, effective["email"].get("smtp_port", 465))
        self.assertFalse(effective["email"]["use_ssl"])
        self.assertEqual({"email": {"use_ssl": False}}, json.loads(Path(srv.NOTIFY_FILE).read_text()))

    def test_m19_old_agent_state_is_sanitized_at_proxy_boundary(self):
        link = self.remote()
        link.endpoint.return_value = {"ok": True, "remote": {"host": "node.example.com"}, "log": ["node.example.com /srv/synthetic-key"]}
        result = self.client.get("/api/state").get_json()
        self.assertTrue(result["remote"]["enabled"])
        self.assertNotIn("host", result["remote"])
        self.assertEqual([], result["log"])
        self.assertNotIn("node.example.com", json.dumps(result))

    def session_request(self, account, expected=None):
        try:
            raw = account.to_dict()
        except srv.AccountConflict:
            from dataclasses import asdict
            raw = asdict(account)
            raw["account_id"] = "10001"
        return self.post("/api/accounts/session", {"account": raw, "expected_version": expected if expected is not None else 0, "operation_id": uuid.uuid4().hex})

    def queue_profile(self, aid="10001"):
        return self.post("/api/account/profile/refresh", {
            "account_id": aid, "expected_version": srv.STATE["accounts"][aid].version})

    def test_batch_validation_rejections_have_explicit_codes_without_upstream_calls(self):
        before = self.snapshot()
        with patch.object(srv, "SveClient", side_effect=AssertionError("Validation must not submit")) as client:
            missing = self.post("/api/batch_enroll", {"account_ids": ["10001"], "tournaments": []})
            self.assertFalse(missing["ok"])
            self.assertEqual("batch_selection_required", missing["code"])
            limited = self.post("/api/batch_enroll", {
                "account_ids": ["10001"], "tournaments": ["test-event-%s" % i for i in range(61)]})
            self.assertFalse(limited["ok"])
            self.assertEqual("batch_limit_exceeded", limited["code"])
            self.assertIn("请减少账号或赛事", limited["error"])
            client.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_version_health_returns_only_pinned_process_identity_without_upstream_io(self):
        pinned = {"release_id": "test-started-release", "source_sha": "a" * 40,
                  "built_at": "2026-11-05T00:00:00+00:00", "schema_version": 2}
        before = self.snapshot()
        with patch.object(srv, "RELEASE_AT_START", pinned), \
                patch.object(srv, "release_snapshot", side_effect=AssertionError("Must not reload disk manifest")):
            for endpoint in ("/api/version", "/api/health"):
                response = self.client.get(endpoint)
                value = response.get_json()
                self.assertTrue(value["ok"])
                self.assertEqual(pinned, value["release"])
                self.assertEqual({"ok", "release", "started_at", "pid", "storage_ready"}, set(value))
                self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(before, self.snapshot())
        self.info.assert_not_called()

    def test_profile_rename_preserves_identity_credentials_workers_and_data(self):
        account = srv.STATE["accounts"]["10001"]
        worker = Mock(completed=False, attempts=0)
        srv.STATE["schedulers"]["10001|event"] = worker
        original = account.to_dict()
        subs, records = copy.deepcopy(srv.STATE["subs"]), copy.deepcopy(srv.STATE["records"])
        self.info.return_value = {"id": 10001, "nickname": "test-'<new>|"}
        self.assertTrue(self.queue_profile()["ok"])
        self.assertTrue(srv.refresh_profile_once("10001"))
        self.assertIs(account, srv.STATE["accounts"]["10001"])
        self.assertIs(worker, srv.STATE["schedulers"]["10001|event"])
        self.assertEqual(subs, srv.STATE["subs"])
        self.assertEqual(records, srv.STATE["records"])
        for field in ("sessionid", "deviceid", "uid", "session_version", "user_agent"):
            self.assertEqual(original[field], account.to_dict()[field])
        self.assertEqual(original["profile_version"] + 1, account.profile_version)
        self.assertEqual(original["version"] + 1, account.version)
        self.assertGreater(account.profile_updated_at, 0)
        state = self.client.get("/api/state").get_json()
        self.assertIn("test-'<new>|", state["active"])
        self.assertIn("test-'<new>|", state["subs"][0]["account"])
        with patch.object(srv, "refresh_records"):
            self.assertIn("test-'<new>|", self.client.get("/api/records").get_json()["records"][0]["account"])
        persisted = json.loads((self.data / "identity-v2.json").read_text(encoding="utf-8"))
        self.assertEqual(account.nickname, persisted["accounts"]["10001"]["nickname"])
        self.restore.assert_not_called()

    def test_profile_network_uses_snapshot_and_holds_neither_live_lock(self):
        import threading
        live = srv.STATE["accounts"]["10001"]
        results = []
        def info(client):
            self.assertIsNot(client.account, live)
            def probe():
                with srv.LOCK, live.credential_lock:
                    results.append(True)
            thread = threading.Thread(target=probe)
            # Explicitly allow only this isolated lock probe; no application workers.
            with patch.object(threading.Thread, "start", REAL_THREAD_START):
                thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), "profile I/O held a live lock")
            return {"id": 10001, "nickname": "test-renamed"}
        with patch.object(srv.SveClient, "info", info):
            self.assertTrue(srv.refresh_profile_once("10001"))
        self.assertEqual([True], results)

    def test_profile_failure_keeps_old_profile_and_backs_off_without_payloads(self):
        account = srv.STATE["accounts"]["10001"]
        before = self.snapshot()
        self.info.side_effect = srv.SveError("synthetic-private-upstream-payload")
        self.assertTrue(self.queue_profile()["ok"])
        self.assertFalse(srv.refresh_profile_once("10001"))
        self.assertEqual(before, self.snapshot())
        state = srv.profile_status(account)
        self.assertEqual("upstream_error", state["profile_error"])
        self.assertGreater(state["profile_next_refresh_at"], __import__("time").time() + 20)
        self.assertFalse(srv.refresh_profile_once("10001"))
        self.assertEqual(1, self.info.call_count)
        self.assertEqual("profile_backoff", self.queue_profile()["code"])
        self.assertNotIn("synthetic-private", json.dumps(self.client.get("/api/state").get_json()))

    def test_profile_invalid_uid_or_nickname_cannot_write(self):
        for info, error in [({"id": 10002, "nickname": "test-other"}, "identity_mismatch"),
                            ({"id": True, "nickname": "test-other"}, "identity_mismatch"),
                            ({"nickname": "test-other"}, "identity_mismatch"),
                            ({"id": 10001, "nickname": {}}, "invalid_response")]:
            srv.PROFILE_JOBS.clear()
            before = self.snapshot()
            self.info.return_value = info
            self.assertFalse(srv.refresh_profile_once("10001"))
            self.assertEqual(before, self.snapshot())
            self.assertEqual(error, srv.profile_status(srv.STATE["accounts"]["10001"])["profile_error"])

    def test_profile_late_response_cannot_overwrite_relogin_or_delete_restore(self):
        for action in ("relogin", "delete", "restore"):
            srv.PROFILE_JOBS.clear()
            live = fake_account()
            srv.STATE["accounts"]["10001"] = live
            def info():
                if action == "relogin":
                    new = fake_account(name="test-new-login", session="synthetic-new-session")
                    srv.commit_login(new, expected_revision=live.version, operation_id=uuid.uuid4().hex,
                                     fingerprint="test-fingerprint")
                else:
                    srv.STATE["accounts"].pop("10001")
                    if action == "restore":
                        srv.STATE["accounts"]["10001"] = fake_account(name="test-restored")
                return {"id": 10001, "nickname": "test-late-profile"}
            self.info.side_effect = info
            self.assertFalse(srv.refresh_profile_once("10001"))
            current = srv.STATE["accounts"].get("10001")
            self.assertTrue(current is None or current.nickname != "test-late-profile")
            self.assertNotIn("10001", srv.PROFILE_JOBS)

    def test_profile_period_dedup_all_accounts_and_continuous_renames(self):
        import time
        self.info.side_effect = lambda: {"id": 10002, "nickname": "test-shared"}
        self.assertTrue(srv.refresh_profile_once("10002"))  # Non-active account.
        self.assertFalse(srv.refresh_profile_once("10002"))
        self.assertEqual(1, self.info.call_count)
        account = srv.STATE["accounts"]["10001"]
        for nickname in ("test-A", "test-B", "test-A", "test-shared"):
            srv.PROFILE_JOBS.clear()
            account.profile_updated_at = time.time() - 601
            self.info.side_effect = None
            self.info.return_value = {"id": 10001, "nickname": nickname}
            self.assertTrue(srv.refresh_profile_once("10001"))
            self.assertEqual(nickname, account.nickname)
        self.assertEqual(14, len(srv.STATE["accounts"]))
        self.assertTrue(self.queue_profile()["ok"] is False)  # Cooldown, no repeated calls.

    def test_profile_queue_dedup_and_stale_version_rejected(self):
        self.assertTrue(self.queue_profile()["ok"])
        job = srv.PROFILE_JOBS["10001"]
        self.assertTrue(self.queue_profile()["ok"])
        self.assertIs(job, srv.PROFILE_JOBS["10001"])
        self.assertFalse(self.post("/api/account/profile/refresh", {
            "account_id": "10001", "expected_version": 0})["ok"])
        self.info.assert_not_called()  # UI polling/queue never requests official info.
        for _ in range(3):
            self.client.get("/api/state")
        self.info.assert_not_called()
        srv.STATE["accounts"]["10001"]._retired = True
        self.assertFalse(self.queue_profile()["ok"])

    def test_profile_global_io_limit_and_remote_mode_never_fetch_local_credentials(self):
        with srv.PROFILE_IO_LOCK:
            self.assertFalse(srv.refresh_profile_once("10001"))
        srv.STATE["remote"] = {"enabled": True}
        self.assertFalse(srv.refresh_profile_once("10001"))
        self.info.assert_not_called()

    def test_profile_loop_rotates_all_accounts_and_honors_configured_period(self):
        import time
        (self.data / "settings.json").write_text('{"profile_refresh_interval_seconds": 120}', encoding="utf-8")
        now = time.time()
        accounts = srv.STATE["accounts"]
        for account in accounts.values():
            account.profile_updated_at = now - 121
        called = []
        def info(client):
            called.append(client.account.account_id)
            return {"id": client.account.uid, "nickname": "test-rotated"}
        stop = Mock()
        stop.is_set.side_effect = [False] * (len(accounts) + 1) + [True]
        with patch.object(srv.SveClient, "info", info):
            srv.profile_refresh_loop(stop)
        self.assertEqual(set(accounts), set(called))
        self.assertEqual(len(accounts), len(called))
        self.assertTrue(all(srv.profile_status(a)["profile_next_refresh_at"] >= now + 120 for a in accounts.values()))
        self.assertFalse(srv.refresh_profile_once("10001"))
        stop.wait.assert_called_once_with(1)

    def test_profile_failure_backoff_increases_then_recovers(self):
        with patch.object(srv.time, "time", return_value=10000):
            self.info.side_effect = srv.SveError("test-offline")
            srv.refresh_profile_once("10001")
            self.assertEqual(10030, srv.PROFILE_JOBS["10001"]["next_at"])
        with patch.object(srv.time, "time", return_value=10031):
            srv.refresh_profile_once("10001")
            self.assertEqual(10091, srv.PROFILE_JOBS["10001"]["next_at"])
        with patch.object(srv.time, "time", return_value=10092):
            self.info.side_effect = None
            self.info.return_value = {"id": 10001, "nickname": "test-recovered"}
            self.assertTrue(srv.refresh_profile_once("10001"))
            self.assertIsNone(srv.profile_status(srv.STATE["accounts"]["10001"])["profile_error"])

    def test_profile_publication_failure_keeps_old_name_and_fences_execution(self):
        original = srv.STATE["accounts"]["10001"].nickname
        self.info.return_value = {"id": 10001, "nickname": "test-unsaved"}
        with patch.object(srv.STORE, "save", side_effect=OSError("test-failure")):
            with self.assertRaises(srv.AccountConflict):
                srv.refresh_profile_once("10001")
        self.assertTrue(srv.STATE["storage_failed"])
        self.assertEqual(original, srv.STATE["accounts"]["10001"].nickname)
        self.assertFalse(srv.PROFILE_IO_LOCK.locked())

    def test_profile_notifications_resolve_current_label_without_rewriting_logs(self):
        row = srv.STATE["subs"]["10001|event"]
        row.update(name="test-event", logs=["test-old-name event body"])
        self.info.return_value = {"id": 10001, "nickname": "test-<new>"}
        srv.refresh_profile_once("10001")
        with patch.object(srv, "send_notify") as send:
            srv.notify_completed(row, already=True)
            srv.notify_time_conflict(row, "test-reason")
        self.assertEqual(2, send.call_count)
        for call in send.call_args_list:
            self.assertIn("test-&lt;new&gt; (UID 10001)", call.args[1])
        self.assertEqual(["test-old-name event body"], row["logs"])

    def test_profile_remote_refresh_forwards_server_version_without_mutating_local_cache(self):
        link = self.remote()
        before = self.snapshot()
        link.endpoint.return_value = {"ok": True, "account_id": "10005", "queued": True}
        body = {"account_id": "10005", "expected_version": 7}
        self.assertTrue(self.post("/api/account/profile/refresh", body)["ok"])
        link.endpoint.assert_called_once_with("POST", "/api/account/profile/refresh", body, {})
        self.info.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_profile_timestamp_survives_reload_and_old_schema2_missing_field_is_supported(self):
        self.info.return_value = {"id": 10001, "nickname": "test-persisted"}
        self.assertTrue(srv.refresh_profile_once("10001"))
        updated = srv.STATE["accounts"]["10001"].profile_updated_at
        srv.load_all()
        self.assertEqual(updated, srv.STATE["accounts"]["10001"].profile_updated_at)
        self.assertFalse(srv.refresh_profile_once("10001"))
        row = fake_account().to_dict()
        row.pop("profile_updated_at")
        self.assertEqual(0, srv.Account.from_dict(row).profile_updated_at)
        value = srv.dataset()
        value["accounts"]["10001"].pop("profile_updated_at")
        from identity_store import validate
        validate(value)
        for invalid in (True, "test-invalid", -1, float("inf"), float("nan")):
            value["accounts"]["10001"]["profile_updated_at"] = invalid
            with self.assertRaises(srv.AccountConflict):
                validate(value)

    def test_legacy_empty_subset_repeat_never_mutates_14_accounts(self):
        before = self.snapshot()
        subset = {a.name: a.to_dict() for a in list(srv.STATE["accounts"].values())[:4]}
        for account in subset.values():
            account["sessionid"] = "synthetic-stale-session"
        for accounts in ({}, subset, subset):
            result = self.post("/api/accounts/sync", {"accounts": accounts, "active": "10002"})
            self.assertTrue(result["ok"])
            self.assertTrue(result["readonly"])
            self.assertEqual([], result["pushed"])
            self.assertEqual([], result["removed"])
            self.assertEqual(before, self.snapshot())
        self.restore.assert_not_called()
        self.info.assert_not_called()

    def test_legacy_new_cache_does_not_create_account(self):
        before = self.snapshot()
        account = fake_account(100000)
        result = self.post("/api/accounts/sync", {"accounts": {account.name: account.to_dict()}})
        self.assertTrue(result["readonly"])
        self.assertEqual(before, self.snapshot())

    def test_invalid_legacy_batch_has_no_partial_writes(self):
        before = self.snapshot()
        valid = fake_account(100000)
        for body in [[], {}, {"accounts": []}, {"accounts": {"test-invalid": None}},
                     {"accounts": {valid.name: valid.to_dict(), "test-invalid": {}}},
                     {"accounts": {"mismatched-name": valid.to_dict()}}]:
            self.assertFalse(self.post("/api/accounts/sync", body)["ok"])
            self.assertEqual(before, self.snapshot())

    def test_malformed_json_legacy_request_preserves_data(self):
        before = self.snapshot()
        result = self.client.post("/api/accounts/sync", data="{invalid", content_type="application/json")
        self.assertEqual(200, result.status_code)
        self.assertFalse(result.get_json()["ok"])
        self.assertEqual(before, self.snapshot())

    def test_link_legacy_push_is_blocked_without_network(self):
        link = srv.AgentLink()
        with patch.object(link, "_req") as request:
            result = link.push_accounts({"10001": fake_account().to_dict()}, "10001")
        self.assertFalse(result["ok"])
        request.assert_not_called()

    def test_legacy_conflicting_name_or_renamed_cache_never_mutates(self):
        before = self.snapshot()
        for account in (fake_account(100000, "10001"), fake_account(10001, "renamed-test")):
            result = self.post("/api/accounts/sync", {"accounts": {account.name: account.to_dict()}})
            self.assertTrue(not result["ok"] or result.get("readonly"))
            self.assertEqual(before, self.snapshot())

    def test_summary_never_returns_credentials_or_mobile(self):
        response = self.client.get("/api/accounts/summary")
        data = response.get_json()
        self.assertEqual(14, len(data["accounts"]))
        self.assertTrue({"name", "uid", "nickname", "account_id", "version"} <= set(data["accounts"][0]))
        self.assertNotIn("sessionid", response.get_data(as_text=True))
        self.assertNotIn("mobile", response.get_data(as_text=True))
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_explicit_session_relogin_updates_existing_scheduler_account_once(self):
        old = srv.STATE["accounts"]["10001"]
        scheduler = Mock(client=Mock(account=old))
        srv.STATE["schedulers"]["10001|event"] = scheduler
        before_subs = Path(srv.SUBS_FILE).read_bytes()
        before_records = Path(srv.RECORDS_FILE).read_bytes()
        account = fake_account(session="synthetic-new-session")
        self.info.return_value = {"id": account.uid, "nickname": account.nickname}
        result = self.session_request(account, srv.revision(old))
        self.assertTrue(result["ok"])
        self.assertIs(old, srv.STATE["accounts"][account.name])
        self.assertEqual(account.sessionid, scheduler.client.account.sessionid)
        self.assertEqual(14, len(srv.STATE["accounts"]))
        self.assertEqual(before_subs, Path(srv.SUBS_FILE).read_bytes())
        self.assertEqual(before_records, Path(srv.RECORDS_FILE).read_bytes())
        self.restore.assert_called_once()
        scheduler.stop.assert_not_called()

    def test_explicit_new_login_preserves_existing_active(self):
        account = fake_account(100000)
        self.info.return_value = {"id": account.uid, "nickname": account.nickname}
        self.assertTrue(self.session_request(account)["ok"])
        self.assertEqual(15, len(srv.STATE["accounts"]))
        self.assertEqual("10001", srv.STATE["active"])

    def test_fresh_install_can_save_more_than_one_explicit_login(self):
        srv.STATE["accounts"] = {}
        srv.STATE["active"] = None
        srv.ACCOUNTS_LOADED_OK = True
        for account in (fake_account(), fake_account(10002)):
            self.info.return_value = {"id": account.uid, "nickname": account.nickname}
            self.assertTrue(self.session_request(account)["ok"])
        self.assertEqual(2, len(srv.STATE["accounts"]))
        self.assertTrue(srv.ACCOUNTS_LOADED_OK)

    def test_unverified_uid_and_upstream_errors_preserve_data(self):
        account = fake_account(session="synthetic-new-session")
        old = srv.STATE["accounts"][account.name]
        before = self.snapshot()
        for info in [{"id": 10002, "nickname": account.nickname}, {"id": None}, {"id": True}]:
            self.info.return_value = info
            self.assertFalse(self.session_request(account, srv.revision(old))["ok"])
            self.assertEqual(before, self.snapshot())
        self.info.side_effect = RuntimeError("synthetic upstream failure")
        self.assertFalse(self.session_request(account, srv.revision(old))["ok"])
        self.assertEqual(before, self.snapshot())

    def test_stale_revision_and_conflicting_name_rejected_before_upstream(self):
        before = self.snapshot()
        self.assertFalse(self.session_request(fake_account())["ok"])
        self.assertFalse(self.session_request(fake_account(10002, "10001"))["ok"])
        self.assertFalse(self.post("/api/accounts/session", {"account": fake_account().to_dict()})["ok"])
        self.assertEqual(before, self.snapshot())
        self.info.assert_not_called()

    def test_concurrent_session_update_during_validation_wins(self):
        old = srv.STATE["accounts"]["10001"]
        expected = srv.revision(old)
        concurrent_session = "synthetic-concurrent-session"
        def concurrent_update():
            old.sessionid = concurrent_session
            old.version += 1
            srv.persist_accounts()
            return {"id": old.uid, "nickname": old.nickname}
        self.info.side_effect = concurrent_update
        self.assertFalse(self.session_request(fake_account(session="synthetic-late-session"), expected)["ok"])
        self.assertEqual("synthetic-concurrent-session", old.sessionid)

    def test_disk_failure_does_not_commit_memory(self):
        before = self.snapshot()
        account = fake_account(session="synthetic-new-session")
        self.info.return_value = {"id": account.uid, "nickname": account.nickname}
        with patch.object(srv, "persist_dataset", side_effect=OSError("synthetic disk failure")):
            self.assertFalse(self.session_request(account, srv.revision(srv.STATE["accounts"][account.name]))["ok"])
        self.assertEqual(before, self.snapshot())

    def test_invalid_uid_types_cannot_be_saved(self):
        before = self.snapshot()
        for value in [None, True, False, 0, -1, 1.5, "", "x", "-1", " 10001", "１２３"]:
            account = fake_account()
            account.uid = value
            self.assertFalse(self.session_request(account)["ok"])
            self.assertEqual(before, self.snapshot())

    def test_unreadable_existing_accounts_file_is_not_overwritten(self):
        before = self.snapshot()
        account = fake_account(session="synthetic-new-session")
        self.info.return_value = {"id": account.uid, "nickname": account.nickname}
        with patch.object(srv, "ACCOUNTS_LOADED_OK", False):
            self.assertFalse(self.session_request(account, srv.revision(srv.STATE["accounts"][account.name]))["ok"])
        self.assertEqual(before, self.snapshot())

    def remote(self):
        rows = self.client.get("/api/accounts/summary").get_json()["accounts"]
        srv.STATE["accounts"] = dict(list(srv.STATE["accounts"].items())[:4])
        srv.persist_accounts()
        srv.STATE["remote"] = {"enabled": True, "host": "sve.example.com", "user": "synthetic-user"}
        srv.STATE["tunnel"] = Mock(running=True, connection_config=dict(srv.STATE["remote"]))
        link = Mock()
        link.accounts_summary.return_value = {"ok": True, "schema_version": 2, "session_write": 2,
                                             "subscription_identity": 2, "accounts": rows}
        link.save_session.return_value = {"ok": True}
        self.patches.enter_context(patch.object(srv, "agent", return_value=link))
        return link

    def test_startup_reconnect_manual_compare_and_stale_callbacks_are_readonly(self):
        link = self.remote()
        Path(srv.REMOTE_FILE).write_text(json.dumps(srv.STATE["remote"]), encoding="utf-8")
        before = self.snapshot()
        with patch.object(srv, "start_tunnel"), patch.object(srv.threading, "Timer") as timer:
            srv.load_all()
            self.assertIs(timer.call_args.args[1], srv.refresh_remote_accounts)
            srv.refresh_remote_accounts()
            self.assertTrue(self.post("/api/remote/config", srv.STATE["remote"])["ok"])
            srv.refresh_remote_accounts()
        for reason in ["startup", "reconnect", "selection", "deletion"]:
            srv.auto_push_accounts(reason)
        result = self.post("/api/remote/push_accounts", {})
        self.assertEqual(14, result["total"])
        self.assertEqual(4, result["local_total"])
        self.assertEqual(10, len(result["remote_only"]))
        self.assertEqual(before[1:], self.snapshot()[1:])
        for name in ("accounts.json", "subscriptions.json", "records.json"):
            self.assertEqual(before[0][name], self.snapshot()[0][name])
        link.push_accounts.assert_not_called()
        link.save_session.assert_not_called()

    def test_remote_restore_never_starts_or_purges_local_subscriptions(self):
        self.remote()
        before = self.snapshot()
        srv.restore_schedulers()
        self.assertEqual(0, RESTORE_MISSING())
        self.assertEqual(before, self.snapshot())

    def test_already_local_restores_eligible_subscriptions_once(self):
        key = "10001|event"
        srv.STATE["subs"][key]["orphan"] = False
        existing_key = "10002|existing-event"
        orphan_key = "test-missing|orphan-event"
        srv.STATE["subs"][existing_key] = {
            "account_id": "10002", "account": "10002", "tournament_id": "existing-event", "orphan": False}
        srv.STATE["pending"].append({"category": "subscriptions_unresolved", "original": {"account": "test-missing"}})
        srv.persist_subs()
        before = self.snapshot()
        self.restore.side_effect = RESTORE_MISSING
        with patch.object(srv, "Scheduler") as scheduler, \
                patch.object(srv.threading, "Thread") as thread, patch.object(srv, "_sub_log"):
            existing = Mock()
            srv.STATE["schedulers"][existing_key] = existing
            tunnel = Mock()
            srv.STATE["tunnel"] = tunnel
            self.assertEqual({"ok": True, "enabled": False},
                             self.post("/api/remote/config", {"enabled": False}))
            tunnel.stop.assert_called_once_with()
            self.assertIsNone(srv.STATE["tunnel"])
            self.assertFalse(srv.remote_enabled())
            self.assertEqual({"enabled": False}, json.loads(Path(srv.REMOTE_FILE).read_text()))
            scheduler.assert_called_once()
            self.assertIs(srv.STATE["accounts"]["10001"],
                          scheduler.call_args.args[0].account)
            self.assertEqual("event", scheduler.call_args.args[1])
            thread.assert_called_once()
            self.assertTrue(callable(thread.call_args.kwargs["target"]))
            thread.return_value.start.assert_called_once_with()
            scheduler.return_value.run.assert_not_called()
            self.assertIs(existing, srv.STATE["schedulers"][existing_key])
            self.assertNotIn(orphan_key, srv.STATE["schedulers"])
            self.assertTrue(self.post("/api/remote/config", {"enabled": False})["ok"])
            scheduler.assert_called_once()
            thread.return_value.start.assert_called_once_with()
        after = self.snapshot()
        self.assertEqual(before[1:], after[1:])
        for name in ("accounts.json", "subscriptions.json", "records.json"):
            self.assertEqual(before[0][name], after[0][name])

    def test_disable_remote_with_cached_subscriptions_requires_handover(self):
        link = self.remote()
        srv.save_json(srv.REMOTE_FILE, srv.STATE["remote"])
        tunnel = Mock()
        srv.STATE["tunnel"] = tunnel
        remote_before = copy.deepcopy(srv.STATE["remote"])
        self.restore.side_effect = RESTORE_MISSING
        for orphan in (False, True):
            with self.subTest(orphan=orphan):
                if orphan:
                    srv.STATE["subs"]["10001|event"]["enabled"] = False
                srv.STATE["subs"]["10001|event"]["orphan"] = orphan
                srv.persist_subs()
                before = self.snapshot()
                for _ in range(2):
                    result = self.post("/api/remote/config", {"enabled": False})
                    self.assertFalse(result["ok"])
                    self.assertEqual("execution_handover_required", result["code"])
                    self.assertTrue(srv.remote_enabled())
                    self.assertEqual(remote_before, srv.STATE["remote"])
                    self.assertIs(tunnel, srv.STATE["tunnel"])
                    self.assertEqual(before, self.snapshot())
                self.assertEqual({}, srv.STATE["schedulers"])
        tunnel.stop.assert_not_called()
        self.restore.assert_not_called()
        link.assert_not_called()
        self.assertEqual([], link.mock_calls)

    def test_disable_remote_without_local_cache_does_not_touch_remote_subscriptions(self):
        link = self.remote()
        remote_config = copy.deepcopy(srv.STATE["remote"])
        srv.STATE["subs"] = {}
        srv.persist_subs()
        before = self.snapshot()
        tunnel = Mock()
        srv.STATE["tunnel"] = tunnel
        self.restore.side_effect = RESTORE_MISSING
        for _ in range(2):
            self.assertEqual({"ok": True, "enabled": False},
                             self.post("/api/remote/config", {"enabled": False}))
        tunnel.stop.assert_called_once_with()
        self.assertIsNone(srv.STATE["tunnel"])
        self.assertFalse(srv.remote_enabled())
        self.assertEqual(dict(remote_config, enabled=False), json.loads(Path(srv.REMOTE_FILE).read_text()))
        self.assertEqual({}, srv.STATE["schedulers"])
        self.assertEqual(before[1:], self.snapshot()[1:])
        for name in ("accounts.json", "subscriptions.json", "records.json"):
            self.assertEqual(before[0][name], self.snapshot()[0][name])
        self.assertEqual([], link.mock_calls)

    def test_enable_remote_refuses_existing_active_local_scheduler(self):
        scheduler = Mock(completed=False)
        scheduler.stop_flag.is_set.return_value = False
        srv.STATE["schedulers"]["10001|event"] = scheduler
        with patch.object(srv, "start_tunnel") as start:
            result = self.post("/api/remote/config", {"enabled": True, "host": "sve.example.com", "user": "synthetic-user"})
        self.assertFalse(result["ok"])
        self.assertFalse(srv.remote_enabled())
        start.assert_not_called()

    def enable_remote_for_interleaving(self):
        # Called at a forced scheduling boundary, with tunnel/timer replaced.
        result = srv.app.test_client().post("/api/remote/config", json={
            "enabled": True, "host": "sve.example.com", "user": "synthetic-user"}).get_json()
        self.assertTrue(result["ok"])
        return result

    def test_local_execution_during_tunnel_start_rolls_back_remote_enable(self):
        for source in ("subscribe", "restore", "draining"):
            with self.subTest(source=source):
                srv.STATE.update(remote={"enabled": False}, tunnel=None,
                                 schedulers={}, scheduler_threads=set())
                saved = {"enabled": False, "host": "sve.example.com", "user": "synthetic-user"}
                srv.STATE["remote"] = dict(saved)
                srv.save_json(srv.REMOTE_FILE, saved)
                before = Path(srv.REMOTE_FILE).read_bytes()

                def start_local():
                    self.assertFalse(srv.remote_enabled())
                    if source == "subscribe":
                        self.assertTrue(self.post("/api/subscribe", {"tournament_id": "new-event"})["ok"])
                    elif source == "restore":
                        self.assertTrue(srv._start_scheduler_for(
                            "10001|event", srv.STATE["subs"]["10001|event"], srv.STATE["accounts"]["10001"]))
                    else:
                        # A cancelled scheduler remains an owner until run() exits.
                        with srv.LOCK:
                            srv.STATE["scheduler_threads"].add("synthetic-draining-thread")

                with patch.object(srv, "Tunnel") as tunnel, \
                        patch.object(srv.SveClient, "detail", return_value={"name": "synthetic-event"}), \
                        patch.object(srv, "Scheduler"), patch.object(srv.threading, "Thread"), \
                        patch.object(srv.threading, "Timer") as timer, patch.object(srv, "_sub_log"):
                    tunnel.return_value.start.side_effect = start_local
                    result = self.post("/api/remote/config", {"enabled": True})
                    self.assertFalse(result["ok"])
                    self.assertEqual("remote_busy", result["code"])
                    tunnel.return_value.stop.assert_called_once_with()
                    timer.assert_not_called()
                self.assertEqual(saved, srv.STATE["remote"])
                self.assertIsNone(srv.STATE["tunnel"])
                self.assertEqual(before, Path(srv.REMOTE_FILE).read_bytes())
                self.assertTrue(srv.STATE["schedulers"] or srv.STATE["scheduler_threads"])

    def test_disabled_remote_settings_survive_restart_and_blank_reenable(self):
        saved = {"enabled": True, "host": "sve.example.com", "user": "synthetic-user",
                 "key": "synthetic-key-path", "ssh_port": 2222, "local_port": 18765, "remote_port": 18766}
        srv.STATE.update(remote=dict(saved), tunnel=Mock(), subs={})
        srv.persist_subs()
        with patch.object(srv, "start_tunnel") as start, patch.object(srv.threading, "Timer") as timer:
            self.assertTrue(self.post("/api/remote/config", {"enabled": False})["ok"])
            disabled = dict(saved, enabled=False)
            before = Path(srv.REMOTE_FILE).read_bytes()
            srv.STATE["remote"] = {"enabled": False}
            srv.load_all()
            self.assertEqual(disabled, srv.STATE["remote"])
            self.assertIsNone(srv.STATE["tunnel"])
            self.assertEqual(before, Path(srv.REMOTE_FILE).read_bytes())
            start.assert_not_called()
            timer.assert_not_called()
            result = self.post("/api/remote/config", {"enabled": True, **{field: "" for field in srv.REMOTE_FIELDS}})
            self.assertTrue(result["ok"])
            start.assert_called_once_with(saved)
            self.assertEqual(saved, srv.STATE["remote"])
            self.assertEqual(saved, json.loads(Path(srv.REMOTE_FILE).read_text()))

    def test_remote_enabled_during_local_detail_prevents_scheduler_registration(self):
        before = self.snapshot()
        def detail(tid):
            self.enable_remote_for_interleaving()
            return {"name": "synthetic-event"}
        with patch.object(srv.SveClient, "detail", side_effect=detail), \
                patch.object(srv, "start_tunnel"), patch.object(srv.threading, "Timer"), \
                patch.object(srv, "Scheduler") as scheduler:
            result = self.post("/api/subscribe", {"tournament_id": "new-event"})
        self.assertFalse(result["ok"])
        self.assertEqual("execution_mode_changed", result["code"])
        self.assertTrue(srv.remote_enabled())
        scheduler.assert_not_called()
        self.assertEqual({}, srv.STATE["schedulers"])
        self.assertEqual(before[1:], self.snapshot()[1:])
        for name in ("accounts.json", "subscriptions.json", "records.json"):
            self.assertEqual(before[0][name], self.snapshot()[0][name])

    def test_restore_rechecks_mode_before_registering_local_scheduler(self):
        before = self.snapshot()
        def construct(*args, **kwargs):
            self.enable_remote_for_interleaving()
            return Mock()
        with patch.object(srv, "Scheduler", side_effect=construct), \
                patch.object(srv, "start_tunnel"), patch.object(srv.threading, "Timer"):
            self.assertEqual(0, RESTORE_MISSING())
        self.assertEqual({}, srv.STATE["schedulers"])
        self.assertEqual(before[1:], self.snapshot()[1:])

    def test_registered_local_scheduler_blocks_remote_even_before_thread_start(self):
        def thread_start():
            result = srv.app.test_client().post("/api/remote/config", json={
                "enabled": True, "host": "sve.example.com", "user": "synthetic-user"}).get_json()
            self.assertFalse(result["ok"])
            self.assertFalse(srv.remote_enabled())
        with patch.object(srv.SveClient, "detail", return_value={"name": "synthetic-event"}), \
                patch.object(srv.threading, "Thread") as thread, \
                patch.object(srv, "start_tunnel") as tunnel:
            thread.return_value.start.side_effect = thread_start
            self.assertTrue(self.post("/api/subscribe", {"tournament_id": "new-event"})["ok"])
            self.assertTrue(self.post("/api/subscribe", {"tournament_id": "new-event"})["already"])
            thread.return_value.start.assert_called_once_with()
        self.assertEqual(1, len(srv.STATE["schedulers"]))
        tunnel.assert_not_called()

    def test_explicit_remote_login_sends_one_account_and_no_legacy_fallback(self):
        link = self.remote()
        observed = srv.login_snapshot()
        account = fake_account(session="synthetic-new-session")
        srv.save_explicit_login(account, observed)
        self.assertEqual(account.account_id, link.save_session.call_args.args[0]["account_id"])
        self.assertEqual(account.sessionid, link.save_session.call_args.args[0]["sessionid"])
        self.assertIsInstance(link.save_session.call_args.args[1], int)
        link.push_accounts.assert_not_called()
        link.accounts_summary.return_value = {"ok": False}
        before = self.snapshot()
        with self.assertRaises(srv.AccountConflict):
            srv.save_explicit_login(fake_account(100000), srv.login_snapshot())
        self.assertEqual(before, self.snapshot())
        self.assertEqual(1, link.save_session.call_count)

    def test_remote_login_keeps_prelogin_revision_when_newer_phone_session_arrives(self):
        link = self.remote()
        row = link.accounts_summary.return_value["accounts"][0]
        original_revision = row["revision"]
        newer = fake_account(session="synthetic-newer-phone-session")
        newer.version += 1
        newer_revision = srv.revision(newer)
        account = fake_account(session="synthetic-delayed-desktop-session")

        def newer_phone_login(*args, **kwargs):
            row["revision"] = row["version"] = newer_revision
            return {"id": account.uid, "nickname": account.nickname}

        def save_session(raw, expected, **kwargs):
            return {"ok": expected == row["revision"], "error": "synthetic revision conflict"}

        def immediate_thread(target, **kwargs):
            thread = Mock()
            thread.start.side_effect = target
            return thread

        link.save_session.side_effect = save_session
        for browser in (False, True):
            with self.subTest(browser=browser):
                row["revision"] = row["version"] = original_revision
                link.reset_mock()
                before = self.snapshot()
                if browser:
                    def browser_login(**kwargs):
                        newer_phone_login()
                        return account
                    with patch.object(srv, "login_capability", return_value={"can_login": True}), \
                            patch.object(srv, "LoginWindow") as window, \
                            patch.object(srv.threading, "Thread", side_effect=immediate_thread):
                        window.return_value.__enter__.return_value.wait_for_login.side_effect = browser_login
                        self.assertTrue(self.post("/api/login/start", {})["ok"])
                    self.assertFalse(srv.STATE["login"]["ok"])
                else:
                    self.info.side_effect = newer_phone_login
                    with patch.object(srv.SveClient, "login_with_cookie", return_value=account.sessionid):
                        self.assertFalse(self.post("/api/login/cookie", {"cookies": "NTES_SESS=fake"})["ok"])
                link.accounts_summary.assert_called_once_with()
                link.save_session.assert_called_once()
                self.assertEqual(original_revision, link.save_session.call_args.args[1])
                self.assertEqual(account.sessionid, link.save_session.call_args.args[0]["sessionid"])
                self.assertEqual(newer_revision, row["revision"])
                self.assertEqual(before, self.snapshot())

    def test_remote_login_without_prelogin_summary_never_acquires_session(self):
        link = self.remote()
        link.accounts_summary.return_value = {"ok": False}
        before = self.snapshot()
        with patch.object(srv, "login_capability", return_value={"can_login": True}), \
                patch.object(srv, "LoginWindow") as window, \
                patch.object(srv.SveClient, "login_with_cookie") as exchange:
            self.assertFalse(self.post("/api/login/start", {})["ok"])
            self.assertFalse(self.post("/api/login/cookie", {"cookies": "NTES_SESS=fake"})["ok"])
        window.assert_not_called()
        exchange.assert_not_called()
        link.save_session.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_login_network_operations_prevent_tunnel_reconfiguration(self):
        link = self.remote()
        response = link.accounts_summary.return_value
        tunnel = srv.STATE["tunnel"]
        before = self.snapshot()
        config = dict(srv.STATE["remote"], host="other.example.com")
        def try_switch():
            result = srv.app.test_client().post("/api/remote/config", json=config).get_json()
            self.assertFalse(result["ok"])
            self.assertIs(tunnel, srv.STATE["tunnel"])
        def summary():
            try_switch()
            return response
        def submit(*args, **kwargs):
            try_switch()
            return {"ok": True}
        link.accounts_summary.side_effect = summary
        link.save_session.side_effect = submit
        observed = srv.login_snapshot()
        srv.save_explicit_login(fake_account(), observed)
        tunnel.stop.assert_not_called()
        self.assertEqual(before[2:], self.snapshot()[2:])
        self.assertEqual(before[1]["10002"], self.snapshot()[1]["10002"])

    def test_config_change_prevents_snapshot_while_old_tunnel_is_being_replaced(self):
        self.remote()
        config = dict(srv.STATE["remote"], host="other.example.com")
        def switching(cfg):
            with self.assertRaises(srv.AccountConflict):
                srv.login_snapshot()
        with patch.object(srv, "start_tunnel", side_effect=switching), \
                patch.object(srv.threading, "Timer"):
            self.assertTrue(self.post("/api/remote/config", config)["ok"])

    def test_login_rejects_unready_mismatched_or_rebuilt_tunnel(self):
        link = self.remote()
        observed = srv.login_snapshot()
        original = srv.STATE["tunnel"]
        for tunnel in (None, Mock(running=False),
                       Mock(running=True, connection_config={}),
                       Mock(running=True, connection_config=dict(srv.STATE["remote"]))):
            with self.subTest(tunnel=tunnel):
                srv.STATE["tunnel"] = tunnel
                with self.assertRaises(srv.AccountConflict):
                    srv.save_explicit_login(fake_account(), observed)
        link.save_session.assert_not_called()
        srv.STATE["tunnel"] = original

    def test_cancelled_scheduler_blocks_remote_until_worker_finishes(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                srv.STATE["remote"] = {"enabled": False}
                sched = srv.Scheduler(srv.SveClient(fake_account()), "event")
                key = "10001|event"
                srv.STATE["subs"][key] = {"account": "10001", "tournament_id": "event"}
                srv.STATE["schedulers"][key] = sched
                with patch.object(srv.threading, "Thread") as thread:
                    with srv.LOCK:
                        worker = srv.scheduler_thread(sched, key)
                    target = thread.call_args.kwargs["target"]
                    self.assertTrue(self.post("/api/unsubscribe", {"tournament_id": "event"})["ok"])
                    self.assertNotIn(key, srv.STATE["schedulers"])
                    self.assertTrue(sched.stop_flag.is_set())
                    self.assertIn(worker, srv.STATE["scheduler_threads"])
                    body = {"enabled": True, "host": "sve.example.com", "user": "synthetic-user"}
                    self.assertFalse(self.post("/api/remote/config", body)["ok"])
                    with patch.object(sched, "run", side_effect=RuntimeError("synthetic") if fail else None):
                        if fail:
                            with self.assertRaises(RuntimeError):
                                target()
                        else:
                            target()
                    self.assertNotIn(worker, srv.STATE["scheduler_threads"])
                with patch.object(srv, "start_tunnel"), patch.object(srv.threading, "Timer"):
                    self.assertTrue(self.post("/api/remote/config", body)["ok"])

    def test_stopping_during_scheduler_detail_prevents_signup(self):
        for run_loop in (False, True):
            with self.subTest(run_loop=run_loop):
                client = Mock()
                sched = srv.Scheduler(client, "event")
                def detail(tid):
                    sched.stop()
                    return {"zh_user_status": "可报名", "player_num": 1, "max_player_number": 2}
                client.detail.side_effect = detail
                if run_loop:
                    sched.run()
                else:
                    self.assertFalse(sched.try_once().attempted)
                client.signup.assert_not_called()

    def test_tunnel_stop_waits_for_process_and_supervisor(self):
        tunnel = srv.Tunnel(host="sve.example.com", user="synthetic-user")
        process = Mock()
        worker = Mock()
        worker.is_alive.return_value = False
        tunnel.proc, tunnel._thread = process, worker
        tunnel.stop()
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=10)
        worker.join.assert_called_once_with(timeout=15)
        self.assertIsNone(tunnel.proc)
        worker.is_alive.return_value = True
        with self.assertRaises(RuntimeError):
            tunnel.stop()

    def test_tunnel_timeout_kills_then_waits_and_failure_prevents_replacement(self):
        tunnel = srv.Tunnel(host="sve.example.com", user="synthetic-user")
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired("synthetic ssh", 10), None]
        tunnel.proc = process
        tunnel.stop()
        process.kill.assert_called_once_with()
        self.assertEqual([10, 5], [call.kwargs["timeout"] for call in process.wait.call_args_list])
        old = Mock()
        old.stop.side_effect = RuntimeError("synthetic tunnel still alive")
        srv.STATE["tunnel"] = old
        with patch.object(srv, "Tunnel") as replacement:
            with self.assertRaises(srv.AccountConflict):
                srv.start_tunnel({"host": "other.example.com", "user": "synthetic-user"})
        replacement.assert_not_called()

    def test_tunnel_keeps_live_process_on_communication_failure(self):
        tunnel = srv.Tunnel(host="sve.example.com", user="synthetic-user", logger=Mock())
        process = Mock()
        process.poll.return_value = None
        process.communicate.side_effect = OSError("synthetic communication failure")
        with patch.object(subprocess, "Popen", return_value=process) as spawn:
            tunnel._run_loop()
        spawn.assert_called_once()
        self.assertIs(process, tunnel.proc)
        tunnel.stop()
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=10)

    def test_cancel_during_detail_blocks_handover_until_run_returns(self):
        client = Mock()
        sched = srv.Scheduler(client, "event")
        key = "10001|event"
        srv.STATE["schedulers"][key] = sched
        body = {"enabled": True, "host": "sve.example.com", "user": "synthetic-user"}
        def detail(tid):
            self.assertTrue(self.post("/api/unsubscribe", {"tournament_id": "event"})["ok"])
            self.assertFalse(self.post("/api/remote/config", body)["ok"])
            return {"zh_user_status": "可报名", "player_num": 1, "max_player_number": 2}
        client.detail.side_effect = detail
        with patch.object(srv.threading, "Thread") as thread:
            with srv.LOCK:
                srv.scheduler_thread(sched, key)
            thread.call_args.kwargs["target"]()
        client.signup.assert_not_called()
        self.assertEqual(set(), srv.STATE["scheduler_threads"])
        with patch.object(srv, "start_tunnel"), patch.object(srv.threading, "Timer"):
            self.assertTrue(self.post("/api/remote/config", body)["ok"])

    def test_login_configuration_change_requires_a_new_login(self):
        observed = srv.login_snapshot()
        link = self.remote()
        before = self.snapshot()
        with self.assertRaises(srv.AccountConflict):
            srv.save_explicit_login(fake_account(), observed)
        link.save_session.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_remote_selection_and_forget_do_not_write_server(self):
        link = self.remote()
        self.assertTrue(self.post("/api/account/select", {"name": "10002"})["ok"])
        self.assertFalse(self.post("/api/account/forget", {"name": "10002"})["ok"])
        link.push_accounts.assert_not_called()
        link.save_session.assert_not_called()

    def test_failed_remote_replacement_rolls_back_configuration(self):
        self.remote()
        srv.save_json(srv.REMOTE_FILE, srv.STATE["remote"])
        before = self.snapshot()
        config = copy.deepcopy(srv.STATE["remote"])
        tunnel = srv.STATE["tunnel"]
        tunnel.stop.side_effect = RuntimeError("synthetic old tunnel still alive")
        with patch.object(srv, "Tunnel") as replacement:
            result = self.post("/api/remote/config", dict(config, host="other.example.com"))
        self.assertFalse(result["ok"])
        self.assertEqual(config, srv.STATE["remote"])
        self.assertIs(tunnel, srv.STATE["tunnel"])
        self.assertEqual(before, self.snapshot())
        replacement.assert_not_called()

    def test_remote_subscription_identity_conflicts_never_forward(self):
        link = self.remote()
        summary = copy.deepcopy(link.accounts_summary.return_value)
        for problem in ("uid", "missing_remote", "missing_local", "old_server"):
            with self.subTest(problem=problem):
                response = copy.deepcopy(summary)
                account = "10001"
                if problem == "uid":
                    response["accounts"][0]["uid"] = 100000
                elif problem == "missing_remote":
                    response["accounts"] = []
                elif problem == "missing_local":
                    account = "test-unknown"
                else:
                    response.pop("subscription_identity")
                link.accounts_summary.return_value = response
                before = self.snapshot()
                for path in ("/api/subscribe", "/api/unsubscribe"):
                    self.assertFalse(self.post(path, {"account": account, "tournament_id": "event"})["ok"])
                self.assertEqual(before, self.snapshot())
        link.subscribe.assert_not_called()
        link.unsubscribe.assert_not_called()

    def test_remote_subscription_identity_is_pinned_and_connection_is_locked(self):
        link = self.remote()
        def operation(tid, account, *, expected_uid):
            self.assertEqual("10001", expected_uid)
            config = dict(srv.STATE["remote"], host="other.example.com")
            result = srv.app.test_client().post("/api/remote/config", json=config).get_json()
            self.assertFalse(result["ok"])
            return {"ok": True}
        link.subscribe.side_effect = operation
        link.unsubscribe.side_effect = operation
        before = self.snapshot()
        for path in ("/api/subscribe", "/api/unsubscribe"):
            self.assertTrue(self.post(path, {"tournament_id": "event"})["ok"])
        self.assertEqual(before, self.snapshot())

    def test_server_rejects_stale_uid_before_changing_subscription(self):
        before = self.snapshot()
        for uid in (None, True, 100000):
            for path in ("/api/subscribe", "/api/unsubscribe"):
                self.assertFalse(self.post(path, {"account": "10001",
                                                 "expected_uid": uid, "tournament_id": "event"})["ok"])
                self.assertEqual(before, self.snapshot())

    def test_server_rechecks_uid_after_detail_before_registering_scheduler(self):
        before_subs = Path(srv.SUBS_FILE).read_bytes()
        def detail(tid):
            srv.STATE["accounts"]["10001"] = fake_account(100000, "10001")
            return {"name": "synthetic-event"}
        with patch.object(srv.SveClient, "detail", side_effect=detail):
            result = self.post("/api/subscribe", {"account": "10001",
                                                  "expected_uid": "10001", "tournament_id": "event"})
        self.assertFalse(result["ok"])
        self.assertEqual(before_subs, Path(srv.SUBS_FILE).read_bytes())
        self.assertEqual({}, srv.STATE["schedulers"])

    def test_agent_subscription_transport_includes_expected_uid(self):
        link = srv.AgentLink()
        with patch.object(link, "_req", return_value={"ok": True}) as send:
            for cancel in (False, True):
                operation = link.unsubscribe if cancel else link.subscribe
                operation("event", "10001", expected_uid="10001")
                self.assertEqual({"tournament_id": "event", "account_id": "10001", "expected_uid": "10001"},
                                 send.call_args.args[2])

    def test_remote_unsubscribe_uses_selected_or_explicit_account(self):
        link = self.remote()
        remote_active = "10001"
        self.assertTrue(self.post("/api/account/select", {"name": "10002"})["ok"])
        before = self.snapshot()
        for body, expected in (({"tournament_id": "event"}, "10002"),
                               ({"tournament_id": "event", "account": "10003"}, "10003")):
            with self.subTest(body=body):
                # Model the server's fallback to its independent active account.
                subscriptions = {(name, "event") for name in
                                 (remote_active, "10002", "10003")}
                def unsubscribe(tid, account=None, **kwargs):
                    subscriptions.discard((account or remote_active, tid))
                    return {"ok": True}
                link.unsubscribe.reset_mock()
                link.unsubscribe.side_effect = unsubscribe
                self.assertTrue(self.post("/api/unsubscribe", body)["ok"])
                link.unsubscribe.assert_called_once_with("event", expected,
                                                         expected_uid=srv.normalized_uid(srv.STATE["accounts"][expected].uid))
                self.assertEqual({(name, "event") for name in
                                  (remote_active, "10002", "10003") if name != expected},
                                 subscriptions)
                self.assertEqual(before, self.snapshot())
        link.push_accounts.assert_not_called()
        link.save_session.assert_not_called()

    def test_remote_unsubscribe_without_any_account_never_uses_server_default(self):
        link = self.remote()
        srv.STATE["active"] = None
        self.client.delete_cookie("sve_account_id")
        before = self.snapshot()
        result = self.post("/api/unsubscribe", {"tournament_id": "event"})
        self.assertFalse(result["ok"])
        self.assertEqual("identity_conflict", result["code"])
        self.assertEqual([], link.mock_calls)
        self.assertEqual(before, self.snapshot())

    def test_readonly_comparison_keeps_uid_identity_after_display_rename(self):
        link = self.remote()
        link.accounts_summary.return_value["accounts"][0]["name"] = "test-renamed"
        before = self.snapshot()
        result = self.post("/api/remote/push_accounts", {})
        self.assertEqual([], result["conflicts"])  # Same UID remains the same identity after rename.
        self.assertEqual(before, self.snapshot())
        link.save_session.assert_not_called()

    def test_subscription_route_serves_the_shared_shell(self):
        with srv.app.test_client() as client:
            with client.get("/") as home, client.get("/subs") as subscriptions:
                self.assertEqual(subscriptions.status_code, 200)
                self.assertIn(b'id="subsPage"', home.data)
                self.assertEqual(subscriptions.data, home.data)
            with client.get("/subs.html") as legacy:
                self.assertEqual(legacy.status_code, 200)
                self.assertIn(b"url=./subs", legacy.data)

    def test_cookie_login_keeps_old_response_contract(self):
        account = fake_account(100000)
        self.info.return_value = {"id": account.uid, "nickname": account.nickname}
        with patch.object(srv.SveClient, "login_with_cookie", return_value=account.sessionid):
            result = self.post("/api/login/cookie", {"cookies": "NTES_SESS=fake"})
        self.assertTrue(result["ok"])
        self.assertTrue({"ok", "name", "uid", "nickname"} <= set(result))
        self.assertEqual(account.name, srv.STATE["active"])

    def test_cookie_login_same_name_different_uid_is_independent(self):
        original = srv.STATE["accounts"]["10001"].to_dict()
        self.info.return_value = {"id": 100000, "nickname": "10001"}
        with patch.object(srv.SveClient, "login_with_cookie", return_value="synthetic-session"):
            self.assertTrue(self.post("/api/login/cookie", {"cookies": "NTES_SESS=fake"})["ok"])
        self.assertEqual(original, srv.STATE["accounts"]["10001"].to_dict())
        self.assertIn("100000", srv.STATE["accounts"])

    def test_browser_login_accepts_same_name_without_overwriting_other_uid(self):
        before = self.snapshot()
        def immediate_thread(target, **kwargs):
            thread = Mock()
            thread.start.side_effect = target
            return thread
        with patch.object(srv, "login_capability", return_value={"can_login": True}), \
                patch.object(srv, "LoginWindow") as window, \
                patch.object(srv.threading, "Thread", side_effect=immediate_thread):
            window.return_value.__enter__.return_value.wait_for_login.return_value = fake_account(100000, "10001")
            self.assertTrue(self.post("/api/login/start", {})["ok"])
        self.assertTrue(srv.STATE["login"]["ok"])
        self.assertEqual(before[1]["10001"], self.snapshot()[1]["10001"])
        self.assertIn("100000", srv.STATE["accounts"])


class UIDIdentityTests(unittest.TestCase):
    setUp = AccountProtectionTests.setUp
    post = AccountProtectionTests.post
    snapshot = AccountProtectionTests.snapshot
    session_request = AccountProtectionTests.session_request
    remote = AccountProtectionTests.remote

    def verified(self, account, expected=0):
        self.info.return_value = {"id": account.uid, "nickname": account.nickname}
        result = self.session_request(account, expected)
        self.assertTrue(result["ok"], result)
        return result

    def test_same_nickname_two_uids_survive_restart_and_rename_with_references(self):
        nickname = "test-同名'\"<>&|"
        original = srv.STATE["accounts"]["10001"]
        a = fake_account(name=nickname, session="synthetic-a-new")
        self.verified(a, original.version)
        b = fake_account(100000, nickname)
        self.verified(b)
        refs = copy.deepcopy((srv.STATE["subs"], srv.STATE["records"]))
        a.nickname = a.name = "test-新名"
        self.verified(a, original.version)
        self.assertIs(original, srv.STATE["accounts"]["10001"])
        self.assertEqual(nickname, srv.STATE["accounts"]["100000"].nickname)
        self.assertEqual(refs, (srv.STATE["subs"], srv.STATE["records"]))
        srv.load_all()
        self.assertEqual(15, len(srv.STATE["accounts"]))
        self.assertEqual("test-新名", srv.STATE["accounts"]["10001"].nickname)
        self.assertEqual(refs, (srv.STATE["subs"], srv.STATE["records"]))

    def test_independent_subscriptions_and_relogin_keep_one_worker_per_id(self):
        a, b = fake_account(name="test-shared"), fake_account(100000, "test-shared")
        self.verified(a, 1)
        self.verified(b)
        srv.STATE["subs"]["10001|shared"] = {
            "account_id": "10001", "uid": 10001, "tournament_id": "shared",
            "migration_sources": ["test-old-name|shared"], "logs": ["synthetic migration log"]}
        with patch.object(srv.SveClient, "detail", return_value={"name": "synthetic-event"}), \
                patch.object(srv.threading, "Thread") as thread:
            for aid in ("10001", "100000", "10001"):
                self.assertTrue(self.post("/api/subscribe", {"account_id": aid, "tournament_id": "shared"})["ok"])
            self.assertEqual(2, thread.return_value.start.call_count)
            self.assertEqual(["test-old-name|shared"], srv.STATE["subs"]["10001|shared"]["migration_sources"])
            first = srv.STATE["schedulers"]["10001|shared"]
            second = srv.STATE["schedulers"]["100000|shared"]
            a.nickname = a.name = "test-renamed"
            relogin_session = "synthetic-relogin-session"
            a.sessionid = relogin_session
            self.verified(a, srv.STATE["accounts"]["10001"].version)
            self.assertEqual(a.sessionid, first.client.account.sessionid)
            self.assertEqual("test-shared", second.client.account.nickname)
            self.assertEqual(2, thread.return_value.start.call_count)

    def test_unknown_ids_ambiguous_names_and_invalid_batch_have_no_upstream_actions(self):
        self.verified(fake_account(100000, "10001"))
        before = self.snapshot()
        with patch.object(srv.SveClient, "detail") as detail:
            for path in ("/api/enroll", "/api/cancel", "/api/subscribe", "/api/unsubscribe"):
                for selector in ({"account_id": "999"}, {"account": "10001"}, {"account_id": "invalid"}):
                    self.assertFalse(self.post(path, dict(selector, tournament_id="event"))["ok"])
            self.assertFalse(self.client.get("/api/tournaments?account_id=999").get_json()["ok"])
            self.assertFalse(self.client.get("/api/records?account_id=999").get_json()["ok"])
            self.assertFalse(self.post("/api/records/scan", {"account_id": "999"})["ok"])
            self.assertFalse(self.post("/api/batch_enroll", {"account_ids": ["10001", "999"], "tournaments": ["event"]})["ok"])
            detail.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_browser_choices_are_per_client_and_do_not_change_global_default(self):
        other = srv.app.test_client()
        self.assertTrue(self.post("/api/account/select", {"account_id": "10002"})["ok"])
        self.assertTrue(other.post("/api/account/select", json={"account_id": "10001"}).get_json()["ok"])
        with patch.object(srv, "login_capability", return_value={"can_login": False, "browser": ""}):
            self.assertEqual("10002", self.client.get("/api/state").get_json()["active_account_id"])
            self.assertEqual("10001", other.get("/api/state").get_json()["active_account_id"])
        self.assertEqual("10001", srv.STATE["active"])

    def test_uid_string_and_integer_represent_one_canonical_identity(self):
        account = fake_account("0010001", "test-normalized")
        self.info.return_value = {"id": 10001, "nickname": "test-normalized"}
        self.assertTrue(self.session_request(account, 1)["ok"])
        self.assertEqual(14, len(srv.STATE["accounts"]))
        self.assertEqual(10001, srv.STATE["accounts"]["10001"].uid)
        self.assertNotIn("0010001", srv.STATE["accounts"])

    def test_migrated_session_uid_is_verified_before_any_action_and_mismatch_is_blocked(self):
        client = srv.SveClient(srv.STATE["accounts"]["10001"])
        self.info.return_value = {"id": 10002}
        with patch.object(client, "_request") as send:
            with self.assertRaises(srv.SveError):
                REAL_ACTION(client, "event", "signup")
            send.assert_not_called()
        self.assertTrue(client.account.__dict__["_identity_blocked"])

    def test_verified_credentials_are_cached_and_new_login_refreshes_live_cache(self):
        account = srv.STATE["accounts"]["10001"]
        client = srv.SveClient(account)
        self.info.return_value = {"id": 10001}
        with patch.object(client, "_request", return_value={"ok": True}) as send:
            REAL_ACTION(client, "event", "signup")
            REAL_ACTION(client, "event", "cancel")
            self.info.assert_called_once()
            self.assertEqual(10001, send.call_args.kwargs["body"]["uid"])
        self.verified(fake_account(session="synthetic-cache-refresh"), 1)
        self.info.reset_mock()
        with patch.object(client, "_request", return_value={"ok": True}):
            REAL_ACTION(client, "event", "signup")
        self.info.assert_not_called()
        self.assertEqual("synthetic-cache-refresh", client.account.sessionid)

    def test_identity_failure_pauses_task_and_retains_original_for_review(self):
        client = Mock(account=srv.STATE["accounts"]["10001"])
        client.detail.side_effect = srv.SveError("synthetic identity mismatch", code="identity_mismatch")
        scheduler = srv.Scheduler(client, "event", logger=Mock(),
                                  on_event=lambda kind, data: srv._on_event("10001|event", kind, data))
        scheduler.run()
        self.assertTrue(scheduler.stop_flag.is_set())
        self.assertFalse(srv.STATE["subs"]["10001|event"]["enabled"])
        self.assertEqual("runtime_identity_mismatch", srv.STATE["pending"][-1]["category"])
        client.signup.assert_not_called()

    def test_identity_blocked_worker_exit_allows_explicit_resubscribe(self):
        key = "10001|event"
        client = Mock(account=srv.STATE["accounts"]["10001"])
        client.detail.side_effect = srv.SveError("synthetic identity mismatch", code="identity_mismatch")
        scheduler = srv.Scheduler(client, "event", logger=Mock(),
                                  on_event=lambda kind, data: srv._on_event(key, kind, data))
        srv.STATE["schedulers"][key] = scheduler
        with patch.object(srv.threading, "Thread") as thread:
            with srv.LOCK:
                worker = srv.scheduler_thread(scheduler, key)
            target = thread.call_args.kwargs["target"]

            # Force login/resubscribe after stop_flag is set but before run returns.
            def on_event(kind, data):
                result = srv._on_event(key, kind, data)
                if kind == "stop":
                    self.verified(fake_account(session="synthetic-corrected-session"), 1)
                    before = self.snapshot()
                    with patch.object(srv.SveClient, "detail", return_value={"name": "synthetic-event"}):
                        response = self.post("/api/subscribe", {"tournament_id": "event"})
                    self.assertFalse(response["ok"])
                    self.assertEqual("subscription_conflict", response["code"])
                    self.assertEqual(before, self.snapshot())
                    self.assertIs(scheduler, srv.STATE["schedulers"][key])
                    self.assertIs(worker, srv.STATE["workers"][key])
                return result

            scheduler.on_event = on_event
            target()
            self.assertNotIn(key, srv.STATE["schedulers"])
            self.assertNotIn(key, srv.STATE["workers"])
            self.assertNotIn(worker, srv.STATE["scheduler_threads"])
            self.assertFalse(srv.STATE["subs"][key]["enabled"])
            # Login alone must not resume a blocked subscription.
            self.assertTrue(srv.STATE["subs"][key]["identity_blocked"])
            with patch.object(srv.SveClient, "detail", return_value={"name": "synthetic-event"}):
                response = self.post("/api/subscribe", {"tournament_id": "event"})
            self.assertTrue(response["ok"])
            self.assertFalse(response.get("already", False))
            self.assertTrue(srv.STATE["subs"][key]["enabled"])
            replacement = srv.STATE["schedulers"][key]
            self.assertIsNot(scheduler, replacement)
            self.assertFalse(replacement.stop_flag.is_set())
            worker.start.assert_called_once_with()
        client.signup.assert_not_called()

    def test_worker_exit_keeps_completed_task_and_newer_registration(self):
        key = "10001|event"
        for completed, replaced, fail in ((True, False, False), (False, True, False),
                                          (False, False, True)):
            with self.subTest(completed=completed, replaced=replaced, fail=fail):
                scheduler = srv.Scheduler(Mock(), "event")
                scheduler.completed = completed
                srv.STATE["schedulers"][key] = scheduler
                with patch.object(srv.threading, "Thread") as thread:
                    with srv.LOCK:
                        worker = srv.scheduler_thread(scheduler, key)
                    target = thread.call_args.kwargs["target"]
                    if replaced:
                        newer = srv.Scheduler(Mock(), "event")
                        newer_worker = Mock()
                        srv.STATE["schedulers"][key] = newer
                        srv.STATE["workers"][key] = newer_worker
                        srv.STATE["scheduler_threads"].add(newer_worker)
                    with patch.object(scheduler, "run", side_effect=RuntimeError("synthetic") if fail else None):
                        if fail:
                            with self.assertRaises(RuntimeError):
                                target()
                        else:
                            target()
                    self.assertNotIn(worker, srv.STATE["scheduler_threads"])
                    if replaced:
                        self.assertIs(newer, srv.STATE["schedulers"][key])
                        self.assertIs(newer_worker, srv.STATE["workers"][key])
                        self.assertIn(newer_worker, srv.STATE["scheduler_threads"])
                    else:
                        self.assertNotIn(key, srv.STATE["workers"])
                        if completed:
                            self.assertIs(scheduler, srv.STATE["schedulers"][key])
                        else:
                            self.assertNotIn(key, srv.STATE["schedulers"])

    def test_late_identity_failure_cannot_disable_a_newly_verified_session(self):
        self.verified(fake_account(session="synthetic-verified-newer"), 1)
        before = self.snapshot()
        self.assertIs(False, srv._on_event("10001|event", "identity_blocked", {"account_version": 1}))
        self.assertEqual(before, self.snapshot())

    def test_region_list_remains_available_with_an_external_identity_directory(self):
        before = self.snapshot()
        result = self.client.get("/api/areas").get_json()
        self.assertTrue(result["ok"])
        self.assertTrue(result["provinces"])
        self.assertEqual(before, self.snapshot())

    def test_session_retry_receipt_survives_restart_and_is_not_a_second_update(self):
        account = fake_account(session="synthetic-retry-session")
        body = {"account": account.to_dict(), "expected_version": 1, "operation_id": uuid.uuid4().hex}
        self.info.return_value = {"id": account.uid, "nickname": account.nickname}
        first = self.post("/api/accounts/session", body)
        self.assertTrue(first["ok"])
        before = Path(self.data / "identity-v2.json").read_bytes()
        srv.load_all()
        self.assertEqual(first, self.post("/api/accounts/session", body))
        self.assertEqual(before, Path(self.data / "identity-v2.json").read_bytes())
        self.info.assert_called_once()
        body["account"] = fake_account(10002).to_dict()
        self.assertFalse(self.post("/api/accounts/session", body)["ok"])
        self.info.assert_called_once()

    def test_delete_is_versioned_idempotent_keeps_history_and_blocks_cache_replay(self):
        account = srv.STATE["accounts"]["10001"]
        scheduler = Mock()
        srv.STATE["schedulers"]["10001|event"] = scheduler
        body = {"account_id": "10001", "expected_version": account.version, "operation_id": uuid.uuid4().hex}
        first = self.post("/api/account/forget", body)
        self.assertTrue(first["ok"])
        scheduler.stop.assert_called_once()
        self.dispatch.assert_called_once()
        before = Path(self.data / "identity-v2.json").read_bytes()
        self.assertEqual(first, self.post("/api/account/forget", body))
        self.assertEqual(before, Path(self.data / "identity-v2.json").read_bytes())
        self.assertIn("10001|event", srv.STATE["records"])
        self.assertNotIn("10001|event", srv.STATE["subs"])
        self.assertIn("10001", srv.STATE["tombstones"])
        self.assertTrue(self.post("/api/accounts/sync", {"accounts": {account.name: account.to_dict()}})["readonly"])
        self.assertFalse(self.session_request(account)["ok"])
        self.assertNotIn("10001", srv.STATE["accounts"])
        self.info.assert_not_called()
        with self.assertRaises(srv.SveError):
            srv.SveClient(account).detail("event")
        with patch.object(srv, "refresh_records"):
            records = self.client.get("/api/records").get_json()
        self.assertTrue(records["records"][0]["deleted"])
        self.assertIn("已删除", records["records"][0]["account"])

    def test_deleted_identity_requires_explicit_restore_version(self):
        old = srv.STATE["accounts"]["10001"]
        self.assertTrue(self.post("/api/account/forget", {"account_id": "10001", "expected_version": 1,
                                                        "operation_id": uuid.uuid4().hex})["ok"])
        replacement = fake_account(session="synthetic-restored-session")
        self.info.return_value = {"id": replacement.uid, "nickname": replacement.nickname}
        body = {"account": replacement.to_dict(), "expected_version": 2, "operation_id": uuid.uuid4().hex}
        self.assertFalse(self.post("/api/accounts/session", body)["ok"])
        body.update(restore=True, operation_id=uuid.uuid4().hex)
        self.assertTrue(self.post("/api/accounts/session", body)["ok"])
        self.assertEqual(3, srv.STATE["accounts"]["10001"].version)
        self.assertEqual(2, srv.STATE["accounts"]["10001"].session_version)
        self.assertIsNot(old, srv.STATE["accounts"]["10001"])
        self.assertEqual({}, srv.STATE["subs"])

    def test_cancelled_worker_prevents_duplicate_registration_until_it_exits(self):
        worker = Mock()
        srv.STATE["workers"]["10001|new-event"] = worker
        with patch.object(srv.SveClient, "detail", return_value={"name": "synthetic-event"}):
            self.assertFalse(self.post("/api/subscribe", {"account_id": "10001", "tournament_id": "new-event"})["ok"])
        self.assertNotIn("10001|new-event", srv.STATE["subs"])

    def test_old_completion_timer_cannot_remove_a_replaced_subscription(self):
        key = "10001|event"
        previous = srv.STATE["subs"][key]
        replacement = dict(previous, logs=["new-generation"])
        srv.STATE["subs"][key] = replacement
        before = self.snapshot()
        srv._auto_remove_sub(key, previous)
        self.assertIs(replacement, srv.STATE["subs"][key])
        self.assertEqual(before, self.snapshot())

    def test_remote_server_only_account_uses_server_for_queries_and_actions(self):
        link = self.remote()
        self.assertNotIn("10005", srv.STATE["accounts"])
        link.endpoint.return_value = {"ok": True, "account_id": "10005", "items": []}
        link.subscribe.return_value = {"ok": True, "account_id": "10005"}
        before = self.snapshot()
        with patch.object(srv.SveClient, "detail") as detail:
            self.assertTrue(self.client.get("/api/tournaments?account_id=10005").get_json()["ok"])
            self.assertTrue(self.post("/api/enroll", {"account_id": "10005", "tournament_id": "event"})["ok"])
            self.assertTrue(self.post("/api/subscribe", {"account_id": "10005", "tournament_id": "event"})["ok"])
            detail.assert_not_called()
        self.assertEqual("10005", link.endpoint.call_args.args[2]["account_id"])
        link.subscribe.assert_called_once_with("event", "10005", expected_uid=str(10001 + 4))
        self.assertEqual(before, self.snapshot())

    def test_deleted_old_name_cannot_transfer_a_request_to_a_new_same_name_uid(self):
        self.assertTrue(self.post("/api/account/forget", {"account_id": "10001", "expected_version": 1,
                                                        "operation_id": uuid.uuid4().hex})["ok"])
        self.verified(fake_account(100000, "10001"))
        with patch.object(srv.SveClient, "detail", return_value={"name": "synthetic-event", "zh_user_status": "已报名"}) as detail:
            self.assertFalse(self.post("/api/enroll", {"account": "10001", "tournament_id": "event"})["ok"])
            detail.assert_not_called()
            self.assertTrue(self.post("/api/enroll", {"account_id": "100000", "tournament_id": "event"})["ok"])
            detail.assert_called_once()

    def numeric_name_subscriptions(self):
        srv.STATE["accounts"]["10001"] = fake_account(10001, "test-alpha")
        srv.STATE["accounts"]["10002"] = fake_account(10002, "10001")
        srv.STATE["accounts"]["10002"].legacy_names = ["0010001"]
        for aid in ("10001", "10002"):
            srv.STATE["subs"][aid + "|event"] = {
                "account_id": aid, "account": aid, "uid": int(aid),
                "tournament_id": "event", "enabled": True, "name": "synthetic-event"}
            srv.STATE["schedulers"][aid + "|event"] = Mock(completed=False, attempts=0)
        srv.persist_accounts()
        srv.persist_subs()

    def test_old_subscription_page_uid_key_cannot_cancel_a_numeric_name_account(self):
        self.numeric_name_subscriptions()
        with patch.object(srv, "login_capability", return_value={"can_login": False, "browser": ""}):
            state = self.client.get("/api/state").get_json()
        # Exact legacy subs.html shape: take the account prefix from the new state key.
        key = state["subs_grouped"]["10001"][0]["key"]
        account, tid = key.split("|")
        before = self.snapshot()
        for field in ("account", "name"):
            for selector in (account, "00" + account):
                result = self.post("/api/unsubscribe", {field: selector, "tournament_id": tid})
                self.assertFalse(result["ok"])
                self.assertEqual("identity_conflict", result["code"])
                self.assertEqual(before, self.snapshot())
        for scheduler in srv.STATE["schedulers"].values():
            scheduler.stop.assert_not_called()
        # New pages can still explicitly cancel either account independently.
        other = srv.STATE["schedulers"]["10002|event"]
        self.assertTrue(self.post("/api/unsubscribe", {"account_id": "10001", "tournament_id": tid})["ok"])
        self.assertIn("10002|event", srv.STATE["subs"])
        other.stop.assert_not_called()
        self.assertTrue(self.post("/api/unsubscribe", {"account_id": "10002", "tournament_id": tid})["ok"])
        other.stop.assert_called_once_with()

    def test_legacy_uid_or_unique_name_still_cancels_the_same_identity(self):
        for selector in ("10001", "0010001", "test-alpha", "test-former-alpha"):
            with self.subTest(selector=selector):
                self.numeric_name_subscriptions()
                account = srv.STATE["accounts"]["10002"]
                account.name = account.nickname = "test-beta"
                account.legacy_names = []
                srv.STATE["accounts"]["10001"].legacy_names = ["test-former-alpha"]
                srv.persist_accounts()
                self.assertTrue(self.post("/api/unsubscribe", {"account": selector, "tournament_id": "event"})["ok"])
                self.assertNotIn("10001|event", srv.STATE["subs"])
                self.assertIn("10002|event", srv.STATE["subs"])
                srv.STATE["schedulers"]["10002|event"].stop.assert_not_called()

    def test_legacy_numeric_alias_cannot_retarget_a_deleted_uid(self):
        self.numeric_name_subscriptions()
        self.assertTrue(self.post("/api/account/forget", {"account_id": "10001", "expected_version": 1,
                                                        "operation_id": uuid.uuid4().hex})["ok"])
        before = self.snapshot()
        for selector in ("10001", "0010001"):
            self.assertFalse(self.post("/api/unsubscribe", {"account": selector, "tournament_id": "event"})["ok"])
            self.assertEqual(before, self.snapshot())
        srv.STATE["schedulers"]["10002|event"].stop.assert_not_called()
        link = self.remote()
        link.accounts_summary.return_value["tombstones"] = copy.deepcopy(srv.STATE["tombstones"])
        before = self.snapshot()
        for selector in ("10001", "0010001"):
            self.assertFalse(self.post("/api/unsubscribe", {"account": selector, "tournament_id": "event"})["ok"])
            self.assertEqual(before, self.snapshot())
        link.unsubscribe.assert_not_called()

    def test_remote_legacy_uid_name_collision_is_rejected_before_forwarding(self):
        self.numeric_name_subscriptions()
        link = self.remote()
        # The local cache cannot be used to resolve a server-only UID.
        srv.STATE["accounts"].pop("10001")
        link.unsubscribe.return_value = {"ok": True, "account_id": "10002"}
        before = self.snapshot()
        for selector in ("10001", "0010001"):
            self.assertFalse(self.post("/api/unsubscribe", {"account": selector, "tournament_id": "event"})["ok"])
            self.assertEqual(before, self.snapshot())
        link.unsubscribe.assert_not_called()
        link.unsubscribe.return_value = {"ok": True, "account_id": "10001"}
        self.assertTrue(self.post("/api/unsubscribe", {"account_id": "10001", "tournament_id": "event"})["ok"])
        link.unsubscribe.assert_called_once_with("event", "10001", expected_uid="10001")
        self.assertEqual(before, self.snapshot())

    def test_record_deletion_rejects_mismatched_id_and_keeps_deleted_history_addressable(self):
        before = self.snapshot()
        self.assertFalse(self.post("/api/records/forget", {"account_id": "10002", "key": "10001|event"})["ok"])
        self.assertEqual(before, self.snapshot())
        self.assertTrue(self.post("/api/account/forget", {"account_id": "10001", "expected_version": 1,
                                                        "operation_id": uuid.uuid4().hex})["ok"])
        self.assertTrue(self.post("/api/records/forget", {"account_id": "10001", "key": "10001|event"})["ok"])
        self.assertNotIn("10001|event", srv.STATE["records"])

    def test_remote_record_deletion_uses_agent_tombstone_validation(self):
        self.assertTrue(self.post("/api/account/forget", {"account_id": "10001", "expected_version": 1,
                                                        "operation_id": uuid.uuid4().hex})["ok"])
        link = self.remote()
        # Use the local isolated server as the agent; its live summary omits the tombstone.
        def endpoint(method, path, body, params):
            with patch.object(srv, "remote_enabled", return_value=False):
                return srv.app.test_client().open(path, method=method, json=body).get_json()
        link.endpoint.side_effect = endpoint
        before = self.snapshot()
        for body in ({"account_id": "10002", "key": "10001|event"},
                     {"account_id": "10001", "expected_uid": "10002", "key": "10001|event"},
                     {"account_id": "100000", "key": "100000|event"}):
            self.assertFalse(self.post("/api/records/forget", body)["ok"])
            self.assertEqual(before, self.snapshot())
        self.assertTrue(self.post("/api/records/forget", {"account_id": "10001", "key": "10001|event"})["ok"])
        self.assertNotIn("10001|event", srv.STATE["records"])
        self.assertIn("10001", srv.STATE["tombstones"])
        link.accounts_summary.assert_not_called()

    def test_atomic_publish_failure_preserves_all_identity_and_receipt_state(self):
        import identity_store
        before = self.snapshot()
        account = fake_account(session="synthetic-failed-publication")
        self.info.return_value = {"id": account.uid, "nickname": account.nickname}
        with patch.object(identity_store.os, "replace", side_effect=OSError("synthetic replace failure")):
            self.assertFalse(self.session_request(account, 1)["ok"])
        self.assertEqual(before, self.snapshot())
        self.assertTrue(srv.STATE["storage_failed"])

    def test_uncertain_publication_stops_execution_until_final_dataset_is_reloaded(self):
        import identity_store
        scheduler = Mock()
        srv.STATE["schedulers"]["10001|event"] = scheduler
        account = fake_account(session="synthetic-uncertain-publication")
        self.info.return_value = {"id": account.uid, "nickname": account.nickname}
        with patch.object(identity_store, "sync_directory", side_effect=OSError("synthetic directory sync failure")):
            self.assertFalse(self.session_request(account, 1)["ok"])
        self.assertTrue(srv.STATE["storage_failed"])
        scheduler.stop.assert_called_once()
        self.assertEqual("storage_unavailable", self.client.get("/api/state").get_json()["code"])
        # Simulate a new process loading the atomically published final document.
        disk = srv.IdentityStore(self.data).load()
        self.assertEqual(account.sessionid, disk["accounts"]["10001"]["sessionid"])

    def test_v2_legacy_reader_is_readonly_and_unknown_id_has_no_fallback(self):
        reader = load_module("sve_m01_environment", "scripts/sveenv.py")
        with patch.object(reader, "DATA_DIR", str(self.data)):
            self.assertEqual("10001", reader.load_session("10001")["account_id"])
            self.assertEqual({}, reader.load_session("999"))
            with self.assertRaises(RuntimeError):
                reader.save_session(fake_account().to_dict())
            with self.assertRaises(RuntimeError):
                reader.assert_legacy_executor_allowed()

    def test_browser_intent_allows_cookie_retry_and_new_intent_refreshes_same_uid_nickname(self):
        account = fake_account()
        self.info.return_value = {"id": account.uid, "nickname": "test-first"}
        self.assertTrue(self.post("/api/login/intent", {})["ok"])
        original_intent = self.client.get_cookie("sve_login_intent").value
        with patch.object(srv.SveClient, "login_with_cookie", return_value=account.sessionid) as exchange:
            first = self.post("/api/login/cookie", {"cookies": "NTES_SESS=fake"})
            self.assertTrue(first["ok"])
            version = srv.STATE["accounts"]["10001"].version
            self.assertIsNone(self.client.get_cookie("sve_login_intent"))
            # Simulate a transport retry where the first response/cookie deletion
            # never arrived at the browser.
            self.client.set_cookie("sve_login_intent", original_intent)
            self.assertTrue(self.post("/api/login/cookie", {"cookies": "NTES_SESS=fake"})["ok"])
            self.assertEqual(version, srv.STATE["accounts"]["10001"].version)
            exchange.assert_called_once()
            self.info.assert_called_once()
            self.assertTrue(self.post("/api/login/intent", {})["ok"])
            self.info.return_value = {"id": account.uid, "nickname": "test-renamed"}
            self.assertTrue(self.post("/api/login/cookie", {"cookies": "NTES_SESS=fake"})["ok"])
        self.assertEqual("test-renamed", srv.STATE["accounts"]["10001"].nickname)
        self.assertEqual(14, len(srv.STATE["accounts"]))

    def test_same_name_batch_records_are_bound_to_each_id(self):
        self.verified(fake_account(name="test-shared"), 1)
        self.verified(fake_account(100000, "test-shared"))
        joined = set()
        def detail(client, tid):
            return {"name": "synthetic-batch", "zh_user_status": "已报名" if client.account.account_id in joined else "可报名"}
        def signup(client, tid):
            joined.add(client.account.account_id)
        window = Mock()
        window.is_open.return_value = True
        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "signup", autospec=True, side_effect=signup), \
                patch.object(srv, "window_from_detail", return_value=window):
            result = self.post("/api/batch_enroll", {"account_ids": ["10001", "100000"], "tournaments": ["batch"]})
        self.assertEqual({"10001", "100000"}, joined)
        self.assertEqual(2, result["summary"]["ok"])
        self.assertTrue({"10001|batch", "100000|batch"} <= set(srv.STATE["records"]))
        self.assertEqual({"10001", "100000"}, {row["account_id"] for row in result["results"]})

    def test_new_notification_uses_current_nickname_and_escapes_html(self):
        nickname = "test-<img>\"'"
        self.verified(fake_account(name=nickname), 1)
        with patch.object(srv, "send_notify") as send:
            srv.notify_completed(srv.STATE["subs"]["10001|event"], already=True)
        body = send.call_args.args[1]
        self.assertIn("&lt;img&gt;", body)
        self.assertNotIn("<img>", body)

    def test_intent_pins_version_before_official_login_and_survives_newer_updates_safely(self):
        self.assertTrue(self.post("/api/login/intent", {})["ok"])
        self.verified(fake_account(session="synthetic-other-client-session"), 1)
        before = self.snapshot()
        with patch.object(srv.SveClient, "login_with_cookie", return_value="synthetic-late-cookie-session"):
            self.assertFalse(self.post("/api/login/cookie", {"cookies": "NTES_SESS=fake"})["ok"])
        self.assertEqual(before, self.snapshot())

    def test_explicit_restore_intent_works_with_installed_apk_cookie_payload(self):
        self.assertTrue(self.post("/api/account/forget", {"account_id": "10001", "expected_version": 1,
                                                        "operation_id": uuid.uuid4().hex})["ok"])
        self.assertTrue(self.post("/api/login/intent", {"restore_account_id": "10001", "expected_version": 2})["ok"])
        self.info.return_value = {"id": 10001, "nickname": "test-restored"}
        with patch.object(srv.SveClient, "login_with_cookie", return_value="synthetic-restored-cookie-session"):
            result = self.post("/api/login/cookie", {"cookies": "NTES_SESS=fake"})
        self.assertTrue(result["ok"])
        self.assertEqual("10001", result["account_id"])
        self.assertEqual("test-restored", result["nickname"])
        self.assertEqual({}, srv.STATE["subs"])

    def test_intent_cannot_be_reused_for_another_cookie_or_after_process_state_is_lost(self):
        self.assertTrue(self.post("/api/login/intent", {})["ok"])
        original_intent = self.client.get_cookie("sve_login_intent").value
        self.info.return_value = {"id": 10001, "nickname": "test-first"}
        with patch.object(srv.SveClient, "login_with_cookie", return_value="synthetic-intent-session"):
            self.assertTrue(self.post("/api/login/cookie", {"cookies": "NTES_SESS=fake"})["ok"])
            self.client.set_cookie("sve_login_intent", original_intent)
            self.assertFalse(self.post("/api/login/cookie", {"cookies": "NTES_SESS=fake-other"})["ok"])
            self.assertTrue(self.post("/api/login/intent", {})["ok"])
            srv.STATE["login_intents"] = {}
            self.assertFalse(self.post("/api/login/cookie", {"cookies": "NTES_SESS=fake"})["ok"])


class SchedulerConfirmationTests(unittest.TestCase):
    post = AccountProtectionTests.post

    def setUp(self):
        AccountProtectionTests.setUp(self)
        from scheduler import Scheduler
        self.Scheduler = Scheduler
        self.clock = 1800000000.0
        self.sleeps = []
        self.patches.enter_context(patch("scheduler.time.time", side_effect=lambda: self.clock))
        self.patches.enter_context(patch("scheduler.time.monotonic", side_effect=lambda: self.clock))
        self.patches.enter_context(patch("scheduler.time.sleep", side_effect=self.sleep))

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock += seconds

    @staticmethod
    def item(status):
        return {"name": "test-event", "zh_user_status": status, "player_num": 1, "max_player_number": 8}

    def scheduler(self, statuses):
        client = Mock()
        client.detail.side_effect = [self.item(s) if isinstance(s, str) else s for s in statuses]
        events = []
        sched = self.Scheduler(client, "event", second_confirm_delay=60, confirm_window=3,
                               confirm_retry=1, on_event=lambda kind, data: events.append((kind, data)))
        return sched, client, events

    def test_shared_success_predicate_excludes_every_makeup_variant(self):
        for status in ("已报名", "签到中"):
            self.assertTrue(srv.is_already_in_status(status))
        for status in ("签到中-未报名", "可补录", "签到中-可补录", "签到中-补录中", "已预约", "可报名", "已结束", "", None):
            with self.subTest(status=status):
                self.assertFalse(srv.is_already_in_status(status))

    def test_both_confirmations_accept_all_registered_checkin_combinations(self):
        for quick in ("已报名", "签到中"):
            for second in ("已报名", "签到中"):
                with self.subTest(quick=quick, second=second):
                    sched, client, events = self.scheduler(["签到中-可补录", quick, second])
                    before = len(self.sleeps)
                    result = sched.try_once()
                    self.assertTrue(result.ok)
                    self.assertEqual((quick, second), (result.quick_status, result.second_status))
                    self.assertEqual([60], self.sleeps[before:])
                    client.signup.assert_called_once_with("event")
                    self.assertEqual(1, sum(kind == "completed" for kind, _ in events))

    def test_real_status_failure_in_either_phase_never_completes(self):
        for phase in ("quick", "second"):
            for status in ("签到中-未报名", "可补录", "签到中-可补录", "可报名", "已结束"):
                with self.subTest(phase=phase, status=status):
                    states = ["可报名"] + ([status] if phase == "quick" else ["签到中", status])
                    sched, client, events = self.scheduler(states)
                    result = sched.try_once()
                    self.assertFalse(result.ok)
                    self.assertFalse(sched.completed)
                    self.assertNotIn("completed", [kind for kind, _ in events])
                    self.assertIn("attempt_failed" if phase == "quick" else "unstable", [kind for kind, _ in events])
                    client.signup.assert_called_once()

    def test_network_errors_retry_in_both_phases_then_recover(self):
        sched, client, events = self.scheduler(["可补录", srv.SveError("test-offline"), "签到中",
                                               srv.SveError("test-offline"), "已报名"])
        self.assertTrue(sched.try_once().ok)
        client.signup.assert_called_once()
        self.assertEqual([3, 60, 1], self.sleeps)
        self.assertEqual(1, sum(kind == "completed" for kind, _ in events))

    def test_persistent_network_failure_in_either_phase_never_completes(self):
        for phase in ("quick", "second"):
            sched, client, events = self.scheduler([])
            states = iter([self.item("可报名")] + ([self.item("签到中")] if phase == "second" else []))
            def detail(tid):
                value = next(states, None)
                if value is None:
                    raise srv.SveError("test-offline")
                return value
            client.detail.side_effect = detail
            self.assertFalse(sched.try_once().ok)
            self.assertFalse(sched.completed)
            self.assertNotIn("completed", [kind for kind, _ in events])
            client.signup.assert_called_once()

    def test_already_registered_does_not_submit_or_wait(self):
        for status in ("已报名", "签到中"):
            sched, client, events = self.scheduler([status])
            result = sched.try_once()
            self.assertTrue(result.ok)
            self.assertFalse(result.attempted)
            self.assertEqual(status, result.quick_status)
            self.assertTrue(events[-1][1]["already"])
            client.signup.assert_not_called()
        self.assertEqual([], self.sleeps)

    def test_completion_chain_records_final_status_and_runs_side_effects_once(self):
        for status in ("已报名", "签到中"):
            key = "10001|event"
            srv.STATE["subs"][key] = {"account_id": "10001", "uid": 10001,
                                       "tournament_id": "event", "name": "test-event", "logs": []}
            sched, client, events = self.scheduler(["可补录", "签到中", status])
            srv.STATE["schedulers"][key] = sched
            sched.on_event = lambda kind, data: srv._on_event(key, kind, data)
            class ImmediateThread:
                def __init__(self, target, args=(), **kwargs):
                    self.target, self.args = target, args
                def start(self):
                    self.target(*self.args)
            with patch.object(srv.threading, "Thread", ImmediateThread), \
                    patch.object(srv.threading, "Timer") as timer, \
                    patch.object(srv, "send_notify") as send, \
                    patch.object(srv, "add_record", wraps=srv.add_record) as record:
                result = sched.try_once()
                self.assertTrue(result.ok)
                srv._on_event(key, "completed", {"result": result})
                srv._on_event(key, "stop", {"completed": True})
                record.assert_called_once()
                send.assert_called_once()
                timer.return_value.start.assert_called_once()
                self.assertEqual(status, srv.STATE["records"][key]["status"])
                saved = json.loads((self.data / "identity-v2.json").read_text(encoding="utf-8"))
                self.assertEqual(status, saved["records"][key]["status"])
                callback, args = timer.call_args.args[1], timer.call_args.kwargs["args"]
                callback(*args)
                callback(*args)
                self.assertNotIn(key, srv.STATE["subs"])
                self.assertIn(key, srv.STATE["records"])

    def test_manual_and_batch_share_success_and_failure_statuses(self):
        for path in ("/api/enroll", "/api/batch_enroll"):
            for status in ("已报名", "签到中", "签到中-未报名", "可补录", "签到中-可补录"):
                with self.subTest(path=path, status=status):
                    calls = []
                    def detail(client, tid):
                        calls.append(tid)
                        return self.item("可补录" if len(calls) == 1 else status)
                    body = {"account_id": "10001", "tournament_id": "test-direct"} if path == "/api/enroll" else {
                        "account_ids": ["10001"], "tournaments": ["test-direct"]}
                    with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                            patch.object(srv.SveClient, "signup") as signup, \
                            patch.object(srv, "add_record") as record:
                        result = self.post(path, body)
                        expected = status in ("已报名", "签到中")
                        success = result["ok"] if path == "/api/enroll" else result["results"][0]["result"] == "ok"
                        self.assertEqual(expected, success)
                        self.assertEqual(int(expected), record.call_count)
                        signup.assert_called_once()

    def test_makeup_time_and_capacity_boundaries_remain_closed(self):
        from datetime import datetime
        from sveapi import is_makeup_opportunity
        now = datetime(2026, 1, 1, 12)
        item = dict(self.item("签到中-未报名"), checkin_time=now.timestamp(), end_time=now.timestamp() + 60)
        self.assertTrue(is_makeup_opportunity(item, now))
        for changes in ({"checkin_time": now.timestamp() + 1}, {"end_time": now.timestamp()},
                        {"player_num": 8}, {"zh_user_status": "已结束"}):
            self.assertFalse(is_makeup_opportunity(dict(item, **changes), now))

    def test_regular_closed_window_stays_closed_but_makeup_can_proceed(self):
        from datetime import datetime, timedelta
        for makeup in (False, True):
            status = "可补录" if makeup else "可报名"
            sched, client, events = self.scheduler([status, status, "签到中", "签到中"])
            win = Mock()
            win.is_open.return_value = False
            win.opens_at.return_value = datetime.now() + timedelta(hours=1)
            with patch("scheduler.window_from_detail", return_value=win), \
                    patch.object(sched.stop_flag, "wait", side_effect=lambda delay: sched.stop()):
                sched.run()
            self.assertEqual(int(makeup), client.signup.call_count)
            self.assertEqual(makeup, sched.completed)

    def test_time_conflict_still_aborts_but_transient_signup_failure_does_not(self):
        for reason, abort in (("test-时间冲突", True), ("test-暂时不可用", False)):
            sched, client, events = self.scheduler(["可补录"])
            client.signup.side_effect = srv.SveError(reason)
            result = sched.try_once()
            self.assertFalse(result.ok)
            self.assertEqual(abort, result.abort)
            self.assertNotIn("completed", [kind for kind, _ in events])


class IdentityMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        import identity_migration
        self.migration = identity_migration
        self.a = fake_account(name="test-shared").to_dict()
        self.b = fake_account(10002, "test-shared").to_dict()

    def legacy(self, accounts=None, subs=None, records=None):
        values = [accounts or {"accounts": {"test-old-a": self.a, "test-old-b": self.b}, "active": "test-old-a"},
                  subs or {}, records or {}]
        for name, value in zip(self.migration.LEGACY, values):
            (self.root / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def original_bytes(self):
        return {name: (self.root / name).read_bytes() for name in self.migration.LEGACY}

    def test_preview_is_readonly_same_names_and_missing_uid_are_retained(self):
        self.legacy({"accounts": {"test-a": self.a, "test-b": self.b, "test-broken": {"nickname": "test-missing"}}})
        before = self.original_bytes()
        data = self.migration.preview(self.root)
        self.assertEqual({"10001", "10002"}, set(data["accounts"]))
        self.assertEqual(1, len(data["pending"]))
        self.assertEqual(before, self.original_bytes())
        self.assertEqual(3, len(list(self.root.iterdir())))
        report = json.dumps(self.migration.report(data), ensure_ascii=False)
        self.assertNotIn(self.a["sessionid"], report)
        self.assertNotIn("test-shared", report)

    def test_invalid_legacy_json_is_not_replaced_or_initialized(self):
        self.legacy()
        (self.root / "accounts.json").write_text("{synthetic-invalid-json")
        before = self.original_bytes()
        with self.assertRaises(ValueError):
            self.migration.preview(self.root)
        with self.assertRaises(ValueError):
            self.migration.apply(self.root)
        self.assertEqual(before, self.original_bytes())
        self.assertFalse((self.root / "identity-v2.json").exists())
        self.assertFalse((self.root / "identity-migration.json").exists())

    def test_alias_merge_duplicate_subscriptions_and_ownership_conflict(self):
        aliases = {"test-a": self.a, "test-alias": self.a, "test-b": self.b}
        subs = {"test-a|event": {"account": "test-a", "uid": 10001, "tournament_id": "event", "logs": ["first"]},
                "test-alias|event": {"account": "test-alias", "uid": 10001, "tournament_id": "event", "logs": ["second"], "enabled": False},
                "conflict": {"account": "test-a", "uid": 10002, "tournament_id": "other"}}
        self.legacy({"accounts": aliases, "active": "test-alias"}, subs)
        data = self.migration.preview(self.root)
        self.assertEqual("10001", data["active_account_id"])
        self.assertEqual(1, len(data["subscriptions"]))
        self.assertFalse(data["subscriptions"]["10001|event"]["enabled"])
        self.assertEqual(["first", "second"], data["subscriptions"]["10001|event"]["logs"])
        self.assertEqual(1, len(data["pending"]))

    def test_differing_sessions_are_quarantined_without_arbitrary_winner(self):
        different_session = "synthetic-different-session"
        second = dict(self.a, sessionid=different_session)
        self.legacy({"accounts": {"test-a": self.a, "test-alias": second}},
                    {"event": {"account": "test-a", "uid": 10001}})
        data = self.migration.preview(self.root)
        self.assertEqual({}, data["accounts"])
        self.assertEqual({}, data["subscriptions"])
        self.assertEqual(2, len(data["pending"]))

    def test_apply_is_repeatable_and_fences_old_readers(self):
        self.legacy()
        self.migration.apply(self.root)
        before = (self.root / "identity-v2.json").read_bytes()
        self.migration.apply(self.root)
        self.assertEqual(before, (self.root / "identity-v2.json").read_bytes())
        with self.assertRaises(AttributeError):
            json.loads((self.root / "accounts.json").read_text())["accounts"].items()
        store = srv.IdentityStore(self.root)
        self.assertEqual(2, len(store.load()["accounts"]))

    def test_each_interruption_can_finish_or_roll_back_exact_original_bytes(self):
        for stage in ("prepared", "fence-0", "fence-1", "fence-2", "activated"):
            for rollback in (False, True):
                with self.subTest(stage=stage, rollback=rollback), tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
                    old_root = self.root
                    self.root = Path(directory)
                    self.legacy()
                    original = self.original_bytes()
                    with self.assertRaises(OSError):
                        self.migration.apply(self.root, fail_after=stage)
                    with self.assertRaises(srv.AccountConflict):
                        srv.IdentityStore(self.root).load()
                    self.migration.recover(self.root, rollback=rollback)
                    if rollback:
                        self.assertEqual(original, self.original_bytes())
                        self.assertFalse((self.root / "identity-v2.json").exists())
                    else:
                        self.assertEqual(2, len(srv.IdentityStore(self.root).load()["accounts"]))
                    self.root = old_root

    def test_rollback_cannot_discard_new_business_data(self):
        self.legacy()
        self.migration.apply(self.root)
        store = srv.IdentityStore(self.root)
        data = store.load()
        data["records"]["10001|new-event"] = {"account_id": "10001", "tournament_id": "new-event"}
        with srv.WriterLock(self.root):
            store.save(data)
        before = (self.root / "identity-v2.json").read_bytes()
        with self.assertRaises(srv.AccountConflict):
            self.migration.recover(self.root, rollback=True)
        self.assertEqual(before, (self.root / "identity-v2.json").read_bytes())

    def test_rollback_itself_is_resumable_and_a_missing_live_dataset_is_not_recreated(self):
        self.legacy()
        original = self.original_bytes()
        self.migration.apply(self.root)
        with self.assertRaises(OSError):
            self.migration.recover(self.root, rollback=True, fail_after="rollback-removed")
        self.migration.recover(self.root, rollback=True)
        self.assertEqual(original, self.original_bytes())
        self.migration.apply(self.root)
        (self.root / "identity-v2.json").unlink()
        with self.assertRaises(srv.AccountConflict):
            self.migration.recover(self.root)
        self.assertFalse((self.root / "identity-v2.json").exists())

    def test_legacy_startup_and_old_writers_are_refused(self):
        self.legacy()
        with self.assertRaises(srv.AccountConflict):
            srv.IdentityStore(self.root).load(initialize=True)
        self.migration.apply(self.root)
        store = srv.IdentityStore(self.root)
        data = store.load()
        (self.root / "accounts.json").write_text("{}")
        with srv.WriterLock(self.root):
            with self.assertRaises(srv.AccountConflict):
                store.save(data)
        with self.assertRaises(srv.AccountConflict):
            store.load()

    def test_writer_lock_excludes_a_second_executor(self):
        with srv.WriterLock(self.root):
            with self.assertRaises(srv.AccountConflict):
                with srv.WriterLock(self.root):
                    self.fail("second writer acquired lock")

    def test_orphan_records_with_known_uid_remain_visible_as_deleted_identity(self):
        self.legacy(records={"old-event": {"account": "test-removed", "uid": 100000, "tournament_id": "old-event"}})
        data = self.migration.preview(self.root)
        self.assertIn("100000|old-event", data["records"])
        self.assertIn("100000", data["tombstones"])
        self.assertNotIn("100000", data["accounts"])

    def test_recovery_refuses_new_legacy_writes_instead_of_overwriting_them(self):
        self.legacy()
        with self.assertRaises(OSError):
            self.migration.apply(self.root, fail_after="prepared")
        latest = {"accounts": {"test-latest": self.b}}
        (self.root / "accounts.json").write_text(json.dumps(latest))
        for rollback in (True, False):
            with self.assertRaises(srv.AccountConflict):
                self.migration.recover(self.root, rollback=rollback)
            self.assertEqual(latest, json.loads((self.root / "accounts.json").read_text()))

    def test_stale_dataset_generation_cannot_overwrite_newer_business_data(self):
        self.legacy()
        self.migration.apply(self.root)
        first, second = srv.IdentityStore(self.root), srv.IdentityStore(self.root)
        one, two = first.load(), second.load()
        with srv.WriterLock(self.root):
            first.save(one)
            before = (self.root / "identity-v2.json").read_bytes()
            with self.assertRaises(srv.AccountConflict):
                second.save(two)
            self.assertEqual(before, (self.root / "identity-v2.json").read_bytes())


class UpgradeProtectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "source"
        self.target = Path(self.directory.name) / "target"
        self.write(self.source, "app/client/server.py", "# synthetic runtime\n")
        self.write(self.source, "app/client/data/areas.json", '{"test-area": []}')
        self.write(self.source, "app/client/core/accountguard.py", "# synthetic core\n")
        self.write(self.source, "config/settings.example.json", "{}")
        self.write(self.source, "deploy/install.sh", "#!/bin/bash\n")
        for name in ["accounts.json", "subscriptions.json", "records.json", "notify.json", "backups/test.json"]:
            self.write(self.source, "app/client/data/" + name, "synthetic source private data")
            self.write(self.target, "app/client/data/" + name, "synthetic target preserved data")
        for name in ["config/settings.json", "config/secrets.json", "config/private-scan.json", "capture/session.json", "capture/agent_stdout.log", "scripts/poller/private.json", "app/client/ui/private.json"]:
            self.write(self.source, name, "synthetic source configuration")
            self.write(self.target, name, "synthetic target configuration")

    @staticmethod
    def write(root, relative, content):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_repeat_upgrade_preserves_all_private_hashes(self):
        before = hashes.inventory(self.target)
        self.assertGreater(copier.copy_runtime(self.source, self.target), 0)
        self.assertGreater(copier.copy_runtime(self.source, self.target), 0)
        self.assertEqual(before, hashes.inventory(self.target))
        self.assertEqual("synthetic target configuration", (self.target / "app/client/ui/private.json").read_text())

    def test_in_place_upgrade_is_noop(self):
        before = hashes.inventory(self.source)
        self.assertEqual(0, copier.copy_runtime(self.source, self.source / "."))
        self.assertEqual(before, hashes.inventory(self.source))

    def test_first_install_gets_areas_but_no_source_private_data(self):
        target = self.target.parent / "fresh"
        copier.copy_runtime(self.source, target)
        self.assertTrue((target / "app/client/data/areas.json").is_file())
        self.assertTrue((target / "app/client/server.py").is_file())
        self.assertEqual({}, hashes.inventory(target))
        self.assertFalse((target / "scripts/poller/private.json").exists())
        self.assertFalse((target / "app/client/ui/private.json").exists())

    def test_missing_required_source_fails_without_changing_target(self):
        before = hashes.inventory(self.target)
        (self.source / "app/client/server.py").unlink()
        with self.assertRaises(ValueError):
            copier.copy_runtime(self.source, self.target)
        self.assertEqual(before, hashes.inventory(self.target))

    def test_symlink_destination_rejected_before_copy(self):
        external = self.target.parent / "outside"
        external.mkdir()
        try:
            (self.target / "deploy").symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("OS does not permit creating test symlinks")
        with self.assertRaises(ValueError):
            copier.copy_runtime(self.source, self.target)
        self.assertEqual([], list(external.iterdir()))

    def test_symlink_preflight_refuses_before_any_runtime_write(self):
        before = hashes.inventory(self.target)
        with patch.object(Path, "is_symlink", autospec=True,
                          side_effect=lambda path: path == self.target / "deploy"):
            with self.assertRaises(ValueError):
                copier.copy_runtime(self.source, self.target)
        self.assertEqual(before, hashes.inventory(self.target))
        self.assertFalse((self.target / "app/client/server.py").exists())


if __name__ == "__main__":
    unittest.main()
