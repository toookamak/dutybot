# dutybot

单人 Telegram 值班 Bot。装在任意带 systemd 的 Linux Guest 上，用菜单远程看机器、重启指定服务、清僵尸/孤儿进程、重启这台机器；服务挂了或机器起来会主动通知。

Telegram 显示名：**值班**。

## 这是什么

给你自己用的值班遥控器，不是运维平台。

- 一个 Bot、一个 Telegram 账号白名单
- 看守名单是配置，可在 Telegram 里添加 / 删除（删除只从名单拿掉，不停那个 unit）
- 只管 Guest Linux，不动 PVE 宿主机
- 不能执行任意 shell，没有网页控制台
- 无用进程 = 僵尸 + 看守服务留下的孤儿 worker，不是随便杀高占用

## 环境

- systemd
- Python 3.10+
- 发行版不绑 Mint

## 文档

- 人看本 README
- 部署代理（Hermes）看 [AGENTS.md](./AGENTS.md)：安装、配置看守名单、SSH 登录通知、验收、卸载

## 状态

当前仓库先放文档。`install.sh`、Bot 和 helper 尚未提交。没有安装脚本时，按 `AGENTS.md` 停下来，不要手写一套安装。

## 计划文件结构

安装只会创建下面这些东西。卸载必须把它们全部撤掉，并核对没有残留。**不准**删除 Hermes / Pi Agent / DSH 或其他看守服务的 unit。

### 仓库里（源码，尚未全部提交）

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
    └── notify.py
```

### Guest 上（安装后的落地路径，卸载对这份清单）

| 路径 | 类型 | 用途 |
| --- | --- | --- |
| `/opt/dutybot/` | 目录 | 应用代码与 venv，用户 `dutybot` 的 home |
| `/opt/dutybot/venv/` | 目录 | Python 虚拟环境 |
| `/etc/dutybot/` | 目录 | 环境配置目录 |
| `/etc/dutybot/env` | 文件 | `BOT_TOKEN`、`ALLOWED_CHAT_ID`，权限 `640`，属主 `root:dutybot` |
| `/var/lib/dutybot/` | 目录 | 可变数据 |
| `/var/lib/dutybot/watch.json` | 文件 | 看守名单，属主 `dutybot:dutybot` |
| `/usr/lib/dutybot/` | 目录 | 特权 helper 目录 |
| `/usr/lib/dutybot/dutyctl` | 文件 | 唯一特权 helper：`restart-unit` / `kill-pids` / `reboot` |
| `/etc/sudoers.d/dutybot` | 文件 | 只放行 `dutyctl`，不要 `NOPASSWD ALL` |
| `/etc/systemd/system/dutybot.service` | 文件 | systemd unit |
| 用户 `dutybot` | 系统用户 | 跑 Bot 的非 root 用户，nologin |
| 组 `dutybot` | 系统组 | 与用户同名 |

没有网页目录、没有 nginx/caddy 站点、没有额外 timer/cron、没有往 sshd/PAM 塞文件。日志走 journal（`journalctl -u dutybot`），不单独写日志文件。

### 卸载后，agent 用这些检查残留

`uninstall.sh` 跑完后，下面每一项都应该是「不存在」。任何一项还在，就是残留，修到没有为止。不要用 `rm -rf /` 一类的扩大删除去「顺便清干净」。

```bash
# 文件和目录：有输出就是残留
ls -ld /opt/dutybot /etc/dutybot /var/lib/dutybot /usr/lib/dutybot \
  /etc/sudoers.d/dutybot /etc/systemd/system/dutybot.service 2>/dev/null

# 用户和组：能查到就是残留
getent passwd dutybot
getent group dutybot

# 服务：还在 loaded/active 就是残留
systemctl status dutybot --no-pager
systemctl is-enabled dutybot 2>/dev/null

# 不该动到的东西（必须仍在，动了就是误删）
systemctl cat hermes.service pi-agent.service dsh.service 2>/dev/null | head
```

检查时用本机真实 unit 名替换上面的 `hermes.service` 等示例。journal 里 `dutybot` 的旧日志可以留着，不算必须清的残留；不要为了清日志去 `journalctl --vacuum` 整台机器。
