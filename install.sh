#!/bin/bash
# Install dutybot on this Guest. Root only.
# Usage (from the repository root): sudo ./install.sh
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "请以 root 运行：sudo ./install.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "需要 Python 3.10+" >&2
  exit 1
fi
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"需要 Python 3.10+，当前 {sys.version}")
print("Python", sys.version.split()[0])
PY

if ! command -v systemctl >/dev/null 2>&1; then
  echo "需要 systemd" >&2
  exit 1
fi

OPT=/opt/dutybot
ETC=/etc/dutybot
VAR=/var/lib/dutybot
LIB=/usr/lib/dutybot
ENV_FILE="${ETC}/env"
WATCH_FILE="${VAR}/watch.json"
UNIT_DST=/etc/systemd/system/dutybot.service
SUDOERS_DST=/etc/sudoers.d/dutybot

echo "==> 创建用户与目录"
if ! getent group dutybot >/dev/null; then
  groupadd --system dutybot
fi
if ! getent passwd dutybot >/dev/null; then
  NOLOGIN=/usr/sbin/nologin
  [[ -x /usr/sbin/nologin ]] || NOLOGIN=/usr/bin/nologin
  [[ -x ${NOLOGIN} ]] || NOLOGIN=/bin/false
  useradd --system --gid dutybot --home-dir "${OPT}" --shell "${NOLOGIN}" \
    --create-home --comment "dutybot" dutybot || true
fi
if getent group systemd-journal >/dev/null; then
  usermod -aG systemd-journal dutybot || true
fi
if getent group adm >/dev/null; then
  usermod -aG adm dutybot || true
fi

install -d -m 0755 -o root -g dutybot "${OPT}"
install -d -m 0750 -o root -g dutybot "${ETC}"
install -d -m 0750 -o dutybot -g dutybot "${VAR}"
install -d -m 0750 -o dutybot -g dutybot "${VAR}/web-sessions"
install -d -m 0755 -o root -g root "${LIB}"

echo "==> 复制应用代码"
rm -rf "${OPT}/dutybot"
cp -a "${SCRIPT_DIR}/src/dutybot" "${OPT}/dutybot"
chown -R root:dutybot "${OPT}/dutybot"
find "${OPT}/dutybot" -type d -exec chmod 0755 {} \;
find "${OPT}/dutybot" -type f -exec chmod 0644 {} \;

have_venv() {
  python3 -c 'import venv, ensurepip' >/dev/null 2>&1
}

ensure_python_venv() {
  if have_venv; then
    return 0
  fi
  PYVER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  echo "python${PYVER} 缺少 venv/ensurepip（Debian/Ubuntu 上常见，包名 python${PYVER}-venv）。"
  if command -v apt-get >/dev/null 2>&1; then
    echo "尝试安装 python${PYVER}-venv ..."
    export DEBIAN_FRONTEND=noninteractive
    if ! apt-get install -y "python${PYVER}-venv" 2>/dev/null; then
      apt-get update -qq
      apt-get install -y "python${PYVER}-venv" || apt-get install -y python3-venv || true
    fi
  fi
  if have_venv; then
    echo "venv 模块已可用。"
    return 0
  fi
  echo "无法创建虚拟环境：当前 python3 没有 venv。" >&2
  echo "Debian/Ubuntu/Mint 请先执行：apt-get install -y python${PYVER}-venv" >&2
  echo "不要用 --without-pip 凑合，也不要降级 Python。" >&2
  exit 1
}

echo "==> 虚拟环境与依赖"
ensure_python_venv
if [[ ! -x ${OPT}/venv/bin/python ]]; then
  python3 -m venv "${OPT}/venv"
fi
"${OPT}/venv/bin/pip" install --upgrade pip
"${OPT}/venv/bin/pip" install -r "${SCRIPT_DIR}/requirements.txt"
chown -R root:dutybot "${OPT}/venv"

echo "==> 安装 dutyctl 与 sudoers"
install -m 0755 -o root -g root "${SCRIPT_DIR}/helper/dutyctl" "${LIB}/dutyctl"
TMP_SUDOERS="$(mktemp)"
cp "${SCRIPT_DIR}/sudoers/dutybot" "${TMP_SUDOERS}"
chmod 0440 "${TMP_SUDOERS}"
if ! visudo -c -f "${TMP_SUDOERS}"; then
  rm -f "${TMP_SUDOERS}"
  echo "sudoers 校验失败" >&2
  exit 1
fi
install -m 0440 -o root -g root "${TMP_SUDOERS}" "${SUDOERS_DST}"
rm -f "${TMP_SUDOERS}"
if ! visudo -c -f "${SUDOERS_DST}"; then
  echo "安装后 sudoers 校验失败，已中止" >&2
  rm -f "${SUDOERS_DST}"
  exit 1
fi

echo "==> systemd unit"
install -m 0644 -o root -g root "${SCRIPT_DIR}/systemd/dutybot.service" "${UNIT_DST}"

env_get() {
  local key="$1"
  if [[ -f ${ENV_FILE} ]]; then
    python3 - "${ENV_FILE}" "${key}" <<'PY'
import sys
path, key = sys.argv[1], sys.argv[2]
try:
    text = open(path, encoding="utf-8").read().splitlines()
except OSError:
    raise SystemExit(0)
for line in text:
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    if k.strip() == key:
        print(v.strip())
        break
PY
  fi
}

