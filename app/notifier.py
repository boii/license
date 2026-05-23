"""Push notifikasi event penting ke admin Telegram.

Dipakai oleh API setelah berhasil mencatat event ke DB. Aman dipanggil
'fire-and-forget'; gagal kirim tidak boleh menggagalkan request.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from telegram import Bot
from telegram.constants import ParseMode

from .db import DB

log = logging.getLogger("notifier")

# Status yang dianggap menarik perhatian admin.
ALERT_STATUSES = {
    "machine_limit_reached",
    "revoked",
    "expired",
    "product_mismatch",
    "machine_not_activated",
    "not_found",
}


class Notifier:
    def __init__(self, bot: Bot, db: DB, admin_ids: Iterable[int]):
        self.bot = bot
        self.db = db
        self.admin_ids = list(admin_ids)

    async def muted(self) -> bool:
        return (await self.db.kv_get("notify_muted")) == "1"

    async def set_muted(self, value: bool) -> None:
        await self.db.kv_set("notify_muted", "1" if value else "0")

    async def notify(
        self,
        *,
        event: str,
        status: str,
        license_key: str | None,
        machine_id: str | None,
        ip: str | None,
    ) -> None:
        # Selalu kirim event activate sukses (penting untuk billing/insight),
        # selain itu hanya alert.
        is_alert = status in ALERT_STATUSES
        is_activate_ok = event == "activate" and status in ("ok", "activated")
        if not (is_alert or is_activate_ok):
            return
        if await self.muted():
            return

        emoji = "🚨" if is_alert else "✅"
        text = (
            f"{emoji} *{event}* · `{status}`\n"
            f"key: `{license_key or '-'}`\n"
            f"machine: `{machine_id or '-'}`\n"
            f"ip: `{ip or '-'}`"
        )
        for chat_id in self.admin_ids:
            try:
                await self.bot.send_message(
                    chat_id=chat_id, text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_notification=not is_alert,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Telegram notify gagal ke %s: %s", chat_id, exc)

    def fire(self, **kwargs) -> None:
        """Versi fire-and-forget. Aman dipanggil dari handler API."""
        asyncio.create_task(self.notify(**kwargs))
