#!/usr/bin/env bash
# Default: read-only local manifest/config/data inspection; no upstream or notification.
# --validate-upstream --account-id ID: one official profile GET; no cache/data writes.
# --test-notify: explicitly sends configured notifications; never implicit in checks.
set -euo pipefail
APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PY="${SVE_PYTHON:-$APP_DIR/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
export PYTHONDONTWRITEBYTECODE=1
ARGS=(--root "$APP_DIR")
[ -z "${SVE_DATA_DIR:-}" ] || ARGS+=(--data-dir "$SVE_DATA_DIR")
"$PY" -B "$APP_DIR/deploy/diagnostics.py" "${ARGS[@]}" "$@"
# Merely read service states. Static staging checks can use diagnostics.py directly.
if command -v systemctl >/dev/null 2>&1; then
  A="$(systemctl is-active sve-agent.service 2>/dev/null || true)"
  P="$(systemctl is-active sve-poller.service 2>/dev/null || true)"
  [ "$A" = active ] && [ "$P" != active ] || { echo 'Expected agent active and retired poller inactive'; exit 1; }
  echo 'Single systemd executor: agent. Scheduler progress is not assessed by this check.'
fi
