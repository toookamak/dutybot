# dutybot

单人 Telegram 值班 Bot。部署于任意带 systemd 的 Linux Guest，通过菜单远程查看主机状态、重启指定服务、清理僵尸与孤儿进程、重启本机；服务异常或主机恢复后主动通知。另提供需登录的 Web 页面，用于配置与查询日志。

Telegram 显示名：**值班**。仅允许一个 Telegram 账号（白名单）。Web 页面同样仅允许一个本地登录账号。

部署、配置与验收说明见 [AGENTS.md](./AGENTS.md)，供 Hermes 等部署代理使用。

## 功能

### 菜单

| 入口 | 行为 |
| --- | --- |
| 设备状态 | 主机名、uptime、CPU、温度、负载、内存/交换、根分区、网卡 IP；各看守服务是否 active；已配置端口则探测连通性。温度不可用时显示「不可用」，不使用估计值。 |
| CPU 前五 | pid、CPU 占用、内存、命令行。 |
| 读写前五 | 当前读/写速率最高的五个进程。 |
| 服务 | 看守名单中每个 unit 提供同一套操作：状态、最近日志、重启（二次确认）。状态包含失败原因（Result、退出码、NRestarts）。 |
| 清理进程 | 先预览僵尸进程与看守服务留下的孤儿 worker，确认后再终止。僵尸无法终止时予以说明，并提示查找父进程。不按 CPU 占用选择目标。 |
| 重启系统 | 两次确认后，约 1 分钟重启本 Guest。启动完成后推送「已恢复」及状态卡。 |
| 添加 / 删除服务 | 依次提供 unit 名、显示名、可选 `host:port`。删除仅从看守名单移除，不停止对应 unit。 |

### 主动通知

- 首次上线，或主机重启后 Bot 启动：推送「已恢复」及状态卡。
- 看守服务停止、自行恢复、意外重启。菜单内发起的重启不重复广播为意外重启。
- 假活：unit 为 active，但已配置的探测端口不可达。
- SSH 成功登录：每次推送（用户、来源 IP、时间），无冷却。
- SSH 失败登录：推送，同一来源 IP 有冷却，避免扫描造成刷屏。
- CPU：排除看守服务后仍偏高，判定为意外负载；整机长时间饱和则另行通知。
- 磁盘读写、网卡流量：持续超过阈值后通知，并可在恢复后通知。
- 根分区剩余空间过低：立即通知。
- 同类告警设有冷却。
- Bot 无响应时由 systemd `WatchdogSec` 拉起，仍按「已恢复」通知。

### Web 管理页

同一 `dutybot` 进程内提供 HTTP 服务，不另建网站目录、不另装控制面板。

- 登录：单一本地账号（用户名与口令哈希写入 `/etc/dutybot/env`）。未登录不可访问配置与日志。
- 配置：编辑看守名单（增删改 unit、显示名、探测地址）、告警阈值与冷却。不在页面上展示或修改 `BOT_TOKEN`。删除名单条目仍不得停止对应 unit。
- 日志：查询 `dutybot` 自身 journal，以及看守名单中各 unit 的 journal。可按服务、时间范围、优先级过滤。不得提供任意 journalctl 表达式或 shell。
- 重启服务、清理进程、重启系统仍仅通过 Telegram 菜单（含确认），Web 页不提供此类操作。
- 默认监听 `127.0.0.1`，端口由配置指定。如需外网访问，由本机已有的 Caddy / Nginx 反向代理，dutybot 不直接对 `0.0.0.0` 暴露。

### 使用边界

- 仅部署于 Guest Linux，不操作 PVE 宿主机。
- 不提供命令终端，不执行任意 shell。
- Token 仅保存在本机环境文件中，不纳入版本库。
- 无用进程限定为僵尸进程，以及看守服务留下的孤儿 worker。

## 运行环境

- systemd
- Python 3.10 或更高版本
- 发行版不限定为 Linux Mint

看守名单为配置，而非写死的服务名。每条记录包含：`id`、显示名、`*.service`、可选探测地址 `host:port`。默认示例为 Hermes、Pi Agent、DSH，亦可加入 Caddy、Nginx、Docker 等，操作方式相同。

