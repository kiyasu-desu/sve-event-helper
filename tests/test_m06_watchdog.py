"""Offline clocks, fake upstream/notifications, protected persistence and HTTP."""
from contextlib import ExitStack
import copy
import contextlib
import hashlib
import io
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

SYNTHETIC_KEY = "synthetic-test-key"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app/client/core"))
sys.path.insert(0, str(ROOT / "scripts/poller"))
from scheduler_health import Progress
from watchdog_state import Monitor, faults, options


def snapshot(status="active", *, overdue=0, failures=0, duration=0):
    progress = {"phase": "waiting_open", "overdue_seconds": overdue,
                "consecutive_upstream_failures": failures, "upstream_failure_seconds": duration}
    return {"health_version": 1, "ok": True, "mode": "executor", "process_id": "synthetic-process",
            "tasks": [{"account_id": "10001", "tournament_id": "test-event", "status": status,
                       "thread_alive": status == "active", "progress": progress}]}


def idle():
    return dict(snapshot(), tasks=[])


class DecisionTests(unittest.TestCase):
    def observe(self, monitor, value, now, **kwargs):
        return monitor.observe(value, now=now, boot="synthetic-boot", **kwargs)

    def test_dead_thread_confirm_dedup_restart_and_recovery(self):
        monitor = Monitor()
        self.assertEqual([], self.observe(monitor, snapshot("worker_dead"), 0))
        events = self.observe(monitor, snapshot("worker_dead"), 60)
        self.assertEqual("worker_dead", events[0]["kind"])
        monitor.accepted(events)
        monitor = Monitor(json.loads(json.dumps(monitor.value)))
        self.assertEqual([], self.observe(monitor, snapshot("worker_dead"), 120))
        self.assertEqual([], self.observe(monitor, idle(), 180))
        events = self.observe(monitor, idle(), 240)
        self.assertTrue(events[0]["recovered"])
        monitor.accepted(events)
        self.assertEqual([], self.observe(monitor, idle(), 300))
        self.assertEqual({}, monitor.value["faults"])

    def test_fast_checks_do_not_confirm_and_failed_delivery_backs_off(self):
        monitor = Monitor()
        self.observe(monitor, snapshot("worker_dead"), 0)
        self.assertEqual([], self.observe(monitor, snapshot("worker_dead"), 1))
        self.assertEqual(1, len(self.observe(monitor, snapshot("worker_dead"), 60)))
        monitor = Monitor(copy.deepcopy(monitor.value))  # Watchdog process restarted.
        self.assertEqual([], self.observe(monitor, snapshot("worker_dead"), 90))
        self.assertEqual(1, len(self.observe(monitor, snapshot("worker_dead"), 120)))
        self.assertEqual([], self.observe(monitor, snapshot("worker_dead"), 180))
        self.assertEqual(1, len(self.observe(monitor, snapshot("worker_dead"), 240)))

    def test_clock_reboot_preserves_announced_incident_without_storm(self):
        monitor = Monitor()
        self.observe(monitor, snapshot("worker_dead"), 1000)
        monitor.accepted(self.observe(monitor, snapshot("worker_dead"), 1060))
        self.assertEqual([], monitor.observe(snapshot("worker_dead"), now=1, boot="synthetic-new-boot"))
        self.assertEqual([], monitor.observe(snapshot("worker_dead"), now=61, boot="synthetic-new-boot"))

    def test_long_wait_silence_and_current_request_use_their_own_deadlines(self):
        clock = [0.0]
        with patch("scheduler_health.time.monotonic", side_effect=lambda: clock[0]), patch("scheduler_health.time.time", return_value=1800000000):
            progress = Progress()
            for phase, budget, elapsed in (("waiting_open", 1800, 1200), ("confirm_silence", 60, 59),
                                            ("confirm_second", 60, 59), ("confirm_retry", 10, 9)):
                progress.enter(phase, budget)
                clock[0] += elapsed
                value = snapshot()
                value["tasks"][0]["progress"] = progress.snapshot()
                self.assertEqual({}, faults(value, options({})))
            clock[0] += 32
            value["tasks"][0]["progress"] = progress.snapshot()
            monitor = Monitor()
            self.assertEqual([], self.observe(monitor, value, 0))
            self.assertEqual("stalled", self.observe(monitor, value, 60)[0]["kind"])

    def test_confirmation_window_ignores_wall_clock_jumps(self):
        from scheduler import Scheduler
        clock, wall = [0.0], [1800000000.0]
        client = Mock()
        client.detail.side_effect = RuntimeError("synthetic-offline")
        sched = Scheduler(client, "test-event")
        def sleep(delay):
            clock[0] += delay
            wall[0] -= 100000
        with patch("scheduler.time.monotonic", side_effect=lambda: clock[0]), patch("scheduler.time.time", side_effect=lambda: wall[0]), patch("scheduler.time.sleep", side_effect=sleep):
            self.assertFalse(sched.confirm("test-confirm", 3, 1)[0])
        self.assertEqual(3, clock[0])
        self.assertEqual(4, client.detail.call_count)

    def test_upstream_errors_are_not_dead_threads_and_transient_errors_are_silent(self):
        self.assertEqual({}, faults(snapshot(failures=2, duration=500), options({})))
        self.assertEqual({}, faults(snapshot(failures=10, duration=179), options({})))
        monitor = Monitor()
        value = snapshot(failures=10, duration=180)
        self.observe(monitor, value, 0)
        self.assertEqual("upstream_unavailable", self.observe(monitor, value, 60)[0]["kind"])

    def test_terminal_states_and_zero_subscriptions_do_not_alert(self):
        monitor = Monitor()
        for now, status in enumerate(("completed", "paused", "cancelled")):
            self.assertEqual([], self.observe(monitor, snapshot(status), now * 60))
        self.assertEqual([], self.observe(monitor, idle(), 180))

    def test_orphan_and_identity_block_are_distinct_and_changes_are_aggregated(self):
        value = snapshot("orphan")
        value["tasks"].append(dict(snapshot("identity_blocked")["tasks"][0], account_id="10002"))
        monitor = Monitor()
        self.observe(monitor, value, 0)
        events = self.observe(monitor, value, 60)
        self.assertEqual({"orphan", "identity_blocked"}, {e["kind"] for e in events})

    def test_unreachable_does_not_claim_task_recovery(self):
        monitor = Monitor()
        self.observe(monitor, snapshot("worker_dead"), 0)
        monitor.accepted(self.observe(monitor, snapshot("worker_dead"), 60))
        self.observe(monitor, None, 120, error="agent_unreachable")
        events = self.observe(monitor, None, 180, error="agent_unreachable")
        self.assertEqual(["agent_unreachable"], [e["kind"] for e in events])
        self.assertFalse(any(e["recovered"] for e in events))
        self.assertEqual(2, len(monitor.value["faults"]))

    def test_unknown_task_health_breaks_confirmation_streaks(self):
        interruptions = (
            (None, {"error": "agent_unreachable"}),
            (None, {"error": "protocol_unsupported"}),
            (None, {"error": "protocol_invalid"}),
            (None, {"error": "storage_unavailable"}),
            (dict(snapshot(), ok=False), {}),
            (dict(snapshot(), mode="remote"), {}),
        )
        for value, kwargs in interruptions:
            for state in ("pending", "failed_delivery", "announced"):
                with self.subTest(interruption=value, error=kwargs, state=state):
                    monitor = Monitor()
                    self.observe(monitor, snapshot("worker_dead"), 0)
                    key = next(iter(monitor.value["faults"]))
                    if state != "pending":
                        events = self.observe(monitor, snapshot("worker_dead"), 60)
                        self.assertEqual(1, len(events))
                        if state == "announced":
                            monitor.accepted(events)
                            self.assertEqual([], self.observe(monitor, snapshot(), 120))
                    before = copy.deepcopy(monitor.value["faults"][key])
                    self.assertGreater(before["healthy"] if state == "announced" else before["count"], 0)
                    self.assertEqual([], self.observe(monitor, value, 180, **kwargs))
                    self.assertEqual(dict(before, count=0, healthy=0), monitor.value["faults"][key])
                    # The interrupted streak must remain reset across watchdog restarts.
                    monitor = Monitor(json.loads(json.dumps(monitor.value)))
                    resumed = snapshot() if state == "announced" else snapshot("worker_dead")
                    self.assertEqual([], self.observe(monitor, resumed, 240))
                    events = self.observe(monitor, resumed, 300)
                    self.assertEqual([{"id": key, "recovered": state == "announced",
                                       "kind": "worker_dead", "account_id": "10001",
                                       "tournament_id": "test-event"}], events)
                    monitor.accepted(events)
                    if state == "announced":
                        self.assertNotIn(key, monitor.value["faults"])
                    else:
                        self.assertTrue(monitor.value["faults"][key]["announced"])

    def test_invalid_protocol_or_duplicate_task_never_means_healthy(self):
        for value in ({}, dict(snapshot(), tasks=None), dict(snapshot(), tasks=[{}]),
                      dict(snapshot(), tasks=snapshot()["tasks"] * 2), snapshot(overdue=float("nan"))):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                faults(value, options({}))
        monitor = Monitor()
        self.observe(monitor, None, 0, error="protocol_unsupported")
        self.assertEqual("protocol_unsupported", self.observe(monitor, None, 60, error="protocol_unsupported")[0]["kind"])

    def test_wrong_executor_and_storage_failure_are_global_faults(self):
        self.assertEqual("not_executor", next(iter(faults(dict(snapshot(), mode="remote"), options({})).values()))["kind"])
        self.assertEqual("storage_unavailable", next(iter(faults(dict(snapshot(), ok=False), options({})).values()))["kind"])
        with self.assertRaises(ValueError):
            Monitor({"version": 1, "faults": {"bad": {}}})
        with self.assertRaises(ValueError):
            options({"confirm_checks": 1})


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sve-m06-")
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        self.guards.enter_context(patch.dict(os.environ, {"SVE_DATA_DIR": str(self.data),
                                                        "SVE_SETTINGS_FILE": str(self.data / "settings.json"),
                                                        "SVE_APP_KEY": SYNTHETIC_KEY}))
        self.real_thread_start = threading.Thread.start
        def production_hashes():
            paths = list((ROOT / "app/client/data").rglob("*")) + list((ROOT / "capture").glob("*.log"))
            paths.append(ROOT / "config/settings.json")
            return {str(p): hashlib.sha256(p.read_bytes()).digest() for p in paths if p.is_file() and p.name != ".identity-writer.lock"}
        before = production_hashes()
        self.addCleanup(lambda: self.assertEqual(before, production_hashes()))
        for obj, name in ((socket.socket, "connect"), (urllib.request, "urlopen"),
                          (subprocess, "Popen"), (threading.Thread, "start")):
            guard = self.guards.enter_context(patch.object(obj, name, side_effect=AssertionError("Unexpected business action")))
            self.addCleanup(guard.assert_not_called)
        import watchdog
        self.watchdog = watchdog
        self.guards.enter_context(patch.object(watchdog.P, "SETTINGS", str(self.data / "settings.json")))
        self.guards.enter_context(patch.object(watchdog.P.sveenv, "DATA_DIR", str(self.data)))
        self.guards.enter_context(patch.object(watchdog, "WATCHDOG_LOG", str(self.data / "watchdog.log")))
        spec = importlib.util.spec_from_file_location("sve_m06_test", ROOT / "app/client/server.py")
        self.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.server)
        self.server.ACCOUNTS_LOADED_OK = True
        self.server.STATE["accounts"] = {"10001": self.server.Account(uid=10001, nickname="test-account")}
        self.server.STATE["active"] = "10001"
        self.server.STATE["subs"] = {"10001|test-event": {"account_id": "10001", "tournament_id": "test-event", "enabled": True}}

    def check(self, value, now, send):
        return self.watchdog.run_agent_check(fetch=lambda: value, send=send, now=now, boot="synthetic-boot")

    def test_standalone_checks_persist_aggregate_dedup_and_recovery(self):
        send = Mock(return_value=True)
        self.check(snapshot("worker_dead"), 0, send)
        send.assert_not_called()
        self.check(snapshot("worker_dead"), 60, send)
        self.assertEqual(1, send.call_count)
        self.check(snapshot("worker_dead"), 120, send)
        self.assertEqual(1, send.call_count)
        self.check(idle(), 180, send)
        self.check(idle(), 240, send)
        self.assertEqual(2, send.call_count)
        self.assertTrue(send.call_args.args[0][0]["recovered"])
        state = self.data / "watchdog/watchdog-state.json"
        self.assertEqual({}, json.loads(state.read_text())["faults"])
        if os.name != "nt":
            self.assertEqual(0, state.stat().st_mode & 0o077)

    def test_send_failure_is_reserved_before_send_and_survives_restart(self):
        state = self.data / "watchdog/watchdog-state.json"
        def fail(events, channels):
            self.assertGreater(next(iter(json.loads(state.read_text())["faults"].values()))["next_try"], 60)
            raise RuntimeError("synthetic-send-failure")
        send = Mock(side_effect=fail)
        self.check(snapshot("worker_dead"), 0, send)
        self.check(snapshot("worker_dead"), 60, send)
        self.check(snapshot("worker_dead"), 90, send)
        self.assertEqual(1, send.call_count)
        self.check(snapshot("worker_dead"), 120, Mock(return_value=True))

    def test_corrupt_state_and_concurrent_check_do_not_send(self):
        from identity_store import WriterLock
        location = self.data / "watchdog"
        with WriterLock(location):
            with self.assertRaises(Exception):
                self.check(snapshot("worker_dead"), 0, Mock(side_effect=AssertionError("send")))
        (location / "watchdog-state.json").write_text("invalid-synthetic-json", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.check(snapshot("worker_dead"), 60, Mock(side_effect=AssertionError("send")))

    def test_http_checks_real_worker_liveness_and_omits_account_secrets(self):
        srv = self.server
        sched = srv.Scheduler(Mock(), "test-event")
        srv.STATE["schedulers"]["10001|test-event"] = sched
        worker = Mock()
        worker.is_alive.return_value = False
        srv.STATE["workers"]["10001|test-event"] = worker
        with srv.app.test_client() as client:
            response = client.get("/api/scheduler/health")
            self.assertEqual(200, response.status_code)
            value = response.get_json()
            self.assertEqual("worker_dead", value["tasks"][0]["status"])
            self.assertNotIn("sessionid", response.get_data(as_text=True))
            self.assertNotIn("nickname", response.get_data(as_text=True))
            self.assertFalse(client.get("/api/state").get_json()["subs"][0]["running"])
            worker.is_alive.return_value = True
            self.assertTrue(client.get("/api/state").get_json()["subs"][0]["running"])
            self.assertEqual("active", client.get("/api/scheduler/health").get_json()["tasks"][0]["status"])
            self.assertEqual(403, client.get("/api/scheduler/health", environ_overrides={"REMOTE_ADDR": "192.0.2.10"}).status_code)
        with patch.object(srv, "LOCK", Mock(acquire=Mock(return_value=False))):
            with srv.app.test_client() as client:
                self.assertEqual(503, client.get("/api/scheduler/health").status_code)

    def test_real_registered_worker_cancel_and_exit_keep_one_executor(self):
        srv = self.server
        release, started = threading.Event(), threading.Event()
        sched = srv.Scheduler(Mock(), "test-event")
        def run():
            sched.progress.enter("waiting_open", 1800)
            started.set()
            release.wait(3)
        sched.run = run
        srv.STATE["schedulers"]["10001|test-event"] = sched
        with srv.LOCK:
            worker = srv.scheduler_thread(sched, "10001|test-event")
        self.real_thread_start(worker)  # Only this fake, bounded test worker may run.
        try:
            self.assertTrue(started.wait(2))
            with srv.app.test_client() as client:
                self.assertEqual("active", client.get("/api/scheduler/health").get_json()["tasks"][0]["status"])
                sched.stop()
                self.assertEqual("cancelled", client.get("/api/scheduler/health").get_json()["tasks"][0]["status"])
            with patch.object(srv, "STORE", Mock()):
                self.assertTrue(srv._start_scheduler_for("10001|test-event", srv.STATE["subs"]["10001|test-event"], srv.STATE["accounts"]["10001"]))
            self.assertIs(worker, srv.STATE["workers"]["10001|test-event"])
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("10001|test-event", srv.STATE["workers"])

    def test_progress_transport_is_loopback_only_no_redirect_or_proxy(self):
        value = idle()
        response = io.BytesIO(json.dumps(value).encode())
        opener = Mock(open=Mock(return_value=response))
        with patch.object(self.watchdog, "AGENT_URL", "http://127.0.0.1:8901"), patch("urllib.request.build_opener", return_value=opener) as build:
            self.assertEqual(value, self.watchdog.fetch_progress())
            self.assertEqual({}, build.call_args.args[0].proxies)
            self.assertIsNone(build.call_args.args[1].redirect_request(None, None, None, None, None, None))
        with patch.object(self.watchdog, "AGENT_URL", "https://sve.example.com"), self.assertRaises(ValueError):
            self.watchdog.fetch_progress()

    def test_old_backend_404_is_reported_as_unsupported_protocol(self):
        import urllib.error
        fetch = Mock(side_effect=urllib.error.HTTPError("http://127.0.0.1:8901/api/scheduler/health", 404, "synthetic", {}, None))
        send = Mock(return_value=True)
        for now in (0, 60):
            self.watchdog.run_agent_check(fetch=fetch, send=send, now=now, boot="synthetic-boot")
        self.assertEqual("protocol_unsupported", send.call_args.args[0][0]["kind"])

    def test_notification_aggregation_escapes_identity_and_suppresses_details(self):
        import notify
        with patch.object(notify, "notify_all", return_value=[("pushplus", True, "synthetic")]) as send:
            self.assertTrue(self.watchdog.send_events([{"kind": "stalled", "account_id": "10001", "tournament_id": "test-<script>", "recovered": False}], {}))
            self.assertNotIn("<script>", send.call_args.args[2])
            self.assertEqual({"quiet": True, "attempts": 1}, send.call_args.kwargs)
        output = io.StringIO()
        with patch.object(notify, "send_pushplus", return_value=(True, "synthetic-private-detail")), contextlib.redirect_stdout(output):
            notify.notify_all({"pushplus": {"enabled": True}}, "synthetic", "synthetic", quiet=True, attempts=1)
        self.assertEqual("", output.getvalue())

    def test_worker_exception_becomes_missing_worker_without_second_executor(self):
        srv = self.server
        sched = Mock(completed=False)
        sched.run.side_effect = RuntimeError("synthetic-worker-crash")
        srv.STATE["schedulers"]["10001|test-event"] = sched
        with srv.LOCK:
            worker = srv.scheduler_thread(sched, "10001|test-event")
        with self.assertRaises(RuntimeError):
            worker.run()  # Run the wrapper synchronously; no application thread.
        self.assertNotIn("10001|test-event", srv.STATE["workers"])
        with srv.app.test_client() as client:
            self.assertEqual("worker_dead", client.get("/api/scheduler/health").get_json()["tasks"][0]["status"])

    def test_scheduled_phases_record_attempts_silence_and_retry_without_network(self):
        from scheduler import Scheduler
        clock = [1000.0]
        client = Mock()
        samples = [{"zh_user_status": status, "player_num": 1, "max_player_number": 8}
                   for status in ("可报名", "已报名", "签到中")]
        client.detail.side_effect = samples
        sched = Scheduler(client, "test-event", second_confirm_delay=60)
        phases = []
        def wait(delay):
            phases.append(sched.progress.snapshot()["phase"])
            clock[0] += delay
        with patch("scheduler.time.monotonic", side_effect=lambda: clock[0]), patch("scheduler.time.sleep", side_effect=wait):
            self.assertTrue(sched.try_once().ok)
        self.assertEqual(["confirm_silence"], phases)
        value = sched.progress.snapshot()
        self.assertIsNotNone(value["last_attempt_at"])
        self.assertIsNotNone(value["last_success_at"])
        self.assertEqual(0, value["consecutive_upstream_failures"])
        client.signup.assert_called_once()

    def test_signup_failures_survive_successful_detail_and_recover_after_signup(self):
        from scheduler import Scheduler
        from sveapi import SveError
        clock = [0.0]
        client = Mock()
        signable = {"zh_user_status": "可报名", "player_num": 1, "max_player_number": 8}
        client.detail.return_value = signable
        client.signup.side_effect = SveError("synthetic-unavailable", http=503)
        sched = Scheduler(client, "test-event", second_confirm_delay=0)
        send = Mock(return_value=True)
        with patch("scheduler.time.monotonic", side_effect=lambda: clock[0]):
            for now in (0, 60, 120, 180, 240):
                clock[0] = now
                self.assertFalse(sched.try_once().ok)
                value = snapshot()
                value["tasks"][0]["progress"] = sched.progress.snapshot()
                self.check(value, now, send)
                if now < 240:
                    send.assert_not_called()
            self.assertEqual(5, value["tasks"][0]["progress"]["upstream_operations"]["signup"]["consecutive_upstream_failures"])
            self.assertEqual(0, value["tasks"][0]["progress"]["upstream_operations"]["detail"]["consecutive_upstream_failures"])
            self.assertEqual(1, send.call_count)
            self.assertEqual("upstream_unavailable", send.call_args.args[0][0]["kind"])
            # A successful submission clears only its own endpoint streak.
            client.signup.side_effect = None
            client.detail.side_effect = [signable, {"zh_user_status": "已报名"}, {"zh_user_status": "已报名"}]
            self.assertTrue(sched.try_once().ok)
            for now in (300, 360):
                clock[0] = now
                value["tasks"][0]["progress"] = sched.progress.snapshot()
                self.check(value, now, send)
            self.assertEqual(2, send.call_count)
            self.assertTrue(send.call_args.args[0][0]["recovered"])

    def test_upstream_operations_confirm_independently_and_validate_protocol(self):
        progress = Progress()
        clock = [0.0]
        with patch("scheduler_health.time.monotonic", side_effect=lambda: clock[0]):
            progress.result(False, "signup")
            clock[0] = 200
            for _ in range(3):
                progress.result(False)
            value = snapshot()
            progress.enter("waiting_poll", 60)
            value["tasks"][0]["progress"] = progress.snapshot()
            # One old signup error plus three new detail errors do not combine thresholds.
            self.assertEqual({}, faults(value, options({})))
            clock[0] = 400
            progress.enter("waiting_poll", 60)
            value["tasks"][0]["progress"] = progress.snapshot()
            self.assertEqual("upstream_unavailable", next(iter(faults(value, options({})).values()))["kind"])
            progress.result(True, "signup")
            self.assertEqual(3, progress.snapshot()["upstream_operations"]["detail"]["consecutive_upstream_failures"])
            for bad in (None, {}, {"detail": {}, "signup": {}}, {"detail": None, "signup": {}}):
                value["tasks"][0]["progress"]["upstream_operations"] = bad
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    faults(value, options({}))

    def test_signup_unexpected_exception_is_recorded_and_still_propagates(self):
        from scheduler import Scheduler
        client = Mock()
        client.detail.return_value = {"zh_user_status": "可报名", "player_num": 1, "max_player_number": 8}
        client.signup.side_effect = RuntimeError("synthetic-transport-error")
        sched = Scheduler(client, "test-event")
        with self.assertRaises(RuntimeError):
            sched.try_once()
        self.assertEqual(1, sched.progress.snapshot()["upstream_operations"]["signup"]["consecutive_upstream_failures"])

    def test_notification_channels_share_ui_and_environment_precedence(self):
        from notification_config import channels
        settings = self.data / "settings.json"
        settings.write_text(json.dumps({"channels": {"email": {"enabled": True, "smtp_port": 465}}}))
        (self.data / "notify.json").write_text(json.dumps({"email": {"smtp_port": 587}}))
        with patch.dict(os.environ, {"SVE_SMTP_PORT": "2525", "SVE_EMAIL_ENABLED": "false"}):
            self.assertEqual(channels(settings, self.data / "notify.json"), self.server.notify_channels())
            self.assertEqual(2525, self.server.notify_channels()["email"]["smtp_port"])
            self.assertFalse(self.server.notify_channels()["email"]["enabled"])


if __name__ == "__main__":
    unittest.main()
