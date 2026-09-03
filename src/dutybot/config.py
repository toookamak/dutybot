"""Load env, watch list, and thresholds. No secrets in defaults."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

# Production paths. Tests/dev override via environment.
DEFAULT_ENV_PATH = Path("/etc/dutybot/env")
DEFAULT_STATE_DIR = Path("/var/lib/dutybot")
DEFAULT_DUTYCTL = Path("/usr/lib/dutybot/dutyctl")

UNIT_RE = re.compile(r"^[A-Za-z0-9:_.\\-]+\.service$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
PROBE_RE = re.compile(r"^(?P<host>[A-Za-z0-9._\-]+):(?P<port>\d{1,5})$")

HASH_PREFIX = "pbkdf2_sha256"
HASH_ITERATIONS = 260000

# Resource / alert defaults (README).
DEFAULT_THRESHOLDS: dict[str, Any] = {
    "root_disk_avail_pct": 10,
    "root_disk_avail_bytes": 1 * 1024 * 1024 * 1024,  # 1 GiB
    "cpu_unexpected_pct": 60,
    "cpu_unexpected_sec": 120,
    "cpu_saturation_pct": 95,
    "cpu_saturation_sec": 180,
    "disk_mbps": 80,
    "disk_sec": 120,
    "net_mbps": 80,
    "net_sec": 120,
    "cooldown_resource_sec": 15 * 60,
    "cooldown_service_sec": 5 * 60,
    "cooldown_ssh_fail_sec": 10 * 60,
    "poll_sec": 10,
}


def env_path() -> Path:
    return Path(os.environ.get("DUTYBOT_ENV", str(DEFAULT_ENV_PATH)))


def state_dir() -> Path:
    return Path(os.environ.get("DUTYBOT_STATE_DIR", str(DEFAULT_STATE_DIR)))


def watch_path() -> Path:
    override = os.environ.get("DUTYBOT_WATCH_JSON")
    if override:
        return Path(override)
    return state_dir() / "watch.json"


def preview_path() -> Path:
    override = os.environ.get("DUTYBOT_PREVIEW_FILE")
    if override:
        return Path(override)
    return state_dir() / "kill-preview.json"


def dutyctl_path() -> Path:
    return Path(os.environ.get("DUTYBOT_DUTYCTL", str(DEFAULT_DUTYCTL)))


def parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    text = path.read_text(encoding="utf-8", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        data[key] = value
    return data


def hash_password(password: str, *, iterations: int = HASH_ITERATIONS) -> str:
    if not password:
        raise ValueError("empty password")
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join(
        [
            HASH_PREFIX,
            str(iterations),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(dk).decode("ascii"),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    if not stored or not password:
        return False
    try:
        algo, iter_s, salt_b64, hash_b64 = stored.split("$", 3)
        if algo != HASH_PREFIX:
            return False
        iterations = int(iter_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


WILDCARD_BINDS = {"0.0.0.0", "::", "[::]", "*"}


def sanitize_web_bind(bind: str) -> str:
    """Never listen on all interfaces. Unset/wildcard -> 127.0.0.1."""
    b = (bind or "").strip() or "127.0.0.1"
    if b in WILDCARD_BINDS:
        return "127.0.0.1"
    return b


def telegram_complete(token: Optional[str], chat_id: Optional[str]) -> bool:
    return bool((token or "").strip() and (chat_id or "").strip())


def web_complete(user: Optional[str], password_hash: Optional[str]) -> bool:
    return bool((user or "").strip() and (password_hash or "").strip())


def any_channel_complete(
    token: Optional[str],
    chat_id: Optional[str],
    user: Optional[str],
    password_hash: Optional[str],
) -> bool:
    return telegram_complete(token, chat_id) or web_complete(user, password_hash)


def validate_unit_name(unit: str) -> bool:
    return bool(UNIT_RE.fullmatch(unit or ""))


def validate_service_id(sid: str) -> bool:
    return bool(ID_RE.fullmatch(sid or ""))


def parse_probe(probe: Any) -> Optional[str]:
    if probe is None:
        return None
    if isinstance(probe, str):
        text = probe.strip()
        if not text or text.lower() in {"null", "none", "-"}:
            return None
        m = PROBE_RE.fullmatch(text)
        if not m:
            raise ValueError(f"invalid probe: {probe!r}")
        port = int(m.group("port"))
        if port < 1 or port > 65535:
            raise ValueError(f"invalid probe port: {port}")
        return f"{m.group('host')}:{port}"
    raise ValueError(f"invalid probe: {probe!r}")


def slug_id(unit: str) -> str:
    base = unit
    if base.endswith(".service"):
        base = base[: -len(".service")]
    slug = re.sub(r"[^a-z0-9_-]+", "-", base.lower()).strip("-")
    return slug or "svc"


@dataclass
class WatchService:
    id: str
    name: str
    unit: str
    probe: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "unit": self.unit,
            "probe": self.probe,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WatchService":
        unit = str(raw.get("unit") or "").strip()
        if not validate_unit_name(unit):
            raise ValueError(f"invalid unit: {unit!r}")
        sid = str(raw.get("id") or slug_id(unit)).strip()
        if not validate_service_id(sid):
            sid = slug_id(unit)
        name = str(raw.get("name") or sid).strip() or sid
        probe = parse_probe(raw.get("probe"))
        return cls(id=sid, name=name, unit=unit, probe=probe)


@dataclass
class WatchList:
    services: list[WatchService] = field(default_factory=list)
    thresholds: dict[str, Any] = field(default_factory=dict)
    path: Optional[Path] = None

    def units(self) -> list[str]:
        return [s.unit for s in self.services]

    def by_id(self, sid: str) -> Optional[WatchService]:
        for s in self.services:
            if s.id == sid:
                return s
        return None

    def by_unit(self, unit: str) -> Optional[WatchService]:
        for s in self.services:
            if s.unit == unit:
                return s
        return None

    def has_unit(self, unit: str) -> bool:
        return self.by_unit(unit) is not None

    def add(self, svc: WatchService) -> None:
        if self.by_id(svc.id) or self.by_unit(svc.unit):
            raise ValueError("duplicate id or unit")
        self.services.append(svc)

    def remove_id(self, sid: str) -> Optional[WatchService]:
        for i, s in enumerate(self.services):
            if s.id == sid:
                return self.services.pop(i)
        return None

    def replace(self, svc: WatchService) -> None:
        for i, s in enumerate(self.services):
            if s.id == svc.id:
                self.services[i] = svc
                return
        self.services.append(svc)

    def merged_thresholds(self) -> dict[str, Any]:
        out = dict(DEFAULT_THRESHOLDS)
        for k, v in (self.thresholds or {}).items():
            if k in DEFAULT_THRESHOLDS and v is not None and v != "":
                try:
                    if isinstance(DEFAULT_THRESHOLDS[k], float):
                        out[k] = float(v)
                    elif isinstance(DEFAULT_THRESHOLDS[k], int):
                        out[k] = int(v)
                    else:
                        out[k] = v
                except (TypeError, ValueError):
                    continue
        return out

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "services": [s.to_dict() for s in self.services],
        }
        if self.thresholds:
            data["thresholds"] = self.thresholds
        return data

    def save(self, path: Optional[Path] = None) -> None:
        dest = path or self.path or watch_path()
        dest.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(prefix="watch.", suffix=".tmp", dir=str(dest.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, dest)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.path = dest

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "WatchList":
        dest = path or watch_path()
        if not dest.is_file():
            return cls(services=[], thresholds={}, path=dest)
        raw = json.loads(dest.read_text(encoding="utf-8"))
        return cls.from_dict(raw, path=dest)

    @classmethod
    def from_dict(cls, raw: Any, path: Optional[Path] = None) -> "WatchList":
        if not isinstance(raw, dict):
            raise ValueError("watch.json must be an object")
        services: list[WatchService] = []
        for item in raw.get("services") or []:
            if not isinstance(item, dict):
                continue
            services.append(WatchService.from_dict(item))
        thresholds = raw.get("thresholds") or {}
        if not isinstance(thresholds, dict):
            thresholds = {}
        return cls(services=services, thresholds=thresholds, path=path)

    def empty_default(self) -> bool:
        return not self.services


@dataclass
class Config:
    bot_token: str = ""
    allowed_chat_id: str = ""
    web_user: str = ""
    web_password_hash: str = ""
    web_bind: str = "127.0.0.1"
    web_port: int = 8787
    env_file: Path = field(default_factory=env_path)
    state: Path = field(default_factory=state_dir)
    watch: WatchList = field(default_factory=WatchList)

    @property
    def telegram_ok(self) -> bool:
        return telegram_complete(self.bot_token, self.allowed_chat_id)

    @property
    def web_ok(self) -> bool:
        return web_complete(self.web_user, self.web_password_hash)

    @property
    def any_channel(self) -> bool:
        return self.telegram_ok or self.web_ok

    @property
    def chat_id_int(self) -> int:
        return int(self.allowed_chat_id)

    @property
    def thresholds(self) -> dict[str, Any]:
        return self.watch.merged_thresholds()

    def reload_watch(self) -> WatchList:
        self.watch = WatchList.load(watch_path())
        return self.watch


def load_config(
    env_file: Optional[Path] = None,
    *,
    require_channel: bool = False,
) -> Config:
    path = env_file or env_path()
    file_vals = parse_env_file(path)

    def pick(key: str, default: str = "") -> str:
        if os.environ.get(key):
            return os.environ[key]
        return file_vals.get(key, default)

    web_port_s = pick("WEB_PORT", "8787") or "8787"
    try:
        web_port = int(web_port_s)
    except ValueError:
        web_port = 8787
    if web_port < 1 or web_port > 65535:
        web_port = 8787

    cfg = Config(
        bot_token=pick("BOT_TOKEN"),
        allowed_chat_id=pick("ALLOWED_CHAT_ID"),
        web_user=pick("WEB_USER"),
        web_password_hash=pick("WEB_PASSWORD_HASH"),
        web_bind=sanitize_web_bind(pick("WEB_BIND", "127.0.0.1")),
        web_port=web_port,
        env_file=path,
        state=state_dir(),
        watch=WatchList.load(watch_path()),
    )
    if require_channel and not cfg.any_channel:
        raise SystemExit(
            "dutybot refuses to start: neither Telegram (BOT_TOKEN+ALLOWED_CHAT_ID) "
            "nor Web (WEB_USER+WEB_PASSWORD_HASH) is complete"
        )
    return cfg
