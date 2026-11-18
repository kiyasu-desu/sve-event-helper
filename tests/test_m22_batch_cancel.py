"""Isolated M22 batch cancel API tests. Never touch production data or network."""
from __future__ import annotations

from contextlib import ExitStack
import copy
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
import time
import unittest
from unittest.mock import Mock, patch
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
SYNTHETIC_KEY = "synthetic-key-for-tests"


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
    return {str(path): hashlib.sha256(path.read_bytes()).digest() for path in paths
            if path.is_file() and path.name != ".identity-writer.lock"}


def fake_account(srv, uid: int | str = 10001, name: str | None = None, session: str | None = None):
    label = name or str(uid)
    return srv.Account(name=label, uid=uid, nickname=label,
                       sessionid=session or ("synthetic-session-%s" % uid),
                       deviceid="synthetic-device")


class BatchCancelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protected_before = protected_snapshot()
        cls.import_dir = tempfile.TemporaryDirectory(prefix="sve-m22-import-")
        cls.addClassCleanup(cls.import_dir.cleanup)
        with patch.dict(os.environ, {"SVE_DATA_DIR": str(Path(cls.import_dir.name) / "import"),
                                     "SVE_APP_KEY": SYNTHETIC_KEY}):
            cls.srv = load_module("sve_m22_server", "app/client/server.py")

    @classmethod
    def tearDownClass(cls):
        if cls.protected_before != protected_snapshot():
            raise AssertionError("Protected production data/log files changed")

    def setUp(self):
        srv = self.srv
        self.directory = tempfile.TemporaryDirectory(prefix="sve-m22-case-")
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)

        self.patches.enter_context(patch.dict(os.environ, {
            "SVE_DATA_DIR": str(self.data),
            "SVE_APP_KEY": SYNTHETIC_KEY,
        }))
        network = self.patches.enter_context(patch.object(urllib.request, "urlopen", side_effect=AssertionError("Unexpected network")))
        sock = self.patches.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("Unexpected socket")))
        proc = self.patches.enter_context(patch.object(subprocess, "Popen", side_effect=AssertionError("Unexpected process")))
        thread = self.patches.enter_context(patch("threading.Thread.start", side_effect=AssertionError("Unexpected thread")))
        for guard in (network, sock, proc, thread):
            self.addCleanup(guard.assert_not_called)

        self.sleep = self.patches.enter_context(patch.object(srv.time, "sleep"))
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
        self._real_restore_missing_schedulers = srv.restore_missing_schedulers
        self.patches.enter_context(patch.object(srv, "restore_missing_schedulers"))
        notify = self.patches.enter_context(patch.object(srv, "send_notify", side_effect=AssertionError("Unexpected notification")))
        self.addCleanup(notify.assert_not_called)
        self.patches.enter_context(patch.object(srv, "dispatch_notification"))
        self.info = self.patches.enter_context(patch.object(srv.SveClient, "info"))
        signup = self.patches.enter_context(patch.object(srv.SveClient, "signup", side_effect=AssertionError("Unexpected signup")))
        self.addCleanup(signup.assert_not_called)

        accounts = {str(uid): fake_account(srv, uid) for uid in (10001, 10002, 100000)}
        srv.STATE.update(
            accounts=accounts,
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
        store = srv.IdentityStore(self.data)
        store.load(initialize=True)
        self.patches.enter_context(patch.object(srv, "STORE", store))
        self.patches.enter_context(patch.dict(srv.RECORDS_REFRESHING, {"flag": False}))

        def _cleanup_refresh():
            with srv.LOCK:
                srv.RECORDS_REFRESHING["flag"] = False
                threads = list(srv._RECONCILE_THREADS)
            for t in threads:
                t.join(timeout=3)

        self.addCleanup(_cleanup_refresh)
        srv.persist_accounts()
        srv.persist_subs()
        srv.persist_records()
        self.client = srv.app.test_client()
        self.client.set_cookie("sve_account_id", "10001")

    def snapshot(self):
        srv = self.srv
        return ({p.name: p.read_bytes() for p in self.data.iterdir() if p.is_file() and p.name != ".identity-writer.lock"},
                {k: a.to_dict() for k, a in srv.STATE["accounts"].items()},
                copy.deepcopy(srv.STATE["subs"]),
                copy.deepcopy(srv.STATE["records"]),
                srv.STATE["active"])

    def post(self, path: str, body):
        response = self.client.post(path, json=body)
        self.assertEqual(200, response.status_code)
        self.assertIn("no-store", response.headers["Cache-Control"])
        return response.get_json()

    def remote(self):
        srv = self.srv
        rows = self.client.get("/api/accounts/summary").get_json()["accounts"]
        srv.STATE["accounts"].pop("100000", None)
        srv.persist_accounts()
        srv.STATE["remote"] = {"enabled": True, "host": "sve.example.com", "user": "synthetic-user"}
        srv.STATE["tunnel"] = Mock(running=True, connection_config=dict(srv.STATE["remote"]))
        link = Mock()
        link.accounts_summary.return_value = {"ok": True, "schema_version": 2, "session_write": 2,
                                             "subscription_identity": 2, "accounts": rows}
        self.patches.enter_context(patch.object(srv, "agent", return_value=link))
        return link

    def test_validation_limits_and_shared_parser_reject_without_upstream_calls(self):
        srv = self.srv
        before = self.snapshot()
        with patch.object(srv, "SveClient", side_effect=AssertionError("Validation must not build client")) as client:
            for path in ("/api/batch_cancel", "/api/batch_enroll"):
                for body in (
                    {"account_ids": [], "tournaments": ["test-e1"]},
                    {"account_ids": ["10001"], "tournaments": []},
                    {"account_ids": ["10001"], "tournaments": ["   "]},
                ):
                    res = self.post(path, body)
                    self.assertFalse(res["ok"])
                    self.assertEqual("batch_selection_required", res["code"])

                over_60_single = self.post(path, {
                    "account_ids": ["10001"],
                    "tournaments": ["test-e-%d" % i for i in range(61)],
                })
                self.assertFalse(over_60_single["ok"])
                self.assertEqual("batch_limit_exceeded", over_60_single["code"])
                self.assertIn("请减少账号或赛事", over_60_single["error"])

                over_60_matrix = self.post(path, {
                    "account_ids": ["10001", "10002", "100000"],
                    "tournaments": ["test-e-%d" % i for i in range(21)],
                })
                self.assertFalse(over_60_matrix["ok"])
                self.assertEqual("batch_limit_exceeded", over_60_matrix["code"])

                for bad_body in (
                    {"account_ids": "10001", "tournaments": ["test-e1"]},
                    {"account_ids": ["10001"], "tournaments": "test-e1"},
                    {"account_ids": ["10001"], "tournaments": ""},
                    {"account_ids": ["10001"], "tournaments": 0},
                    {"account_ids": ["10001"], "tournaments": False},
                    {"account_ids": ["10001", "999"], "tournaments": ["test-e1"]},
                ):
                    bad = self.post(path, bad_body)
                    self.assertFalse(bad["ok"])
                    self.assertEqual("identity_conflict", bad["code"])
            client.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_duplicate_and_none_tournaments_are_deduplicated(self):
        srv = self.srv
        calls = []
        def detail(client, tid):
            calls.append(tid)
            return {"name": "test-dup", "zh_user_status": "已报名" if len(calls) == 1 else "可报名"}
        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "cancel", autospec=True) as cancel:
            res = self.post("/api/batch_cancel", {
                "account_ids": ["10001"],
                "tournaments": ["test-dup", None, " ", "test-dup"],
            })
        self.assertTrue(res["ok"])
        self.assertEqual(1, len(res["results"]))
        self.assertEqual({"ok": 1, "skip": 0, "fail": 0, "total": 1}, res["summary"])
        cancel.assert_called_once()

    def test_rule_a_only_cancels_registered_and_booked_and_skips_all_others(self):
        srv = self.srv
        for status in ("已报名", "已预约"):
            with self.subTest(cancellable_status=status):
                calls = []
                def detail(client, tid, s=status):
                    calls.append(tid)
                    return {"name": "test-event", "zh_user_status": s if len(calls) == 1 else "可报名"}
                with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                        patch.object(srv.SveClient, "cancel", autospec=True) as cancel:
                    res = self.post("/api/batch_cancel", {"account_ids": ["10001"], "tournaments": ["test-e1"]})
                self.assertTrue(res["ok"])
                self.assertEqual({"ok": 1, "skip": 0, "fail": 0, "total": 1}, res["summary"])
                self.assertEqual("ok", res["results"][0]["result"])
                self.assertEqual("", res["results"][0]["reason"])
                cancel.assert_called_once()
                self.sleep.assert_not_called()

        non_cancellable = (
            "签到中", "可报名", "已报满", "签到中-未报名",
            "可补录", "签到中-可补录", "已结束", "已取消", "", None,
        )
        key = "10001|test-skip"
        sched = Mock()
        srv.STATE["subs"][key] = {"account_id": "10001", "uid": 10001, "tournament_id": "test-skip", "name": "test-skip"}
        srv.STATE["schedulers"][key] = sched
        srv.STATE["records"][key] = {"account_id": "10001", "uid": 10001, "tournament_id": "test-skip", "status": "已报名"}
        srv.persist_subs()
        srv.persist_records()
        for status in non_cancellable:
            with self.subTest(skipped_status=status):
                with patch.object(srv.SveClient, "detail", autospec=True,
                                  return_value={"name": "test-skip", "zh_user_status": status}), \
                        patch.object(srv.SveClient, "cancel", autospec=True) as cancel:
                    res = self.post("/api/batch_cancel", {"account_ids": ["10001"], "tournaments": ["test-skip"]})
                self.assertTrue(res["ok"])
                self.assertEqual({"ok": 0, "skip": 1, "fail": 0, "total": 1}, res["summary"])
                row = res["results"][0]
                self.assertEqual("skip", row["result"])
                self.assertEqual("当前状态不可取消（%s）" % (status or "未知状态"), row["reason"])
                cancel.assert_not_called()
                sched.stop.assert_not_called()
                self.assertIn(key, srv.STATE["subs"])
                self.assertIn(key, srv.STATE["schedulers"])
                self.assertEqual("已报名", srv.STATE["records"][key]["status"])

    def test_explicit_client_per_account_with_duplicate_nicknames_never_uses_active(self):
        srv = self.srv
        srv.STATE["accounts"]["10002"] = fake_account(srv, 10002, "test-shared")
        srv.STATE["accounts"]["100000"] = fake_account(srv, 100000, "test-shared")
        srv.persist_accounts()
        self.client.set_cookie("sve_account_id", "10001")

        cancelled = set()
        seen_clients = []
        def detail(client, tid):
            aid = client.account.account_id
            seen_clients.append(aid)
            return {"name": "test-shared-event", "zh_user_status": "可报名" if aid in cancelled else "已报名"}
        def cancel(client, tid):
            cancelled.add(client.account.account_id)

        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel):
            res = self.post("/api/batch_cancel", {
                "account_ids": ["10002", "100000", "10002"],
                "tournaments": ["test-e1"],
            })
        self.assertTrue(res["ok"])
        self.assertEqual({"10002", "100000"}, cancelled)
        self.assertNotIn("10001", seen_clients)
        self.assertEqual(["10002", "100000"], [r["account_id"] for r in res["results"]])
        self.assertEqual({"ok": 2, "skip": 0, "fail": 0, "total": 2}, res["summary"])

    def test_fast_verification_retries_once_after_half_second_then_resolves_or_fails(self):
        srv = self.srv
        for initial in ("已报名", "已预约"):
            # Case 1: first verification still shows initial status, second after 0.5s succeeds.
            with self.subTest(retry_succeeds=initial):
                self.sleep.reset_mock()
                seq = iter([
                    {"name": "test-lag", "zh_user_status": initial},
                    {"name": "test-lag", "zh_user_status": initial},
                    {"name": "test-lag", "zh_user_status": "可报名"},
                ])
                with patch.object(srv.SveClient, "detail", autospec=True, side_effect=lambda *_: next(seq)), \
                        patch.object(srv.SveClient, "cancel", autospec=True) as cancel:
                    res = self.post("/api/batch_cancel", {"account_ids": ["10001"], "tournaments": ["test-lag"]})
                cancel.assert_called_once()
                self.sleep.assert_called_once_with(0.5)
                self.assertEqual("ok", res["results"][0]["result"])

            # Case 2: both verifications remain in initial status -> fail without touching Rule B.
            with self.subTest(retry_still_enrolled=initial):
                self.sleep.reset_mock()
                key = "10001|test-stuck"
                sched = Mock()
                srv.STATE["subs"][key] = {"account_id": "10001", "uid": 10001, "tournament_id": "test-stuck"}
                srv.STATE["schedulers"][key] = sched
                srv.STATE["records"][key] = {"account_id": "10001", "uid": 10001, "tournament_id": "test-stuck", "status": initial}
                seq = iter([
                    {"name": "test-stuck", "zh_user_status": initial},
                    {"name": "test-stuck", "zh_user_status": initial},
                    {"name": "test-stuck", "zh_user_status": initial},
                ])
                with patch.object(srv.SveClient, "detail", autospec=True, side_effect=lambda *_: next(seq)), \
                        patch.object(srv.SveClient, "cancel", autospec=True) as cancel:
                    res = self.post("/api/batch_cancel", {"account_ids": ["10001"], "tournaments": ["test-stuck"]})
                cancel.assert_called_once()
                self.sleep.assert_called_once_with(0.5)
                row = res["results"][0]
                self.assertEqual("fail", row["result"])
                self.assertEqual("提交后复核状态仍为 %r" % initial, row["reason"])
                sched.stop.assert_not_called()
                self.assertIn(key, srv.STATE["subs"])
                self.assertEqual(initial, srv.STATE["records"][key]["status"])

    def test_error_isolation_across_detail_cancel_and_verification_failures(self):
        srv = self.srv
        for tid in ("test-d-err", "test-c-err", "test-v1-err", "test-v2-err", "test-good"):
            key = "10001|" + tid
            srv.STATE["subs"][key] = {"account_id": "10001", "uid": 10001, "tournament_id": tid}
            srv.STATE["schedulers"][key] = Mock()
            srv.STATE["records"][key] = {"account_id": "10001", "uid": 10001, "tournament_id": tid, "status": "已报名"}

        calls = {}
        def detail(client, tid):
            n = calls.get(tid, 0) + 1
            calls[tid] = n
            if tid == "test-d-err":
                raise srv.SveError("test-detail-fail")
            if tid == "test-c-err":
                return {"name": tid, "zh_user_status": "已报名"}
            if tid == "test-v1-err":
                if n == 1:
                    return {"name": tid, "zh_user_status": "已报名"}
                raise srv.SveError("test-verify-1")
            if tid == "test-v2-err":
                if n <= 2:
                    return {"name": tid, "zh_user_status": "已报名"}
                raise srv.SveError("test-verify-2")
            return {"name": tid, "zh_user_status": "已报名" if n == 1 else "已取消"}

        def cancel(client, tid):
            if tid == "test-c-err":
                raise srv.SveError("test-cancel-fail")

        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel), \
                patch.object(srv, "_spawn_cancel_reconciler") as spawn_reconciler:
            res = self.post("/api/batch_cancel", {
                "account_ids": ["10001"],
                "tournaments": ["test-d-err", "test-c-err", "test-v1-err", "test-v2-err", "test-good"],
            })

        self.assertTrue(res["ok"])
        self.assertEqual({"ok": 1, "skip": 0, "fail": 4, "total": 5}, res["summary"])
        by_tid = {r["tournament_id"]: r for r in res["results"]}
        self.assertEqual(("fail", "test-detail-fail"), (by_tid["test-d-err"]["result"], by_tid["test-d-err"]["reason"]))
        self.assertEqual(("fail", "test-cancel-fail"), (by_tid["test-c-err"]["result"], by_tid["test-c-err"]["reason"]))
        self.assertEqual(("fail", "提交后复核失败：test-verify-1"), (by_tid["test-v1-err"]["result"], by_tid["test-v1-err"]["reason"]))
        self.assertEqual(("fail", "提交后复核失败：test-verify-2"), (by_tid["test-v2-err"]["result"], by_tid["test-v2-err"]["reason"]))
        self.assertEqual("ok", by_tid["test-good"]["result"])
        self.sleep.assert_called_once_with(0.5)
        self.assertEqual(["10001|test-v1-err", "10001|test-v2-err"], [c.args[0] for c in spawn_reconciler.call_args_list])

        for failed_tid in ("test-d-err", "test-c-err", "test-v1-err", "test-v2-err"):
            key = "10001|" + failed_tid
            self.assertIn(key, srv.STATE["subs"])
            srv.STATE["schedulers"][key].stop.assert_not_called()
            self.assertEqual("已报名", srv.STATE["records"][key]["status"])
        self.assertNotIn("10001|test-good", srv.STATE["subs"])
        self.assertEqual("已取消", srv.STATE["records"]["10001|test-good"]["status"])

    def test_rule_b_cleans_lingering_subscription_outside_lock_and_updates_records(self):
        srv = self.srv
        lock_held_during_stop = []
        sched_10001 = Mock()
        sched_10001.stop.side_effect = lambda: lock_held_during_stop.append(srv.LOCK._is_owned())
        sched_10002 = Mock()

        srv.STATE["subs"] = {
            "10001|test-t1": {"account_id": "10001", "uid": 10001, "tournament_id": "test-t1", "name": "test-t1"},
            "10002|test-t1": {"account_id": "10002", "uid": 10002, "tournament_id": "test-t1", "name": "test-t1"},
        }
        srv.STATE["schedulers"] = {
            "10001|test-t1": sched_10001,
            "10002|test-t1": sched_10002,
        }
        srv.STATE["records"] = {
            "10001|test-t1": {"account_id": "10001", "uid": 10001, "tournament_id": "test-t1",
                              "status": "已报名", "updated": "2026-01-01 00:00:00", "checked_at": 0.0},
            "10001|test-t3": {"account_id": "10001", "uid": 10001, "tournament_id": "test-t3",
                              "status": "已报名", "updated": "2026-01-01 00:00:00", "checked_at": 0.0},
            "10002|test-t1": {"account_id": "10002", "uid": 10002, "tournament_id": "test-t1",
                              "status": "已报名", "updated": "2026-01-01 00:00:00", "checked_at": 0.0},
        }
        srv.persist_subs()
        srv.persist_records()

        phase = {}
        def detail(client, tid):
            n = phase.get(tid, 0) + 1
            phase[tid] = n
            if n == 1:
                return {"name": tid, "zh_user_status": "已报名"}
            if tid == "test-t1":
                return {"name": tid, "zh_user_status": "可报名"}
            return {"name": tid, "zh_user_status": ""}  # Triggers "已取消" fallback on t3; t2 has no record.

        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "cancel", autospec=True):
            res = self.post("/api/batch_cancel", {
                "account_ids": ["10001"],
                "tournaments": ["test-t1", "test-t2", "test-t3"],
            })

        self.assertTrue(res["ok"])
        self.assertEqual({"ok": 3, "skip": 0, "fail": 0, "total": 3}, res["summary"])
        sched_10001.stop.assert_called_once_with()
        self.assertEqual([False], lock_held_during_stop)
        sched_10002.stop.assert_not_called()

        self.assertNotIn("10001|test-t1", srv.STATE["subs"])
        self.assertNotIn("10001|test-t1", srv.STATE["schedulers"])
        self.assertIn("10002|test-t1", srv.STATE["subs"])
        self.assertIn("10002|test-t1", srv.STATE["schedulers"])

        self.assertEqual("可报名", srv.STATE["records"]["10001|test-t1"]["status"])
        self.assertGreater(srv.STATE["records"]["10001|test-t1"]["checked_at"], 0.0)
        self.assertNotEqual("2026-01-01 00:00:00", srv.STATE["records"]["10001|test-t1"]["updated"])
        self.assertEqual("已取消", srv.STATE["records"]["10001|test-t3"]["status"])
        self.assertNotIn("10001|test-t2", srv.STATE["records"])
        self.assertEqual("已报名", srv.STATE["records"]["10002|test-t1"]["status"])

        srv.load_all()
        self.assertNotIn("10001|test-t1", srv.STATE["subs"])
        self.assertIn("10002|test-t1", srv.STATE["subs"])
        self.assertEqual("可报名", srv.STATE["records"]["10001|test-t1"]["status"])
        self.assertEqual("已取消", srv.STATE["records"]["10001|test-t3"]["status"])

    def test_remote_proxy_forwards_batch_cancel_with_resolved_account_ids(self):
        srv = self.srv
        link = self.remote()
        before = self.snapshot()
        link.endpoint.return_value = {
            "ok": True,
            "results": [{"account_id": "10001", "tournament_id": "test-t1", "result": "ok", "reason": ""}],
            "summary": {"ok": 1, "skip": 0, "fail": 0, "total": 1},
        }
        with patch.object(srv.SveClient, "detail") as detail, \
                patch.object(srv.SveClient, "cancel") as cancel:
            res = self.post("/api/batch_cancel", {"accounts": ["10001", "100000"], "tournaments": ["test-t1"]})
            self.assertTrue(res["ok"])
            link.endpoint.assert_called_once_with(
                "POST", "/api/batch_cancel",
                {"account_ids": ["10001", "100000"], "tournaments": ["test-t1"]}, {},
            )

            link.endpoint.reset_mock()
            bad = self.post("/api/batch_cancel", {"account_ids": "10001", "tournaments": ["test-t1"]})
            self.assertFalse(bad["ok"])
            self.assertEqual("identity_conflict", bad["code"])
            link.endpoint.assert_not_called()
            detail.assert_not_called()
            cancel.assert_not_called()
        self.assertEqual(before, self.snapshot())


class BatchCancelConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protected_before = protected_snapshot()
        cls.srv = BatchCancelTests.srv if hasattr(BatchCancelTests, "srv") else load_module("sve_m22_server", "app/client/server.py")

    @classmethod
    def tearDownClass(cls):
        if cls.protected_before != protected_snapshot():
            raise AssertionError("Protected production data/log files changed")

    def setUp(self):
        srv = self.srv
        self.directory = tempfile.TemporaryDirectory(prefix="sve-m22-conc-")
        self.addCleanup(self.directory.cleanup)
        self.data = Path(self.directory.name)
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)

        self.patches.enter_context(patch.dict(os.environ, {
            "SVE_DATA_DIR": str(self.data),
            "SVE_APP_KEY": SYNTHETIC_KEY,
        }))
        network = self.patches.enter_context(patch.object(urllib.request, "urlopen", side_effect=AssertionError("Unexpected network")))
        sock = self.patches.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("Unexpected socket")))
        proc = self.patches.enter_context(patch.object(subprocess, "Popen", side_effect=AssertionError("Unexpected process")))
        notify = self.patches.enter_context(patch.object(srv, "send_notify", side_effect=AssertionError("Unexpected notification")))
        for guard in (network, sock, proc, notify):
            self.addCleanup(guard.assert_not_called)

        self.patches.enter_context(patch.object(srv.time, "sleep"))
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
        self._real_restore_missing_schedulers = srv.restore_missing_schedulers
        self.patches.enter_context(patch.object(srv, "restore_missing_schedulers"))
        self.patches.enter_context(patch.object(srv, "dispatch_notification"))
        self.patches.enter_context(patch.object(srv.SveClient, "info"))

        accounts = {str(uid): fake_account(srv, uid) for uid in (10001, 10002)}
        srv.STATE.update(
            accounts=accounts,
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
        store = srv.IdentityStore(self.data)
        store.load(initialize=True)
        self.patches.enter_context(patch.object(srv, "STORE", store))
        self.patches.enter_context(patch.dict(srv.RECORDS_REFRESHING, {"flag": False}))

        def _cleanup_refresh():
            with srv.LOCK:
                srv.RECORDS_REFRESHING["flag"] = False
                threads = list(srv._RECONCILE_THREADS)
            for t in threads:
                t.join(timeout=3)

        self.addCleanup(_cleanup_refresh)
        srv.persist_accounts()
        srv.persist_subs()
        srv.persist_records()

    def _get(self, path: str):
        with self.srv.app.test_client() as client:
            client.set_cookie("sve_account_id", "10001")
            response = client.get(path)
            self.assertEqual(200, response.status_code)
            return response.get_json()

    def _post(self, path: str, body: dict):
        with self.srv.app.test_client() as client:
            client.set_cookie("sve_account_id", "10001")
            response = client.post(path, json=body)
            self.assertEqual(200, response.status_code)
            return response.get_json()

    def _setup_scheduler(self, key: str, tid: str, *, on_event=None):
        srv = self.srv
        acc = srv.STATE["accounts"]["10001"]
        sched = srv.Scheduler(srv.SveClient(acc), tid, second_confirm_delay=0,
                              confirm_window=1, confirm_retry=0.01,
                              on_event=on_event)
        srv.STATE["subs"][key] = {"account_id": "10001", "uid": 10001, "tournament_id": tid, "name": tid}
        srv.STATE["schedulers"][key] = sched
        thread = srv.scheduler_thread(sched, key)
        self.addCleanup(lambda: (sched.stop(), thread.join(timeout=3) if thread.ident is not None else None))
        return sched, thread

    def _wait_for_quiesce_hold(self, sched, timeout: float = 5.0):
        wait_evt = threading.Event()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with sched._action_cond:
                if sched._action_holds > 0:
                    return
            wait_evt.wait(0.01)
        self.fail("Timed out waiting for scheduler quiesce hold")

    def test_quiesce_waits_for_inflight_signup_before_cancelling_and_stops_resignup(self):
        srv = self.srv
        key, tid = "10001|test-race", "test-race"
        state_lock = threading.Lock()
        server_state = {"status": "可报名", "signups": 0, "cancels": 0}
        signup_entered = threading.Event()
        release_signup = threading.Event()
        cancel_detail_called = threading.Event()

        sched, thread = self._setup_scheduler(key, tid)
        sched.second_confirm_delay = 0.2
        sched.stop_flag.clear()

        def detail(client, qtid):
            if client is not sched.client:
                cancel_detail_called.set()
            with state_lock:
                return {"name": "test-race", "zh_user_status": server_state["status"],
                        "player_num": 1, "max_player_number": 8}

        def signup(client, qtid):
            signup_entered.set()
            release_signup.wait(timeout=5)
            with state_lock:
                server_state["signups"] += 1
                server_state["status"] = "已报名"

        def cancel(client, qtid):
            with state_lock:
                server_state["cancels"] += 1
                server_state["status"] = "可报名"

        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "signup", autospec=True, side_effect=signup), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel):
            thread.start()
            self.assertTrue(signup_entered.wait(timeout=5))

            result_box = {}
            def run_batch():
                result_box["res"] = self._post("/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid]})

            batch_thread = threading.Thread(target=run_batch, daemon=True)
            batch_thread.start()
            # Wait until api_batch_cancel enters quiesce() while signup is still in flight.
            self._wait_for_quiesce_hold(sched)
            self.assertFalse(cancel_detail_called.is_set())

            release_signup.set()
            batch_thread.join(timeout=5)
            thread.join(timeout=5)

        res = result_box["res"]
        self.assertEqual("ok", res["results"][0]["result"])
        self.assertEqual(1, server_state["signups"])
        self.assertEqual(1, server_state["cancels"])
        self.assertEqual("可报名", server_state["status"])
        self.assertTrue(sched.stop_flag.is_set())
        self.assertNotIn(key, srv.STATE["subs"])

    def test_quiesce_blocks_queued_scheduler_during_cancel_until_stopped(self):
        srv = self.srv
        key, tid = "10001|test-queued", "test-queued"
        state_lock = threading.Lock()
        server_state = {"status": "已报名", "signups": 0}
        sample_seen = threading.Event()
        release_sample = threading.Event()
        cancel_entered = threading.Event()
        release_cancel = threading.Event()

        def on_event(kind, data):
            if kind == "sample":
                sample_seen.set()
                release_sample.wait(timeout=5)

        sched, thread = self._setup_scheduler(key, tid, on_event=on_event)

        def detail(client, qtid):
            if client is sched.client:
                return {"name": "test-queued", "zh_user_status": "可报名",
                        "player_num": 1, "max_player_number": 8}
            with state_lock:
                return {"name": "test-queued", "zh_user_status": server_state["status"],
                        "player_num": 1, "max_player_number": 8}

        def signup(client, qtid):
            with state_lock:
                server_state["signups"] += 1
                server_state["status"] = "已报名"

        def cancel(client, qtid):
            cancel_entered.set()
            release_cancel.wait(timeout=5)
            with state_lock:
                server_state["status"] = "可报名"

        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "signup", autospec=True, side_effect=signup), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel):
            thread.start()
            self.assertTrue(sample_seen.wait(timeout=5))

            result_box = {}
            batch_thread = threading.Thread(
                target=lambda: result_box.__setitem__("res", self._post(
                    "/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid]}
                )),
                daemon=True,
            )
            batch_thread.start()
            self.assertTrue(cancel_entered.wait(timeout=5))

            # Release scheduler past sample() toward try_once()'s signup gate while cancel is mid-flight.
            release_sample.set()
            wait_evt = threading.Event()
            wait_evt.wait(0.05)
            self.assertEqual(0, server_state["signups"])

            release_cancel.set()
            batch_thread.join(timeout=5)
            thread.join(timeout=5)

        self.assertEqual("ok", result_box["res"]["results"][0]["result"])
        self.assertEqual(0, server_state["signups"])
        self.assertEqual("可报名", server_state["status"])
        self.assertTrue(sched.stop_flag.is_set())
        self.assertNotIn(key, srv.STATE["subs"])

    def test_quiesce_tracks_stopping_worker_already_popped_from_schedulers(self):
        srv = self.srv
        key, tid = "10001|test-popped", "test-popped"
        state_lock = threading.Lock()
        server_state = {"status": "可报名", "cancels": 0}
        signup_entered = threading.Event()
        release_signup = threading.Event()

        sched, thread = self._setup_scheduler(key, tid)

        def detail(client, qtid):
            with state_lock:
                return {"name": "test-popped", "zh_user_status": server_state["status"],
                        "player_num": 1, "max_player_number": 8}

        def signup(client, qtid):
            signup_entered.set()
            release_signup.wait(timeout=5)
            with state_lock:
                server_state["status"] = "已报名"

        def cancel(client, qtid):
            with state_lock:
                server_state["cancels"] += 1
                server_state["status"] = "可报名"

        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "signup", autospec=True, side_effect=signup), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel):
            thread.start()
            self.assertTrue(signup_entered.wait(timeout=5))
            # Simulate concurrent unsubscribe popping STATE["schedulers"][key] after signup already started.
            with srv.LOCK:
                srv.STATE["schedulers"].pop(key, None)
                srv.STATE["subs"].pop(key, None)
            sched.stop()

            result_box = {}
            batch_thread = threading.Thread(
                target=lambda: result_box.__setitem__("res", self._post(
                    "/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid]}
                )),
                daemon=True,
            )
            batch_thread.start()
            self._wait_for_quiesce_hold(sched)
            release_signup.set()
            batch_thread.join(timeout=5)
            thread.join(timeout=5)

        self.assertEqual("ok", result_box["res"]["results"][0]["result"])
        self.assertEqual(1, server_state["cancels"])
        self.assertEqual("可报名", server_state["status"])

    def test_quiesce_preserves_scheduler_on_skip_and_blocks_concurrent_subscribe(self):
        srv = self.srv
        key, tid = "10001|test-skip-race", "test-skip-race"
        sched, thread = self._setup_scheduler(key, tid)
        detail_entered = threading.Event()
        release_detail = threading.Event()
        sub_detail_only = threading.Event()

        def detail(client, qtid):
            if not sub_detail_only.is_set():
                detail_entered.set()
                release_detail.wait(timeout=5)
            return {"name": "test-skip-race", "zh_user_status": "可报名",
                    "player_num": 1, "max_player_number": 8}

        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "cancel", side_effect=AssertionError("Must not cancel")):
            result_box = {}
            batch_thread = threading.Thread(
                target=lambda: result_box.__setitem__("res", self._post(
                    "/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid]}
                )),
                daemon=True,
            )
            batch_thread.start()
            self.assertTrue(detail_entered.wait(timeout=5))

            sub_detail_only.set()
            sub_res = self._post("/api/subscribe", {"account_id": "10001", "tournament_id": tid})
            self.assertFalse(sub_res["ok"])
            self.assertEqual("subscription_conflict", sub_res["code"])

            release_detail.set()
            batch_thread.join(timeout=5)

        self.assertEqual("skip", result_box["res"]["results"][0]["result"])
        self.assertFalse(sched.stop_flag.is_set())
        self.assertIn(key, srv.STATE["subs"])
        self.assertNotIn(key, srv.STATE["batch_cancel_keys"])

    def test_quiesce_suppresses_stale_completed_event_when_already_registered_during_cancel(self):
        srv = self.srv
        key, tid = "10001|test-stale-complete", "test-stale-complete"
        state_lock = threading.Lock()
        server_state = {"status": "已报名"}
        sample_seen = threading.Event()
        release_sample = threading.Event()
        cancel_entered = threading.Event()
        release_cancel = threading.Event()
        completed_events = []

        def on_event(kind, data):
            if kind == "sample":
                sample_seen.set()
                release_sample.wait(timeout=5)
            elif kind == "completed":
                completed_events.append(data)

        sched, thread = self._setup_scheduler(key, tid, on_event=on_event)

        def detail(client, qtid):
            with state_lock:
                return {"name": "test-stale-complete", "zh_user_status": server_state["status"],
                        "player_num": 1, "max_player_number": 8}

        def cancel(client, qtid):
            cancel_entered.set()
            release_cancel.wait(timeout=5)
            with state_lock:
                server_state["status"] = "可报名"

        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel):
            thread.start()
            self.assertTrue(sample_seen.wait(timeout=5))

            result_box = {}
            batch_thread = threading.Thread(
                target=lambda: result_box.__setitem__("res", self._post(
                    "/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid]}
                )),
                daemon=True,
            )
            batch_thread.start()
            self.assertTrue(cancel_entered.wait(timeout=5))

            release_sample.set()
            threading.Event().wait(0.05)
            self.assertEqual([], completed_events)
            release_cancel.set()
            batch_thread.join(timeout=5)
            thread.join(timeout=5)

        self.assertEqual("ok", result_box["res"]["results"][0]["result"])
        self.assertEqual([], completed_events)
        self.assertFalse(sched.completed)
        self.assertTrue(sched.stop_flag.is_set())
        self.assertNotIn(key, srv.STATE["subs"])

    def _wait_for_refresh_idle(self, timeout: float = 5.0):
        srv = self.srv
        wait_evt = threading.Event()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with srv.LOCK:
                if not srv.RECORDS_REFRESHING["flag"]:
                    return
            wait_evt.wait(0.01)
        self.fail("Timed out waiting for refresh_records worker to finish")

    def test_stale_refresh_worker_cannot_overwrite_cancelled_record_status(self):
        srv = self.srv
        key, tid = "10001|test-stale-rec", "test-stale-rec"
        srv.STATE["records"][key] = {
            "account_id": "10001", "account": "10001", "uid": 10001,
            "tournament_id": tid, "name": tid, "source": "测试",
            "status": "已报名", "at": "2026-01-01 00:00:00",
            "updated": "2026-01-01 00:00:00", "checked_at": 0.0,
        }
        srv.persist_records()

        state_lock = threading.Lock()
        server_state = {"status": "已报名"}
        detail_threads = []
        refresh_fetched = threading.Event()
        release_refresh_write = threading.Event()
        self.addCleanup(release_refresh_write.set)

        def detail(client, qtid):
            name = threading.current_thread().name
            detail_threads.append(name)
            with state_lock:
                snapshot = {"name": qtid, "zh_user_status": server_state["status"],
                            "player_num": 5, "max_player_number": 8}
            if name == "rec-refresh":
                refresh_fetched.set()
                release_refresh_write.wait(timeout=5)
            return snapshot

        def cancel(client, qtid):
            with state_lock:
                server_state["status"] = "可报名"

        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel):
            initial = self._get("/api/records")
            self.assertEqual("已报名", initial["records"][0]["status"])
            self.assertTrue(refresh_fetched.wait(timeout=5))

            res = self._post("/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid]})
            self.assertEqual("ok", res["results"][0]["result"])
            cancel_checked_at = srv.STATE["records"][key]["checked_at"]
            self.assertGreater(cancel_checked_at, 0.0)
            self.assertEqual("可报名", srv.STATE["records"][key]["status"])

            release_refresh_write.set()
            self._wait_for_refresh_idle()

            self.assertEqual("可报名", srv.STATE["records"][key]["status"])
            self.assertEqual(cancel_checked_at, srv.STATE["records"][key]["checked_at"])
            on_disk = json.loads(srv.STORE.path.read_text(encoding="utf-8"))["records"]
            self.assertEqual("可报名", on_disk[key]["status"])
            self.assertEqual(cancel_checked_at, on_disk[key]["checked_at"])

            calls_before = len(detail_threads)
            after = self._get("/api/records")
            self.assertEqual("可报名", after["records"][0]["status"])
            self.assertEqual(calls_before, len(detail_threads))

    def test_stale_refresh_worker_skips_cancelled_key_and_refreshes_other_keys(self):
        srv = self.srv
        k1, t1 = "10001|test-rec-1", "test-rec-1"
        k2, t2 = "10001|test-rec-2", "test-rec-2"
        for k, tid in ((k1, t1), (k2, t2)):
            srv.STATE["records"][k] = {
                "account_id": "10001", "account": "10001", "uid": 10001,
                "tournament_id": tid, "name": tid, "source": "测试",
                "status": "已报名", "at": "2026-01-01 00:00:00",
                "updated": "2026-01-01 00:00:00", "checked_at": 0.0,
            }
        srv.persist_records()

        state_lock = threading.Lock()
        server_state = {t1: "已报名", t2: "已报名"}
        t1_refresh_fetched = threading.Event()
        release_refresh = threading.Event()
        self.addCleanup(release_refresh.set)

        def detail(client, qtid):
            with state_lock:
                snapshot = {"name": qtid, "zh_user_status": server_state[qtid],
                            "player_num": 4, "max_player_number": 8}
            if threading.current_thread().name == "rec-refresh" and qtid == t1:
                t1_refresh_fetched.set()
                release_refresh.wait(timeout=5)
            return snapshot

        def cancel(client, qtid):
            with state_lock:
                server_state[qtid] = "可报名"

        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel):
            self._get("/api/records")
            self.assertTrue(t1_refresh_fetched.wait(timeout=5))

            res = self._post("/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [t1]})
            self.assertEqual("ok", res["results"][0]["result"])
            cancel_checked_at = srv.STATE["records"][k1]["checked_at"]

            release_refresh.set()
            self._wait_for_refresh_idle()

        self.assertEqual("可报名", srv.STATE["records"][k1]["status"])
        self.assertEqual(cancel_checked_at, srv.STATE["records"][k1]["checked_at"])
        self.assertEqual("已报名", srv.STATE["records"][k2]["status"])
        self.assertGreater(srv.STATE["records"][k2]["checked_at"], 0.0)
        self.assertEqual(4, srv.STATE["records"][k2]["player_num"])

    def test_overlapping_subscribe_during_cancel_rejected_even_after_cancel_finishes(self):
        srv = self.srv
        key, tid = "10001|test-overlap-sub", "test-overlap-sub"
        sched, sched_thread = self._setup_scheduler(key, tid)
        sched.stop_flag.clear()

        state_lock = threading.Lock()
        server_state = {"status": "已报名"}
        sub_detail_entered = threading.Event()
        release_sub_detail = threading.Event()
        signup_calls = []
        self.addCleanup(release_sub_detail.set)

        def detail(client, qtid):
            if client is sched.client:
                return {"name": qtid, "zh_user_status": "未报名", "player_num": 1, "max_player_number": 8}
            name = threading.current_thread().name
            with state_lock:
                snapshot = {"name": qtid, "zh_user_status": server_state["status"],
                            "player_num": 1, "max_player_number": 8}
            if name.startswith("sub-overlap"):
                sub_detail_entered.set()
                release_sub_detail.wait(timeout=5)
            return snapshot

        def cancel(client, qtid):
            with state_lock:
                server_state["status"] = "可报名"

        def signup(client, qtid):
            signup_calls.append(qtid)

        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel), \
                patch.object(srv.SveClient, "signup", autospec=True, side_effect=signup):
            sched_thread.start()
            sub_result = {}

            def run_subscribe():
                sub_result["res"] = self._post("/api/subscribe", {"account_id": "10001", "tournament_id": tid})

            sub_thread = threading.Thread(target=run_subscribe, daemon=True, name="sub-overlap")
            sub_thread.start()
            self.assertTrue(sub_detail_entered.wait(timeout=5))

            cancel_res = self._post("/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid]})
            self.assertEqual("ok", cancel_res["results"][0]["result"])
            sched_thread.join(timeout=5)
            self.assertNotIn(key, srv.STATE["batch_cancel_keys"])
            self.assertNotIn(key, srv.STATE["subs"])
            self.assertNotIn(key, srv.STATE["schedulers"])
            self.assertNotIn(key, srv.STATE["workers"])

            release_sub_detail.set()
            sub_thread.join(timeout=5)

            self.assertFalse(sub_result["res"]["ok"])
            self.assertEqual("subscription_conflict", sub_result["res"]["code"])
            self.assertNotIn(key, srv.STATE["subs"])
            self.assertNotIn(key, srv.STATE["schedulers"])
            self.assertEqual([], signup_calls)

            # A subsequent fresh subscribe initiated after cancellation completes works normally.
            fresh_res = self._post("/api/subscribe", {"account_id": "10001", "tournament_id": tid})
            self.assertTrue(fresh_res["ok"])
            self.assertIn(key, srv.STATE["subs"])
            new_sched = srv.STATE["schedulers"].get(key)
            new_worker = srv.STATE["workers"].get(key)
            if new_sched is not None:
                new_sched.stop()
            if new_worker is not None and new_worker.ident is not None:
                new_worker.join(timeout=3)

    def test_subscribe_while_batch_cancel_active_rejects_without_upstream_detail(self):
        srv = self.srv
        tid = "test-active-cancel-sub"
        cancel_entered = threading.Event()
        release_cancel = threading.Event()
        server_state = {"status": "已报名"}
        detail_threads = []
        self.addCleanup(release_cancel.set)

        def detail(client, qtid):
            detail_threads.append(threading.current_thread().name)
            return {"name": qtid, "zh_user_status": server_state["status"], "player_num": 1, "max_player_number": 8}

        def cancel(client, qtid):
            cancel_entered.set()
            release_cancel.wait(timeout=5)
            server_state["status"] = "可报名"

        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel):
            batch_box = {}
            batch_thread = threading.Thread(
                target=lambda: batch_box.__setitem__("res", self._post(
                    "/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid]}
                )),
                daemon=True,
                name="batch-cancel-worker",
            )
            batch_thread.start()
            self.assertTrue(cancel_entered.wait(timeout=5))

            calls_before = len(detail_threads)
            sub_res = self._post("/api/subscribe", {"account_id": "10001", "tournament_id": tid})
            self.assertFalse(sub_res["ok"])
            self.assertEqual("subscription_conflict", sub_res["code"])
            self.assertEqual(calls_before, len(detail_threads))

            release_cancel.set()
            batch_thread.join(timeout=5)

        self.assertEqual("ok", batch_box["res"]["results"][0]["result"])

    def test_batch_cancel_serializes_with_inflight_explicit_enroll_and_batch_enroll(self):
        srv = self.srv
        key, tid, tid2 = "10001|test-enroll-race", "test-enroll-race", "test-enroll-other"
        state_lock = threading.Lock()
        server_state = {tid: "可报名", tid2: "可报名"}
        enroll_signup_entered = threading.Event()
        release_enroll_signup = threading.Event()
        cancel_calls = []
        self.addCleanup(release_enroll_signup.set)

        def detail(client, qtid):
            with state_lock:
                st = server_state[qtid]
            return {"name": qtid, "zh_user_status": st, "player_num": 1, "max_player_number": 8,
                    "start_time": "2000-01-01 00:00:00", "end_time": "2099-12-31 23:59:59"}

        def signup(client, qtid):
            if qtid == tid and threading.current_thread().name == "explicit-enroll-1":
                enroll_signup_entered.set()
                release_enroll_signup.wait(timeout=5)
            with state_lock:
                server_state[qtid] = "已报名"

        def cancel(client, qtid):
            cancel_calls.append(qtid)
            with state_lock:
                server_state[qtid] = "可报名"

        # Case 1: in-flight /api/enroll finishes before batch_cancel queries & cancels the pair.
        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "signup", autospec=True, side_effect=signup), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel):
            enroll_box = {}
            enroll_thread = threading.Thread(
                target=lambda: enroll_box.__setitem__("res", self._post(
                    "/api/enroll", {"account_id": "10001", "tournament_id": tid}
                )),
                daemon=True,
                name="explicit-enroll-1",
            )
            enroll_thread.start()
            self.assertTrue(enroll_signup_entered.wait(timeout=5))

            cancel_box = {}
            cancel_thread = threading.Thread(
                target=lambda: cancel_box.__setitem__("res", self._post(
                    "/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid]}
                )),
                daemon=True,
                name="batch-cancel-after-enroll",
            )
            cancel_thread.start()
            threading.Event().wait(0.05)
            self.assertEqual([], cancel_calls)

            release_enroll_signup.set()
            enroll_thread.join(timeout=5)
            cancel_thread.join(timeout=5)

        self.assertTrue(enroll_box["res"]["ok"])
        self.assertEqual("ok", cancel_box["res"]["results"][0]["result"])
        self.assertEqual([tid], cancel_calls)
        self.assertEqual("可报名", server_state[tid])

        # Case 2: active batch_cancel blocks concurrent /api/enroll and same-key /api/batch_enroll
        # without blocking unrelated tournament tid2.
        with state_lock:
            server_state[tid] = "已报名"
            server_state[tid2] = "可报名"
        cancel_entered = threading.Event()
        release_cancel = threading.Event()
        blocked_signups = []
        self.addCleanup(release_cancel.set)

        def cancel_blocking(client, qtid):
            cancel_entered.set()
            release_cancel.wait(timeout=5)
            with state_lock:
                server_state[qtid] = "可报名"

        def signup_track(client, qtid):
            blocked_signups.append(qtid)
            with state_lock:
                server_state[qtid] = "已报名"

        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "signup", autospec=True, side_effect=signup_track), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel_blocking):
            batch_cancel_box = {}
            t_cancel = threading.Thread(
                target=lambda: batch_cancel_box.__setitem__("res", self._post(
                    "/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid]}
                )),
                daemon=True,
            )
            t_cancel.start()
            self.assertTrue(cancel_entered.wait(timeout=5))

            r_enroll = self._post("/api/enroll", {"account_id": "10001", "tournament_id": tid})
            self.assertFalse(r_enroll["ok"])
            self.assertEqual("batch_cancel_conflict", r_enroll["code"])

            r_batch_enroll = self._post("/api/batch_enroll", {"account_ids": ["10001"], "tournaments": [tid, tid2]})
            self.assertEqual("fail", r_batch_enroll["results"][0]["result"])
            self.assertEqual("ok", r_batch_enroll["results"][1]["result"])
            self.assertEqual([tid2], blocked_signups)

            release_cancel.set()
            t_cancel.join(timeout=5)

        self.assertEqual("ok", batch_cancel_box["res"]["results"][0]["result"])
        self.assertEqual("可报名", server_state[tid])
        self.assertEqual({}, srv._PAIR_LOCKS)

    def test_enroll_that_entered_before_cancel_commit_is_rejected_without_resignup(self):
        srv = self.srv
        tid = "test-enroll-overlap"
        server_state = {"status": "已报名"}
        real_pair_lock = srv._pair_action_lock
        enroll_at_lock = threading.Event()
        release_enroll = threading.Event()
        upstream_calls = []
        self.addCleanup(release_enroll.set)

        def wrapped_pair_lock(key, *args, **kwargs):
            if threading.current_thread().name == "enroll-overlap":
                enroll_at_lock.set()
                release_enroll.wait(timeout=5)
            return real_pair_lock(key, *args, **kwargs)

        def detail(client, qtid):
            upstream_calls.append(("detail", threading.current_thread().name))
            return {"name": qtid, "zh_user_status": server_state["status"], "player_num": 1, "max_player_number": 8,
                    "start_time": "2000-01-01 00:00:00", "end_time": "2099-12-31 23:59:59"}

        def cancel(client, qtid):
            upstream_calls.append(("cancel", threading.current_thread().name))
            server_state["status"] = "可报名"

        with patch.object(srv, "_pair_action_lock", side_effect=wrapped_pair_lock), \
                patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel), \
                patch.object(srv.SveClient, "signup", side_effect=AssertionError("Must not signup")):
            enroll_box = {}
            t_enroll = threading.Thread(
                target=lambda: enroll_box.__setitem__("res", self._post(
                    "/api/enroll", {"account_id": "10001", "tournament_id": tid}
                )),
                daemon=True,
                name="enroll-overlap",
            )
            t_enroll.start()
            self.assertTrue(enroll_at_lock.wait(timeout=5))

            cancel_res = self._post("/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid]})
            self.assertEqual("ok", cancel_res["results"][0]["result"])

            release_enroll.set()
            t_enroll.join(timeout=5)

        self.assertFalse(enroll_box["res"]["ok"])
        self.assertEqual("batch_cancel_conflict", enroll_box["res"]["code"])
        self.assertFalse(any(tname == "enroll-overlap" for _, tname in upstream_calls))
        self.assertEqual("可报名", server_state["status"])

    def test_batch_cancel_stops_at_budget_without_orphan_upstream_work(self):
        srv = self.srv
        tids = ["test-b1", "test-b2", "test-b3", "test-b4"]
        for tid in tids:
            key = f"10001|{tid}"
            srv.STATE["subs"][key] = {"account_id": "10001", "uid": 10001, "tournament_id": tid, "name": tid}
        srv.persist_subs()

        server_status = {tid: "已报名" for tid in tids}
        seen_calls = []

        def detail(client, qtid):
            seen_calls.append(("detail", qtid))
            if qtid == "test-b1" and server_status[qtid] == "已报名":
                threading.Event().wait(0.06)
            return {"name": qtid, "zh_user_status": server_status[qtid], "player_num": 1, "max_player_number": 8}

        def cancel(client, qtid):
            seen_calls.append(("cancel", qtid))
            server_status[qtid] = "可报名"

        # Case 1: budget expires during detail() of test-b1 before cancel() -> zero cancels attempted.
        with patch.object(srv, "BATCH_CANCEL_BUDGET", 0.03), \
                patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel):
            res1 = self._post("/api/batch_cancel", {"account_ids": ["10001"], "tournaments": tids})

        self.assertEqual(4, len(res1["results"]))
        self.assertTrue(all(r["result"] == "fail" for r in res1["results"]))
        self.assertEqual([("detail", "test-b1")], seen_calls)
        self.assertIn("未提交取消", res1["results"][0]["reason"])
        self.assertIn("未执行", res1["results"][1]["reason"])
        for tid in tids:
            self.assertIn(f"10001|{tid}", srv.STATE["subs"])

        # Case 2: test-b1 completes cancel within budget; test-b2..b4 stop cleanly at budget cutoff.
        seen_calls.clear()

        def detail_fast(client, qtid):
            seen_calls.append(("detail", qtid))
            return {"name": qtid, "zh_user_status": server_status[qtid], "player_num": 1, "max_player_number": 8}

        def cancel_slow_after_b1(client, qtid):
            seen_calls.append(("cancel", qtid))
            server_status[qtid] = "可报名"
            threading.Event().wait(0.06)

        with patch.object(srv, "BATCH_CANCEL_BUDGET", 0.04), \
                patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail_fast), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel_slow_after_b1):
            res2 = self._post("/api/batch_cancel", {"account_ids": ["10001"], "tournaments": tids})

        self.assertEqual("ok", res2["results"][0]["result"])
        self.assertEqual(["fail", "fail", "fail"], [r["result"] for r in res2["results"][1:]])
        self.assertNotIn("10001|test-b1", srv.STATE["subs"])
        for tid in tids[1:]:
            self.assertIn(f"10001|{tid}", srv.STATE["subs"])
            self.assertNotIn(("cancel", tid), seen_calls)
        self.assertEqual([("detail", "test-b1"), ("cancel", "test-b1"), ("detail", "test-b1")], seen_calls)

    def test_batch_cancel_pair_lock_wait_times_out_at_budget_without_queueing_cancel(self):
        srv = self.srv
        t1, t2 = "test-lock-timeout-1", "test-lock-timeout-2"
        state_lock = threading.Lock()
        server_state = {t1: "可报名", t2: "已报名"}
        enroll_entered_signup = threading.Event()
        release_slow_enroll = threading.Event()
        cancel_calls = []
        self.addCleanup(release_slow_enroll.set)

        def detail(client, qtid):
            with state_lock:
                st = server_state[qtid]
            return {"name": qtid, "zh_user_status": st, "player_num": 1, "max_player_number": 8,
                    "start_time": "2000-01-01 00:00:00", "end_time": "2099-12-31 23:59:59"}

        def signup(client, qtid):
            enroll_entered_signup.set()
            release_slow_enroll.wait(timeout=5)
            with state_lock:
                server_state[qtid] = "已报名"

        def cancel(client, qtid):
            cancel_calls.append(qtid)

        with patch.object(srv, "BATCH_CANCEL_BUDGET", 0.08), \
                patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "signup", autospec=True, side_effect=signup), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel):
            enroll_box = {}
            enroll_thread = threading.Thread(
                target=lambda: enroll_box.__setitem__("res", self._post(
                    "/api/enroll", {"account_id": "10001", "tournament_id": t1}
                )),
                daemon=True,
                name="slow-enroll",
            )
            enroll_thread.start()
            self.addCleanup(lambda: (release_slow_enroll.set(), enroll_thread.join(timeout=3)))
            self.assertTrue(enroll_entered_signup.wait(timeout=5))

            started = time.monotonic()
            cancel_res = self._post("/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [t1, t2]})
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 1.0)
            self.assertEqual(["fail", "fail"], [r["result"] for r in cancel_res["results"]])
            self.assertIn("未执行", cancel_res["results"][0]["reason"])
            self.assertIn("未执行", cancel_res["results"][1]["reason"])
            self.assertEqual([], cancel_calls)
            self.assertNotIn(f"10001|{t1}", srv.STATE["batch_cancel_keys"])

            release_slow_enroll.set()
            enroll_thread.join(timeout=5)

        self.assertTrue(enroll_box["res"]["ok"])
        self.assertEqual("已报名", server_state[t1])
        self.assertEqual([], cancel_calls)
        self.assertEqual({}, srv._PAIR_LOCKS)

    def test_batch_cancel_serializes_with_single_cancel_endpoint(self):
        srv = self.srv
        tid = "test-single-cancel-race"
        state_lock = threading.Lock()
        server_state = {"status": "已报名"}
        cancel_entered = threading.Event()
        release_cancel = threading.Event()
        cancel_threads = []
        self.addCleanup(release_cancel.set)

        def detail(client, qtid):
            with state_lock:
                st = server_state["status"]
            return {"name": qtid, "zh_user_status": st, "player_num": 1, "max_player_number": 8}

        def cancel(client, qtid):
            tname = threading.current_thread().name
            cancel_threads.append(tname)
            cancel_entered.set()
            release_cancel.wait(timeout=5)
            with state_lock:
                server_state["status"] = "可报名"

        # Case 1: in-flight /api/cancel finishes first; waiting /api/batch_cancel sees 可报名 and skips.
        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel):
            single_box = {}
            t_single = threading.Thread(
                target=lambda: single_box.__setitem__("res", self._post(
                    "/api/cancel", {"account_id": "10001", "tournament_id": tid}
                )),
                daemon=True,
                name="single-cancel-worker",
            )
            t_single.start()
            self.addCleanup(lambda: (release_cancel.set(), t_single.join(timeout=3)))
            self.assertTrue(cancel_entered.wait(timeout=5))

            batch_box = {}
            t_batch = threading.Thread(
                target=lambda: batch_box.__setitem__("res", self._post(
                    "/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid]}
                )),
                daemon=True,
                name="batch-cancel-worker",
            )
            t_batch.start()
            self.addCleanup(lambda: t_batch.join(timeout=3))
            threading.Event().wait(0.05)
            self.assertEqual(["single-cancel-worker"], cancel_threads)

            release_cancel.set()
            t_single.join(timeout=5)
            t_batch.join(timeout=5)

        self.assertTrue(single_box["res"]["ok"])
        self.assertEqual("skip", batch_box["res"]["results"][0]["result"])
        self.assertEqual(["single-cancel-worker"], cancel_threads)
        self.assertEqual({}, srv._PAIR_LOCKS)

        # Case 2: active /api/batch_cancel finishes first; waiting /api/cancel sees 可报名 and does not re-cancel.
        server_state["status"] = "已报名"
        cancel_threads.clear()
        cancel_entered.clear()
        release_cancel.clear()

        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel):
            batch_box2 = {}
            t_batch2 = threading.Thread(
                target=lambda: batch_box2.__setitem__("res", self._post(
                    "/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid]}
                )),
                daemon=True,
                name="batch-cancel-worker-2",
            )
            t_batch2.start()
            self.addCleanup(lambda: t_batch2.join(timeout=3))
            self.assertTrue(cancel_entered.wait(timeout=5))

            single_box2 = {}
            t_single2 = threading.Thread(
                target=lambda: single_box2.__setitem__("res", self._post(
                    "/api/cancel", {"account_id": "10001", "tournament_id": tid}
                )),
                daemon=True,
                name="single-cancel-worker-2",
            )
            t_single2.start()
            self.addCleanup(lambda: t_single2.join(timeout=3))
            threading.Event().wait(0.05)
            self.assertEqual(["batch-cancel-worker-2"], cancel_threads)

            release_cancel.set()
            t_batch2.join(timeout=5)
            t_single2.join(timeout=5)

        self.assertEqual("ok", batch_box2["res"]["results"][0]["result"])
        self.assertFalse(single_box2["res"]["ok"])
        self.assertEqual(["batch-cancel-worker-2"], cancel_threads)
        self.assertEqual({}, srv._PAIR_LOCKS)

    def test_scheduler_rechecks_stop_flag_after_attempt_callback_before_signup(self):
        srv = self.srv
        tid = "test-stop-on-attempt"
        acc = srv.STATE["accounts"]["10001"]
        events = []

        def on_event(kind, data):
            events.append(kind)
            if kind == "attempt":
                sched.stop()

        sched = srv.Scheduler(srv.SveClient(acc), tid, second_confirm_delay=0,
                              confirm_window=1, confirm_retry=0.01, on_event=on_event)

        with patch.object(srv.SveClient, "detail", autospec=True, return_value={
            "name": tid, "zh_user_status": "可报名", "player_num": 1, "max_player_number": 8
        }), patch.object(srv.SveClient, "signup", side_effect=AssertionError("Must not signup")) as signup:
            res = sched.try_once()

        self.assertEqual(["sample", "attempt"], events)
        self.assertFalse(res.attempted)
        self.assertEqual("守护已停止", res.reason)
        signup.assert_not_called()
        with sched._action_cond:
            self.assertFalse(sched._action_in_flight)
        with sched.quiesce():
            pass

    def _wait_for_reconcile_idle(self, timeout: float = 5.0):
        srv = self.srv
        wait_evt = threading.Event()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with srv.LOCK:
                if not srv._RECONCILE_THREADS:
                    return
            wait_evt.wait(0.01)
        self.fail("Timed out waiting for cancel reconcilers to finish")

    def test_post_cancel_verification_bounded_by_batch_deadline(self):
        srv = self.srv
        tid = "test-verify-budget"
        detail_calls = []

        def slow_cancel_past_deadline(client, qtid):
            threading.Event().wait(0.07)

        def detail_still_registered(client, qtid):
            detail_calls.append(qtid)
            return {"name": qtid, "zh_user_status": "已报名", "player_num": 1, "max_player_number": 8}

        # Case 1: cancel exceeds BATCH_CANCEL_BUDGET (0.04) but stays under VERIFY_BUDGET (0.15):
        # at most ONE verification detail() executes; 0.5s second retry is skipped.
        with patch.object(srv, "BATCH_CANCEL_BUDGET", 0.04), \
                patch.object(srv, "BATCH_CANCEL_VERIFY_BUDGET", 0.25), \
                patch.object(srv, "BATCH_CANCEL_RECONCILE_DELAYS", (0.01,)), \
                patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail_still_registered), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=slow_cancel_past_deadline):
            started = time.monotonic()
            res1 = self._post("/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid, "test-second"]})
            elapsed1 = time.monotonic() - started

        self._wait_for_reconcile_idle()
        self.assertLess(elapsed1, 0.25)
        self.assertEqual(["fail", "fail"], [r["result"] for r in res1["results"]])
        self.assertEqual([tid, tid], detail_calls[:2])  # preflight + single bounded verify only
        self.assertNotIn("test-second", detail_calls)
        self.assertIn("未执行", res1["results"][1]["reason"])

        # Case 2: cancel exceeds BATCH_CANCEL_VERIFY_BUDGET: zero verification detail calls run.
        detail_calls.clear()

        def slow_cancel_past_verify_budget(client, qtid):
            threading.Event().wait(0.10)

        with patch.object(srv, "BATCH_CANCEL_BUDGET", 0.03), \
                patch.object(srv, "BATCH_CANCEL_VERIFY_BUDGET", 0.06), \
                patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail_still_registered), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=slow_cancel_past_verify_budget):
            started = time.monotonic()
            res2 = self._post("/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid]})
            elapsed2 = time.monotonic() - started

        self.assertLess(elapsed2, 0.25)
        self.assertEqual("fail", res2["results"][0]["result"])
        self.assertIn("待后台确认", res2["results"][0]["reason"])
        self.assertEqual([tid], detail_calls)

    def test_ambiguous_cancel_error_rechecks_and_cleans_up_if_cancelled(self):
        srv = self.srv
        key, tid = "10001|test-ambig-cancel", "test-ambig-cancel"
        srv.STATE["records"][key] = {
            "account_id": "10001", "account": "10001", "uid": 10001,
            "tournament_id": tid, "name": tid, "source": "测试",
            "status": "已报名", "at": "2026-01-01 00:00:00",
            "updated": "2026-01-01 00:00:00", "checked_at": 0.0,
        }
        state_lock = threading.Lock()
        server_state = {"status": "已报名"}
        sample_seen = threading.Event()
        release_sample = threading.Event()
        self.addCleanup(release_sample.set)

        def on_event(kind, data):
            if kind == "sample":
                sample_seen.set()
                release_sample.wait(timeout=5)

        sched, thread = self._setup_scheduler(key, tid, on_event=on_event)

        def detail(client, qtid):
            if client is sched.client:
                return {"name": tid, "zh_user_status": "可报名", "player_num": 1, "max_player_number": 8}
            with state_lock:
                return {"name": tid, "zh_user_status": server_state["status"], "player_num": 1, "max_player_number": 8}

        def cancel_drop_after_apply(client, qtid):
            with state_lock:
                server_state["status"] = "可报名"
            raise srv.SveError("网络请求失败: timed out")

        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel_drop_after_apply), \
                patch.object(srv.SveClient, "signup", side_effect=AssertionError("Must not re-signup")):
            thread.start()
            self.assertTrue(sample_seen.wait(timeout=5))

            res = self._post("/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid]})
            release_sample.set()
            thread.join(timeout=5)

        self.assertEqual("ok", res["results"][0]["result"])
        self.assertNotIn(key, srv.STATE["subs"])
        self.assertNotIn(key, srv.STATE["schedulers"])
        self.assertTrue(sched.stop_flag.is_set())
        self.assertEqual("可报名", srv.STATE["records"][key]["status"])

    def test_uncertain_cancel_verification_holds_quiesce_until_reconciler_resolves(self):
        srv = self.srv
        # Subcase A: upstream accepted cancel, verification fails initially, reconciler sees 可报名 -> stops sub.
        key, tid = "10001|test-reconcile-ok", "test-reconcile-ok"
        srv.STATE["records"][key] = {
            "account_id": "10001", "account": "10001", "uid": 10001,
            "tournament_id": tid, "name": tid, "source": "测试",
            "status": "已报名", "at": "2026-01-01 00:00:00",
            "updated": "2026-01-01 00:00:00", "checked_at": 0.0,
        }
        state_lock = threading.Lock()
        server_state = {"status": "已报名", "detail_up": True}
        sample_seen = threading.Event()
        release_sample = threading.Event()
        self.addCleanup(release_sample.set)

        def on_event(kind, data):
            if kind == "sample":
                sample_seen.set()
                release_sample.wait(timeout=5)

        sched, thread = self._setup_scheduler(key, tid, on_event=on_event)

        def detail(client, qtid):
            if client is sched.client:
                return {"name": tid, "zh_user_status": "可报名", "player_num": 1, "max_player_number": 8}
            with state_lock:
                if not server_state["detail_up"]:
                    raise srv.SveError("verify-timeout")
                return {"name": tid, "zh_user_status": server_state["status"], "player_num": 1, "max_player_number": 8}

        def cancel(client, qtid):
            with state_lock:
                server_state["status"] = "可报名"
                server_state["detail_up"] = False

        with patch.object(srv, "BATCH_CANCEL_RECONCILE_DELAYS", (0.08, 0.08)), \
                patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel), \
                patch.object(srv.SveClient, "signup", side_effect=AssertionError("Must not signup")):
            thread.start()
            self.assertTrue(sample_seen.wait(timeout=5))

            res = self._post("/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid]})
            self.assertEqual("fail", res["results"][0]["result"])
            # Quiesce hold remains active while status is uncertain.
            with sched._action_cond:
                self.assertGreater(sched._action_holds, 0)
            self.assertIn(key, srv.STATE["batch_cancel_keys"])

            # Release scheduler past sample(): it stays blocked at quiesce and does NOT signup.
            release_sample.set()
            threading.Event().wait(0.03)
            with sched._action_cond:
                self.assertGreater(sched._action_holds, 0)

            # Upstream recovers reporting 可报名 -> reconciler cleans up subscription & stops scheduler.
            with state_lock:
                server_state["detail_up"] = True
            self._wait_for_reconcile_idle()
            thread.join(timeout=5)

        self.assertTrue(sched.stop_flag.is_set())
        self.assertNotIn(key, srv.STATE["subs"])
        self.assertNotIn(key, srv.STATE["batch_cancel_keys"])
        self.assertEqual("可报名", srv.STATE["records"][key]["status"])

        # Subcase B: upstream remained 已报名 -> reconciler releases quiesce, scheduler drops stale pre-quiesce sample.
        key2, tid2 = "10001|test-reconcile-keep", "test-reconcile-keep"
        acc = srv.STATE["accounts"]["10001"]
        sched2 = srv.Scheduler(srv.SveClient(acc), tid2, second_confirm_delay=0,
                               confirm_window=1, confirm_retry=0.01)
        srv.STATE["subs"][key2] = {"account_id": "10001", "uid": 10001, "tournament_id": tid2, "name": tid2}
        srv.STATE["schedulers"][key2] = sched2

        with srv.LOCK:
            lease_box = {}
        try_result = {}
        state_b = {"phase": 0}

        def detail_b(client, qtid):
            if client is sched2.client:
                return {"name": tid2, "zh_user_status": "可报名", "player_num": 1, "max_player_number": 8}
            state_b["phase"] += 1
            if state_b["phase"] == 1:
                return {"name": tid2, "zh_user_status": "已报名", "player_num": 1, "max_player_number": 8}
            if state_b["phase"] == 2:
                raise srv.SveError("temp-timeout")
            return {"name": tid2, "zh_user_status": "已报名", "player_num": 1, "max_player_number": 8}

        def on_event_b(kind, data):
            if kind == "sample":
                # Enter batch_cancel right while sched2 has stale 可报名 sample in hand.
                with patch.object(srv, "BATCH_CANCEL_RECONCILE_DELAYS", (0.04,)):
                    try_result["cancel_res"] = self._post(
                        "/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid2]}
                    )

        sched2.on_event = on_event_b
        with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail_b), \
                patch.object(srv.SveClient, "cancel", side_effect=srv.SveError("网络请求失败")), \
                patch.object(srv.SveClient, "signup", side_effect=AssertionError("Must not signup on stale sample")):
            attempt_res = sched2.try_once()
        self._wait_for_reconcile_idle()
        self.assertIn("已作废", attempt_res.reason)
        self.assertIn(key2, srv.STATE["subs"])
        self.assertNotIn(key2, srv.STATE["batch_cancel_keys"])
        self.assertFalse(sched2.stop_flag.is_set())

        # Subcase C: upstream stays unreachable across all retries -> reconciler fails closed & disables sub.
        key3, tid3 = "10001|test-reconcile-failclosed", "test-reconcile-failclosed"
        sched3, thread3 = self._setup_scheduler(key3, tid3)
        calls3 = {"n": 0}

        def detail_unreachable(client, qtid):
            calls3["n"] += 1
            if calls3["n"] == 1:
                return {"name": tid3, "zh_user_status": "已报名", "player_num": 1, "max_player_number": 8}
            raise srv.SveError("still-down")

        with patch.object(srv, "BATCH_CANCEL_RECONCILE_DELAYS", (0.01, 0.01)), \
                patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail_unreachable), \
                patch.object(srv.SveClient, "cancel", return_value={}):
            res3 = self._post("/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid3]})
            self._wait_for_reconcile_idle()
        self.assertEqual("fail", res3["results"][0]["result"])
        self.assertTrue(sched3.stop_flag.is_set())
        self.assertFalse(srv.STATE["subs"][key3]["enabled"])
        self.assertNotIn(key3, srv.STATE["schedulers"])
        self.assertNotIn(key3, srv.STATE["batch_cancel_keys"])

    def test_restore_missing_schedulers_retries_after_batch_cancel_hold_releases(self):
        srv = self.srv
        key1, tid1 = "10001|test-restore-skip", "test-restore-skip"
        key2, tid2 = "10001|test-restore-ok", "test-restore-ok"

        for key, tid, init_status, after_status, should_restore in (
            (key1, tid1, "可报名", "可报名", True),
            (key2, tid2, "已报名", "可报名", False),
        ):
            srv.STATE["subs"] = {
                key: {
                    "account_id": "10001", "account": "10001", "uid": 10001,
                    "tournament_id": tid, "name": tid, "enabled": True,
                }
            }
            srv.persist_subs()
            detail_entered = threading.Event()
            release_detail = threading.Event()
            cancelled = threading.Event()
            self.addCleanup(release_detail.set)

            def detail(client, qtid, *, init=init_status, after=after_status, de=detail_entered, rd=release_detail, cx=cancelled):
                if qtid == tid and threading.current_thread().name.startswith("restore-cancel"):
                    if not de.is_set():
                        de.set()
                        rd.wait(timeout=5)
                    return {"name": qtid, "zh_user_status": after if cx.is_set() else init,
                            "player_num": 1, "max_player_number": 8}
                return {"name": qtid, "zh_user_status": "未报名", "player_num": 1, "max_player_number": 8}

            def cancel(client, qtid, *, cx=cancelled):
                cx.set()

            with patch.object(srv.SveClient, "detail", autospec=True, side_effect=detail), \
                    patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel):
                box = {}
                t = threading.Thread(
                    target=lambda: box.__setitem__("res", self._post(
                        "/api/batch_cancel", {"account_ids": ["10001"], "tournaments": [tid]}
                    )),
                    daemon=True,
                    name=f"restore-cancel-{tid}",
                )
                t.start()
                self.addCleanup(lambda k=key, rd=release_detail, th=t: (
                    rd.set(),
                    th.join(timeout=3),
                    srv.STATE["schedulers"][k].stop() if k in srv.STATE["schedulers"] else None,
                    srv.STATE["workers"][k].join(timeout=3)
                    if (k in srv.STATE["workers"] and srv.STATE["workers"][k].ident is not None) else None,
                ))
                self.assertTrue(detail_entered.wait(timeout=5))

                started_during_hold = self._real_restore_missing_schedulers("test")
                self.assertEqual(0, started_during_hold)
                if should_restore:
                    self.assertIn(key, srv.STATE["deferred_restores"])
                self.assertNotIn(key, srv.STATE["schedulers"])

                release_detail.set()
                t.join(timeout=5)

            if should_restore:
                self.assertEqual("skip", box["res"]["results"][0]["result"])
                self.assertIn(key, srv.STATE["schedulers"])
                self.assertNotIn(key, srv.STATE["deferred_restores"])
                sched = srv.STATE["schedulers"][key]
                worker = srv.STATE["workers"].get(key)
                sched.stop()
                if worker is not None and worker.ident is not None:
                    worker.join(timeout=3)
            else:
                self.assertEqual("ok", box["res"]["results"][0]["result"])
                self.assertNotIn(key, srv.STATE["schedulers"])
                self.assertNotIn(key, srv.STATE["subs"])
                self.assertNotIn(key, srv.STATE["deferred_restores"])


if __name__ == "__main__":
    unittest.main()
