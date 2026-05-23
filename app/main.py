"""Entry point: run FastAPI + Telegram bot in a single process."""
from __future__ import annotations

import asyncio
import logging
import signal
import time

import uvicorn

from .api import build_app
from .bot import build_application
from .config import load_settings
from .db import DB

log = logging.getLogger("license-server")


async def _retention_loop(db: DB, retention_days: int) -> None:
    """Periodically purge old events. Runs at boot, then every 24h."""
    if retention_days <= 0:
        return
    while True:
        cutoff = int(time.time()) - retention_days * 86400
        try:
            n = await db.purge_events(cutoff)
            if n:
                log.info("Purged %d old events (> %d days)", n, retention_days)
        except Exception as exc:  # noqa: BLE001
            log.warning("Purge failed: %s", exc)
        await asyncio.sleep(24 * 3600)


async def _run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    settings = load_settings()
    db = DB(settings.db_path)
    await db.init()

    # One-time cleanup: remove leftover greeting throttle rows from older versions.
    try:
        removed = await db.kv_delete_prefix("greet:")
        if removed:
            log.info("Removed %d stale greet:* rows from kv", removed)
    except Exception as exc:  # noqa: BLE001
        log.warning("kv cleanup skipped: %s", exc)

    bot_app, notifier = build_application(settings, db)
    api = build_app(settings, db, notifier)

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)
    log.info("Telegram bot polling started")

    purge_task = asyncio.create_task(
        _retention_loop(db, settings.event_retention_days), name="retention"
    )

    config = uvicorn.Config(
        api, host=settings.api_host, port=settings.api_port,
        log_level="info", access_log=False,
    )
    server = uvicorn.Server(config)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop(*_a):  # type: ignore[no-untyped-def]
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            pass

    server_task = asyncio.create_task(server.serve(), name="uvicorn")
    stop_task = asyncio.create_task(stop_event.wait(), name="stop")

    try:
        await asyncio.wait(
            {server_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        log.info("Shutting down...")
        server.should_exit = True
        purge_task.cancel()
        try:
            await bot_app.updater.stop()
        except Exception:  # noqa: BLE001
            pass
        await bot_app.stop()
        await bot_app.shutdown()
        if not server_task.done():
            await server_task


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
