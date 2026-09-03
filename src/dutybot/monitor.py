"""Poll loop: service/SSH/resource alerts and systemd watchdog notify."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import time
from pathlib import Path
from typing import Optional

from dutybot.config import Config, WatchList
from dutybot.notify import Notifier, escape, notify_watchdog, notify_status
from dutybot.services import ExpectedRestarts
from dutybot import services, status

log = logging.getLogger("dutybot.monitor")

SSH_ACCEPTED = re.compile(
    r"Accepted \S+ for (\S+) from (\S+)",
    re.IGNORECASE,
)
SSH_FAILED = re.compile(
    r"(?:Failed \S+ for (?:invalid user )?(\S+) from (\S+)"
    r"|Invalid user (\S+) from (\S+)"
    r"|Connection closed by (?:authenticating user )?(\S+) (\d+\.\d+\.\d+\.\d+))",
    re.IGNORECASE,
)



def sd_notify(message: str) -> None:
    """Compat wrapper; prefer notify_ready/notify_watchdog/notify_status."""
    from dutybot.notify import _sd_send
    _sd_send(message)


class _Rate:
    def __init__(self) -> None:
        self.prev: Optional[float] = None
        self.prev_t: Optional[float] = None
        self.over_since: Optional[float] = None
        self.alerted: bool = False

    def update(self, value: float, now: float, limit: float, need: float) -> str:
        """Return '', 'fire', or 'recover'."""
        if value >= limit:
            if self.over_since is None:
                self.over_since = now
            elif (now - self.over_since) >= need and not self.alerted:
                self.alerted = True
                return "fire"
        else:
            if self.alerted:
                self.alerted = False
                self.over_since = None
                return "recover"
            self.over_since = None
        return ""


class Monitor:
    def __init__(self, cfg: Config, notifier: Notifier, expected: ExpectedRestarts | None = None) -> None:
        self.cfg = cfg
        self.n = notifier
        self.expected = expected or ExpectedRestarts()
        self._bootstrapped = False
        self._states: dict[str, str] = {}
        self._nrestarts: dict[str, int] = {}
        self._mainpid: dict[str, int] = {}
        self._hung: dict[str, bool] = {}
        self._ssh_cursor: Optional[str] = None
        self._ssh_ok: Optional[bool] = None
        self._cpu_unexp = _Rate()
        self._cpu_sat = _Rate()
        self._disk_r = _Rate()
        self._disk_w = _Rate()
        self._net_rx = _Rate()
        self._net_tx = _Rate()
        self._disk_prev: Optional[tuple[int, int, float]] = None
        self._net_prev: Optional[tuple[int, int, float]] = None
        self._cgroup_prev: dict[str, tuple[int, float]] = {}
        self._cpu_prev: Optional[tuple[float, float]] = None  # idle, total from /proc/stat
        self._root_low = False

    def _th(self) -> dict:
        return self.cfg.reload_watch().merged_thresholds()

    def _persist_boot_id(self) -> None:
        try:
            current = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        except OSError:
            return
        dest = self.cfg.state / "boot-id"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(current + "\n", encoding="utf-8")
        except OSError:
            log.debug("boot-id persist failed", exc_info=True)

    async def recovered(self) -> None:
        self._persist_boot_id()
        watch = self.cfg.reload_watch()
        rows = []
        snap = services.snapshot(watch)
        for svc in watch.services:
            st = snap.units.get(svc.unit) or services.systemctl_show(svc.unit)
            hung = bool(svc.probe and st.is_active and snap.probes.get(svc.unit) is False)
            rows.append(services.format_unit_line(svc, st, hung=hung))
        data = status.collect(watch, unit_rows=rows)
        card = "已恢复\n" + status.format_status_card(data, title="设备状态")
        ssh_ok, reason = services.ssh_journal_available()
        self._ssh_ok = ssh_ok
        if not ssh_ok:
            card += f"\nSSH 通知：不可用（{escape(reason or 'journal 没有 ssh/sshd')}）"
        await self.n.send(card, force=True)

    def _proc_stat_cpu(self) -> Optional[float]:
        try:
            line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
            parts = [float(x) for x in line.split()[1:]]
            idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)
            total = sum(parts)
        except (OSError, ValueError, IndexError):
            return None
        prev = self._cpu_prev
        self._cpu_prev = (idle, total)
        if not prev:
            return None
        didle = idle - prev[0]
        dtotal = total - prev[1]
        if dtotal <= 0:
            return None
        return max(0.0, min(100.0, (1.0 - didle / dtotal) * 100.0))

    def _watched_cpu_pct(self, watch: WatchList, elapsed: float) -> float:
        if elapsed <= 0:
            return 0.0
        ncpu = os.cpu_count() or 1
        used = 0.0
        now = time.monotonic()
        for unit in watch.units():
            usec = services.cgroup_usage_usec(unit)
            if usec is None:
                continue
            prev = self._cgroup_prev.get(unit)
            self._cgroup_prev[unit] = (usec, now)
            if not prev:
                continue
            du = usec - prev[0]
            if du < 0:
                continue
            used += (du / 1_000_000.0) / elapsed / ncpu * 100.0
        return max(0.0, used)

    def _disk_rates(self) -> tuple[float, float]:
        """Return (read_Bps, write_Bps) across all disks."""
        now = time.monotonic()
        rb = wb = 0
        try:
            import psutil

            io = psutil.disk_io_counters()
            if io:
                rb, wb = int(io.read_bytes), int(io.write_bytes)
        except Exception:
            rb = wb = 0
            try:
                for line in Path("/proc/diskstats").read_text(encoding="utf-8").splitlines():
                    p = line.split()
                    if len(p) < 14:
                        continue
                    name = p[2]
                    if name.startswith("loop") or name.startswith("ram") or name.startswith("dm-"):
                        continue
                    # sectors of 512
                    rb += int(p[5]) * 512
                    wb += int(p[9]) * 512
            except OSError:
                return 0.0, 0.0
        prev = self._disk_prev
        self._disk_prev = (rb, wb, now)
        if not prev:
            return 0.0, 0.0
        dt = now - prev[2]
        if dt <= 0:
            return 0.0, 0.0
        return max(0.0, (rb - prev[0]) / dt), max(0.0, (wb - prev[1]) / dt)

    def _net_rates(self) -> tuple[float, float]:
        now = time.monotonic()
        rx = tx = 0
        try:
            import psutil

            pernic = psutil.net_io_counters(pernic=True) or {}
            for name, io in pernic.items():
                if name == "lo" or name.startswith("lo"):
                    continue
                rx += int(io.bytes_recv)
                tx += int(io.bytes_sent)
        except Exception:
            try:
                for line in Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]:
                    if ":" not in line:
                        continue
                    name, rest = line.split(":", 1)
                    name = name.strip()
                    if name == "lo":
                        continue
                    p = rest.split()
                    rx += int(p[0])
                    tx += int(p[8])
            except (OSError, ValueError, IndexError):
                return 0.0, 0.0
        prev = self._net_prev
        self._net_prev = (rx, tx, now)
        if not prev:
            return 0.0, 0.0
        dt = now - prev[2]
        if dt <= 0:
            return 0.0, 0.0
        return max(0.0, (rx - prev[0]) / dt), max(0.0, (tx - prev[1]) / dt)

    async def _tick_services(self, watch: WatchList, th: dict) -> None:
        cd = float(th["cooldown_service_sec"])
        snap = services.snapshot(watch)
        for svc in watch.services:
            st = snap.units.get(svc.unit)
            if st is None:
                continue
            unit = svc.unit
            prev_active = self._states.get(unit)
            prev_nr = self._nrestarts.get(unit)
            prev_pid = self._mainpid.get(unit)
            self._states[unit] = st.active
            self._nrestarts[unit] = st.nrestarts
            self._mainpid[unit] = st.main_pid
            if not self._bootstrapped:
                continue
            expected = self.expected.is_expected(unit)
            if prev_active is not None:
                was_up = prev_active == "active"
                now_up = st.is_active
                if was_up and not now_up and not expected:
                    await self.n.send(
                        f"⚠️ 服务停止：{escape(svc.name)}（{escape(unit)}）\n"
                        f"ActiveState={escape(st.active)} Result={escape(st.result)}",
                        key=f"svc-down:{unit}",
                        cooldown=cd,
                    )
                elif (not was_up) and now_up:
                    await self.n.send(
                        f"✅ 服务恢复：{escape(svc.name)}（{escape(unit)}）",
                        key=f"svc-up:{unit}",
                        cooldown=cd,
                    )
            pid_changed = (
                prev_pid is not None
                and st.main_pid
                and prev_pid
                and st.main_pid != prev_pid
                and st.is_active
            )
            nr_up = prev_nr is not None and st.nrestarts > prev_nr and st.is_active
            if (pid_changed or nr_up) and not expected:
                await self.n.send(
                    f"⚠️ 意外重启：{escape(svc.name)}（{escape(unit)}）\n"
                    f"NRestarts {prev_nr} → {st.nrestarts}",
                    key=f"svc-restart:{unit}",
                    cooldown=cd,
                )
            hung_now = bool(svc.probe and st.is_active and snap.probes.get(unit) is False)
            hung_prev = self._hung.get(unit, False)
            self._hung[unit] = hung_now
            if hung_now and not hung_prev:
                await self.n.send(
                    f"⚠️ 假活：{escape(svc.name)} 为 active，但 {escape(svc.probe)} 不可达",
                    key=f"svc-hung:{unit}",
                    cooldown=cd,
                )
            elif hung_prev and not hung_now and st.is_active:
                await self.n.send(
                    f"✅ 探测恢复：{escape(svc.name)} {escape(svc.probe)}",
                    key=f"svc-hung-ok:{unit}",
                    cooldown=cd,
                )

    async def _tick_ssh(self, th: dict) -> None:
        cd_fail = float(th["cooldown_ssh_fail_sec"])
        since = self._ssh_cursor
        args_since = since or "now"
        # First pass: set cursor to now, skip backlog.
        if self._ssh_cursor is None:
            self._ssh_cursor = time.strftime("%Y-%m-%d %H:%M:%S")
            return
        text, err = services.read_journal(
            list(services.JOURNAL_UNITS_SSH),
            since=args_since,
            lines=200,
            output="short-iso",
        )
        if err:
            self._ssh_ok = False
            return
        self._ssh_ok = True
        newest = self._ssh_cursor
        if not hasattr(self, "_ssh_seen"):
            self._ssh_seen = set()
        for line in text.splitlines():
            if line.startswith("…") or line == "（无日志）":
                continue
            # journal short-iso: 2026-09-03T01:02:03+00:00 host sshd[n]: msg
            stamp = line.split(" ", 1)[0] if line else ""
            if stamp and stamp > (newest or ""):
                newest = stamp
            fp = line[-240:]
            if fp in self._ssh_seen:
                continue
            self._ssh_seen.add(fp)
            if len(self._ssh_seen) > 800:
                self._ssh_seen = set(list(self._ssh_seen)[-400:])
            m = SSH_ACCEPTED.search(line)
            if m:
                user, ip = m.group(1), m.group(2)
                await self.n.send(
                    f"🔑 SSH 登录成功\n用户：{escape(user)}\n来源：{escape(ip)}\n{escape(stamp)}",
                    force=True,
                )
                continue
            fm = SSH_FAILED.search(line)
            if fm:
                user = fm.group(1) or fm.group(3) or fm.group(5) or "?"
                ip = fm.group(2) or fm.group(4) or fm.group(6) or "?"
                await self.n.send(
                    f"🚫 SSH 登录失败\n用户：{escape(user)}\n来源：{escape(ip)}\n{escape(stamp)}",
                    key=f"ssh-fail:{ip}",
                    cooldown=cd_fail,
                )
        if newest:
            # Advance slightly so we don't re-read the same second forever.
            self._ssh_cursor = newest

    async def _tick_resources(self, watch: WatchList, th: dict, elapsed: float) -> None:
        cd = float(th["cooldown_resource_sec"])
        now = time.monotonic()
        total = self._proc_stat_cpu()
        if total is None:
            total = status.cpu_percent() or 0.0
        watched = self._watched_cpu_pct(watch, elapsed)
        unwatched = max(0.0, total - watched)

        ev = self._cpu_unexp.update(unwatched, now, float(th["cpu_unexpected_pct"]), float(th["cpu_unexpected_sec"]))
        if ev == "fire":
            await self.n.send(
                f"⚠️ CPU 意外负载：排除看守服务后 CPU {unwatched:.0f}% "
                f"（阈值 {th['cpu_unexpected_pct']}% / {th['cpu_unexpected_sec']}s）",
                key="cpu-unexp",
                cooldown=cd,
            )
        elif ev == "recover":
            await self.n.send("✅ CPU 意外负载已恢复", key="cpu-unexp-ok", cooldown=cd)

        ev = self._cpu_sat.update(total, now, float(th["cpu_saturation_pct"]), float(th["cpu_saturation_sec"]))
        if ev == "fire":
            await self.n.send(
                f"⚠️ CPU 饱和：整机 {total:.0f}% "
                f"（阈值 {th['cpu_saturation_pct']}% / {th['cpu_saturation_sec']}s）",
                key="cpu-sat",
                cooldown=cd,
            )
        elif ev == "recover":
            await self.n.send("✅ CPU 饱和已恢复", key="cpu-sat-ok", cooldown=cd)

        rB, wB = self._disk_rates()
        limit = float(th["disk_mbps"]) * 1024 * 1024
        need = float(th["disk_sec"])
        ev = self._disk_r.update(rB, now, limit, need)
        if ev == "fire":
            await self.n.send(
                f"⚠️ 磁盘读持续偏高：{status.format_bytes(rB)}/s（阈值 {th['disk_mbps']} MB/s / {th['disk_sec']}s）",
                key="disk-r",
                cooldown=cd,
            )
        elif ev == "recover":
            await self.n.send("✅ 磁盘读已恢复", key="disk-r-ok", cooldown=cd)
        ev = self._disk_w.update(wB, now, limit, need)
        if ev == "fire":
            await self.n.send(
                f"⚠️ 磁盘写持续偏高：{status.format_bytes(wB)}/s（阈值 {th['disk_mbps']} MB/s / {th['disk_sec']}s）",
                key="disk-w",
                cooldown=cd,
            )
        elif ev == "recover":
            await self.n.send("✅ 磁盘写已恢复", key="disk-w-ok", cooldown=cd)

        rx, tx = self._net_rates()
        nlimit = float(th["net_mbps"]) * 1024 * 1024
        nneed = float(th["net_sec"])
        ev = self._net_rx.update(rx, now, nlimit, nneed)
        if ev == "fire":
            await self.n.send(
                f"⚠️ 网卡收持续偏高：{status.format_bytes(rx)}/s（阈值 {th['net_mbps']} MB/s / {th['net_sec']}s）",
                key="net-rx",
                cooldown=cd,
            )
        elif ev == "recover":
            await self.n.send("✅ 网卡收已恢复", key="net-rx-ok", cooldown=cd)
        ev = self._net_tx.update(tx, now, nlimit, nneed)
        if ev == "fire":
            await self.n.send(
                f"⚠️ 网卡发持续偏高：{status.format_bytes(tx)}/s（阈值 {th['net_mbps']} MB/s / {th['net_sec']}s）",
                key="net-tx",
                cooldown=cd,
            )
        elif ev == "recover":
            await self.n.send("✅ 网卡发已恢复", key="net-tx-ok", cooldown=cd)

        disk = status.root_disk()
        if disk:
            low = disk["avail"] < int(th["root_disk_avail_bytes"]) or disk["pct_avail"] < float(
                th["root_disk_avail_pct"]
            )
            if low and not self._root_low:
                self._root_low = True
                await self.n.send(
                    f"⚠️ 根分区剩余过低：{status.format_bytes(disk['avail'])} "
                    f"（{disk['pct_avail']:.0f}%）",
                    key="root-disk",
                    cooldown=cd,
                    force=True,  # immediate first notice
                )
            elif low:
                await self.n.send(
                    f"⚠️ 根分区剩余过低：{status.format_bytes(disk['avail'])} "
                    f"（{disk['pct_avail']:.0f}%）",
                    key="root-disk",
                    cooldown=cd,
                )
            elif self._root_low and not low:
                self._root_low = False
                await self.n.send("✅ 根分区空间已恢复", key="root-disk-ok", cooldown=cd)

    async def tick(self, elapsed: float) -> None:
        watch = self.cfg.reload_watch()
        th = watch.merged_thresholds()
        await self._tick_services(watch, th)
        await self._tick_ssh(th)
        await self._tick_resources(watch, th, elapsed)
        if not self._bootstrapped:
            self._bootstrapped = True


    async def run(self, stop: asyncio.Event | None = None) -> None:
        self._persist_boot_id()
        last = time.monotonic()
        first = True
        while True:
            if stop is not None and stop.is_set():
                break
            notify_watchdog()
            notify_status("running")
            now = time.monotonic()
            try:
                if first:
                    try:
                        await self.recovered()
                    except Exception:
                        log.exception("recovered notify failed")
                    first = False
                await self.tick(now - last)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("monitor tick failed")
            last = now
            th = self.cfg.watch.merged_thresholds()
            poll = max(2, int(th.get("poll_sec", 10)))
            if stop is None:
                await asyncio.sleep(poll)
            else:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll)
                except asyncio.TimeoutError:
                    pass


async def monitor_loop(
    cfg: Config,
    notifier: Notifier,
    expected: ExpectedRestarts | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    mon = Monitor(cfg, notifier, expected)
    await mon.run(stop)
