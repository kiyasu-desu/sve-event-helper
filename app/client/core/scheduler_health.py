"""Thread-safe progress only: no persistence, I/O, credentials or task actions."""
from __future__ import annotations

from datetime import datetime, timezone
import threading
import time


def utc_stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


class Progress:
    def __init__(self):
        self.lock = threading.Lock()
        self.sequence = 0
        self.last_attempt = None
        self.last_success = None
        self.upstream = {name: {"failures": 0, "started": None} for name in ("detail", "signup")}
        self.phase = "created"
        self.started = time.monotonic()
        self.wall_started = time.time()
        self.deadline = self.started + 60
        self.deadline_wall = self.wall_started + 60
        self.last_progress = self.wall_started

    def enter(self, phase, budget):
        with self.lock:
            self.sequence += 1
            self.phase = phase
            self.started = time.monotonic()
            self.wall_started = time.time()
            self.last_progress = self.wall_started
            self.deadline = self.started + max(0.0, budget)
            self.deadline_wall = self.wall_started + max(0.0, budget)

    def attempted(self):
        with self.lock:
            self.last_attempt = time.time()

    def result(self, success, operation="detail"):
        with self.lock:
            row = self.upstream[operation]
            if success:
                if operation == "detail":
                    self.last_success = time.time()
                row.update(failures=0, started=None)
            else:
                if row["started"] is None:
                    row["started"] = time.monotonic()
                row["failures"] += 1

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            upstream = {name: {"consecutive_upstream_failures": row["failures"],
                               "upstream_failure_seconds": max(0.0, now - row["started"]) if row["started"] is not None else 0.0}
                        for name, row in self.upstream.items()}
            # Keep the version-1 summary fields for existing health consumers.
            worst = max(upstream.values(), key=lambda row: (row["upstream_failure_seconds"], row["consecutive_upstream_failures"]))
            return {"phase": self.phase, "sequence": self.sequence,
                    "phase_started_at": utc_stamp(self.wall_started),
                    "last_progress_at": utc_stamp(self.last_progress),
                    "last_attempt_at": utc_stamp(self.last_attempt),
                    "last_success_at": utc_stamp(self.last_success),
                    "next_progress_at": utc_stamp(self.deadline_wall),
                    "phase_age_seconds": max(0.0, now - self.started),
                    "overdue_seconds": max(0.0, now - self.deadline),
                    "remaining_seconds": max(0.0, self.deadline - now),
                    **worst, "upstream_operations": upstream}
