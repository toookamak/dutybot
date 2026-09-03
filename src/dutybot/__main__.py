"""Entry point: refuse to start without a complete channel; run bot + web + monitor."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from dutybot.config import load_config
from dutybot.monitor import Monitor
from dutybot.notify import Notifier, notify_ready, notify_status, notify_watchdog

log = logging.getLogger("dutybot")


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)


def _watchdog_interval() -> float:
    raw = os.environ.get("WATCHDOG_USEC")
    if raw:
        try:
            return max(1.0, int(raw) / 1e6 / 2.0)
        except ValueError:
            pass
    return 15.0


async def _watchdog_loop() -> None:
    interval = _watchdog_interval()
    while True:
        notify_watchdog()
        await asyncio.sleep(interval)


async def amain() -> None:
    setup_logging()
    cfg = load_config(require_channel=True)
    cfg.state.mkdir(parents=True, exist_ok=True)
    log.info(
        "starting dutybot telegram=%s web=%s bind=%s:%s watch=%s",
        cfg.telegram_ok,
        cfg.web_ok,
        cfg.web_bind if cfg.web_ok else "-",
        cfg.web_port if cfg.web_ok else "-",
        len(cfg.watch.services),
    )

    chat_id = int(cfg.allowed_chat_id) if cfg.telegram_ok else None
    notifier = Notifier(None, chat_id)
    monitor = Monitor(cfg, notifier)
    tasks: list[asyncio.Task] = []

    tg_app = None
    web_runner = None

    if cfg.telegram_ok:
        from dutybot.bot import build_application

        tg_app = build_application(cfg, notifier)
        await tg_app.initialize()
        await tg_app.start()
        if tg_app.updater:
            await tg_app.updater.start_polling(drop_pending_updates=True)
        notifier.bind_bot(tg_app.bot, cfg.chat_id_int)
        log.info("Telegram polling started")
    else:
        log.info("Telegram 未完整配置，菜单与主动通知关闭")

    if cfg.web_ok:
        from dutybot.web import start_web

        web_runner = await start_web(cfg)
    else:
        log.info("WEB_USER 未配置，不监听 HTTP")

    # Recovered notify must run after bind_bot, or the first card is dropped.
    notify_ready()
    notify_status("running")
    tasks.append(asyncio.create_task(monitor.run(), name="monitor"))
    tasks.append(asyncio.create_task(_watchdog_loop(), name="watchdog"))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    await stop.wait()
    log.info("shutting down")
    notify_status("stopping")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if tg_app:
        if tg_app.updater:
            await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
    if web_runner:
        await web_runner.cleanup()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
