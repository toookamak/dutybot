"""CPU/IO top, zombie/orphan preview, kill-preview store."""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from dutybot.config import WatchList, WatchService, preview_path, slug_id, validate_unit_name
from dutybot.notify import escape
from dutybot.status import format_bytes

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

log = logging.getLogger("dutybot.procs")

PREVIEW_TTL_SEC = 10 * 60


@dataclass
class ProcRow:
    pid: int
    ppid: int
    starttime: int
    kind: str  # zombie | orphan
    cmd: str
    unit: str = ""
    state: str = ""


def _proc_starttime(pid: int) -> int:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        rparen = stat.rfind(")")
        fields = stat[rparen + 2 :].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return 0


def _proc_state_ppid(pid: int) -> tuple[str, int]:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        rparen = stat.rfind(")")
        fields = stat[rparen + 2 :].split()
        return fields[0], int(fields[1])
    except (OSError, ValueError, IndexError):
        return "", 0


def _cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        if raw:
            return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()[:200]
        comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace").strip()
        return comm
    except OSError:
        return ""


def _cgroup(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _unit_from_cgroup(text: str, units: list[str]) -> Optional[str]:
    for unit in units:
        if unit and unit in text:
            return unit
    return None


def cpu_top(n: int = 5, sample: float = 0.4) -> list[dict[str, Any]]:
    if not psutil:
        return []
    procs = list(psutil.process_iter(["pid", "name", "cmdline", "memory_percent"]))
    for p in procs:
        try:
            p.cpu_percent(None)
        except (psutil.Error, OSError):
            pass
    time.sleep(sample)
    rows: list[dict[str, Any]] = []
    for p in procs:
        try:
            cpu = p.cpu_percent(None)
            info = p.info
            cmd = " ".join(info.get("cmdline") or []) or info.get("name") or ""
            rows.append(
                {
                    "pid": p.pid,
                    "cpu": cpu,
                    "mem": info.get("memory_percent") or 0.0,
                    "cmd": cmd[:180],
                    "cmdline": cmd[:180],
                }
            )
        except (psutil.Error, OSError):
            continue
    rows.sort(key=lambda r: r["cpu"], reverse=True)
    return rows[:n]


def io_top(n: int = 5, sample: float = 0.4) -> list[dict[str, Any]]:
    first: dict[int, tuple[int, int, str]] = {}
    if psutil:
        for p in psutil.process_iter(["pid", "cmdline", "name"]):
            try:
                io = p.io_counters()
                cmd = " ".join(p.info.get("cmdline") or []) or p.info.get("name") or ""
                first[p.pid] = (int(io.read_bytes), int(io.write_bytes), cmd)
            except (psutil.Error, OSError, AttributeError):
                continue
    else:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                text = Path(f"/proc/{pid}/io").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rb = wb = 0
            for line in text.splitlines():
                if line.startswith("read_bytes:"):
                    rb = int(line.split()[1])
                elif line.startswith("write_bytes:"):
                    wb = int(line.split()[1])
            first[pid] = (rb, wb, _cmdline(pid))
    time.sleep(sample)
    rates: list[dict[str, Any]] = []
    for pid, (rb0, wb0, cmd) in first.items():
        try:
            if psutil:
                p = psutil.Process(pid)
                io = p.io_counters()
                rb1, wb1 = int(io.read_bytes), int(io.write_bytes)
            else:
                text = Path(f"/proc/{pid}/io").read_text(encoding="utf-8", errors="replace")
                rb1 = wb1 = 0
                for line in text.splitlines():
                    if line.startswith("read_bytes:"):
                        rb1 = int(line.split()[1])
                    elif line.startswith("write_bytes:"):
                        wb1 = int(line.split()[1])
        except Exception:
            continue
        rrate = max(0, rb1 - rb0) / sample
        wrate = max(0, wb1 - wb0) / sample
        rates.append(
            {
                "pid": pid,
                "read": rrate,
                "write": wrate,
                "cmd": cmd[:180],
            }
        )
    rates.sort(key=lambda r: r["read"] + r["write"], reverse=True)
    return rates[:n]


def format_cpu_top(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<b>CPU 前五</b>\n（无数据）"
    lines = ["<b>CPU 前五</b>", "<code>pid   CPU%   内存%  命令</code>"]
    for r in rows:
        cmd = (r.get("cmd") or "")[:60]
        lines.append(
            f"<code>{r['pid']:<6}</code> {r['cpu']:5.1f}% {r['mem']:5.1f}% {escape(cmd)}"
        )
    return "\n".join(lines)


def format_io_top(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<b>读写前五</b>\n（无数据）"
    lines = ["<b>读写前五</b>（当前速率）", "<code>pid    读/s      写/s     命令</code>"]
    for r in rows:
        cmd = (r.get("cmd") or "")[:50]
        lines.append(
            f"<code>{r['pid']:<6}</code> {format_bytes(r['read'])}/s  {format_bytes(r['write'])}/s  {escape(cmd)}"
        )
    return "\n".join(lines)


def list_zombies() -> list[ProcRow]:
    rows: list[ProcRow] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return rows
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == 1:
            continue
        state, ppid = _proc_state_ppid(pid)
        if state != "Z":
            continue
        rows.append(
            ProcRow(
                pid=pid,
                ppid=ppid,
                starttime=_proc_starttime(pid),
                kind="zombie",
                cmd=_cmdline(pid) or f"pid {pid}",
                state=state,
            )
        )
    return rows


def list_orphans(watch: WatchList) -> list[ProcRow]:
    """PPID==1 and attributable to a watched unit (cgroup or cmdline)."""
    units = watch.units()
    if not units:
        return []
    names = [u.removesuffix(".service") if hasattr(u, "removesuffix") else (u[:-8] if u.endswith(".service") else u) for u in units]
    rows: list[ProcRow] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return rows
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == 1:
            continue
        state, ppid = _proc_state_ppid(pid)
        if state == "Z":
            continue
        if ppid != 1:
            continue
        cg = _cgroup(pid)
        unit = _unit_from_cgroup(cg, units)
        cmd = _cmdline(pid)
        if unit is None:
            low = cmd.lower()
            for u, n in zip(units, names):
                if n and n.lower() in low:
                    unit = u
                    break
        if unit is None:
            continue
        rows.append(
            ProcRow(
                pid=pid,
                ppid=ppid,
                starttime=_proc_starttime(pid),
                kind="orphan",
                cmd=cmd or f"pid {pid}",
                unit=unit,
                state=state,
            )
        )
    return rows


def format_preview(zombies: list[ProcRow], orphans: list[ProcRow]) -> str:
    lines = ["<b>清理进程预览</b>"]
    if not zombies and not orphans:
        lines.append("没有发现僵尸或看守服务孤儿进程。")
        return "\n".join(lines)
    if zombies:
        lines.append("僵尸（无法 SIGKILL，请检查父进程）：")
        for z in zombies:
            lines.append(f"  pid={z.pid} ppid={z.ppid} {escape(z.cmd[:80])}")
    if orphans:
        lines.append("看守服务孤儿 worker（确认后终止）：")
        for o in orphans:
            lines.append(f"  pid={o.pid} unit={escape(o.unit)} {escape(o.cmd[:80])}")
    else:
        lines.append("没有可终止的孤儿进程。僵尸不会被发送 SIGKILL。")
    return "\n".join(lines)


def write_preview(pids: list[int], ttl: int = PREVIEW_TTL_SEC) -> str:
    """Atomic preview JSON: token, pids, expires=now+600, chmod 600."""
    token = secrets.token_hex(16)
    dest = preview_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "token": token,
        "pids": [int(p) for p in pids],
        "expires": int(time.time()) + int(ttl),
    }
    fd, tmp = tempfile.mkstemp(prefix="preview.", suffix=".tmp", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
        try:
            os.chmod(dest, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return token


def save_preview(orphans: list[ProcRow], ttl: int = PREVIEW_TTL_SEC) -> str:
    return write_preview([o.pid for o in orphans], ttl=ttl)


def zombies() -> list[ProcRow]:
    return list_zombies()


def orphans(watch_units: list[str]) -> list[ProcRow]:
    units = [u for u in watch_units if validate_unit_name(u)]
    dummy = WatchList(
        services=[
            WatchService(id=slug_id(u), name=u, unit=u) for u in units
        ]
    )
    return list_orphans(dummy)


def clear_preview() -> None:
    dest = preview_path()
    try:
        dest.unlink()
    except OSError:
        pass
