"""Telegram bot for license management.

Commands:
    /start                              - help
    /new [product] [days] [machines]    - create license
                                          (defaults: default 0 1, days 0 = lifetime)
    /list [n]                           - last n licenses (default 10)
    /info <KEY>                         - license details + activations + log summary
    /revoke <KEY>                       - disable license
    /unrevoke <KEY>                     - re-enable license
    /extend <KEY> <days>                - extend validity (days, 0 = lifetime)
    /seats <KEY> <n>                    - change max_machines
    /reset <KEY>                        - clear all activations
    /reset <KEY> <machine_id>           - clear one activation
    /delete <KEY>                       - delete license permanently

    /log [n]                            - last n global events (default 20)
    /log <KEY> [n]                      - last n events for one license
    /errors [n]                         - last n failed events (status != ok/activated)
    /stats <KEY> [days]                 - usage stats (default 7 days, 0 = all-time)
    /mute  /unmute                      - toggle event push notifications
"""
from __future__ import annotations

import html
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from .config import Settings
from .db import DB
from .notifier import Notifier


HELP_TEXT = (
    "<b>License Gabot</b>\n"
    "<i>License server management over Telegram.</i>\n"
    "\n"
    "<b>📜 License management</b>\n"
    "<blockquote>"
    "<code>/new [product] [days] [machines]</code> — create (days 0 = lifetime)\n"
    "<code>/list [n]</code> — recent licenses\n"
    "<code>/info &lt;KEY&gt;</code> — details + activations\n"
    "<code>/revoke &lt;KEY&gt;</code> · <code>/unrevoke &lt;KEY&gt;</code>\n"
    "<code>/extend &lt;KEY&gt; &lt;days&gt;</code> — extend (0 = lifetime)\n"
    "<code>/seats &lt;KEY&gt; &lt;n&gt;</code> — set machine limit\n"
    "<code>/reset &lt;KEY&gt; [machine_id]</code> — clear activation(s)\n"
    "<code>/delete &lt;KEY&gt;</code> — delete permanently"
    "</blockquote>\n"
    "<b>📊 Usage logs</b>\n"
    "<blockquote>"
    "<code>/log [n]</code> or <code>/log &lt;KEY&gt; [n]</code>\n"
    "<code>/errors [n]</code> — failed events only\n"
    "<code>/stats &lt;KEY&gt; [days]</code> — summary (0 = all-time)\n"
    "<code>/mute</code> · <code>/unmute</code> — push notifications"
    "</blockquote>"
)

OK_STATUSES = {"ok", "activated", "deactivated"}


def _esc(value: Any) -> str:
    """HTML-escape a dynamic value for safe rendering in Telegram HTML mode."""
    if value is None or value == "":
        return "-"
    return html.escape(str(value), quote=False)


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "lifetime"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_short_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")


def _fmt_license(lic: dict) -> str:
    return (
        f"<b>License</b> <code>{_esc(lic['key'])}</code>\n"
        "<blockquote>"
        f"<b>Product:</b>  <code>{_esc(lic['product'])}</code>\n"
        f"<b>Status:</b>   <b>{_esc(lic['status'])}</b>\n"
        f"<b>Seats:</b>    {lic.get('activations', '?')} / {lic['max_machines']}\n"
        f"<b>Expires:</b>  {_esc(_fmt_ts(lic['expires_at']))}\n"
        f"<b>Owner:</b>    {_esc(lic.get('owner'))}\n"
        f"<b>Created:</b>  {_esc(_fmt_ts(lic['created_at']))}"
        "</blockquote>"
    )


def _fmt_event(e: dict) -> str:
    icon = "✅" if e["status"] in OK_STATUSES else "⚠️"
    key = e["license_key"] or "-"
    short_key = key.split("-")[0] if key != "-" else "-"
    machine = (e["machine_id"] or "-")[:12]
    return (
        f"{icon} <code>{_esc(_fmt_short_ts(e['created_at']))}</code> "
        f"<code>{_esc(e['event'])}/{_esc(e['status'])}</code>\n"
        f"     <i>key</i> <code>{_esc(short_key)}</code> · "
        f"<i>m</i> <code>{_esc(machine)}</code> · "
        f"<i>ip</i> <code>{_esc(e['ip'])}</code>"
    )


Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def _admin_only(admin_ids: set[int]) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        @wraps(fn)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            user = update.effective_user
            if not user or user.id not in admin_ids:
                if update.effective_message:
                    uid = user.id if user else "?"
                    await update.effective_message.reply_text(
                        f"🚫 <b>Access denied</b>\nYour Telegram ID: <code>{uid}</code>",
                        parse_mode=ParseMode.HTML,
                    )
                return
            await fn(update, context)
        return wrapper
    return deco


