# 给部署代理的说明

本文件给 **Hermes**（以及其他要在本机落地 dutybot 的代理）读。人类看 [README.md](./README.md)。读完再动手。不要发挥。

你要在 **这台 Guest Linux** 上安装并配置「单人 Telegram 值班 Bot」（`dutybot`），含需登录的配置与日志页。

## 1. 你在干什么

装一个只给主人用的值班遥控器：

- 一个 Bot、一个 Telegram 账号白名单
- 菜单看机器、管看守名单里的 unit、清僵尸/孤儿、重启这台 Guest
- 服务挂了、假活（unit 在跑但端口不通）、机器起来、SSH 登录会主动推送到白名单
- 看守名单是配置，不是写死的三个名字
- 同一进程提供 Web：登录后可改配置、查 journal；不是命令终端

## 2. 禁止事项（必须遵守）

- **不要装在 PVE 宿主机上。** 只装当前这台 Guest。不确定就停，问主人。
- **不要做成网页命令终端，不要开放任意 shell，不要加 SSH 远程命令入口。** Web 仅配置与日志。
- **不要把 Web 直接监听 `0.0.0.0`。** 默认 `127.0.0.1`。外网访问须经本机已有反向代理，且必须先有登录。
- **不要按 CPU 占用杀进程。** 无用进程只包括：僵尸，以及看守服务留下的孤儿 worker。
- 从看守名单删除服务时，**只改配置，不要 stop/disable 那个 unit。**
- Token、Chat ID、Web 口令 **不要写入 git、不要写进本说明、不要贴到群里。**
- 发行版不限 Mint。需要：`systemd` + **Python 3.10+**。不够就停，不要降级硬装。

## 3. 动手前向主人要齐这些

完整对照见 README「配置说明」。未配置的项只关闭对应能力，其余功能仍可用。

**至少一条完整通道**（Telegram 的 Token+Chat ID，或 Web 的用户+口令）才能安装。两条都没有就停，提问补齐。不要为了「先装上」编造 Token 或口令。

| 项 | 必要性 | 说明 |
| --- | --- | --- |
| 确认本机是 Guest | 必须 | 不是 PVE 宿主机。不确定就停。 |
| `BOT_TOKEN` + `ALLOWED_CHAT_ID` | 使用 Telegram 时必须成对 | 缺一则 Telegram 菜单和通知都不可用。Web 已配则可只开 Web。 |
| `WEB_USER` / Web 口令 | 使用 Web 时必须 | 未给则不启用 Web，Telegram 照常。口令只写环境文件哈希，不要回显、不要进仓库。 |
| `WEB_BIND` / `WEB_PORT` | 可选 | 启用 Web 时默认 `127.0.0.1:8787`。主人要求走 Caddy/Nginx 再问反代主机名，不要擅自对公网打开。 |
| 看守名单 | 可选，允许空 | 空名单可装。设备状态、CPU/读写前五、重启本机、僵尸、SSH、资源告警仍可用。建议写入 Hermes（及 Pi Agent、DSH），但不是安装前提。每条要有本机 **真实 unit 名**，`probe` 可空。 |

Chat ID 若主人不知道：让他先随便给 Bot 发一条 `/start`，你再查更新，或请主人用自己的方式把数字 id 给你。不要猜。

## 4. 安装前在本机核对

```bash
systemctl --version
python3 -c 'import sys; print(sys.version); assert sys.version_info >= (3, 10)'
hostnamectl
ip -br addr
```

找到真实 unit（名字以本机为准，不要照抄示例）：

```bash
systemctl list-units --type=service --all --no-pager | grep -iE 'hermes|pi-?agent|dsh|caddy|nginx|docker'
systemctl list-unit-files --type=service --no-pager | grep -iE 'hermes|pi-?agent|dsh'
```

若某服务有 HTTP/TCP 口，记下探测地址（优先本机回环）：

```bash
ss -lntp | grep -iE 'hermes|python|caddy|nginx|docker|8080|443|80'
```

sshd 日志来源（后面 SSH 通知要用）：

