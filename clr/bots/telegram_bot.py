"""Telegram front-end: a single /usage command.

Uses HTML parse mode rather than MarkdownV2 so the ASCII bars and pace text do
not need per-character escaping.
"""

from __future__ import annotations

import asyncio
import html
import logging

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from ..auth import AuthError
from ..config import Config
from ..render import _severity_icon, build_paces, pace_block, window_line
from ..usage import UsageError, fetch_usage

log = logging.getLogger("clr.telegram")


def build_message(cfg: Config) -> str:
    report = fetch_usage()

    parts = ["<b>Claude usage</b>", ""]
    for window, pace in build_paces(report, cfg.pace):
        icon = _severity_icon(window)
        parts.append(f"{icon} <b>{html.escape(window.label)}</b>")
        parts.append(f"<pre>{html.escape(window_line(window, pace))}</pre>")

    parts.append("<b>Pace</b>")
    parts.append(html.escape(pace_block(report, cfg.pace)))
    parts.append("")

    footer = f"Checked {report.fetched_at:%Y-%m-%d %H:%M} UTC"
    if report.account_email:
        footer = f"{report.account_email} · {footer}"
    parts.append(f"<i>{html.escape(footer)}</i>")

    return "\n".join(parts)


def _make_handler(cfg: Config):
    allowed = set(cfg.telegram.allowed_user_ids)

    async def usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.id not in allowed:
            log.warning("denied /usage for user id %s", getattr(user, "id", None))
            return  # Silent: unauthorized users get no signal that the bot exists.

        # effective_message, not message: an edited "/usage" arrives as
        # edited_message, and update.message is None then.
        message = update.effective_message
        if message is None:
            return

        chat = update.effective_chat
        if chat is not None:
            await context.bot.send_chat_action(chat.id, "typing")

        try:
            text = await asyncio.get_running_loop().run_in_executor(
                None, build_message, cfg
            )
        except AuthError as e:
            text = f"⚠️ <b>Auth problem</b>\n<pre>{html.escape(str(e))}</pre>"
        except UsageError as e:
            text = f"⚠️ <b>Could not read usage</b>\n<pre>{html.escape(str(e))}</pre>"
        except Exception:
            log.exception("unexpected failure building usage message")
            text = "⚠️ Unexpected error — check the bot logs."

        await message.reply_text(
            text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )

    return usage


async def _post_init(app: Application) -> None:
    await app.bot.set_my_commands(
        [BotCommand("usage", "Show remaining Claude quota and burn rate")]
    )
    log.info("commands registered")


def build_application(cfg: Config) -> Application:
    app = Application.builder().token(cfg.telegram.token).post_init(_post_init).build()
    app.add_handler(CommandHandler("usage", _make_handler(cfg)))
    return app
