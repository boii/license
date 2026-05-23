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
from typing import Any, Awaitable, Callable, Sequence

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
    "<b>License management</b>\n"
    "<blockquote>"
    "<code>/new</code> [product] [days] [machines] — create (days 0 = lifetime)\n"
    "<code>/list</code> [n] — recent licenses\n"
    "<code>/info</code> &lt;KEY&gt; — details + activations\n"
    "<code>/revoke</code> &lt;KEY&gt; · <code>/unrevoke</code> &lt;KEY&gt;\n"
    "<code>/extend</code> &lt;KEY&gt; &lt;days&gt; — extend (0 = lifetime)\n"
    "<code>/seats</code> &lt;KEY&gt; &lt;n&gt; — set machine limit\n"
    "<code>/reset</code> &lt;KEY&gt; [machine_id] — clear activation(s)\n"
    "<code>/delete</code> &lt;KEY&gt; — delete permanently"
    "</blockquote>\n"
    "<b>Usage logs</b>\n"
    "<blockquote>"
    "<code>/log</code> [n] or <code>/log</code> &lt;KEY&gt; [n]\n"
    "<code>/errors</code> [n] — failed events only\n"
    "<code>/stats</code> &lt;KEY&gt; [days] — summary (0 = all-time)\n"
    "<code>/mute</code> · <code>/unmute</code> — push notifications"
    "</blockquote>"
)

OK_STATUSES = {"ok", "activated", "deactivated"}


# ---- formatting helpers -------------------------------------------------

def _esc(value: Any) -> str:
    """HTML-escape a value for Telegram HTML mode (treat empty as '-')."""
    if value is None or value == "":
        return "-"
    return html.escape(str(value), quote=False)


def _fmt_dt(ts: int | None) -> str:
    if not ts:
        return "lifetime"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_date(ts: int | None) -> str:
    if not ts:
        return "lifetime"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _fmt_short_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")


def _cell(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def _pre_kv(rows: Sequence[tuple[str, Any]]) -> str:
    """Aligned key-value block. Both columns rendered in monospace."""
    if not rows:
        return ""
    width = max(len(k) for k, _ in rows) + 2
    lines = [f"{k.ljust(width)}{_cell(v)}" for k, v in rows]
    return "<pre>" + html.escape("\n".join(lines)) + "</pre>"


def _pre_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """Aligned table in a <pre> block. Empty header label is allowed."""
    str_rows = [[_cell(c) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in str_rows:
        for i, c in enumerate(r):
            if len(c) > widths[i]:
                widths[i] = len(c)

    def fmt(cells: Sequence[str]) -> str:
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()

    body = "\n".join(fmt(line) for line in [headers, *str_rows])
    return "<pre>" + html.escape(body) + "</pre>"


def _fmt_license(lic: dict) -> str:
    """Hero key + monospace property table. Header is added by callers."""
    items = [
        ("product", lic["product"]),
        ("status", lic["status"]),
        ("seats", f"{lic.get('activations', '?')} / {lic['max_machines']}"),
        ("expires", _fmt_dt(lic["expires_at"])),
        ("owner", lic.get("owner")),
        ("created", _fmt_dt(lic["created_at"])),
    ]
    return f"<code>{_esc(lic['key'])}</code>\n{_pre_kv(items)}"


# ---- guard --------------------------------------------------------------

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


# ---- application --------------------------------------------------------

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
        table = _pre_table(
            ["KEY", "PRODUCT", "STATUS", "SEATS", "EXPIRES"],
            [
                [r["key"], r["product"], r["status"], r["max_machines"],
                 _fmt_date(r["expires_at"])]
                for r in rows
            ],
        )
        await reply(update, f"<b>Recent licenses</b> <i>({len(rows)})</i>\n{table}")

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
            text += "\n<b>Machines</b>\n" + _pre_table(
                ["MACHINE-ID", "LAST SEEN"],
                [[a["machine_id"], _fmt_dt(a["last_seen"])] for a in acts],
            )
        # last 7 days
        since = int(time.time()) - 7 * 86400
        s = await db.event_stats(key, since=since)
        text += "\n<b>Last 7 days</b>\n" + _pre_kv([
            ("events", s["total"]),
            ("unique machines", s["distinct_machines"]),
            ("last call", _fmt_dt(s["last_seen"]) if s["last_seen"] else "-"),
        ])
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
                f"✅ <b>Extended</b> → <code>{_esc(_fmt_dt(new_exp))}</code>"
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
        await reply(update, f"✅ <b>Seats</b> → <code>{n}</code>" if ok else "<i>Not found.</i>")

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
    def _events_table(events: list[dict]) -> str:
        rows = []
        for e in events:
            mark = "✓" if e["status"] in OK_STATUSES else "✗"
            short_key = (e["license_key"] or "-").split("-")[0]
            rows.append([
                mark,
                _fmt_short_ts(e["created_at"]),
                e["event"],
                e["status"],
                short_key,
                e["ip"] or "-",
            ])
        return _pre_table(["", "TIME", "EVENT", "STATUS", "KEY", "IP"], rows)

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
        scope = f" — <code>{_esc(key)}</code>" if key else ""
        await reply(
            update,
            f"<b>Last {len(rows)} events</b>{scope}\n{_events_table(rows)}",
        )

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
        await reply(
            update,
            f"<b>Last {len(bad)} failed events</b>\n{_events_table(bad)}",
        )

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
            f"<b>Stats</b> <code>{_esc(key)}</code> — <i>{_esc(period)}</i>\n"
            + _pre_kv([
                ("total events", s["total"]),
                ("unique machines", s["distinct_machines"]),
                ("last call", _fmt_dt(s["last_seen"]) if s["last_seen"] else "-"),
            ])
        )
        if s["buckets"]:
            sorted_buckets = sorted(s["buckets"], key=lambda x: -x["n"])
            text += "\n<b>Breakdown</b>\n" + _pre_table(
                ["EVENT", "STATUS", "COUNT"],
                [[b["event"], b["status"], b["n"]] for b in sorted_buckets],
            )
        await reply(update, text)

    @admin
    async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await notifier.set_muted(True)
        await reply(update, "🔕 <b>Notifications muted</b>")

    @admin
    async def cmd_unmute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await notifier.set_muted(False)
        await reply(update, "🔔 <b>Notifications on</b>")

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
