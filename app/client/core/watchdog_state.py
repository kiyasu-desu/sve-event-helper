"""Pure, persistent watchdog decisions. No network, notification or task control."""
from __future__ import annotations

import hashlib
import math


DEFAULTS = {"confirm_checks": 2, "min_check_seconds": 30, "grace_seconds": 30,
            "upstream_failures": 3, "upstream_seconds": 180,
            "retry_seconds": 60, "max_retry_seconds": 3600}
KINDS = {"worker_dead", "stalled", "upstream_unavailable", "orphan", "identity_blocked",
         "agent_unreachable", "protocol_unsupported", "protocol_invalid", "storage_unavailable", "not_executor"}
TASK_STATES = {"active", "completed", "paused", "cancelled", "orphan", "worker_dead", "identity_blocked"}


def options(config):
    result = dict(DEFAULTS)
    for name, default in DEFAULTS.items():
        value = config.get(name, default)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("Invalid watchdog option")
        result[name] = value
    if type(result["confirm_checks"]) is not int or type(result["upstream_failures"]) is not int:
        raise ValueError("Watchdog counts must be integers")
    if result["confirm_checks"] < 2 or result["upstream_failures"] < 1 or result["min_check_seconds"] < 1 or result["retry_seconds"] < 1:
        raise ValueError("Watchdog confirmation/retry too short")
    if result["max_retry_seconds"] < result["retry_seconds"]:
        raise ValueError("Invalid watchdog backoff")
    return result


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid progress duration")
    return value


def faults(snapshot, config):
    """Validate the entire protocol before deriving any task recoveries."""
    if not isinstance(snapshot, dict) or snapshot.get("health_version") != 1:
        raise ValueError("Unsupported progress protocol")
    if snapshot.get("ok") is not True:
        return {"agent:storage": {"kind": "storage_unavailable"}}
    if snapshot.get("mode") != "executor":
        return {"agent:mode": {"kind": "not_executor"}}
    tasks = snapshot.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("Invalid task list")
    result, seen = {}, set()
    for task in tasks:
        aid, tid, status = task.get("account_id"), task.get("tournament_id"), task.get("status")
        if (not isinstance(aid, str) or not aid.isascii() or not aid.isdecimal() or int(aid) < 1
                or not isinstance(tid, str) or not tid or len(tid) > 200 or status not in TASK_STATES
                or type(task.get("thread_alive")) is not bool):
            raise ValueError("Invalid task identity/state")
        identity = aid + "|" + tid
        if identity in seen:
            raise ValueError("Duplicate task")
        seen.add(identity)
        kind = None
        if status in {"completed", "paused", "cancelled"}:
            continue
        if status in {"worker_dead", "orphan", "identity_blocked"}:
            kind = status
        elif not task["thread_alive"]:
            kind = "worker_dead"
        else:
            progress = task.get("progress")
            if not isinstance(progress, dict):
                raise ValueError("Missing progress")
            overdue = number(progress.get("overdue_seconds"))
            failures = number(progress.get("consecutive_upstream_failures"))
            duration = number(progress.get("upstream_failure_seconds"))
            upstream = [(failures, duration)]
            if "upstream_operations" in progress:
                operations = progress["upstream_operations"]
                if not isinstance(operations, dict) or set(operations) != {"detail", "signup"}:
                    raise ValueError("Invalid upstream operations")
                upstream = []
                for row in operations.values():
                    if not isinstance(row, dict):
                        raise ValueError("Invalid upstream observation")
                    upstream.append((number(row.get("consecutive_upstream_failures")),
                                     number(row.get("upstream_failure_seconds"))))
            if overdue > config["grace_seconds"]:
                kind = "stalled"
            elif any(count >= config["upstream_failures"] and age >= config["upstream_seconds"] for count, age in upstream):
                kind = "upstream_unavailable"
        if kind:
            key = hashlib.sha256(identity.encode()).hexdigest() + ":" + kind
            result[key] = {"kind": kind, "account_id": aid, "tournament_id": tid}
    return result


class Monitor:
    def __init__(self, value=None, config=None):
        self.config = options(config or {})
        self.value = value if value is not None else {"version": 1, "boot": None, "last_check": None, "faults": {}}
        if (not isinstance(self.value, dict) or self.value.get("version") != 1
                or not isinstance(self.value.get("faults"), dict)):
            raise ValueError("Invalid watchdog state; preserve for offline inspection")
        for row in self.value["faults"].values():
            if not isinstance(row, dict) or type(row.get("announced")) is not bool or row.get("kind") not in KINDS:
                raise ValueError("Invalid watchdog incident")
            for field in ("count", "healthy", "attempts", "next_try"):
                number(row.get(field))

    def observe(self, snapshot, *, now, boot, error=None):
        number(now)
        preserve_tasks = error is not None
        if error:
            current = {"agent:" + error: {"kind": error}}
        else:
            current = faults(snapshot, self.config)
            preserve_tasks = any(key.startswith("agent:") for key in current)
        previous = self.value.get("last_check")
        reboot = self.value.get("boot") != boot or (previous is not None and now < previous)
        if reboot:
            previous = None
            for row in self.value["faults"].values():
                row.update(count=0, healthy=0, next_try=now + self.config["retry_seconds"])
        if previous is not None and now - previous < self.config["min_check_seconds"]:
            return []  # Repeated invocations cannot fast-forward confirmation.
        self.value.update(last_check=now, boot=boot)
        incidents = self.value["faults"]
        for key, detail in current.items():
            if key not in incidents:
                incidents[key] = dict(detail, count=0, healthy=0, announced=False, attempts=0, next_try=0)
            row = incidents[key]
            row["count"] += 1
            row["healthy"] = 0
        events = []
        for key, row in list(incidents.items()):
            if key not in current:
                if preserve_tasks and not key.startswith("agent:"):
                    row.update(count=0, healthy=0)
                    continue
                if not row["announced"]:
                    del incidents[key]
                    continue
                row["healthy"] += 1
                row["count"] = 0
            recovered = row["healthy"] >= self.config["confirm_checks"]
            alert = row["count"] >= self.config["confirm_checks"] and not row["announced"]
            if (alert or recovered) and now >= row["next_try"]:
                row["attempts"] += 1
                delay = min(self.config["max_retry_seconds"], self.config["retry_seconds"] * 2 ** min(row["attempts"] - 1, 16))
                row["next_try"] = now + delay
                events.append({"id": key, "recovered": recovered,
                               **{field: row[field] for field in ("kind", "account_id", "tournament_id") if field in row}})
        return events

    def accepted(self, events):
        for event in events:
            row = self.value["faults"].get(event["id"])
            if row is None:
                continue
            if event["recovered"]:
                del self.value["faults"][event["id"]]
            else:
                row.update(announced=True, attempts=0, next_try=0)
