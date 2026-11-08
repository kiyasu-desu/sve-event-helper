#!/usr/bin/env bash
# SVE 监控 —— 卸载脚本
#
# 默认**保留** capture/ 与 config/（里面有会话凭据与通知配置），
# 只移除服务与虚拟环境。要连数据一起删，加 --purge。
#
# 用法：
#   sudo bash deploy/uninstall.sh
#   sudo APP_DIR=/srv/sve bash deploy/uninstall.sh --purge
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/sve-monitor}"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

say() { printf '\033[36m[uninstall]\033[0m %s\n' "$*"; }
[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行" >&2; exit 1; }

say "停止并禁用服务…"
systemctl disable --now sve-watchdog.timer 2>/dev/null || true
systemctl disable --now sve-poller.service 2>/dev/null || true
rm -f /etc/systemd/system/sve-poller.service \
      /etc/systemd/system/sve-watchdog.service \
      /etc/systemd/system/sve-watchdog.timer
systemctl daemon-reload
say "systemd 单元已移除"

if [ "$PURGE" -eq 1 ]; then
  say "彻底删除 $APP_DIR …"
  rm -rf "$APP_DIR"
  say "已删除（含会话与配置）"
else
  say "保留数据目录: $APP_DIR/capture  $APP_DIR/config"
  say "（如确认要一并删除，重跑时加 --purge）"
fi