```bash
systemctl cat ssh.service 2>/dev/null | head
systemctl cat sshd.service 2>/dev/null | head
journalctl -u ssh -u sshd -n 5 --no-pager
```

## 5. 怎么装

应用根目录执行（脚本会建 `dutybot` 用户、venv、sudoers、systemd、拉起服务）：

```bash
sudo ./install.sh
```

- 缺当前启用通道所需的密钥时，脚本必须 **提问**，不要自己编。只开 Telegram 就不要强要 Web 口令。
- 装完后服务名：`dutybot.service`（Telegram 与 Web 同一进程）
- 进程用户：`dutybot`（非 root）
- 特权动作只允许通过固定 helper：`/usr/lib/dutybot/dutyctl`
  - 仅三件事：`restart-unit` / `kill-pids` / `reboot`
  - sudoers **只放行这一条**，不要给 `dutybot` 用户 NOPASSWD ALL
- Web 默认绑定 `127.0.0.1`。未经主人明确要求，不要改成 `0.0.0.0`，不要新建第二个 unit 专门跑网站。

若 `install.sh` 尚未生成：停下来，不要手写一套「等价安装」。等应用代码就绪后再装。

## 6. 你必须写入的配置

### 6.1 密钥与白名单

文件：`/etc/dutybot/env`  
属主：`root:dutybot`，权限 `640`

Telegram 通道示例：

```
BOT_TOKEN=...
ALLOWED_CHAT_ID=...
```

Web 通道示例（未启用 Web 则不要写这四项，进程不监听 HTTP）：

```
WEB_USER=...
WEB_PASSWORD_HASH=...
WEB_BIND=127.0.0.1
WEB_PORT=8787
```

只允许这一个 Chat ID、这一个 Web 用户。不要加第二个。改完：

```bash
sudo systemctl restart dutybot
```

口令以哈希写入。不要把明文口令写进 watch.json、日志或回报。

### 6.2 看守名单

文件：`/var/lib/dutybot/watch.json`  
属主：`dutybot:dutybot`，权限 `640`  
（Telegram 菜单与 Web 配置页都会改这份文件；**删除只从名单拿掉，不准 stop 对应 unit。**）

格式：

```json
{
  "services": [
    {
      "id": "hermes",
      "name": "Hermes",
      "unit": "hermes.service",
      "probe": "127.0.0.1:PORT"
    },
    {
      "id": "pi-agent",
      "name": "Pi Agent",
      "unit": "REPLACE.service",
      "probe": null
    },
    {
      "id": "dsh",
      "name": "DSH",
      "unit": "REPLACE.service",
      "probe": null
    }
  ]
}
```

规则：

- `unit` 必须是本机真实存在的 `*.service`。用第 4 节命令查到的名字替换 `hermes.service` / `REPLACE.service`。
- 没有探测端口就写 `null`，不要填假端口。
- 有端口则写 `host:port`（Guest 内用 `127.0.0.1`）。**假活告警依赖这个**：unit active 但端口不通要报。
- 可以加 Caddy / Nginx / Docker 等，同一结构，不要为此做专用面板。
- `id` 用稳定短名（小写、无空格）。

写好后确认 JSON 合法，重启或热加载：

```bash
python3 -m json.tool /var/lib/dutybot/watch.json >/dev/null
sudo systemctl reload dutybot 2>/dev/null || sudo systemctl restart dutybot
```

### 6.3 SSH 登录通知（要做）

不改 PAM、不改 `sshd_config`。Bot 自己读 `ssh.service` / `sshd.service` 的 journal。

你要保证：

1. 本机 OpenSSH 在跑，日志进 journal（第 4 节那条 `journalctl` 能看到记录就算通）。
2. 成功登录：**每次**推白名单（用户、来源 IP、时间）。不要做成功登录冷却。
3. 失败登录：要报，但 **同一 IP 必须冷却**，防止扫号刷屏。
4. 不要用 `ForceCommand`、不要给 sshd 塞脚本。

若 journal 里根本没有 sshd 登录记录：告诉主人「SSH 通知不可用，原因是 journal 没有 ssh/sshd」，不要假装已经接上。

### 6.4 Web 页

