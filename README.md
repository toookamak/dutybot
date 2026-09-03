# dutybot

给自己用的 Telegram 值班 Bot。装在一台带 systemd 的 Linux Guest 上，用聊天菜单看机器、管服务、清僵尸，服务挂了或有人 SSH 上来会主动推给你。

Telegram 里叫 **值班**。只认你一个账号。

部署和验收步骤看 [AGENTS.md](./AGENTS.md)（给 Hermes 等代理读）。

## 功能

### 菜单里能点的

- **设备状态**：主机名、运行时间、CPU、温度、负载、内存/交换、根分区、网卡 IP；每个看守服务是否在跑；配了端口的会探一下通不通。虚拟机没有温度就显示不可用，不编数字。
- **CPU 前五**：pid、占用、内存、命令行。
- **读写前五**：当前读/写速率最高的五个进程。
- **服务**：看守名单里每一条都一样——看状态、看最近日志、重启（要二次确认）。状态会带失败原因（Result、退出码、重启次数）。
- **清理进程**：先列出僵尸，以及看守服务留下的孤儿 worker；你确认后才杀。僵尸杀不掉时会说明，让你去找父进程。不会因为占用高就杀。
- **重启系统**：两次确认，大约一分钟后重启这台 Guest。起来后再推一条「已恢复」和一张状态卡。
- **添加 / 删除服务**：发 unit 名、显示名、可选的 `host:port`。删除只从名单拿掉，不停那个服务。

### 自己会报的

- Bot 第一次上线，或机器重启后它拉起来：推「已恢复」+ 状态卡。
- 看守服务挂了、自己恢复了、意外重启了。你刚在菜单里点的重启，不会再当意外重启广播一遍。
- **假活**：服务显示在跑，但探测端口不通。
- **SSH**：成功登录每次都报（谁、从哪个 IP、什么时候）。失败登录也报，同一 IP 有冷却，避免扫号刷屏。
- **CPU**：去掉看守服务之后仍然很高 → 意外负载；整机长时间打满 → 饱和。
- **磁盘读写、网卡流量**：持续超过阈值才报，恢复了也可以报一声。
- **根分区剩余过低**：立刻报。
- 同类告警有冷却，避免刷屏。
- Bot 自己卡住时，由 systemd watchdog 拉起，仍走「已恢复」。

### 不做的事

只装 Guest，不动 PVE 宿主机。没有网页控制台，不能执行任意命令。Token 只活在本机环境文件里，不进 git。

## 环境

任意发行版，只要有 systemd 和 Python 3.10+。

看守名单是配置，不是写死的三个名字。默认会管 Hermes，也可以加 Pi Agent、DSH、Caddy、Nginx、Docker 等，每一条都是：id、显示名、`*.service`、可选探测地址。

## 当前进度

仓库里目前是文档。安装脚本、Bot 和 helper 还没提交。没有 `install.sh` 就不要手写一套安装。

## 装到机器上会留下什么

安装**只会**创建这些。卸载必须全部撤掉，并核对没有残留。不要删除 Hermes 或其他看守服务。

**源码（计划）**

```
README.md          AGENTS.md
install.sh         uninstall.sh
requirements.txt
systemd/dutybot.service
sudoers/dutybot
helper/dutyctl
src/dutybot/       bot、状态、服务、进程、监控、通知
```

**装完以后在 Guest 上**

| 路径 | 干什么 |
| --- | --- |
| `/opt/dutybot/` | 程序和 venv，用户 `dutybot` 的家目录 |
| `/etc/dutybot/env` | Token 和白名单 Chat ID |
| `/var/lib/dutybot/watch.json` | 看守名单 |
| `/usr/lib/dutybot/dutyctl` | 唯一能提权的助手（重启服务 / 杀指定进程 / 重启系统） |
| `/etc/sudoers.d/dutybot` | 只允许上面那个助手 |
| `/etc/systemd/system/dutybot.service` | 开机拉起 Bot |
| 用户和组 `dutybot` | 非 root，不能登录 |

没有网站目录，不改 sshd/PAM，不加 cron。日志在 journal 里：`journalctl -u dutybot`。

### 卸完怎么确认没残留

`uninstall.sh` 跑完后，下面这些都应该找不到。还在就是没卸干净。不要扩大删除范围去「顺便清」。journal 里的旧日志可以留着。

```bash
ls -ld /opt/dutybot /etc/dutybot /var/lib/dutybot /usr/lib/dutybot \
  /etc/sudoers.d/dutybot /etc/systemd/system/dutybot.service 2>/dev/null

getent passwd dutybot
getent group dutybot
systemctl status dutybot --no-pager
```

Hermes 等看守服务的 unit 必须还在。检查时用这台机器上的真实服务名。
