#!/bin/bash
# Remove only dutybot itself. Does not stop other units or Caddy/Nginx.
# Does not require hermes.service (or any watched unit) to exist.
# Usage: sudo ./uninstall.sh
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "请以 root 运行：sudo ./uninstall.sh" >&2
  exit 1
fi

echo "==> 停止并禁用 dutybot（不影响其它 unit）"
if command -v systemctl >/dev/null 2>&1; then
  systemctl stop dutybot.service 2>/dev/null || true
  systemctl disable dutybot.service 2>/dev/null || true
fi

echo "==> 删除 dutybot 文件与目录"
rm -f /etc/systemd/system/dutybot.service
rm -f /etc/sudoers.d/dutybot
rm -rf /opt/dutybot
rm -rf /etc/dutybot
rm -rf /var/lib/dutybot
rm -rf /usr/lib/dutybot

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload
  systemctl reset-failed dutybot.service 2>/dev/null || true
fi

echo "==> 删除用户与组"
if getent passwd dutybot >/dev/null; then
  userdel dutybot 2>/dev/null || userdel -f dutybot 2>/dev/null || true
fi
if getent group dutybot >/dev/null; then
  groupdel dutybot 2>/dev/null || true
fi

echo "卸载完成。仅移除了 dutybot 自身路径/用户/unit/sudoers。"
echo "未停止、未删除看守名单中的服务，也未改动 Caddy/Nginx/sshd/PAM。"

echo
echo "残留检查（有输出即为残留）："
ls -ld /opt/dutybot /etc/dutybot /var/lib/dutybot /usr/lib/dutybot \
  /etc/sudoers.d/dutybot /etc/systemd/system/dutybot.service 2>/dev/null || true
getent passwd dutybot || true
getent group dutybot || true
systemctl status dutybot --no-pager 2>/dev/null || true
echo "请以本机真实 unit 名核对看守服务仍在（不得被本次卸载误删）。"