未配置 `WEB_USER` 时跳过本节，不要强行启 Web。

- 确认 `ss -lntp` 上 `WEB_BIND:WEB_PORT` 在听，且进程是 `dutybot`。
- 未登录访问配置/日志应被拒绝。
- 日志页只能选 `dutybot` 与看守名单里的 unit，不要接任意 unit 名。
- 主人若要求用已有 Caddy/Nginx 反代：只加一条指向 `127.0.0.1:WEB_PORT` 的站点，**不要安装新的 Web 服务器**，不要在卸载 dutybot 时删掉 Caddy/Nginx。
- 不要在 Web 上提供 restart / kill / reboot。那三件事只走 Telegram 与 `dutyctl`。

### 6.5 建议保持默认的行为（不用改代码）

这些第一版就有，你配置好名单即可：

- 开机 / Bot 起来：推「已恢复」+ 一张状态卡
- 服务挂了 / 自己恢复 / 意外重启要报；**菜单里刚点的重启不要再广播成意外重启**
- 服务状态带失败原因（Result、退出码、NRestarts）
- Bot 自身 systemd watchdog：卡住由 systemd 拉起
- 无用进程 = 僵尸 + 看守服务留下的孤儿 worker

不要加「一键杀高占用」。

## 7. 验收（必须做，做完才算部署成功）

1. `systemctl is-active dutybot` 为 `active`
2. `systemctl show dutybot -p WatchdogTimestamp` 有喂狗（若 unit 配了 WatchdogSec）
3. 若启用了 Telegram：主人收到上线/恢复状态卡，且能打开菜单
4. 若看守名单非空：状态卡里已写入的服务显示 **真实** active/inactive，不是 unknown
5. 若填了 probe：端口通/不通与事实一致
6. 若启用了 Telegram：菜单「服务 → 状态 / 最近日志」能看，**先不要点重启系统**
7. 若启用了 Telegram：用主人账号以外的账号发消息，Bot **必须不理**
8. 若启用了 Telegram：能 `journalctl -u ssh -u sshd -n 20 --no-pager` 的机器，告诉主人下一次 SSH 登录应收到通知；journal 没有则标明 SSH 通知不可用，不视为整机安装失败
9. 若启用了 Web：本机可打开登录页；错误口令进不去；正确口令后能改看守名单、能看到 `dutybot` 与看守 unit 的日志。未启用 Web 则 `ss -lntp` 不应出现 dutybot 的 HTTP 端口，Telegram 功能仍须通过验收第 3–8 项
10. 若启用了 Web：`ss -lntp` 显示绑在 `127.0.0.1`（除非主人书面要求并已加反向代理）

验收失败：修配置或停在当前步骤，不要扩大权限「先跑起来再说」。

## 8. 卸载

```bash
sudo ./uninstall.sh
```

卸载不得顺手删掉 Hermes / Pi Agent / DSH 的 unit，也不得删除本机原有的 Caddy / Nginx。只撤 `dutybot` 自己（用户、venv、sudoers、unit、配置、Web 会话目录）。残留检查见 README「计划文件结构」。

## 9. 路径速查

| 路径 | 用途 |
| --- | --- |
| `/opt/dutybot` | 应用与 venv |
| `/etc/dutybot/env` | Token、白名单 Chat ID、Web 登录与监听 |
| `/var/lib/dutybot/watch.json` | 看守名单（Telegram 与 Web 均可改） |
| `/var/lib/dutybot/web-sessions/` | Web 会话 |
| `/usr/lib/dutybot/dutyctl` | 唯一特权 helper |
| `/etc/sudoers.d/dutybot` | 只放行 helper |
| `dutybot.service` | systemd 服务（Bot + Web） |

## 10. 回报主人时说这些

- 是否装在 Guest（hostname）
- `dutybot` 是否 active
- 看守名单最终条目（显示名 + 真实 unit + probe）
- SSH 通知是否能从 journal 读到
- 若启用了 Web：地址（`http://127.0.0.1:端口` 或反代 URL）、登录用户名（不要报口令）；未启用则明确说 Web 未开
- Token 与 Web 口令 **不要**出现在回报里
