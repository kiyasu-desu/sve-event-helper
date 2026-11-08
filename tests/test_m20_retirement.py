"""M20: inert old entry points and maintained scheduler regressions; synthetic only."""
from datetime import datetime
import builtins
import importlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app/client/core"))
sys.path.insert(0, str(ROOT))
from scheduler import Scheduler, SignupWindow


class RetirementTests(unittest.TestCase):
    def test_old_python_entrypoints_exit_before_any_side_effect_even_when_imported(self):
        for name in ("poll", "action", "autosignup", "session_manager", "probe_action"):
            folder = "scripts/probe" if name == "probe_action" else "scripts/poller"
            path = ROOT / folder / (name + ".py")
            code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
            for mode, args in (("__main__", []), ("__main__", ["--once"]),
                               ("__main__", ["--signup"]), ("__main__", ["--help"]),
                               ("synthetic_import", [])):
                with self.subTest(name=name, mode=mode, args=args):
                    with patch.object(builtins, "open", side_effect=AssertionError("read")), \
                         patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
                         patch.object(subprocess, "Popen", side_effect=AssertionError("process")), \
                         patch.object(os, "mkdir", side_effect=AssertionError("mkdir")), \
                         patch.object(sys, "argv", [str(path), *args]):
                        with self.assertRaises(SystemExit) as result:
                            exec(code, {"__name__": mode, "__file__": str(path)})
                    self.assertIn("retired", str(result.exception))

    @unittest.skipUnless(os.name == "nt", "Windows controls")
    def test_old_windows_controls_reject_all_operations(self):
        for name in ("poller_ctl", "watchdog", "watchdog_ctl", "test_window"):
            for flag in ([], ["-Start"], ["-Stop"], ["-Status"]):
                result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                         str(ROOT / "scripts/poller" / (name + ".ps1")), *flag],
                                        capture_output=True, timeout=15)
                self.assertEqual(2, result.returncode, (name, flag))

    def test_compatibility_aliases_share_runtime_module_and_lock(self):
        from scripts.runtime import watchdog
        from scripts.poller import watchdog as old
        from scripts.poller import notify
        import notification_sender
        self.assertIs(watchdog, old)
        self.assertIs(notify, notification_sender)
        self.assertNotIn("poll", sys.modules)
        with tempfile.TemporaryDirectory(prefix="sve-m20-") as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text('{"channels": {}}', encoding="utf-8")
            from identity_store import WriterLock
            with patch.object(watchdog, "SETTINGS", str(settings)), \
                 patch.object(watchdog, "DATA_DIR", directory), \
                 patch.object(watchdog, "WATCHDOG_LOG", str(Path(directory) / "log")):
                with WriterLock(Path(directory) / "watchdog"):
                    with self.assertRaises(Exception):
                        old.run_agent_check(fetch=Mock(side_effect=AssertionError("fetch")))
                settings.write_text("invalid-synthetic-json", encoding="utf-8")
                with self.assertRaises(ValueError):
                    watchdog.run_agent_check(fetch=Mock(side_effect=AssertionError("fetch")))

    def test_agent_watchdog_requires_explicit_legacy_mode_before_loading_runtime(self):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/poller/watchdog.py"), "--once"],
                                capture_output=True, timeout=15)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(b"retired", result.stderr)

    def test_daily_windows_include_edges_and_midnight(self):
        for start, end, times in (
            ("16:00", "23:59", (("00:01", False), ("15:59", False), ("16:00", True), ("23:59", True))),
            ("23:00", "02:00", (("22:59", False), ("23:00", True), ("00:30", True), ("02:00", True), ("02:01", False))),
        ):
            win = SignupWindow(kind="daily", daily_start=start, daily_end=end)
            for stamp, expected in times:
                self.assertEqual(expected, win.is_open(datetime.fromisoformat("2026-01-01T" + stamp)))

    def test_state_lag_waits_for_next_sample_and_submits_only_when_signable(self):
        client = Mock()
        client.detail.side_effect = [{"zh_user_status": status, "player_num": pn, "max_player_number": 8}
                                     for status, pn in (("已报满", 7), ("已报满", 8), ("可报名", 7),
                                                        ("已报名", 8), ("签到中", 8))]
        events = Mock()
        sched = Scheduler(client, "synthetic-event", second_confirm_delay=0, on_event=events)
        with patch("scheduler.time.sleep"):
            self.assertFalse(sched.try_once().attempted)
            self.assertFalse(sched.try_once().attempted)
            client.signup.assert_not_called()
            self.assertTrue(sched.try_once().ok)
        client.signup.assert_called_once()
        self.assertEqual(1, sum(call.args[0] == "completed" for call in events.call_args_list))

    def test_two_confirmations_retry_network_and_reject_lost_seat(self):
        for status, expected in (("签到中", True), ("可报名", False)):
            clock = [0.0]
            client = Mock()
            client.detail.side_effect = [{"zh_user_status": "可报名"}, OSError("synthetic-network"),
                                         {"zh_user_status": "已报名"}, {"zh_user_status": status}]
            events = Mock()
            sched = Scheduler(client, "synthetic-event", second_confirm_delay=60, on_event=events)
            def wait(delay):
                clock[0] += delay
            with patch("scheduler.time.monotonic", side_effect=lambda: clock[0]), \
                 patch("scheduler.time.sleep", side_effect=wait):
                self.assertEqual(expected, sched.try_once().ok)
            client.signup.assert_called_once()
            self.assertEqual(int(expected), sum(call.args[0] == "completed" for call in events.call_args_list))
            self.assertGreaterEqual(clock[0], 60)


if __name__ == "__main__":
    unittest.main()
