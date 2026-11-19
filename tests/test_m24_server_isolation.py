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


if __name__ == "__main__":
    unittest.main()