## 当前进度

本仓库目前仅包含文档。`install.sh`、Bot 与 helper 尚未提交。安装脚本就绪前，请勿编写替代安装流程。

## 计划文件结构

安装仅创建下列对象。卸载须全部移除，并核对无残留。**不得**删除 Hermes、Pi Agent、DSH 或其他看守服务的 unit。

### 仓库（源码，尚未全部提交）

```
.
├── README.md
├── AGENTS.md
├── install.sh
├── uninstall.sh
├── requirements.txt
├── systemd/
│   └── dutybot.service
├── sudoers/
│   └── dutybot
├── helper/
│   └── dutyctl
└── src/dutybot/
    ├── __init__.py
    ├── __main__.py
    ├── bot.py
    ├── config.py
    ├── status.py
    ├── services.py
    ├── procs.py
    ├── monitor.py
    ├── notify.py
    ├── web.py
    └── web/
        ├── templates/
        └── static/
```

### Guest（安装后的落地路径，卸载对照此表）

| 路径 | 类型 | 用途 |
| --- | --- | --- |
| `/opt/dutybot/` | 目录 | 应用代码与 venv，用户 `dutybot` 的 home |
| `/opt/dutybot/venv/` | 目录 | Python 虚拟环境 |
| `/etc/dutybot/` | 目录 | 环境配置目录 |
| `/etc/dutybot/env` | 文件 | `BOT_TOKEN`、`ALLOWED_CHAT_ID`、Web 登录账号与口令哈希、`WEB_BIND`、`WEB_PORT`；权限 `640`，属主 `root:dutybot` |
| `/var/lib/dutybot/` | 目录 | 可变数据 |
| `/var/lib/dutybot/watch.json` | 文件 | 看守名单，属主 `dutybot:dutybot` |
| `/var/lib/dutybot/web-sessions/` | 目录 | Web 登录会话（若落盘）；属主 `dutybot:dutybot` |
| `/usr/lib/dutybot/` | 目录 | 特权 helper 目录 |
| `/usr/lib/dutybot/dutyctl` | 文件 | 唯一特权 helper：`restart-unit` / `kill-pids` / `reboot` |
| `/etc/sudoers.d/dutybot` | 文件 | 仅放行 `dutyctl`，不得授予 `NOPASSWD ALL` |
| `/etc/systemd/system/dutybot.service` | 文件 | systemd unit（同时运行 Telegram Bot 与 Web） |
| 用户 `dutybot` | 系统用户 | 运行 Bot 的非 root 用户，nologin |
| 组 `dutybot` | 系统组 | 与用户同名 |

不创建独立网站根目录，不修改 sshd 或 PAM，不额外注册 timer、cron 或第二个 systemd unit。日志写入 journal（`journalctl -u dutybot`），不单独落盘。反向代理配置若由本机已有 Caddy / Nginx 提供，不属于 dutybot 安装产物，卸载时不得删除这些软件的 unit。

### 卸载后残留检查

`uninstall.sh` 执行完毕后，下列各项均应不存在。任一仍存在即视为残留，须修复至清除。不得以扩大删除范围的方式清理。

```bash
# 文件和目录：有输出即为残留
ls -ld /opt/dutybot /etc/dutybot /var/lib/dutybot /usr/lib/dutybot \
  /etc/sudoers.d/dutybot /etc/systemd/system/dutybot.service 2>/dev/null

# 用户和组：能查询到即为残留
getent passwd dutybot
getent group dutybot

# 服务：仍为 loaded/active 即为残留
systemctl status dutybot --no-pager
systemctl is-enabled dutybot 2>/dev/null

# 本机监听：dutybot 占用的 Web 端口不应再存在
ss -lntp | grep dutybot || true

# 不得改动的对象（必须仍存在；缺失即为误删）
systemctl cat hermes.service pi-agent.service dsh.service 2>/dev/null | head
```

请以本机真实 unit 名替换上述 `hermes.service` 等示例。journal 中 `dutybot` 的历史记录可保留，不作为必须清除的残留；请勿为此对整机执行 `journalctl --vacuum`。
