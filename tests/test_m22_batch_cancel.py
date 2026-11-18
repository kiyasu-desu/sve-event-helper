"""Isolated M22 batch cancel API tests. Never touch production data or network."""
from __future__ import annotations

from contextlib import ExitStack
import copy
import hashlib
import importlib.util
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
                self.assertEqual("未报名或不可取消（%s）" % (status or "未知状态"), row["reason"])
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
                patch.object(srv.SveClient, "cancel", autospec=True, side_effect=cancel):
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


if __name__ == "__main__":
    unittest.main()
