"""Push important event notifications to Telegram admins.

Used by the API after it has logged an event to the DB. Safe to call
fire-and-forget; a failed send must never break the request.
"""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any, Iterable

from telegram import Bot
from telegram.constants import ParseMode

from .db import DB

log = logging.getLogger("notifier")

# Statuses that warrant admin attention.
ALERT_STATUSES = {
    "machine_limit_reached",
    "revoked",
    "expired",
    "product_mismatch",
    "machine_not_activated",
    "not_found",
}


def _esc(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return html.escape(str(value), quote=False)


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
        # Always send successful activations (useful for billing/insight),
        # otherwise only alerts.
        is_alert = status in ALERT_STATUSES
        is_activate_ok = event == "activate" and status in ("ok", "activated")
        if not (is_alert or is_activate_ok):
            return
        if await self.muted():
            return

        emoji = "🚨" if is_alert else "✅"
        text = (
            f"{emoji} <b>{_esc(event)}</b> · <code>{_esc(status)}</code>\n"
            "<blockquote>"
            f"<b>Key:</b>     <code>{_esc(license_key)}</code>\n"
            f"<b>Machine:</b> <code>{_esc(machine_id)}</code>\n"
            f"<b>IP:</b>      <code>{_esc(ip)}</code>"
            "</blockquote>"
        )
        for chat_id in self.admin_ids:
            try:
                await self.bot.send_message(
                    chat_id=chat_id, text=text,
                    parse_mode=ParseMode.HTML,
                    disable_notification=not is_alert,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Telegram notify failed for %s: %s", chat_id, exc)

    def fire(self, **kwargs) -> None:
        """Fire-and-forget version. Safe to call from API handlers."""
        asyncio.create_task(self.notify(**kwargs))