def build_application(settings: Settings, db: DB) -> tuple[Application, Notifier]:
    """Build the Telegram Application + Notifier sharing one Bot instance.

    Returns (application, notifier).
    """
    app = Application.builder().token(settings.bot_token).build()
    notifier = Notifier(app.bot, db, settings.admin_ids)
    admin = _admin_only(settings.admin_ids)

    async def reply(update: Update, text: str) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                text, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )

    @admin
    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await reply(update, HELP_TEXT)

    @admin
    async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        args = ctx.args or []
        product = args[0] if len(args) >= 1 else "default"
        try:
            days = int(args[1]) if len(args) >= 2 else 0
            machines = int(args[2]) if len(args) >= 3 else 1
        except ValueError:
            await reply(update, "<b>Usage:</b> <code>/new [product] [days] [machines]</code>")
            return
        expires_at = int(time.time()) + days * 86400 if days > 0 else None
        lic = await db.create_license(
            product=product, max_machines=machines, expires_at=expires_at,
            created_by=update.effective_user.id if update.effective_user else None,
        )
        await reply(update, "✅ <b>License created</b>\n\n" + _fmt_license(lic))

    @admin
    async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            n = int(ctx.args[0]) if ctx.args else 10
        except ValueError:
            n = 10
        rows = await db.list_licenses(limit=max(1, min(n, 50)))
        if not rows:
            await reply(update, "<i>No licenses yet.</i>")
            return
        lines = [f"<b>📜 Recent licenses</b> <i>({len(rows)})</i>", "<blockquote>"]
        for r in rows:
            lines.append(
                f"<code>{_esc(r['key'])}</code>\n"
                f"  {_esc(r['product'])} · <b>{_esc(r['status'])}</b> · "
                f"{r['max_machines']} seat · {_esc(_fmt_ts(r['expires_at']))}"
            )
        lines.append("</blockquote>")
        await reply(update, "\n".join(lines))

    async def _need_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str | None:
        if not ctx.args:
            await reply(update, "Provide a key, e.g. <code>/info ABCDE-12345-...</code>")
            return None
        return ctx.args[0]

    @admin
    async def cmd_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = await _need_key(update, ctx)
        if not key:
            return
        lic = await db.get_license(key)
        if not lic:
            await reply(update, "<i>Not found.</i>")
            return
        text = _fmt_license(lic)
        acts = await db.list_activations(key)
        if acts:
            text += "\n<b>💻 Machines</b>\n<blockquote>"
            parts = []
            for a in acts:
                parts.append(
                    f"<code>{_esc(a['machine_id'])}</code>\n"
                    f"  <i>last seen</i> {_esc(_fmt_ts(a['last_seen']))}"
                )
            text += "\n".join(parts) + "</blockquote>"
        # last 7 days
        since = int(time.time()) - 7 * 86400
        s = await db.event_stats(key, since=since)
        text += (
            "\n<b>📈 Last 7 days</b>\n<blockquote>"
            f"<b>Events:</b>          {s['total']}\n"
            f"<b>Unique machines:</b> {s['distinct_machines']}"
        )
        if s["last_seen"]:
            text += f"\n<b>Last call:</b>       {_esc(_fmt_ts(s['last_seen']))}"
        text += "</blockquote>"
        await reply(update, text)

    @admin
    async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = await _need_key(update, ctx)
        if not key:
            return
        ok = await db.set_status(key, "revoked")
        await reply(update, "✅ <b>Revoked</b>" if ok else "<i>Not found.</i>")

    @admin
    async def cmd_unrevoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = await _need_key(update, ctx)
        if not key:
            return
        ok = await db.set_status(key, "active")
        await reply(update, "✅ <b>Re-enabled</b>" if ok else "<i>Not found.</i>")

    @admin
    async def cmd_extend(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if len(ctx.args or []) < 2:
            await reply(update, "<b>Usage:</b> <code>/extend &lt;KEY&gt; &lt;days&gt;</code> <i>(0 = lifetime)</i>")
            return
        key = ctx.args[0]
        try:
            days = int(ctx.args[1])
        except ValueError:
            await reply(update, "<i>days</i> must be a number.")
            return
        if days <= 0:
            ok = await db.set_expiry(key, None)
            msg = "✅ <b>Set to lifetime</b>" if ok else "<i>Not found.</i>"
        else:
            lic = await db.get_license(key)
            if not lic:
                await reply(update, "<i>Not found.</i>")
                return
            base = max(int(time.time()), lic["expires_at"] or 0)
            new_exp = base + days * 86400
            ok = await db.set_expiry(key, new_exp)
            msg = (
                f"✅ <b>New expiry:</b> {_esc(_fmt_ts(new_exp))}"
                if ok else "<i>Failed.</i>"
            )
        await reply(update, msg)

    @admin
    async def cmd_seats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if len(ctx.args or []) < 2:
            await reply(update, "<b>Usage:</b> <code>/seats &lt;KEY&gt; &lt;n&gt;</code>")
            return
        key = ctx.args[0]
        try:
            n = int(ctx.args[1])
            if n < 1:
                raise ValueError
        except ValueError:
            await reply(update, "<i>n</i> must be ≥ 1.")
            return
        ok = await db.set_max_machines(key, n)
        await reply(
            update,
            f"✅ <b>max_machines</b> = {n}" if ok else "<i>Not found.</i>",
        )

    @admin
    async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not ctx.args:
            await reply(update, "<b>Usage:</b> <code>/reset &lt;KEY&gt; [machine_id]</code>")
            return
        key = ctx.args[0]
        if len(ctx.args) >= 2:
            ok = await db.remove_activation(key, ctx.args[1])
            await reply(
                update,
                "✅ <b>Activation removed</b>" if ok else "<i>Not found.</i>",
            )
            return
        acts = await db.list_activations(key)
        for a in acts:
            await db.remove_activation(key, a["machine_id"])
        await reply(update, f"✅ <b>Reset</b> {len(acts)} activation(s)")

    @admin
    async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = await _need_key(update, ctx)
        if not key:
            return
        ok = await db.delete_license(key)
        await reply(update, "✅ <b>Deleted</b>" if ok else "<i>Not found.</i>")

    # --- log & stats ---
    @admin
    async def cmd_log(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        args = ctx.args or []
        key: str | None = None
        n = 20
        # /log 50            -> n=50, all keys
        # /log KEY           -> key=KEY, n=20
        # /log KEY 50        -> key=KEY, n=50
        if args:
            if args[0].isdigit():
                n = int(args[0])
            else:
                key = args[0]
                if len(args) > 1 and args[1].isdigit():
                    n = int(args[1])
        n = max(1, min(n, 50))
        rows = await db.recent_events(limit=n, license_key=key)
        if not rows:
            await reply(update, "<i>No events yet.</i>")
            return
        scope = f" for <code>{_esc(key)}</code>" if key else ""
        title = f"<b>📋 Last {len(rows)} events</b>{scope}"
        body = "\n".join(_fmt_event(r) for r in rows)
        await reply(update, f"{title}\n<blockquote expandable>{body}</blockquote>")

    @admin
    async def cmd_errors(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            n = int(ctx.args[0]) if ctx.args else 20
        except ValueError:
            n = 20
        n = max(1, min(n, 50))
        # Fetch a wider window then filter; simple is fine.
        rows = await db.recent_events(limit=200)
        bad = [r for r in rows if r["status"] not in OK_STATUSES][:n]
        if not bad:
            await reply(update, "✅ <i>No recent errors.</i>")
            return
        title = f"<b>⚠️ Last {len(bad)} failed events</b>"
        body = "\n".join(_fmt_event(r) for r in bad)
        await reply(update, f"{title}\n<blockquote expandable>{body}</blockquote>")

    @admin
    async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not ctx.args:
            await reply(update, "<b>Usage:</b> <code>/stats &lt;KEY&gt; [days]</code> <i>(0 = all-time)</i>")
            return
        key = ctx.args[0]
        try:
            days = int(ctx.args[1]) if len(ctx.args) > 1 else 7
        except ValueError:
            days = 7
        since = int(time.time()) - days * 86400 if days > 0 else None
        lic = await db.get_license(key)
        if not lic:
            await reply(update, "<i>Not found.</i>")
            return
        s = await db.event_stats(key, since=since)
        period = f"last {days} days" if days > 0 else "all-time"
        text = (
            f"<b>📊 Stats</b> <code>{_esc(key)}</code> · <i>{_esc(period)}</i>\n"
            "<blockquote>"
            f"<b>Total events:</b>    {s['total']}\n"
            f"<b>Unique machines:</b> {s['distinct_machines']}\n"
            f"<b>Last call:</b>       "
            f"{_esc(_fmt_ts(s['last_seen'])) if s['last_seen'] else '-'}"
            "</blockquote>"
        )
        if s["buckets"]:
            text += "\n<b>Breakdown</b>\n<blockquote>"
            parts = []
            for b in sorted(s["buckets"], key=lambda x: -x["n"]):
                icon = "✅" if b["status"] in OK_STATUSES else "⚠️"
                parts.append(
                    f"{icon} <code>{_esc(b['event'])}/{_esc(b['status'])}</code> · {b['n']}"
                )
            text += "\n".join(parts) + "</blockquote>"
        await reply(update, text)

    @admin
    async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await notifier.set_muted(True)
        await reply(update, "🔕 <b>Notifications muted</b>")

    @admin
    async def cmd_unmute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await notifier.set_muted(False)
        await reply(update, "🔔 <b>Notifications enabled</b>")

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("unrevoke", cmd_unrevoke))
    app.add_handler(CommandHandler("extend", cmd_extend))
    app.add_handler(CommandHandler("seats", cmd_seats))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("errors", cmd_errors))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))

    return app, notifier
