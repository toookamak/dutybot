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
- 部署代理（Hermes）看 [AGENTS.md](./AGENTS.md)：安装、配置看守名单、SSH 登录通知、验收

## 状态

当前仓库先放文档。`install.sh`、Bot 和 helper 尚未提交。没有安装脚本时，按 `AGENTS.md` 停下来，不要手写一套安装。
