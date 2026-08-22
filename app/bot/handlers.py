"""Telegram surface.

Every clock action goes through app.services, the same path the web app uses.
The bot owns no rules of its own - that separation is the point.
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app.config import get_settings
from app.db import repo
from app.db.session import session_scope
from app.domain import clock, payroll, shifts
from app.domain.strings import bilingual, t
from app.services import (
    ACTION_BREAK,
    ACTION_IN,
    ACTION_OUT,
    ACTION_RESUME,
    current_state,
    month_summary,
    perform,
)

logger = logging.getLogger(__name__)


def _full_name(user) -> str:
    return " ".join(part for part in (user.first_name, user.last_name) if part) or str(user.id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    telegram_user = update.effective_user
    if telegram_user is None or update.message is None:
        return

    settings = get_settings()
    with session_scope() as session:
        repo.upsert_user(session, telegram_user.id, _full_name(telegram_user))
        state = current_state(session, telegram_user.id)
        token = repo.create_magic_link(session, telegram_user.id) if settings.public_url else None

    buttons = [
        [
            InlineKeyboardButton(label, callback_data=action)
            for action, label in _action_labels(state)
        ]
    ]
    if token:
        # One tap into the web app, signed in. No password, no new account.
        buttons.append(
            [
                InlineKeyboardButton(
                    t("open_timesheet", "en"),
                    url=f"{settings.public_url}/auth/{token}",
                )
            ]
        )

    await update.message.reply_text(
        _status_text(state), reply_markup=InlineKeyboardMarkup(buttons)
    )


def _action_labels(state):
    labels = {
        ACTION_IN: t("clock_in", "en"),
        ACTION_BREAK: t("start_break", "en"),
        ACTION_RESUME: t("end_break", "en"),
        ACTION_OUT: t("clock_out", "en"),
    }
    return [(action, labels[action]) for action in state.available_actions()]


def _status_text(state) -> str:
    if state.is_working:
        return bilingual("on_shift") + f"\n{clock.format_hhmm(state.worked_seconds)}"
    if state.is_on_break:
        return bilingual("on_break")
    return bilingual("not_working")


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    query = update.callback_query
    telegram_user = update.effective_user
    if query is None or telegram_user is None:
        return

    await query.answer()

    try:
        with session_scope() as session:
            repo.upsert_user(session, telegram_user.id, _full_name(telegram_user))
            try:
                result = perform(session, telegram_user.id, query.data)
            except shifts.ShiftError as error:
                await query.edit_message_text(str(error))
                return
            message = t(result.message_key, "en", **result.params)
            state = result.state

        await query.edit_message_text(
            f"{message}\n\n{_status_text(state)}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(label, callback_data=action)
                  for action, label in _action_labels(state)]]
            ),
        )
    except TelegramError:
        logger.exception("Telegram error handling callback")


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.message and update.effective_user:
        await update.message.reply_text(f"Your ID: {update.effective_user.id}")


async def salary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    month = context.args[0] if context.args else clock.now().strftime("%Y-%m")
    with session_scope() as session:
        summary = month_summary(session, update.effective_user.id, month)

    await update.message.reply_text(
        f"*{summary.month}* — {summary.full_name}\n"
        f"Hours: {clock.format_duration(summary.work_seconds)} over {summary.shift_count} shifts\n"
        f"Gross: {payroll.format_money(summary.gross_cents)}\n"
        f"Advances: {payroll.format_money(summary.advance_cents)}\n"
        f"Net: {payroll.format_money(summary.net_cents)}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def open_shifts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.message is None or update.effective_user is None:
        return

    with session_scope() as session:
        user = repo.upsert_user(session, update.effective_user.id, "")
        if not repo.is_admin(user):
            await update.message.reply_text("Admins only.")
            return
        rows = repo.open_shifts(session)
        lines = [
            f"{row.user.full_name}: since {row.date} {clock.format_time(row.in_time)} "
            f"({row.status})"
            for row in rows
        ]

    await update.message.reply_text("\n".join(lines) if lines else "Nobody is clocked in.")


def build_application(token: str) -> Application:
    application = Application.builder().token(token).updater(None).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("myid", myid))
    application.add_handler(CommandHandler("salary", salary))
    application.add_handler(CommandHandler("open_shifts", open_shifts))
    application.add_handler(CallbackQueryHandler(handle_button))
    return application