write_env_key() {
  local key="$1" value="$2"
  python3 - "${ENV_FILE}" "${key}" "${value}" <<'PY'
import os, sys
path, key, value = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(os.path.dirname(path), exist_ok=True)
lines = []
if os.path.isfile(path):
    lines = open(path, encoding="utf-8").read().splitlines()
found = False
out = []
for line in lines:
    raw = line.strip()
    if raw and not raw.startswith("#") and raw.split("=", 1)[0] == key:
        out.append(f"{key}={value}")
        found = True
    else:
        out.append(line)
if not found:
    out.append(f"{key}={value}")
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out).rstrip() + "\n")
os.replace(tmp, path)
PY
}

echo "==> 通道配置"
token="$(env_get BOT_TOKEN)"
chat="$(env_get ALLOWED_CHAT_ID)"
web_user="$(env_get WEB_USER)"
web_hash="$(env_get WEB_PASSWORD_HASH)"
token="${BOT_TOKEN:-${token}}"
chat="${ALLOWED_CHAT_ID:-${chat}}"
web_user="${WEB_USER:-${web_user}}"
web_hash="${WEB_PASSWORD_HASH:-${web_hash}}"

tg_ok=0
web_ok=0
[[ -n ${token} && -n ${chat} ]] && tg_ok=1
[[ -n ${web_user} && -n ${web_hash} ]] && web_ok=1

if [[ ${tg_ok} -eq 1 || ${web_ok} -eq 1 ]]; then
  echo "已检测到完整通道：Telegram=${tg_ok} Web=${web_ok}"
else
  echo "至少需要一条完整通道（Telegram 的 Token+Chat ID，或 Web 的用户+口令）。"
  read -r -p "配置 Telegram？[y/N] " ans || true
  if [[ ${ans:-} =~ ^[yY] ]]; then
    read -r -p "BOT_TOKEN: " token
    read -r -p "ALLOWED_CHAT_ID: " chat
  fi
  read -r -p "配置 Web 管理页？[y/N] " ans || true
  if [[ ${ans:-} =~ ^[yY] ]]; then
    read -r -p "WEB_USER: " web_user
    read -r -s -p "Web 口令: " pw1; echo
    read -r -s -p "再输入一次: " pw2; echo
    if [[ -z ${pw1:-} || ${pw1} != "${pw2:-}" ]]; then
      echo "口令为空或两次不一致" >&2
      exit 1
    fi
    web_hash="$(
      printf '%s' "${pw1}" | PYTHONPATH="${OPT}" "${OPT}/venv/bin/python" -c \
        'import sys; from dutybot.config import hash_password; print(hash_password(sys.stdin.read()))'
    )"
    unset pw1 pw2
  fi
fi

[[ -n ${token:-} && -n ${chat:-} ]] && tg_ok=1 || tg_ok=0
[[ -n ${web_user:-} && -n ${web_hash:-} ]] && web_ok=1 || web_ok=0
if [[ ${tg_ok} -eq 0 && ${web_ok} -eq 0 ]]; then
  echo "安装未成功：未提供任何完整通道。不会编造 Token 或口令。" >&2
  exit 1
fi

umask 077
touch "${ENV_FILE}"
if [[ ${tg_ok} -eq 1 ]]; then
  write_env_key BOT_TOKEN "${token}"
  write_env_key ALLOWED_CHAT_ID "${chat}"
fi
if [[ ${web_ok} -eq 1 ]]; then
  write_env_key WEB_USER "${web_user}"
  write_env_key WEB_PASSWORD_HASH "${web_hash}"
  bind="${WEB_BIND:-127.0.0.1}"
  if [[ ${bind} == "0.0.0.0" || ${bind} == "::" || ${bind} == "*" ]]; then
    echo "WEB_BIND=${bind} 不允许，已改为 127.0.0.1" >&2
    bind="127.0.0.1"
  fi
  write_env_key WEB_BIND "${bind}"
  write_env_key WEB_PORT "${WEB_PORT:-8787}"
fi
if ! grep -q '^WEB_BIND=' "${ENV_FILE}"; then
  write_env_key WEB_BIND "127.0.0.1"
fi
if ! grep -q '^WEB_PORT=' "${ENV_FILE}"; then
  write_env_key WEB_PORT "8787"
fi
chown root:dutybot "${ENV_FILE}"
chmod 640 "${ENV_FILE}"

if [[ ! -f ${WATCH_FILE} ]]; then
  printf '%s\n' '{"services":[]}' > "${WATCH_FILE}"
fi
chown dutybot:dutybot "${WATCH_FILE}"
chmod 640 "${WATCH_FILE}"

echo "==> 启用并启动 dutybot.service"
systemctl daemon-reload
systemctl enable --now dutybot.service
sleep 1
if systemctl is-active --quiet dutybot; then
  echo "安装完成。dutybot 为 active。"
else
  echo "服务未能保持 active，请检查：journalctl -u dutybot -n 50 --no-pager" >&2
  systemctl --no-pager --full status dutybot || true
  exit 1
fi
echo "卸载只移除 dutybot 自身，不会停止看守名单中的服务，也不会删除 Caddy/Nginx。"
