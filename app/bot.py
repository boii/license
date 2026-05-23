"""Telegram bot untuk kelola lisensi.

Perintah:
    /start                              - bantuan
    /new [product] [days] [machines]    - buat lisensi baru
                                          (kosongkan = default 0 0 1, 0 = lifetime)
    /list [n]                           - daftar n lisensi terakhir (default 10)
    /info <KEY>                         - detail lisensi + activations + ringkasan log
    /revoke <KEY>                       - matikan lisensi
    /unrevoke <KEY>                     - aktifkan lagi
    /extend <KEY> <days>                - perpanjang masa aktif (days, 0 = lifetime)
    /seats <KEY> <n>                    - ubah max_machines
    /reset <KEY>                        - hapus semua activations
    /reset <KEY> <machine_id>           - hapus 1 activation
    /delete <KEY>                       - hapus lisensi permanen

    /log [n]                            - n event terakhir global (default 20)
    /log <KEY> [n]                      - n event terakhir untuk 1 lisensi
    /errors [n]                         - n event gagal terakhir (status != ok/activated)
    /stats <KEY> [days]                 - statistik pemakaian (default 7 hari, 0 = all-time)
    /mute  /unmute                      - matikan / nyalakan push notifikasi event
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from functools import wraps
from typing import Awaitable, Callable

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from .config import Settings
from .db import DB
from .notifier import Notifier


HELP_TEXT = (
    "*KISS License Bot*\n"
    "_Kelola lisensi:_\n"
    "`/new [product] [days] [machines]`  buat lisensi (days 0 = lifetime)\n"
    "`/list [n]`  daftar terbaru\n"
    "`/info <KEY>` · `/revoke` · `/unrevoke`\n"
    "`/extend <KEY> <days>` · `/seats <KEY> <n>`\n"
    "`/reset <KEY> [machine_id]` · `/delete <KEY>`\n"
    "\n_Log pemakaian:_\n"
    "`/log [n]` atau `/log <KEY> [n]`\n"
    "`/errors [n]`  hanya event gagal\n"
    "`/stats <KEY> [days]`  ringkasan (0 = all-time)\n"
    "`/mute` · `/unmute`  push notifikasi event\n"
)

OK_STATUSES = {"ok", "activated", "deactivated"}


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "lifetime"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_short_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")


def _fmt_license(lic: dict) -> str:
    return (
        f"`{lic['key']}`\n"
        f"product: `{lic['product']}`\n"
        f"status: *{lic['status']}*\n"
        f"seats: {lic.get('activations', '?')}/{lic['max_machines']}\n"
        f"expires: {_fmt_ts(lic['expires_at'])}\n"
        f"owner: {lic.get('owner') or '-'}\n"
        f"created: {_fmt_ts(lic['created_at'])}"
    )


def _fmt_event(e: dict) -> str:
    icon = "✅" if e["status"] in OK_STATUSES else "⚠️"
    key = e["license_key"] or "-"
    short_key = key.split("-")[0] if key != "-" else "-"
    machine = e["machine_id"] or "-"
    return (
        f"{icon} {_fmt_short_ts(e['created_at'])} "
        f"{e['event']}/`{e['status']}` "
        f"key=`{short_key}` mach=`{machine[:12]}` ip=`{e['ip'] or '-'}`"
    )


Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def _admin_only(admin_ids: set[int]) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        @wraps(fn)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            user = update.effective_user
            if not user or user.id not in admin_ids:
                if update.effective_message:
                    await update.effective_message.reply_text(
                        f"Akses ditolak. Telegram ID kamu: `{user.id if user else '?'}`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                return
            await fn(update, context)
        return wrapper
    return deco


def build_application(settings: Settings, db: DB) -> tuple[Application, Notifier]:
    """Bangun Telegram Application + Notifier yang berbagi instance Bot.

    Returns (application, notifier).
    """
    app = Application.builder().token(settings.bot_token).build()
    notifier = Notifier(app.bot, db, settings.admin_ids)
    admin = _admin_only(settings.admin_ids)

    async def reply(update: Update, text: str) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

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
            await reply(update, "Usage: `/new [product] [days] [machines]`")
            return
        expires_at = int(time.time()) + days * 86400 if days > 0 else None
        lic = await db.create_license(
            product=product, max_machines=machines, expires_at=expires_at,
            created_by=update.effective_user.id if update.effective_user else None,
        )
        await reply(update, "Lisensi dibuat:\n" + _fmt_license(lic))

    @admin
    async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            n = int(ctx.args[0]) if ctx.args else 10
        except ValueError:
            n = 10
        rows = await db.list_licenses(limit=max(1, min(n, 50)))
        if not rows:
            await reply(update, "Belum ada lisensi.")
            return
        lines = ["*Lisensi terbaru:*"]
        for r in rows:
            lines.append(
                f"`{r['key']}` · {r['product']} · {r['status']} · "
                f"{r['max_machines']} seat · {_fmt_ts(r['expires_at'])}"
            )
        await reply(update, "\n".join(lines))

    async def _need_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str | None:
        if not ctx.args:
            await reply(update, "Sertakan key, contoh: `/info ABCDE-12345-...`")
            return None
        return ctx.args[0]

    @admin
    async def cmd_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = await _need_key(update, ctx)
        if not key:
            return
        lic = await db.get_license(key)
        if not lic:
            await reply(update, "Tidak ditemukan.")
            return
        text = _fmt_license(lic)
        acts = await db.list_activations(key)
        if acts:
            text += "\n\n*Machines:*"
            for a in acts:
                text += f"\n• `{a['machine_id']}` · last_seen {_fmt_ts(a['last_seen'])}"
        # 7 hari terakhir
        since = int(time.time()) - 7 * 86400
        s = await db.event_stats(key, since=since)
        text += (
            f"\n\n*7 hari:* {s['total']} event · "
            f"{s['distinct_machines']} mesin unik"
        )
        if s["last_seen"]:
            text += f"\nlast call: {_fmt_ts(s['last_seen'])}"
        await reply(update, text)

    @admin
    async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = await _need_key(update, ctx)
        if not key:
            return
        ok = await db.set_status(key, "revoked")
        await reply(update, "OK, di-revoke." if ok else "Tidak ditemukan.")

    @admin
    async def cmd_unrevoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = await _need_key(update, ctx)
        if not key:
            return
        ok = await db.set_status(key, "active")
        await reply(update, "OK, aktif lagi." if ok else "Tidak ditemukan.")

    @admin
    async def cmd_extend(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if len(ctx.args or []) < 2:
            await reply(update, "Usage: `/extend <KEY> <days>` (0 = lifetime)")
            return
        key = ctx.args[0]
        try:
            days = int(ctx.args[1])
        except ValueError:
            await reply(update, "days harus angka")
            return
        if days <= 0:
            ok = await db.set_expiry(key, None)
            msg = "Diset lifetime." if ok else "Tidak ditemukan."
        else:
            lic = await db.get_license(key)
            if not lic:
                await reply(update, "Tidak ditemukan.")
                return
            base = max(int(time.time()), lic["expires_at"] or 0)
            new_exp = base + days * 86400
            ok = await db.set_expiry(key, new_exp)
            msg = f"Expires baru: {_fmt_ts(new_exp)}" if ok else "Gagal."
        await reply(update, msg)

    @admin
    async def cmd_seats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if len(ctx.args or []) < 2:
            await reply(update, "Usage: `/seats <KEY> <n>`")
            return
        key = ctx.args[0]
        try:
            n = int(ctx.args[1])
            if n < 1:
                raise ValueError
        except ValueError:
            await reply(update, "n harus >= 1")
            return
        ok = await db.set_max_machines(key, n)
        await reply(update, f"max_machines = {n}" if ok else "Tidak ditemukan.")

    @admin
    async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not ctx.args:
            await reply(update, "Usage: `/reset <KEY> [machine_id]`")
            return
        key = ctx.args[0]
        if len(ctx.args) >= 2:
            ok = await db.remove_activation(key, ctx.args[1])
            await reply(update, "Activation dihapus." if ok else "Tidak ditemukan.")
            return
        acts = await db.list_activations(key)
        for a in acts:
            await db.remove_activation(key, a["machine_id"])
        await reply(update, f"Reset {len(acts)} activation.")

    @admin
    async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        key = await _need_key(update, ctx)
        if not key:
            return
        ok = await db.delete_license(key)
        await reply(update, "Dihapus." if ok else "Tidak ditemukan.")

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
            await reply(update, "Belum ada event.")
            return
        title = f"*{n} event terakhir{' untuk `' + key + '`' if key else ''}:*"
        lines = [title] + [_fmt_event(r) for r in rows]
        await reply(update, "\n".join(lines))

    @admin
    async def cmd_errors(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            n = int(ctx.args[0]) if ctx.args else 20
        except ValueError:
            n = 20
        n = max(1, min(n, 50))
        # Ambil banyak lalu filter; sederhana cukup.
        rows = await db.recent_events(limit=200)
        bad = [r for r in rows if r["status"] not in OK_STATUSES][:n]
        if not bad:
            await reply(update, "Tidak ada error baru-baru ini. 👌")
            return
        lines = [f"*{len(bad)} event gagal terakhir:*"] + [_fmt_event(r) for r in bad]
        await reply(update, "\n".join(lines))

    @admin
    async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not ctx.args:
            await reply(update, "Usage: `/stats <KEY> [days]` (0 = all-time)")
            return
        key = ctx.args[0]
        try:
            days = int(ctx.args[1]) if len(ctx.args) > 1 else 7
        except ValueError:
            days = 7
        since = int(time.time()) - days * 86400 if days > 0 else None
        lic = await db.get_license(key)
        if not lic:
            await reply(update, "Tidak ditemukan.")
            return
        s = await db.event_stats(key, since=since)
        period = f"{days} hari terakhir" if days > 0 else "all-time"
        lines = [
            f"*Stats* `{key}` · _{period}_",
            f"total: {s['total']}",
            f"mesin unik: {s['distinct_machines']}",
            f"last call: {_fmt_ts(s['last_seen']) if s['last_seen'] else '-'}",
        ]
        if s["buckets"]:
            lines.append("\n*Breakdown:*")
            for b in sorted(s["buckets"], key=lambda x: -x["n"]):
                icon = "✅" if b["status"] in OK_STATUSES else "⚠️"
                lines.append(f"{icon} {b['event']}/`{b['status']}` · {b['n']}")
        await reply(update, "\n".join(lines))

    @admin
    async def cmd_mute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await notifier.set_muted(True)
        await reply(update, "🔕 Push notifikasi event di-mute.")

    @admin
    async def cmd_unmute(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await notifier.set_muted(False)
        await reply(update, "🔔 Push notifikasi event ON.")

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
