#!/usr/bin/env bash
# Offline code switch only. Never stop/start services, install dependencies, or change units.
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/sve-monitor}"
ACTION="${1:?Usage: upgrade.sh apply|rollback|recover [staged-release]}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "$APP_DIR" in /*) ;; *) echo 'APP_DIR must be absolute'; exit 1;; esac
[ "$(realpath -m -- "$APP_DIR")" != / ] || exit 1
case "$ACTION" in apply|rollback|recover) ;; *) echo 'Invalid action'; exit 1;; esac
for unit in sve-agent.service sve-poller.service sve-watchdog.service sve-watchdog.timer; do
  state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  case "$state" in inactive|failed|unknown) ;; *) echo 'Stop agent, retired poller and watchdog/timer before switching'; exit 1;; esac
done
ARGS=("$ACTION" --target "$APP_DIR" --writers-stopped)
[ -z "${SVE_DATA_DIR:-}" ] || ARGS+=(--data-dir "$SVE_DATA_DIR")
if [ "$ACTION" = apply ]; then ARGS+=(--package "${2:?A staged package is required}"); fi
exec "$APP_DIR/.venv/bin/python" -B "$SCRIPT_DIR/release.py" "${ARGS[@]}"
