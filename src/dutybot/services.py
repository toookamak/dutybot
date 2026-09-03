"""systemd unit status, logs, TCP probe, and dutyctl wrappers."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from dutybot.config import WatchList, WatchService, dutyctl_path, validate_unit_name
from dutybot.notify import escape

log = logging.getLogger("dutybot.services")

JOURNAL_UNITS_SSH = ("ssh.service", "sshd.service")
_ISO_RE = re.compile(r"^[0-9]{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")


@dataclass
class UnitStatus:
    unit: str
    exists: bool = False
    active: str = "unknown"
    sub: str = ""
    result: str = ""
    exec_main_code: str = ""
    exec_main_status: str = ""
    nrestarts: int = 0
    main_pid: int = 0
    description: str = ""

    @property
    def is_active(self) -> bool:
        return self.active == "active"

    @property
    def is_failed(self) -> bool:
        return self.active == "failed"


def _run(args: list[str], timeout: float = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def systemctl_show(unit: str) -> UnitStatus:
    st = UnitStatus(unit=unit)
    if not validate_unit_name(unit):
        return st
    try:
        cp = _run(
            [
                "systemctl",
                "show",
                unit,
                "--no-pager",
                "-p",
                "LoadState",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "Result",
                "-p",
                "ExecMainCode",
                "-p",
                "ExecMainStatus",
                "-p",
                "NRestarts",
                "-p",
                "MainPID",
                "-p",
                "Description",
                "-p",
                "UnitFileState",
            ]
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return st
    if cp.returncode != 0 and not cp.stdout:
        return st
    props: dict[str, str] = {}
    for line in cp.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            props[k] = v
    load = props.get("LoadState", "")
    st.exists = load not in {"not-found", "error", ""}
    st.active = props.get("ActiveState") or "unknown"
    st.sub = props.get("SubState") or ""
    st.result = props.get("Result") or ""
    st.exec_main_code = props.get("ExecMainCode") or ""
    st.exec_main_status = props.get("ExecMainStatus") or ""
    try:
        st.nrestarts = int(props.get("NRestarts") or 0)
    except ValueError:
        st.nrestarts = 0
    try:
        st.main_pid = int(props.get("MainPID") or 0)
    except ValueError:
        st.main_pid = 0
    st.description = props.get("Description") or ""
    if load == "not-found":
        st.exists = False
        st.active = "not-found"
    return st


def probe_tcp(spec: str, timeout: float = 1.5) -> bool:
    if ":" not in spec:
        return False
    host, _, port_s = spec.rpartition(":")
    try:
        port = int(port_s)
    except ValueError:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def format_unit_line(svc: WatchService, st: UnitStatus, hung: Optional[bool] = None) -> str:
    if not st.exists:
        mark = "不可用"
    elif hung:
        mark = "假活"
    elif st.is_active:
        mark = "active"
    else:
        mark = st.active
    return f"  {escape(svc.name)}（{escape(svc.unit)}）：{escape(mark)}"


def format_unit_detail(svc: WatchService, st: UnitStatus, probe_ok: Optional[bool]) -> str:
    lines = [
        f"<b>{escape(svc.name)}</b>",
        f"unit：<code>{escape(svc.unit)}</code>",
    ]
    if not st.exists:
        lines.append("状态：本机不存在该 unit")
        return "\n".join(lines)
    lines.append(f"ActiveState：{escape(st.active)} / {escape(st.sub)}")
    lines.append(f"Result：{escape(st.result or '—')}")
    lines.append(f"ExecMainCode：{escape(st.exec_main_code or '—')}")
    lines.append(f"ExecMainStatus：{escape(st.exec_main_status or '—')}")
    lines.append(f"NRestarts：{st.nrestarts}")
    if st.main_pid:
        lines.append(f"MainPID：{st.main_pid}")
    if svc.probe:
        if probe_ok is None:
            probe_s = "未探测"
        elif probe_ok:
            probe_s = f"通 {svc.probe}"
        else:
            probe_s = f"不通 {svc.probe}"
        lines.append(f"探测：{escape(probe_s)}")
    return "\n".join(lines)


def read_journal(
    units: list[str],
    *,
    since: Optional[str] = None,
    until: Optional[str] = None,
    priority: Optional[str] = None,
    lines: int = 80,
    output: str = "short-iso",
) -> tuple[str, Optional[str]]:
    """Return (text, error_reason). Only named units; no arbitrary expressions."""
    safe: list[str] = []
    for u in units:
        if u in {"dutybot", "dutybot.service"}:
            safe.append("dutybot.service")
            continue
        if u in {"ssh", "sshd", "ssh.service", "sshd.service"}:
            safe.append(u if u.endswith(".service") else f"{u}.service")
            continue
        if validate_unit_name(u):
            safe.append(u)
    if not safe:
        return "", "没有允许查询的 unit"
    args = ["journalctl", "--no-pager", f"--output={output}", f"-n{max(1, min(int(lines), 500))}"]
    for u in safe:
        args.extend(["-u", u])
    if since:
        if since not in {"now", "today", "yesterday"} and not _ISO_RE.match(since) and not since.startswith("-"):
            return "", "时间格式无效"
        if since.startswith("-") and not re.fullmatch(r"-\d+[smhd]", since):
            return "", "时间格式无效"
        args.append(f"--since={since}")
    if until:
        if not _ISO_RE.match(until) and until not in {"today", "yesterday"}:
            return "", "时间格式无效"
        args.append(f"--until={until}")
    if priority not in (None, "", "all"):
        if not re.fullmatch(r"[0-7]", str(priority)):
            return "", "优先级无效"
        args.extend(["-p", str(priority)])
    try:
        cp = _run(args, timeout=20)
    except FileNotFoundError:
        return "", "journalctl 不可用"
    except subprocess.TimeoutExpired:
        return "", "journalctl 超时"
    text = (cp.stdout or "").strip()
    if cp.returncode != 0 and not text:
        err = (cp.stderr or "").strip() or "journalctl 失败"
        return "", err
    if not text:
        return "（无日志）", None
    if len(text) > 3500:
        text = text[-3500:]
        text = "…\n" + text.split("\n", 1)[-1]
    return text, None


def ssh_journal_available() -> tuple[bool, str]:
    text, err = read_journal(list(JOURNAL_UNITS_SSH), lines=5, since="-7d")
    if err and "不可用" in err:
        return False, "journalctl 不可用"
    # Empty is still "available" if journalctl ran.
    if err:
        return False, f"journal 没有 ssh/sshd：{err}"
    return True, ""


def dutyctl(*args: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    cmd = ["sudo", "-n", str(dutyctl_path()), *args]
    log.info("dutyctl %s", " ".join(args))
    return _run(cmd, timeout=timeout)



async def dutyctl_async(*args: str, timeout: float = 30) -> tuple[int, str, str]:
    """sudo -n dutyctl via create_subprocess_exec (no shell)."""
    cmd = ["sudo", "-n", str(dutyctl_path()), *args]
    log.info("dutyctl %s", " ".join(args))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", "timeout"
    out = (out_b or b"").decode("utf-8", "replace")
    err = (err_b or b"").decode("utf-8", "replace")
    return int(proc.returncode or 0), out, err


def restart_unit(unit: str) -> tuple[bool, str]:
    if not validate_unit_name(unit):
        return False, "unit 名无效"
    cp = dutyctl("restart-unit", unit)
    if cp.returncode == 0:
        return True, (cp.stdout or "").strip() or "已发出重启"
    err = (cp.stderr or cp.stdout or "").strip() or f"exit {cp.returncode}"
    return False, err


def kill_pids(token: str, pids: list[int]) -> tuple[bool, str]:
    if not token or not pids:
        return False, "缺少 token 或 pid"
    args = ["kill-pids", token, *[str(int(p)) for p in pids]]
    cp = dutyctl(*args)
    if cp.returncode == 0:
        return True, (cp.stdout or "").strip() or "已发出终止"
    err = (cp.stderr or cp.stdout or "").strip() or f"exit {cp.returncode}"
    return False, err


def reboot_host() -> tuple[bool, str]:
    cp = dutyctl("reboot")
    if cp.returncode == 0:
        return True, (cp.stdout or "").strip() or "已安排约 1 分钟后重启"
    err = (cp.stderr or cp.stdout or "").strip() or f"exit {cp.returncode}"
    return False, err


def cgroup_usage_usec(unit: str) -> Optional[int]:
    if not validate_unit_name(unit):
        return None
    candidates = [
        Path(f"/sys/fs/cgroup/system.slice/{unit}/cpu.stat"),
        Path(f"/sys/fs/cgroup/unified/system.slice/{unit}/cpu.stat"),
    ]
    for p in candidates:
        if not p.is_file():
            continue
        try:
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("usage_usec"):
                    return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            continue
    return None


@dataclass
class ServiceSnapshot:
    at: float = field(default_factory=time.time)
    units: dict[str, UnitStatus] = field(default_factory=dict)
    probes: dict[str, Optional[bool]] = field(default_factory=dict)


def snapshot(watch: WatchList) -> ServiceSnapshot:
    snap = ServiceSnapshot()
    for svc in watch.services:
        st = systemctl_show(svc.unit)
        snap.units[svc.unit] = st
        if svc.probe and st.is_active:
            snap.probes[svc.unit] = probe_tcp(svc.probe)
        elif svc.probe:
            snap.probes[svc.unit] = None
        else:
            snap.probes[svc.unit] = None
    return snap


SHOW_KEYS = (
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
    "NRestarts",
    "MainPID",
    "Id",
)


def show(unit: str) -> dict[str, str]:
    """systemctl show selected properties as a dict. argv list, no shell."""
    out = {k: "" for k in SHOW_KEYS}
    if not validate_unit_name(unit):
        return out
    try:
        cp = _run(
            [
                "systemctl",
                "show",
                "-p",
                ",".join(SHOW_KEYS),
                "--value",
                "--",
                unit,
            ]
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return out
    values = (cp.stdout or "").splitlines()
    for i, key in enumerate(SHOW_KEYS):
        if i < len(values):
            out[key] = values[i]
    return out


def logs(unit: str, n: int = 40) -> str:
    text, err = read_journal([unit], lines=n, output="short-iso")
    if err:
        return f"（{err}）"
    return text


def format_service_status(svc: WatchService, show_dict: dict) -> str:
    active = show_dict.get("ActiveState") or "unknown"
    sub = show_dict.get("SubState") or ""
    result = show_dict.get("Result") or "—"
    code = show_dict.get("ExecMainCode") or "—"
    status_n = show_dict.get("ExecMainStatus") or "—"
    nrestarts = show_dict.get("NRestarts") or "0"
    main_pid = show_dict.get("MainPID") or "0"
    lines = [
        f"<b>{escape(svc.name)}</b>",
        f"unit：<code>{escape(svc.unit)}</code>",
        f"ActiveState：{escape(active)} / {escape(sub)}",
        f"Result：{escape(result)}",
        f"退出：code={escape(code)} status={escape(status_n)}",
        f"NRestarts：{escape(nrestarts)}",
    ]
    if str(main_pid) not in {"", "0"}:
        lines.append(f"MainPID：{escape(main_pid)}")
    if svc.probe:
        ok = probe_tcp(svc.probe, timeout=1.5)
        lines.append(f"探测：{'通' if ok else '不通'} {escape(svc.probe)}")
    return "\n".join(lines)


class ExpectedRestarts:
    """Menu restart must not broadcast as unexpected. Window = 120s."""

    WINDOW = 120.0
    _until: dict[str, float] = {}

    @classmethod
    def mark(cls, unit: str) -> None:
        cls._until[unit] = time.monotonic() + cls.WINDOW

    @classmethod
    def is_expected(cls, unit: str) -> bool:
        until = cls._until.get(unit)
        if until is None:
            return False
        if time.monotonic() >= until:
            cls._until.pop(unit, None)
            return False
        return True
