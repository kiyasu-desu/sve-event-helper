"""M24 isolated server tests for role boundaries and per-user notification isolation."""
from __future__ import annotations

import base64
from contextlib import ExitStack
import hashlib
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
from unittest.mock import Mock, patch
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True

SYNTHETIC_KEY = "synthetic-key-for-m24-tests"
SYNTHETIC_TEAM = "test-team"
SYNTHETIC_AUD = "synthetic-aud-m24"
SYNTHETIC_ADMIN_PUSH = "synthetic-admin-push-val"
SYNTHETIC_USER_PUSH = "synthetic-user-push-val"
SYNTHETIC_ADMIN_MAIL = "synthetic-admin-mail-val"


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def protected_snapshot() -> dict[str, bytes]:
    paths = list((ROOT / "app/client/data").rglob("*"))
    paths += [ROOT / "config/settings.json"]
    paths += [ROOT / "capture" / name for name in ("session.json", "agent_stdout.log", "poll.log", "watchdog.log")]
    return {
        str(path): hashlib.sha256(path.read_bytes()).digest()
        for path in paths
        if path.is_file() and path.name != ".identity-writer.lock"
    }


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_test_jwt(email: str, *, team: str = SYNTHETIC_TEAM, aud: str = SYNTHETIC_AUD, exp: int = 2_000_000_000) -> str:
    header = _b64u(json.dumps({"alg": "RS256", "kid": "test-kid"}).encode("utf-8"))
    payload = _b64u(json.dumps({
        "iss": "https://" + team + ".cloudflareaccess.com",
        "aud": [aud],
        "exp": exp,
        "email": email,
    }).encode("utf-8"))
    sig = _b64u(("sig:" + email.strip().lower()).encode("utf-8"))
    return f"{header}.{payload}.{sig}"


def fake_account(srv, uid: int = 10001, name: str | None = None):
    label = name or str(uid)
    return srv.Account(
        name=label,
        uid=uid,
        nickname=label,
        sessionid=f"synthetic-session-{uid}",
        deviceid="synthetic-device",
    )


class ServerIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protected_before = protected_snapshot()
        cls.import_dir = tempfile.TemporaryDirectory(prefix="sve-m24-import-")
        cls.addClassCleanup(cls.import_dir.cleanup)
        with patch.dict(os.environ, {
            "SVE_DATA_DIR": str(Path(cls.import_dir.name) / "import"),
            "SVE_APP_KEY": SYNTHETIC_KEY,
        }):
            cls.srv = load_module("sve_m24_server", "app/client/server.py")

    @classmethod
    def tearDownClass(cls):
        if cls.protected_before != protected_snapshot():
            raise AssertionError("Protected production data/log files changed")

    def setUp(self):
        srv = self.srv
        self.directory = tempfile.TemporaryDirectory(prefix="sve-m24-case-")
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)

        self.cf_env = {
            "SVE_DATA_DIR": str(self.data),
            "SVE_APP_KEY": SYNTHETIC_KEY,
            "SVE_AUTH_MODE": "cloudflare",
            "SVE_CF_ACCESS_TEAM": SYNTHETIC_TEAM,
            "SVE_CF_ACCESS_AUD": SYNTHETIC_AUD,
            "SVE_ADMIN_EMAILS": "admin@example.com",
            "SVE_ALLOW_LOCAL_ADMIN": "1",
        }
        self.patches.enter_context(patch.dict(os.environ, self.cf_env))

        network = self.patches.enter_context(
            patch.object(urllib.request, "urlopen", side_effect=AssertionError("Unexpected network"))
        )
        sock = self.patches.enter_context(
            patch.object(socket.socket, "connect", side_effect=AssertionError("Unexpected socket"))
        )
        proc = self.patches.enter_context(
            patch.object(subprocess, "Popen", side_effect=AssertionError("Unexpected process"))
        )
        thread = self.patches.enter_context(
            patch("threading.Thread.start", side_effect=AssertionError("Unexpected thread"))
        )
        for guard in (network, sock, proc, thread):
            self.addCleanup(guard.assert_not_called)

        for field, filename in {
            "ACCOUNTS_FILE": "accounts.json",
            "SUBS_FILE": "subscriptions.json",
            "RECORDS_FILE": "records.json",
            "REMOTE_FILE": "remote.json",
            "LOG_FILE": "client.log",
            "NOTIFY_FILE": "notify.json",
            "AREAS_FILE": "areas.json",
            "AGENT_SETTINGS": "settings.json",
        }.items():
            self.patches.enter_context(patch.object(srv, field, str(self.data / filename)))
        self.patches.enter_context(patch.object(srv, "DATA_DIR", str(self.data)))
        self.patches.enter_context(srv.WriterLock(self.data))
        self.patches.enter_context(patch.object(srv, "ACCOUNTS_LOADED_OK", True))
        self.patches.enter_context(patch.object(srv, "log"))
        self.patches.enter_context(patch.object(srv, "restore_missing_schedulers"))
        self.patches.enter_context(patch.object(srv, "login_capability", return_value={"can_login": False, "browser": ""}))

        # Build test Cloudflare verifier using fake JWK + sig_verifier (no external network/openssl).
        import auth_context as ac
        dummy_jwk = {
            "kty": "RSA",
            "kid": "test-kid",
            "n": _b64u(b"\x01" * 256),
            "e": _b64u((65537).to_bytes(3, "big")),
        }

        def fake_sig_verify(signing_input: bytes, signature: bytes, _pem: bytes) -> bool:
            try:
                payload = json.loads(
                    base64.urlsafe_b64decode(signing_input.split(b".")[1] + b"===").decode("utf-8")
                )
                expected = ("sig:" + str(payload.get("email", "")).strip().lower()).encode("utf-8")
                return signature == expected
            except Exception:
                return False

        self.verifier = ac.CloudflareAccessVerifier(
            SYNTHETIC_TEAM,
            SYNTHETIC_AUD,
            certs_fetcher=lambda _url: {"keys": [dummy_jwk]},
            sig_verifier=fake_sig_verify,
            now_fn=lambda: 1_900_000_000.0,
        )
        self.patches.enter_context(patch.object(srv, "CF_VERIFIER", self.verifier))
        self.patches.enter_context(patch.object(srv, "_CF_VERIFIER_CONFIG", (SYNTHETIC_TEAM, SYNTHETIC_AUD)))

        srv.STATE.update(
            accounts={"10001": fake_account(srv, 10001, "owner-acct")},
            active="10001",
            remote={"enabled": False},
            tunnel=None,
            subs={},
            records={},
            schedulers={},
            scheduler_threads=set(),
            workers={},
            batch_cancel_keys={},
            deferred_restores=set(),
            cancel_epochs={},
            record_epochs={},
            pending=[],
            tombstones={},
            operations={},
            login_intents={},
            storage_failed=False,
            log=[],
        )
        srv.STATE.pop("users", None)
        store = srv.IdentityStore(self.data)
        store.load(initialize=True)
        self.patches.enter_context(patch.object(srv, "STORE", store))
        srv.persist_accounts()
        srv.persist_subs()
        srv.persist_records()
        # Reset UserStore cache so each test starts cleanly with DATA_DIR.
        if hasattr(srv, "USER_STORE"):
            self.patches.enter_context(patch.object(srv, "USER_STORE", None))
        if hasattr(srv, "_USER_STORE_DIR"):
            self.patches.enter_context(patch.object(srv, "_USER_STORE_DIR", None))

        self.client = srv.app.test_client()

    def cf_headers(self, email: str) -> dict[str, str]:
        return {
            "Cf-Connecting-IP": "192.0.2.20",
            "Cf-Access-Jwt-Assertion": make_test_jwt(email),
        }

    def test_ordinary_user_cannot_read_or_modify_remote_config(self):
        srv = self.srv
        srv.STATE["remote"] = {
            "enabled": False,
            "host": "node.example.com",
            "user": "ops-user",
            "key": "/srv/synthetic-ssh-key",
            "ssh_port": 2222,
        }
        srv.save_json(srv.REMOTE_FILE, srv.STATE["remote"])

        user_hdrs = self.cf_headers("friend@example.com")
        admin_hdrs = self.cf_headers("admin@example.com")

        for method, path, body in (
            ("GET", "/api/remote/status", None),
            ("POST", "/api/remote/config", {"enabled": True, "host": "evil.example.com", "user": "x"}),
            ("POST", "/api/remote/push_accounts", {}),
            ("GET", "/api/accounts/summary", None),
            ("POST", "/api/accounts/sync", {"accounts": {}}),
        ):
            with self.subTest(method=method, path=path):
                resp = self.client.open(path, method=method, json=body, headers=user_hdrs)
                self.assertEqual(403, resp.status_code)
                payload = resp.get_json()
                self.assertFalse(payload["ok"])
                self.assertEqual("forbidden", payload["code"])

        # Remote config on disk & memory remains untouched.
        self.assertEqual("node.example.com", json.loads(Path(srv.REMOTE_FILE).read_text())["host"])
        self.assertFalse(srv.STATE["remote"]["enabled"])

        # User /api/state hides remote config and exposes role="user".
        user_state = self.client.get("/api/state", headers=user_hdrs).get_json()
        self.assertTrue(user_state["ok"])
        self.assertEqual("user", user_state["user"]["role"])
        self.assertEqual({"enabled": False, "restricted": True, "fields": {}}, user_state["remote"])
        self.assertNotIn("node.example.com", json.dumps(user_state))

        # Admin succeeds on /api/remote/status and /api/accounts/summary.
        self.assertEqual(200, self.client.get("/api/remote/status", headers=admin_hdrs).status_code)
        admin_summary = self.client.get("/api/accounts/summary", headers=admin_hdrs)
        self.assertEqual(200, admin_summary.status_code)
        self.assertTrue(admin_summary.get_json()["ok"])

    def test_ordinary_user_never_inherits_admin_env_or_settings_notify(self):
        srv = self.srv
        # Populate admin AGENT_SETTINGS, admin DATA_DIR/notify.json, and ENV_NOTIFY.
        srv.save_json(srv.AGENT_SETTINGS, {
            "channels": {
                "pushplus": {"enabled": True, "token": SYNTHETIC_ADMIN_PUSH},
                "email": {
                    "enabled": True,
                    "smtp_host": "smtp.example.com",
                    "smtp_port": 465,
                    "use_ssl": True,
                    "username": "sender@example.com",
                    "authcode": SYNTHETIC_ADMIN_MAIL,
                    "to_addrs": ["recipient@example.com"],
                },
            }
        })
        srv.save_json(srv.NOTIFY_FILE, {"pushplus": {"enabled": True, "token": SYNTHETIC_ADMIN_PUSH}})
        admin_notify_before = Path(srv.NOTIFY_FILE).read_bytes()

        user_hdrs = self.cf_headers("friend@example.com")
        admin_hdrs = self.cf_headers("admin@example.com")

        with patch.dict(os.environ, {
            "SVE_PUSHPLUS_ENABLED": "true",
            "SVE_PUSHPLUS_TOKEN": SYNTHETIC_ADMIN_PUSH,
            "SVE_EMAIL_ENABLED": "true",
            "SVE_SMTP_HOST": "env-smtp.example.com",
        }):
            # User sees blank/unconfigured channels and notify_configured == False.
            user_state = self.client.get("/api/state", headers=user_hdrs).get_json()
            self.assertEqual("user", user_state["user"]["role"])
            self.assertFalse(user_state["user"]["notify_configured"])

            user_cfg = self.client.get("/api/notify/config", headers=user_hdrs).get_json()
            self.assertTrue(user_cfg["ok"])
            self.assertFalse(user_cfg["channels"]["pushplus"]["enabled"])
            self.assertFalse(user_cfg["channels"]["pushplus"]["token__set"])
            self.assertTrue(user_cfg["channels"]["pushplus"]["token__editable"])
            self.assertEqual("default", user_cfg["channels"]["pushplus"]["token__source"])
            self.assertFalse(user_cfg["channels"]["email"]["enabled"])
            self.assertEqual("default", user_cfg["channels"]["email"]["smtp_host__source"])
            self.assertTrue(user_cfg["channels"]["email"]["smtp_host__editable"])
            self.assertNotIn(SYNTHETIC_ADMIN_PUSH, json.dumps(user_cfg))

            # Admin still sees ENV_NOTIFY and notify_configured == True.
            admin_state = self.client.get("/api/state", headers=admin_hdrs).get_json()
            self.assertEqual("admin", admin_state["user"]["role"])
            self.assertTrue(admin_state["user"]["notify_configured"])
            admin_cfg = self.client.get("/api/notify/config", headers=admin_hdrs).get_json()
            self.assertEqual("env", admin_cfg["channels"]["pushplus"]["token__source"])
            self.assertFalse(admin_cfg["channels"]["pushplus"]["token__editable"])

            # User /api/notify/test before configuring must NOT trigger admin notification!
            sender = Mock()
            sender.notify_all.return_value = []
            with patch.dict(sys.modules, {"notification_sender": sender}):
                res_empty = self.client.post("/api/notify/test", json={}, headers=user_hdrs).get_json()
                self.assertFalse(res_empty["ok"])
                sender.notify_all.assert_not_called()

            # User saves own PushPlus token; admin notify.json remains byte-identical.
            save_res = self.client.post("/api/notify/config", json={
                "channels": {"pushplus": {"enabled": True, "token": SYNTHETIC_USER_PUSH}},
            }, headers=user_hdrs).get_json()
            self.assertTrue(save_res["ok"])
            self.assertTrue(save_res["channels"]["pushplus"]["enabled"])
            self.assertTrue(save_res["channels"]["pushplus"]["token__set"])
            self.assertEqual(admin_notify_before, Path(srv.NOTIFY_FILE).read_bytes())

            # State now reflects notify_configured == True.
            user_state_after = self.client.get("/api/state", headers=user_hdrs).get_json()
            self.assertTrue(user_state_after["user"]["notify_configured"])

            # User /api/notify/test sends ONLY to user's own PushPlus channel.
            sender.notify_all.reset_mock()
            sender.notify_all.return_value = [("pushplus", True, "ok")]
            with patch.dict(sys.modules, {"notification_sender": sender}):
                test_res = self.client.post("/api/notify/test", json={}, headers=user_hdrs).get_json()
                self.assertTrue(test_res["ok"])
                sender.notify_all.assert_called_once()
                sent_channels = sender.notify_all.call_args.args[0]
                self.assertEqual({"pushplus": {"enabled": True, "token": SYNTHETIC_USER_PUSH}}, sent_channels)
                self.assertNotIn("email", sent_channels)

            # Second user still has no notification config.
            other_state = self.client.get("/api/state", headers=self.cf_headers("other@example.com")).get_json()
            self.assertFalse(other_state["user"]["notify_configured"])

    def test_shared_uid_notifications_dispatch_to_all_bound_users_individually(self):
        srv = self.srv
        # Ensure owner + two friends bind UID 10001; one friend has PushPlus, another has none.
        with srv.LOCK:
            store, users_doc = srv._ensure_user_state_locked()
            store.bind_account(users_doc, "friend@example.com", "user", "10001")
            store.bind_account(users_doc, "silent@example.com", "user", "10001")
            store.save(users_doc)

        srv.save_json(srv.NOTIFY_FILE, {"pushplus": {"enabled": True, "token": SYNTHETIC_ADMIN_PUSH}})
        friend_notify = srv.USER_STORE.notify_file_for("friend@example.com")
        friend_notify.parent.mkdir(parents=True, exist_ok=True)
        srv.save_json(str(friend_notify), {"pushplus": {"enabled": True, "token": SYNTHETIC_USER_PUSH}})

        sent_calls = []
        sender = Mock()

        def record_send(channels, title, html):
            sent_calls.append((channels, title, html))
            return [("pushplus", True, "ok")]

        sender.notify_all.side_effect = record_send
        sub = {"account_id": "10001", "tournament_id": "ev-shared", "name": "SharedCup"}
        with patch.dict(sys.modules, {"notification_sender": sender}):
            srv.notify_completed(sub, already=False)
            srv.notify_time_conflict(sub, "时间冲突", code=409)

        # 2 events * 2 configured users (owner + friend); silent@example.com is skipped without error.
        self.assertEqual(4, len(sent_calls))
        tokens_seen = [call[0]["pushplus"]["token"] for call in sent_calls]
        self.assertEqual(2, tokens_seen.count(SYNTHETIC_ADMIN_PUSH))
        self.assertEqual(2, tokens_seen.count(SYNTHETIC_USER_PUSH))

    def test_cloudflare_mode_rejects_unverified_proxy_requests_with_401(self):
        # Missing JWT with proxy headers -> 401 on API, UI routes, and proxied health.
        for path in ("/", "/subs", "/settings", "/api/state", "/api/notify/config", "/api/scheduler/health"):
            resp = self.client.get(path, headers={"Cf-Connecting-IP": "192.0.2.30"})
            self.assertEqual(401, resp.status_code, path)
            self.assertFalse(resp.get_json()["ok"])

        # Spoofed Cf-Access-Authenticated-User-Email without JWT -> 401.
        spoof = self.client.get(
            "/api/state",
            headers={"Cf-Access-Authenticated-User-Email": "admin@example.com"},
        )
        self.assertEqual(401, spoof.status_code)

        # Invalid/tampered JWT -> 401.
        bad_jwt = make_test_jwt("admin@example.com")[:-4] + "ZZZZ"
        bad = self.client.get(
            "/api/state",
            headers={"Cf-Connecting-IP": "192.0.2.30", "Cf-Access-Jwt-Assertion": bad_jwt},
        )
        self.assertEqual(401, bad.status_code)

        # Direct loopback /api/scheduler/health without proxy headers remains 200 even if local admin disabled.
        with patch.dict(os.environ, {"SVE_ALLOW_LOCAL_ADMIN": "0"}):
            health = self.client.get("/api/scheduler/health", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
            self.assertEqual(200, health.status_code)
            self.assertEqual(401, self.client.get("/api/state", environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).status_code)

    def test_cross_user_account_and_record_access_denied(self):
        srv = self.srv
        with srv.LOCK:
            srv.STATE["accounts"]["10002"] = fake_account(srv, 10002, "friend-acct")
            srv.STATE["subs"]["10001|ev1"] = {
                "account_id": "10001", "account": "10001", "uid": 10001,
                "tournament_id": "ev1", "name": "OwnerEvent", "enabled": True, "logs": [],
            }
            srv.STATE["subs"]["10002|ev2"] = {
                "account_id": "10002", "account": "10002", "uid": 10002,
                "tournament_id": "ev2", "name": "FriendEvent", "enabled": True, "logs": [],
            }
            srv.STATE["records"]["10001|ev1"] = {
                "account_id": "10001", "account": "10001", "uid": 10001,
                "tournament_id": "ev1", "name": "OwnerEvent", "status": "已报名", "checked_at": 1e12,
            }
            srv.STATE["records"]["10002|ev2"] = {
                "account_id": "10002", "account": "10002", "uid": 10002,
                "tournament_id": "ev2", "name": "FriendEvent", "status": "已报名", "checked_at": 1e12,
            }
            srv.persist_dataset()
            store, users_doc = srv._ensure_user_state_locked()
            users_doc["users"]["owner"]["bound_account_ids"] = ["10001"]
            users_doc["users"]["owner"]["active_account_id"] = "10001"
            store.bind_account(users_doc, "friend@example.com", "user", "10002", select=True)
            store.save(users_doc)

        admin_hdrs = self.cf_headers("admin@example.com")
        friend_hdrs = self.cf_headers("friend@example.com")

        admin_state = self.client.get("/api/state", headers=admin_hdrs).get_json()
        self.assertEqual(["10001"], [a["account_id"] for a in admin_state["accounts"]])
        self.assertEqual("10001", admin_state["active_account_id"])
        self.assertEqual(["10001|ev1"], [s["key"] for s in admin_state["subs"]])
        self.assertEqual(["ev1"], admin_state["mine"])
        self.assertEqual(1, admin_state["sub_count"])

        friend_state = self.client.get("/api/state", headers=friend_hdrs).get_json()
        self.assertEqual(["10002"], [a["account_id"] for a in friend_state["accounts"]])
        self.assertEqual("10002", friend_state["active_account_id"])
        self.assertEqual(["10002|ev2"], [s["key"] for s in friend_state["subs"]])
        self.assertEqual(["ev2"], friend_state["mine"])
        self.assertEqual(1, friend_state["sub_count"])

        admin_recs = self.client.get("/api/records", headers=admin_hdrs).get_json()
        self.assertEqual(["10001|ev1"], [r["key"] for r in admin_recs["records"]])
        friend_recs = self.client.get("/api/records", headers=friend_hdrs).get_json()
        self.assertEqual(["10002|ev2"], [r["key"] for r in friend_recs["records"]])

        # Friend attempting any operation on Owner's UID 10001 -> HTTP 403 forbidden.
        forbidden_calls = [
            ("GET", "/api/tournaments?account_id=10001", None),
            ("GET", "/api/my_tournaments?account_id=10001", None),
            ("GET", "/api/detail?account_id=10001&tournament_id=ev1", None),
            ("POST", "/api/subscribe", {"account_id": "10001", "tournament_id": "ev9"}),
            ("POST", "/api/unsubscribe", {"account_id": "10001", "tournament_id": "ev1"}),
            ("POST", "/api/enroll", {"account_id": "10001", "tournament_id": "ev1"}),
            ("POST", "/api/cancel", {"account_id": "10001", "tournament_id": "ev1"}),
            ("POST", "/api/batch_enroll", {"account_ids": ["10001"], "tournaments": ["ev1"]}),
            ("POST", "/api/batch_cancel", {"account_ids": ["10001"], "tournaments": ["ev1"]}),
            ("POST", "/api/account/select", {"account_id": "10001"}),
            ("POST", "/api/account/forget", {"account_id": "10001", "expected_version": 1, "operation_id": "op-cross-forget-1"}),
            ("POST", "/api/account/profile/refresh", {"account_id": "10001", "expected_version": 1}),
            ("GET", "/api/records?account_id=10001", None),
            ("POST", "/api/records/scan", {"account_id": "10001"}),
            ("POST", "/api/records/forget", {"account_id": "10001", "key": "10001|ev1"}),
        ]
        for method, path, body in forbidden_calls:
            with self.subTest(caller="friend", method=method, path=path):
                resp = self.client.open(path, method=method, json=body, headers=friend_hdrs)
                self.assertEqual(403, resp.status_code)
                self.assertEqual("forbidden", resp.get_json()["code"])

        # Owner attempting to operate on Friend's unshared UID 10002 -> HTTP 403 forbidden.
        for method, path, body in (
            ("GET", "/api/tournaments?account_id=10002", None),
            ("POST", "/api/account/forget", {"account_id": "10002", "expected_version": 1, "operation_id": "op-admin-cross-1"}),
            ("GET", "/api/records?account_id=10002", None),
        ):
            with self.subTest(caller="admin", method=method, path=path):
                resp = self.client.open(path, method=method, json=body, headers=admin_hdrs)
                self.assertEqual(403, resp.status_code)
                self.assertEqual("forbidden", resp.get_json()["code"])

    def test_real_login_binds_existing_shared_uid_and_shares_subs_and_records(self):
        srv = self.srv
        sched = Mock(completed=False, attempts=1)
        sched.stop_flag.is_set.return_value = False
        with srv.LOCK:
            srv.STATE["subs"]["10001|ev1"] = {
                "account_id": "10001", "account": "10001", "uid": 10001,
                "tournament_id": "ev1", "name": "SharedTour", "enabled": True, "logs": [],
            }
            srv.STATE["schedulers"]["10001|ev1"] = sched
            srv.STATE["records"]["10001|ev1"] = {
                "account_id": "10001", "account": "10001", "uid": 10001,
                "tournament_id": "ev1", "name": "SharedTour", "status": "已报名", "checked_at": 1e12,
            }
            srv.persist_dataset()

        admin_hdrs = self.cf_headers("admin@example.com")
        friend_hdrs = self.cf_headers("friend@example.com")

        # Before login, friend sees no accounts.
        before_state = self.client.get("/api/state", headers=friend_hdrs).get_json()
        self.assertEqual([], before_state["accounts"])
        self.assertEqual([], before_state["subs"])

        # Friend initiates login intent and completes /api/login/cookie for UID 10001.
        intent_resp = self.client.post("/api/login/intent", json={}, headers=friend_hdrs)
        self.assertTrue(intent_resp.get_json()["ok"])
        with patch.object(srv.SveClient, "login_with_cookie", return_value="synthetic-shared-new-sid"), \
                patch.object(srv.SveClient, "info", return_value={"id": 10001, "nickname": "test-shared-hero"}):
            login_resp = self.client.post(
                "/api/login/cookie",
                json={"cookies": "NTES_SESS=fake-cookie-value"},
                headers=friend_hdrs,
            ).get_json()
        self.assertTrue(login_resp["ok"], login_resp)
        self.assertEqual("10001", login_resp["account_id"])
        self.assertEqual(2, srv.STATE["accounts"]["10001"].version)
        self.assertEqual("synthetic-shared-new-sid", srv.STATE["accounts"]["10001"].sessionid)
        self.assertIs(sched, srv.STATE["schedulers"]["10001|ev1"])

        # Now both Admin and Friend see UID 10001, its subscription, and its record.
        for hdrs in (admin_hdrs, friend_hdrs):
            st = self.client.get("/api/state", headers=hdrs).get_json()
            self.assertEqual(["10001"], [a["account_id"] for a in st["accounts"]])
            self.assertEqual(["10001|ev1"], [s["key"] for s in st["subs"]])
            recs = self.client.get("/api/records", headers=hdrs).get_json()
            self.assertEqual(["10001|ev1"], [r["key"] for r in recs["records"]])

    def test_shared_uid_forget_unbinds_without_stopping_other_user_until_last(self):
        srv = self.srv
        sched = Mock(completed=False, attempts=0)
        sched.stop_flag.is_set.return_value = False
        with srv.LOCK:
            srv.STATE["subs"]["10001|ev1"] = {
                "account_id": "10001", "account": "10001", "uid": 10001,
                "tournament_id": "ev1", "name": "SharedTour", "enabled": True, "logs": [],
            }
            srv.STATE["schedulers"]["10001|ev1"] = sched
            srv.persist_dataset()
            store, users_doc = srv._ensure_user_state_locked()
            store.bind_account(users_doc, "friend@example.com", "user", "10001", select=True)
            store.save(users_doc)

        admin_hdrs = self.cf_headers("admin@example.com")
        friend_hdrs = self.cf_headers("friend@example.com")
        dispatch = self.patches.enter_context(patch.object(srv, "dispatch_notification"))

        # Step 1: Admin unbinds UID 10001 while Friend still holds it.
        res1 = self.client.post("/api/account/forget", json={
            "account_id": "10001",
            "expected_version": 1,
            "operation_id": "op-unbind-admin-10001",
        }, headers=admin_hdrs).get_json()
        self.assertTrue(res1["ok"], res1)
        self.assertTrue(res1["unbound"])
        self.assertTrue(res1["shared_remaining"])
        sched.stop.assert_not_called()
        dispatch.assert_not_called()
        self.assertIn("10001", srv.STATE["accounts"])
        self.assertIn("10001|ev1", srv.STATE["subs"])
        self.assertNotIn("10001", srv.STATE["tombstones"])

        # Admin no longer sees 10001 in active accounts/subs, but sees it in deleted_accounts.
        admin_st = self.client.get("/api/state", headers=admin_hdrs).get_json()
        self.assertEqual([], admin_st["accounts"])
        self.assertEqual([], admin_st["subs"])
        self.assertEqual(["10001"], [d["account_id"] for d in admin_st["deleted_accounts"]])

        # Friend still sees 10001 and active subscription 10001|ev1.
        friend_st = self.client.get("/api/state", headers=friend_hdrs).get_json()
        self.assertEqual(["10001"], [a["account_id"] for a in friend_st["accounts"]])
        self.assertEqual(["10001|ev1"], [s["key"] for s in friend_st["subs"]])

        # Step 2: Friend unbinds UID 10001 (last holder -> full purge).
        res2 = self.client.post("/api/account/forget", json={
            "account_id": "10001",
            "expected_version": 1,
            "operation_id": "op-unbind-friend-10001",
        }, headers=friend_hdrs).get_json()
        self.assertTrue(res2["ok"], res2)
        self.assertTrue(res2["unbound"])
        self.assertFalse(res2["shared_remaining"])
        sched.stop.assert_called_once()
        self.assertNotIn("10001", srv.STATE["accounts"])
        self.assertNotIn("10001|ev1", srv.STATE["subs"])
        self.assertIn("10001", srv.STATE["tombstones"])
        dispatch.assert_called_once()
        self.assertEqual("friend@example.com", dispatch.call_args.kwargs.get("user_id"))

    def test_login_intent_cookie_cannot_cross_users(self):
        srv = self.srv
        admin_hdrs = self.cf_headers("admin@example.com")
        friend_hdrs = self.cf_headers("friend@example.com")

        intent_resp = self.client.post("/api/login/intent", json={}, headers=admin_hdrs)
        self.assertTrue(intent_resp.get_json()["ok"])
        with patch.object(srv.SveClient, "login_with_cookie", return_value="synthetic-cross-sid"), \
                patch.object(srv.SveClient, "info", return_value={"id": 10002, "nickname": "test-cross-attempt"}):
            cross_resp = self.client.post(
                "/api/login/cookie",
                json={"cookies": "NTES_SESS=fake-cookie-value"},
                headers=friend_hdrs,
            ).get_json()
        self.assertFalse(cross_resp["ok"])
        self.assertNotIn("10002", srv.STATE["accounts"])

    def test_remote_mode_blocks_non_admin_subscription_forwarding_and_enforces_bindings(self):
        srv = self.srv
        link = Mock()
        link.accounts_summary.return_value = {
            "ok": True,
            "schema_version": 2,
            "session_write": 2,
            "subscription_identity": 2,
            "accounts": [
                {"account_id": "10001", "uid": 10001, "name": "test-a1", "nickname": "test-a1", "version": 1},
            ],
            "tombstones": {},
        }
        link.subscribe.return_value = {"ok": True, "account_id": "10001"}
        link.unsubscribe.return_value = {"ok": True, "account_id": "10001"}
        with srv.LOCK:
            srv.STATE["remote"] = {"enabled": True, "host": "sve.example.com", "user": "synthetic-user"}
            srv.STATE["tunnel"] = Mock(running=True, connection_config=dict(srv.STATE["remote"]))
        self.patches.enter_context(patch.object(srv, "agent", return_value=link))

        friend_hdrs = self.cf_headers("friend@example.com")
        for path in ("/api/subscribe", "/api/unsubscribe"):
            resp = self.client.post(
                path,
                json={"account_id": "10001", "tournament_id": "ev1"},
                headers=friend_hdrs,
            )
            self.assertEqual(403, resp.status_code)
            self.assertEqual("forbidden", resp.get_json()["code"])
        link.subscribe.assert_not_called()
        link.unsubscribe.assert_not_called()

    def test_stale_account_cookie_from_another_user_falls_back_to_caller_default_and_clears_cookie(self):
        srv = self.srv
        sid_val = "synthetic-" + "sid-10002"
        acc2 = srv.Account(name="test-a2", sessionid=sid_val, uid=10002, nickname="test-a2")
        acc2.version = 1
        with srv.LOCK:
            srv.STATE["accounts"]["10002"] = acc2
            srv.persist_dataset()
            store, users_doc = srv._ensure_user_state_locked()
            store.bind_account(users_doc, "friend@example.com", "user", "10002", select=True)
            store.save(users_doc)

        admin_hdrs = self.cf_headers("admin@example.com")
        friend_hdrs = self.cf_headers("friend@example.com")

        # Admin selects 10001, setting sve_account_id=10001 in the shared browser cookie jar.
        sel_resp = self.client.post("/api/account/select", json={"account_id": "10001"}, headers=admin_hdrs)
        self.assertTrue(sel_resp.get_json()["ok"])
        self.assertEqual("10001", self.client.get_cookie("sve_account_id").value)

        # Friend now uses the same browser without an explicit account_id parameter.
        st_resp = self.client.get("/api/state", headers=friend_hdrs)
        st_json = st_resp.get_json()
        self.assertTrue(st_json["ok"])
        self.assertEqual("10002", st_json["active_account_id"])

        with patch.object(srv.SveClient, "tournaments", return_value=[]):
            tour_resp = self.client.get("/api/tournaments?area=广东省-广州市&enrich=0", headers=friend_hdrs)
        self.assertEqual(200, tour_resp.status_code)
        tour_json = tour_resp.get_json()
        self.assertTrue(tour_json["ok"], tour_json)
        self.assertEqual("10002", tour_json["account_id"])

    def test_login_status_is_scoped_per_user_without_leaking_nickname_or_uid(self):
        srv = self.srv
        admin_hdrs = self.cf_headers("admin@example.com")
        friend_hdrs = self.cf_headers("friend@example.com")

        with srv.LOCK:
            srv.STATE["login"] = {
                "running": False,
                "ok": True,
                "message": "登录成功: test-secret-admin (uid=10001)",
                "error": "",
                "user_id": "owner",
            }
            srv.STATE.setdefault("login_by_user", {})["owner"] = dict(srv.STATE["login"])

        admin_st = self.client.get("/api/state", headers=admin_hdrs).get_json()
        self.assertTrue(admin_st["login"]["ok"])
        self.assertIn("10001", admin_st["login"]["message"])

        friend_st = self.client.get("/api/state", headers=friend_hdrs).get_json()
        self.assertFalse(friend_st["login"]["ok"])
        self.assertEqual("", friend_st["login"]["message"])
        self.assertNotIn("10001", json.dumps(friend_st["login"]))

        friend_status = self.client.get("/api/login/status", headers=friend_hdrs).get_json()
        self.assertFalse(friend_status["ok"])
        self.assertEqual("", friend_status["message"])

    def test_forget_account_Saves_user_store_before_retiring_dataset_or_rolls_back(self):
        srv = self.srv
        admin_hdrs = self.cf_headers("admin@example.com")
        with srv.LOCK:
            store, _ = srv._ensure_user_state_locked()

        with patch.object(store, "save", side_effect=OSError("synthetic users-v1 disk full")):
            resp = self.client.post(
                "/api/account/forget",
                json={"account_id": "10001", "expected_version": 1, "operation_id": "op-atomic-fail-1"},
                headers=admin_hdrs,
            )
            self.assertFalse(resp.get_json()["ok"])

        # Since user_store.save failed, the account and subscription must remain intact (not half-deleted).
        self.assertIn("10001", srv.STATE["accounts"])
        self.assertNotIn("10001", srv.STATE["tombstones"])
        persisted = srv.IdentityStore(self.data).load()
        self.assertIn("10001", persisted["accounts"])
        self.assertNotIn("10001", persisted["tombstones"])




if __name__ == "__main__":
    unittest.main()
