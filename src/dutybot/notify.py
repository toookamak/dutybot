"""Telegram sender and alert cooldown book."""

from __future__ import annotations

import html
import logging
import os
import socket
import time
from typing import Callable, Optional

log = logging.getLogger("dutybot.notify")


class CooldownBook:
    """Track last-sent timestamps. Injectable clock for tests."""

    def __init__(self, now: Optional[Callable[[], float]] = None) -> None:
        self._now = now or time.monotonic
        self._last: dict[str, float] = {}

    def last(self, key: str) -> Optional[float]:
        return self._last.get(key)

    def remaining(self, key: str, interval: float) -> float:
        last = self._last.get(key)
        if last is None:
            return 0.0
        left = interval - (self._now() - last)
        return left if left > 0 else 0.0

    def allow(self, key: str, interval: float) -> bool:
        """Return True and record if interval has elapsed (or first time)."""
        if interval <= 0:
            self._last[key] = self._now()
            return True
        last = self._last.get(key)
        t = self._now()
        if last is not None and (t - last) < interval:
            return False
        self._last[key] = t
        return True

    def peek(self, key: str, interval: float) -> bool:
        last = self._last.get(key)
        if last is None:
            return True
        return (self._now() - last) >= interval

    def mark(self, key: str) -> None:
        self._last[key] = self._now()

    def clear(self, key: str) -> None:
        self._last.pop(key, None)


def escape(text: object) -> str:
    return html.escape(str(text), quote=False)


class Notifier:
    def __init__(
        self,
        bot: object | None,
        chat_id: Optional[int],
        cooldowns: Optional[CooldownBook] = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self.cooldowns = cooldowns or CooldownBook()

    @property
    def enabled(self) -> bool:
        return self._bot is not None and self._chat_id is not None

    def bind_bot(self, bot: object, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id

    async def send(
        self,
        text: str,
        *,
        key: Optional[str] = None,
        cooldown: Optional[float] = None,
        force: bool = False,
    ) -> bool:
        if key is not None and not force:
            interval = 0.0 if cooldown is None else float(cooldown)
            if not self.cooldowns.allow(key, interval):
                log.debug("suppressed by cooldown: %s", key)
                return False
        if not self.enabled:
            log.info("notify (no telegram): %s", text.splitlines()[0] if text else "")
            return False
        try:
            await self._bot.send_message(  # type: ignore[union-attr]
                chat_id=self._chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            log.info("notify sent: %s", text.splitlines()[0] if text else "")
            return True
        except Exception:
            log.exception("telegram send failed")
            return False


def _sd_send(payload: str) -> None:
    """UNIX datagram to $NOTIFY_SOCKET (READY=1 / WATCHDOG=1 / STATUS=)."""
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            addr: str | bytes
            if path.startswith(
"@"):
                addr = "\0" + path[1:]
            else:
                addr = path
            sock.sendto(payload.encode("utf-8"), addr)
        finally:
            sock.close()
    except OSError:
        log.debug("sd_notify failed", exc_info=True)


def notify_ready() -> None:
    _sd_send("READY=1")


def notify_watchdog() -> None:
    _sd_send("WATCHDOG=1")


def notify_status(msg: str) -> None:
    safe = str(msg).replace("\n", " ").replace("\r", " ")[:256]
    _sd_send(f"STATUS={safe}")
