#!/usr/bin/env bash
# First installation only. Existing installations must use upgrade.sh.
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/sve-monitor}"
SVE_USER="${SVE_USER:-${SUDO_USER:-$(id -un)}}"
PACKAGE="${1:?Usage: install.sh /absolute/staged-release}"
[ "$(id -u)" -eq 0 ] || { echo 'Run initial installation as root'; exit 1; }
case "$APP_DIR" in /*) ;; *) echo 'APP_DIR must be absolute'; exit 1;; esac
APP_DIR="$(realpath -m -- "$APP_DIR")"
[ "$APP_DIR" != / ] || exit 1
case "$APP_DIR" in *[!a-zA-Z0-9_/.-]*) echo 'Unsupported unit path'; exit 1;; esac
id "$SVE_USER" >/dev/null
[ ! -e "$APP_DIR/app/client/server.py" ] || { echo 'Existing code detected; use upgrade.sh'; exit 1; }
for unit in sve-agent.service sve-poller.service sve-watchdog.service sve-watchdog.timer; do
  state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  case "$state" in inactive|failed|unknown) ;; *) echo 'Stop all SVE services before installation'; exit 1;; esac
done
for unit in sve-agent.service sve-watchdog.service sve-watchdog.timer; do
  [ ! -e "/etc/systemd/system/$unit" ] || { echo 'Existing units require the documented manual upgrade procedure'; exit 1; }
done
for legacy in config/settings.json app/client/data/accounts.json app/client/data/identity-v2.json capture/session.json .deployment-backups; do
  [ ! -e "$APP_DIR/$legacy" ] || { echo 'Legacy runtime files detected; review offline migration instead of initial installation'; exit 1; }
done
python3 -B "$PACKAGE/deploy/release.py" verify --package "$PACKAGE"
# Bootstrap installs dependencies explicitly; upgrades never run apt/pip or delete venvs.
apt-get update -qq
apt-get install -y -qq python3 python3-venv ca-certificates
# Provision /etc/sve/secrets.env and signing configuration privately first.
# This script does not parse/source an EnvironmentFile or put secrets in argv.
[ "${SVE_DATA_DIR:-}" = /var/lib/sve ] || { echo 'Set SVE_DATA_DIR=/var/lib/sve in this installation environment'; exit 1; }
[ -f /etc/sve/secrets.env ] || { echo 'Provision protected /etc/sve/secrets.env first'; exit 1; }
[ "$(stat -c %a /etc/sve/secrets.env)" = 600 ] || { echo 'EnvironmentFile must be mode 600'; exit 1; }
[ "$(stat -c %u /etc/sve/secrets.env)" = 0 ] || { echo 'EnvironmentFile must be root owned'; exit 1; }
[ ! -L "$SVE_DATA_DIR" ] || { echo 'Symlink runtime directory refused'; exit 1; }
[ ! -e "$SVE_DATA_DIR/identity-v2.json" ] || { echo 'Existing data detected; use upgrade/migration procedure'; exit 1; }
install -d -m 700 -o "$SVE_USER" -g "$SVE_USER" "$SVE_DATA_DIR"
[ -x "$APP_DIR/.venv/bin/python" ] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -B -m pip install -r "$PACKAGE/deploy/requirements.txt"
# Signing configuration is read from private environment or external settings.
"$APP_DIR/.venv/bin/python" -B "$PACKAGE/deploy/release.py" apply --package "$PACKAGE" --target "$APP_DIR" --data-dir "$SVE_DATA_DIR" --writers-stopped
chown "$SVE_USER:$SVE_USER" "$SVE_DATA_DIR/.identity-writer.lock"
# The root-run release creates private backups; hand them to the service user.
chown -R "$SVE_USER:$SVE_USER" "$SVE_DATA_DIR/deployment-backups"
# Preserve preexisting units/drop-ins. Install templates only for a fresh service.
for unit in sve-agent.service sve-watchdog.service sve-watchdog.timer; do
  destination="/etc/systemd/system/$unit"
  [ ! -e "$destination" ] || { echo 'Existing unit detected; review it before activation'; exit 1; }
  python3 -B - "$PACKAGE/deploy/systemd/$unit" "$destination" "$APP_DIR" "$SVE_USER" <<'PY'
import pathlib,re,sys
source,destination,root,user=sys.argv[1:]
if not re.fullmatch(r'/[A-Za-z0-9_./-]+',root) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*',user):
 raise SystemExit('Unsupported unit substitution; review paths/user')
pathlib.Path(destination).write_bytes(pathlib.Path(source).read_text().replace('__APP_DIR__',root).replace('__SVE_USER__',user).encode('utf-8'))
PY
done
systemctl daemon-reload
echo 'Installed, services remain stopped. Run read-only checks and the documented mobile acceptance before release completion.'
echo 'Enable only sve-agent and sve-watchdog.timer after reviewing existing network/authentication configuration.'
