"""Host status collection for the status card."""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from typing import Any, Optional

from dutybot.config import WatchList
from dutybot.notify import escape

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def uptime_seconds() -> Optional[float]:
    raw = _read_text(Path("/proc/uptime"))
    if not raw:
        if psutil:
            try:
                return time.time() - psutil.boot_time()
            except Exception:
                return None
        return None
    try:
        return float(raw.split()[0])
    except (IndexError, ValueError):
        return None


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "不可用"
    secs = int(seconds)
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}天")
    if hours or days:
        parts.append(f"{hours}小时")
    parts.append(f"{minutes}分")
    return "".join(parts)


def format_bytes(n: Optional[int | float]) -> str:
    if n is None:
        return "不可用"
    n = float(n)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    if i == 0:
        return f"{int(n)} {units[i]}"
    return f"{n:.1f} {units[i]}"


def cpu_percent() -> Optional[float]:
    if not psutil:
        return None
    try:
        return float(psutil.cpu_percent(interval=None))
    except Exception:
        return None


def loadavg() -> Optional[tuple[float, float, float]]:
    try:
        return os.getloadavg()
    except OSError:
        return None


def cpu_temperature() -> Optional[float]:
    """Return CPU temp in Celsius, or None if unavailable. Never estimate."""
    thermal = Path("/sys/class/thermal")
    if thermal.is_dir():
        preferred: list[tuple[int, float]] = []
        others: list[float] = []
        for zone in sorted(thermal.glob("thermal_zone*")):
            tfile = zone / "temp"
            type_file = zone / "type"
            raw = _read_text(tfile)
            if not raw:
                continue
            try:
                milli = float(raw)
            except ValueError:
                continue
            temp = milli / 1000.0 if milli > 200 else milli
            ztype = (_read_text(type_file) or "").lower()
            rank = 0
            if "x86_pkg" in ztype or "cpu" in ztype or "soc" in ztype:
                rank = 1
            if rank:
                preferred.append((rank, temp))
            else:
                others.append(temp)
        if preferred:
            preferred.sort(key=lambda x: -x[0])
            return preferred[0][1]
        if others:
            return others[0]
    if psutil:
        try:
            sensors = psutil.sensors_temperatures() or {}
            for name, entries in sensors.items():
                lname = name.lower()
                if "cpu" in lname or "coretemp" in lname or "k10temp" in lname or "x86" in lname:
                    for e in entries:
                        if e.current is not None:
                            return float(e.current)
            for entries in sensors.values():
                for e in entries:
                    if e.current is not None:
                        return float(e.current)
        except Exception:
            return None
    return None


def memory_info() -> dict[str, Any]:
    if not psutil:
        return {}
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    return {
        "mem_total": vm.total,
        "mem_used": vm.used,
        "mem_avail": vm.available,
        "mem_pct": vm.percent,
        "swap_total": sm.total,
        "swap_used": sm.used,
        "swap_pct": sm.percent,
    }


def root_disk() -> dict[str, Any]:
    if not psutil:
        try:
            st = os.statvfs("/")
            total = st.f_frsize * st.f_blocks
            avail = st.f_frsize * st.f_bavail
            used = total - avail
            pct_used = (used / total * 100.0) if total else 0.0
            pct_avail = (avail / total * 100.0) if total else 0.0
            return {
                "total": total,
                "used": used,
                "avail": avail,
                "pct_used": pct_used,
                "pct_avail": pct_avail,
            }
        except OSError:
            return {}
    du = psutil.disk_usage("/")
    pct_avail = 100.0 - float(du.percent)
    return {
        "total": du.total,
        "used": du.used,
        "avail": du.free,
        "pct_used": du.percent,
        "pct_avail": pct_avail,
    }


def ipv4_addresses() -> list[str]:
    addrs: list[str] = []
    if psutil:
        try:
            nic = psutil.net_if_addrs()
            for name, items in nic.items():
                if name == "lo" or name.startswith("lo:"):
                    continue
                for item in items:
                    fam = getattr(item, "family", None)
                    if fam == socket.AF_INET:
                        ip = item.address
                        if ip and not ip.startswith("127."):
                            addrs.append(f"{name}:{ip}")
                    elif fam == socket.AF_INET6:
                        ip = (item.address or "").split("%", 1)[0]
                        low = ip.lower()
                        if (
                            ip
                            and ip != "::1"
                            and not low.startswith("fe80:")
                            and not low.startswith("::ffff:")
                        ):
                            addrs.append(f"{name}:{ip}")
        except Exception:
            pass
    return addrs


def collect(watch: Optional[WatchList] = None, unit_rows: Optional[list[str]] = None) -> dict[str, Any]:
    temp = cpu_temperature()
    disk = root_disk()
    mem = memory_info()
    load = loadavg()
    return {
        "hostname": hostname(),
        "uptime": uptime_seconds(),
        "cpu": cpu_percent(),
        "temp": temp,
        "load": load,
        "mem": mem,
        "disk": disk,
        "ips": ipv4_addresses(),
        "unit_rows": unit_rows or [],
    }


def format_status_card(
    data: dict[str, Any],
    *,
    title: str = "设备状态",
) -> str:
    temp = data.get("temp")
    temp_s = f"{temp:.1f}°C" if isinstance(temp, (int, float)) else "不可用"
    cpu = data.get("cpu")
    cpu_s = f"{cpu:.0f}%" if isinstance(cpu, (int, float)) else "不可用"
    load = data.get("load")
    if load:
        load_s = " ".join(f"{x:.2f}" for x in load)
    else:
        load_s = "不可用"
    mem = data.get("mem") or {}
    if mem:
        mem_s = (
            f"{format_bytes(mem.get('mem_used'))} / {format_bytes(mem.get('mem_total'))}"
            f" ({mem.get('mem_pct', 0):.0f}%)"
        )
        swap_total = mem.get("swap_total") or 0
        if swap_total:
            swap_s = (
                f"{format_bytes(mem.get('swap_used'))} / {format_bytes(swap_total)}"
                f" ({mem.get('swap_pct', 0):.0f}%)"
            )
        else:
            swap_s = "无"
    else:
        mem_s = "不可用"
        swap_s = "不可用"
    disk = data.get("disk") or {}
    if disk:
        disk_s = (
            f"{format_bytes(disk.get('avail'))} 可用 / {format_bytes(disk.get('total'))}"
            f" （剩余 {disk.get('pct_avail', 0):.0f}%）"
        )
    else:
        disk_s = "不可用"
    ips = data.get("ips") or []
    ip_s = ", ".join(ips) if ips else "无"
    lines = [
        f"<b>{escape(title)}</b>",
        f"主机：{escape(data.get('hostname') or 'unknown')}",
        f"运行时间：{escape(format_duration(data.get('uptime')))}",
        f"CPU：{escape(cpu_s)}",
        f"温度：{escape(temp_s)}",
        f"负载：{escape(load_s)}",
        f"内存：{escape(mem_s)}",
        f"交换：{escape(swap_s)}",
        f"根分区：{escape(disk_s)}",
        f"IP：{escape(ip_s)}",
    ]
    unit_rows = data.get("unit_rows") or []
    if unit_rows:
        lines.append("服务：")
        lines.extend(unit_rows)
    return "\n".join(lines)
