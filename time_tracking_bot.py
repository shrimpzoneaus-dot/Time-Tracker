import asyncio
import logging
import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =========================
# Config
# =========================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "time_tracker.db"
DEFAULT_TIMEZONE = "Australia/Sydney"

STATUS_WORKING = "WORKING"
STATUS_ON_BREAK = "ON_BREAK"
STATUS_FINISHED = "FINISHED"

ROLE_ADMIN = "ADMIN"
ROLE_EMPLOYEE = "EMPLOYEE"

ACTION_IN = "IN"
ACTION_BREAK = "BREAK"
ACTION_RESUME = "RESUME"
ACTION_OUT = "OUT"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


# =========================
# Database
# =========================

@contextmanager
def db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'EMPLOYEE',
                hourly_rate_cents INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        columns = conn.execute("PRAGMA table_info(users)").fetchall()
        column_names = {column["name"] for column in columns}
        if "role" not in column_names:
            conn.execute(
                "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'EMPLOYEE'"
            )
        if "hourly_rate_cents" not in column_names:
            conn.execute(
                "ALTER TABLE users ADD COLUMN hourly_rate_cents INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS timesheets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date DATE NOT NULL,
                in_time DATETIME,
                out_time DATETIME,
                break_start DATETIME,
                total_break_seconds INTEGER DEFAULT 0,
                status TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS advances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date DATE NOT NULL,
                amount_cents INTEGER NOT NULL,
                note TEXT,
                created_at DATETIME NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_timesheets_user_date
            ON timesheets (user_id, date)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_advances_user_date
            ON advances (user_id, date)
            """
        )


def upsert_user(user_id: int, full_name: str, role: str | None = None) -> None:
    role = role or get_default_role(user_id)
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, full_name, role)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name = excluded.full_name,
                role = excluded.role
            """,
            (user_id, full_name, role),
        )


def get_user(user_id: int) -> sqlite3.Row | None:
    with db_connection() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def ensure_configured_admins() -> None:
    with db_connection() as conn:
        for admin_id in get_admin_chat_ids():
            conn.execute(
                """
                INSERT INTO users (user_id, full_name, role)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET role = excluded.role
                """,
                (admin_id, f"Admin {admin_id}", ROLE_ADMIN),
            )


def list_users() -> list[sqlite3.Row]:
    with db_connection() as conn:
        return conn.execute(
            """
            SELECT user_id, full_name, role
            FROM users
            ORDER BY role, full_name
            """
        ).fetchall()


def get_today_timesheet(user_id: int) -> sqlite3.Row | None:
    with db_connection() as conn:
        return conn.execute(
            """
            SELECT *
            FROM timesheets
            WHERE user_id = ? AND date = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, today_local().isoformat()),
        ).fetchone()


def get_active_today_timesheet(user_id: int) -> sqlite3.Row | None:
    with db_connection() as conn:
        return conn.execute(
            """
            SELECT *
            FROM timesheets
            WHERE user_id = ? AND date = ? AND status IN (?, ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, today_local().isoformat(), STATUS_WORKING, STATUS_ON_BREAK),
        ).fetchone()


def list_timesheets_by_date(work_date: str) -> list[sqlite3.Row]:
    with db_connection() as conn:
        return conn.execute(
            """
            SELECT t.*, u.full_name
            FROM timesheets t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.date = ?
            ORDER BY u.full_name
            """,
            (work_date,),
        ).fetchall()


def list_open_timesheets() -> list[sqlite3.Row]:
    with db_connection() as conn:
        return conn.execute(
            """
            SELECT t.*, u.full_name
            FROM timesheets t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.status IN (?, ?)
            ORDER BY t.date, t.in_time
            """,
            (STATUS_WORKING, STATUS_ON_BREAK),
        ).fetchall()


def list_timesheets_by_month(user_id: int, month_prefix: str) -> list[sqlite3.Row]:
    with db_connection() as conn:
        return conn.execute(
            """
            SELECT t.*, u.full_name, u.hourly_rate_cents
            FROM timesheets t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.user_id = ? AND t.date LIKE ?
            ORDER BY t.date, t.id
            """,
            (user_id, f"{month_prefix}-%"),
        ).fetchall()


def list_users_with_timesheets_by_month(month_prefix: str) -> list[sqlite3.Row]:
    with db_connection() as conn:
        return conn.execute(
            """
            SELECT DISTINCT u.user_id, u.full_name, u.role, u.hourly_rate_cents
            FROM users u
            JOIN timesheets t ON t.user_id = u.user_id
            WHERE t.date LIKE ?
            ORDER BY u.full_name
            """,
            (f"{month_prefix}-%",),
        ).fetchall()


def get_timesheet_by_user_date(user_id: int, work_date: str) -> sqlite3.Row | None:
    with db_connection() as conn:
        return conn.execute(
            """
            SELECT t.*, u.full_name
            FROM timesheets t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.user_id = ? AND t.date = ?
            ORDER BY t.id DESC
            LIMIT 1
            """,
            (user_id, work_date),
        ).fetchone()


def set_hourly_rate(user_id: int, hourly_rate_cents: int) -> None:
    with db_connection() as conn:
        conn.execute(
            "UPDATE users SET hourly_rate_cents = ? WHERE user_id = ?",
            (hourly_rate_cents, user_id),
        )


def add_advance(user_id: int, amount_cents: int, note: str | None = None) -> None:
    now = now_local()
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO advances (user_id, date, amount_cents, note, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                now.date().isoformat(),
                amount_cents,
                note,
                format_datetime_for_db(now),
            ),
        )


def list_advances_by_month(user_id: int, month_prefix: str) -> list[sqlite3.Row]:
    with db_connection() as conn:
        return conn.execute(
            """
            SELECT *
            FROM advances
            WHERE user_id = ? AND date LIKE ?
            ORDER BY date, id
            """,
            (user_id, f"{month_prefix}-%"),
        ).fetchall()


def delete_timesheets_by_user_date(user_id: int, work_date: str) -> int:
    with db_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM timesheets WHERE user_id = ? AND date = ?",
            (user_id, work_date),
        )
        return cursor.rowcount


def create_check_in(user_id: int, now: datetime) -> None:
    with db_connection() as conn:
        conn.execute(
            """
            INSERT INTO timesheets
                (user_id, date, in_time, total_break_seconds, status)
            VALUES (?, ?, ?, 0, ?)
            """,
            (
                user_id,
                now.date().isoformat(),
                format_datetime_for_db(now),
                STATUS_WORKING,
            ),
        )


def start_break(timesheet_id: int, now: datetime) -> None:
    with db_connection() as conn:
        conn.execute(
            """
            UPDATE timesheets
            SET break_start = ?, status = ?
            WHERE id = ?
            """,
            (format_datetime_for_db(now), STATUS_ON_BREAK, timesheet_id),
        )


def resume_work(timesheet: sqlite3.Row, now: datetime) -> int:
    break_start = parse_db_datetime(timesheet["break_start"])
    break_seconds = max(0, int((now - break_start).total_seconds()))
    total_break_seconds = int(timesheet["total_break_seconds"] or 0) + break_seconds

    with db_connection() as conn:
        conn.execute(
            """
            UPDATE timesheets
            SET break_start = NULL, total_break_seconds = ?, status = ?
            WHERE id = ?
            """,
            (total_break_seconds, STATUS_WORKING, timesheet["id"]),
        )

    return break_seconds


def finish_work(timesheet_id: int, now: datetime) -> sqlite3.Row:
    with db_connection() as conn:
        conn.execute(
            """
            UPDATE timesheets
            SET out_time = ?, status = ?
            WHERE id = ?
            """,
            (format_datetime_for_db(now), STATUS_FINISHED, timesheet_id),
        )
        return conn.execute(
            """
            SELECT t.*, u.full_name
            FROM timesheets t
            JOIN users u ON u.user_id = t.user_id
            WHERE t.id = ?
            """,
            (timesheet_id,),
        ).fetchone()


def update_timesheet_field(timesheet_id: int, field_name: str, value: str | int | None) -> None:
    allowed_fields = {"in_time", "out_time", "total_break_seconds", "status"}
    if field_name not in allowed_fields:
        raise ValueError("Invalid timesheet field.")

    with db_connection() as conn:
        conn.execute(
            f"UPDATE timesheets SET {field_name} = ? WHERE id = ?",
            (value, timesheet_id),
        )


# =========================
# Helpers
# =========================

def format_datetime_for_db(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat(sep=" ")


def get_app_timezone() -> ZoneInfo:
    timezone_name = os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.warning("Invalid APP_TIMEZONE=%s; using %s.", timezone_name, DEFAULT_TIMEZONE)
        return ZoneInfo(DEFAULT_TIMEZONE)


def now_local() -> datetime:
    return datetime.now(get_app_timezone()).replace(tzinfo=None)


def today_local() -> date:
    return now_local().date()


def parse_db_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def format_time(value: str | None) -> str:
    if not value:
        return "-"
    return parse_db_datetime(value).strftime("%H:%M")


def format_hhmm(total_seconds: int) -> str:
    total_minutes = max(0, total_seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours:02d}:{minutes:02d}"


def format_work_duration(total_seconds: int) -> str:
    total_minutes = max(0, total_seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m"


def get_full_name(user: Any) -> str:
    parts = [user.first_name, user.last_name]
    full_name = " ".join(part for part in parts if part).strip()
    return full_name or user.username or str(user.id)


def reload_runtime_env() -> None:
    load_dotenv(BASE_DIR / ".env", override=True)


def get_admin_chat_ids() -> set[int]:
    reload_runtime_env()
    admin_chat_id = os.getenv("ADMIN_CHAT_ID", "")
    ids: set[int] = set()
    for raw_id in admin_chat_id.split(","):
        raw_id = raw_id.strip()
        if raw_id.lstrip("-").isdigit():
            ids.add(int(raw_id))
    return ids


def is_configured_admin(user_id: int) -> bool:
    return user_id in get_admin_chat_ids()


def is_admin_user(user_id: int) -> bool:
    saved_user = get_user(user_id)
    return is_configured_admin(user_id) or bool(saved_user and saved_user["role"] == ROLE_ADMIN)


def get_default_role(user_id: int) -> str:
    return ROLE_ADMIN if is_configured_admin(user_id) else ROLE_EMPLOYEE


def get_role_from_message(user_id: int, message_text: str) -> str:
    if message_text.strip().lower() == "admin" and is_configured_admin(user_id):
        return ROLE_ADMIN
    return get_default_role(user_id)


def parse_work_date(raw_date: str) -> str:
    return datetime.strptime(raw_date, "%Y-%m-%d").date().isoformat()


def parse_month(raw_month: str) -> str:
    datetime.strptime(raw_month, "%Y-%m")
    return raw_month


def parse_time_for_date(work_date: str, raw_time: str) -> str:
    parsed_date = datetime.strptime(work_date, "%Y-%m-%d").date()
    parsed_time = datetime.strptime(raw_time, "%H:%M").time()
    return format_datetime_for_db(datetime.combine(parsed_date, parsed_time))


def parse_money_to_cents(raw_amount: str) -> int:
    cleaned = raw_amount.strip().replace("$", "").replace(",", "")
    amount = float(cleaned)
    if amount < 0:
        raise ValueError("Amount cannot be negative.")
    return int(round(amount * 100))


def format_money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents / 100:,.2f}"


def build_keyboard(timesheet: sqlite3.Row | None) -> InlineKeyboardMarkup:
    if not timesheet or timesheet["status"] == STATUS_FINISHED:
        buttons = [[InlineKeyboardButton("Clock In", callback_data=ACTION_IN)]]
    elif timesheet["status"] == STATUS_WORKING:
        buttons = [
            [
                InlineKeyboardButton("Start Break", callback_data=ACTION_BREAK),
                InlineKeyboardButton("Clock Out", callback_data=ACTION_OUT),
            ]
        ]
    elif timesheet["status"] == STATUS_ON_BREAK:
        buttons = [[InlineKeyboardButton("End Break", callback_data=ACTION_RESUME)]]
    else:
        buttons = []

    return InlineKeyboardMarkup(buttons) if buttons else InlineKeyboardMarkup([])


async def show_status(update: Update, text: str) -> None:
    if not update.effective_user:
        return

    timesheet = get_today_timesheet(update.effective_user.id)
    keyboard = build_keyboard(timesheet)

    if update.callback_query:
        await update.callback_query.edit_message_text(text=text, reply_markup=keyboard)
    elif update.message:
        await update.message.reply_text(text, reply_markup=keyboard)


def calculate_work_seconds(timesheet: sqlite3.Row) -> int:
    in_time = parse_db_datetime(timesheet["in_time"])
    out_time = parse_db_datetime(timesheet["out_time"])
    total_break_seconds = int(timesheet["total_break_seconds"] or 0)
    return max(0, int((out_time - in_time).total_seconds()) - total_break_seconds)


def format_timesheet_line(timesheet: sqlite3.Row) -> str:
    break_minutes = int(timesheet["total_break_seconds"] or 0) // 60
    if timesheet["in_time"] and timesheet["out_time"]:
        work_time = format_work_duration(calculate_work_seconds(timesheet))
    else:
        work_time = "-"

    return (
        f"{timesheet['full_name']} ({timesheet['user_id']})\n"
        f"Status: {timesheet['status']}\n"
        f"In: {format_time(timesheet['in_time'])} | Out: {format_time(timesheet['out_time'])}\n"
        f"Break: {break_minutes}m | Total: {work_time}"
    )


def build_salary_report_for_user(user_id: int, month_prefix: str) -> str:
    user = get_user(user_id)
    if not user:
        return f"User {user_id} is not saved yet."

    timesheets = list_timesheets_by_month(user_id, month_prefix)
    advances = list_advances_by_month(user_id, month_prefix)
    hourly_rate_cents = int(user["hourly_rate_cents"] or 0)

    total_work_seconds = 0
    detail_lines = []
    for row in timesheets:
        if row["in_time"] and row["out_time"]:
            work_seconds = calculate_work_seconds(row)
            total_work_seconds += work_seconds
            detail_lines.append(
                f"{row['date']}: {format_time(row['in_time'])}-{format_time(row['out_time'])}, "
                f"break {int(row['total_break_seconds'] or 0) // 60}m, "
                f"work {format_work_duration(work_seconds)}"
            )
        else:
            detail_lines.append(f"{row['date']}: incomplete shift ({row['status']})")

    gross_cents = round(total_work_seconds * hourly_rate_cents / 3600)
    advance_cents = sum(int(row["amount_cents"] or 0) for row in advances)
    net_cents = gross_cents - advance_cents

    advance_lines = [
        f"{row['date']}: {format_money(int(row['amount_cents']))}"
        + (f" - {row['note']}" if row["note"] else "")
        for row in advances
    ]

    report = [
        f"Salary report {month_prefix}",
        f"Employee: {user['full_name']} ({user['user_id']})",
        f"Hourly rate: {format_money(hourly_rate_cents)}/h",
        f"Total work: {format_work_duration(total_work_seconds)}",
        f"Gross salary: {format_money(gross_cents)}",
        f"Advances: {format_money(advance_cents)}",
        f"Net salary: {format_money(net_cents)}",
    ]

    if detail_lines:
        report.append("\nWork details:")
        report.extend(detail_lines)
    else:
        report.append("\nWork details: none")

    if advance_lines:
        report.append("\nAdvance details:")
        report.extend(advance_lines)
    else:
        report.append("\nAdvance details: none")

    return "\n".join(report)


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, timesheet: sqlite3.Row) -> None:
    admin_chat_ids = get_admin_chat_ids()
    if not admin_chat_ids:
        logger.warning("ADMIN_CHAT_ID is not configured; skipping admin notification.")
        return

    work_seconds = calculate_work_seconds(timesheet)
    break_seconds = int(timesheet["total_break_seconds"] or 0)
    report_date = datetime.fromisoformat(timesheet["date"]).strftime("%d/%m/%Y")

    message = (
        f"Timesheet report {report_date}\n"
        f"Employee: {timesheet['full_name']}\n"
        f"Clock In: {format_time(timesheet['in_time'])}\n"
        f"Clock Out: {format_time(timesheet['out_time'])}\n"
        f"Break: {break_seconds // 60} minutes\n"
        f"Total Work: {format_work_duration(work_seconds)}"
    )

    for admin_chat_id in admin_chat_ids:
        try:
            await context.bot.send_message(chat_id=admin_chat_id, text=message)
        except TelegramError:
            logger.exception("Failed to send admin notification to %s.", admin_chat_id)


# =========================
# Handlers
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.effective_user:
        return

    try:
        user = update.effective_user
        existing_user = get_user(user.id)
        upsert_user(user.id, get_full_name(user))
        timesheet = get_today_timesheet(user.id)
        first_start_line = ""
        is_placeholder_admin = bool(
            existing_user and existing_user["full_name"] == f"Admin {user.id}"
        )
        if not existing_user or is_placeholder_admin:
            saved_user = get_user(user.id)
            saved_role = saved_user["role"] if saved_user else get_default_role(user.id)
            first_start_line = f"Your ID: {user.id}\nRole: {saved_role}\n"

        if not timesheet:
            text = first_start_line + "Welcome! Press Clock In to start your shift."
        elif timesheet["status"] == STATUS_WORKING:
            text = first_start_line + f"You are clocked in from {format_time(timesheet['in_time'])}."
        elif timesheet["status"] == STATUS_ON_BREAK:
            text = first_start_line + "You are on break. Press End Break when you return."
        else:
            work_seconds = calculate_work_seconds(timesheet)
            text = (
                first_start_line
                + "Your last shift is finished. Press Clock In to start another shift.\n"
                f"Last shift work time: {format_hhmm(work_seconds)}"
            )

        await update.message.reply_text(text, reply_markup=build_keyboard(timesheet))
    except sqlite3.DatabaseError:
        logger.exception("Database error in /start.")
        if update.message:
            await update.message.reply_text("Có lỗi database. Vui lòng thử lại sau.")
    except TelegramError:
        logger.exception("Telegram error in /start.")


async def process_time_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    user: Any,
) -> None:
    upsert_user(user.id, get_full_name(user))
    latest_timesheet = get_today_timesheet(user.id)
    active_timesheet = get_active_today_timesheet(user.id)
    timesheet = active_timesheet or latest_timesheet
    now = now_local()

    if action == ACTION_IN:
        if active_timesheet:
            await show_status(update, "You are already clocked in.")
            return

        create_check_in(user.id, now)
        await show_status(update, f"Clocked in at {now.strftime('%H:%M')}.")
        return

    if not active_timesheet:
        await show_status(update, "You have not clocked in yet. Press Clock In first.")
        return

    timesheet = active_timesheet

    if action == ACTION_BREAK:
        if timesheet["status"] != STATUS_WORKING:
            await show_status(update, "You are not currently clocked in.")
            return
        start_break(timesheet["id"], now)
        await show_status(update, "Break started.")
        return

    if action == ACTION_RESUME:
        if timesheet["status"] != STATUS_ON_BREAK or not timesheet["break_start"]:
            await show_status(update, "You are not currently on break.")
            return
        resume_work(timesheet, now)
        await show_status(update, "Break ended. You are back at work.")
        return

    if action == ACTION_OUT:
        if timesheet["status"] != STATUS_WORKING:
            await show_status(update, "Please end your break before clocking out.")
            return
        finished_timesheet = finish_work(timesheet["id"], now)
        work_seconds = calculate_work_seconds(finished_timesheet)
        await notify_admin(context, finished_timesheet)
        finished_text = (
            "Clocked out at "
            f"{now.strftime('%H:%M')}.\n"
            f"Total work time: {format_hhmm(work_seconds)}"
        )
        next_shift_text = "Shift finished. Press Clock In to start a new shift."
        next_shift_keyboard = build_keyboard(None)

        if update.callback_query:
            await update.callback_query.edit_message_text(text=finished_text)
            if update.effective_chat:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=next_shift_text,
                    reply_markup=next_shift_keyboard,
                )
        elif update.message:
            await update.message.reply_text(finished_text)
            await update.message.reply_text(
                next_shift_text,
                reply_markup=next_shift_keyboard,
            )
        return

    await show_status(update, "Invalid action.")


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return

    await query.answer()

    try:
        await process_time_action(update, context, query.data, user)
    except sqlite3.DatabaseError:
        logger.exception("Database error while handling callback.")
        await query.edit_message_text("Có lỗi database. Vui lòng thử lại sau.")
    except TelegramError:
        logger.exception("Telegram error while handling callback.")


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.effective_user or not update.message:
        return

    user = update.effective_user
    upsert_user(user.id, get_full_name(user))
    saved_user = get_user(user.id)
    role = saved_user["role"] if saved_user else get_default_role(user.id)
    await update.message.reply_text(f"Your ID: {user.id}\nRole: {role}")


async def admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.effective_user or not update.message:
        return

    upsert_user(update.effective_user.id, get_full_name(update.effective_user))
    if not is_admin_user(update.effective_user.id):
        await update.message.reply_text("Ban khong co quyen admin.")
        return

    await update.message.reply_text(
        "Admin commands:\n"
        "/myid - show your Telegram ID\n"
        "/users - list saved users\n"
        "/today - today timesheet report\n"
        "/today <YYYY-MM-DD> - report by date\n"
        "/open_shifts - admin: list shifts not clocked out\n"
        "/backup - admin: backup database\n"
        "/set_rate <user_id> <amount_per_hour> - set hourly rate\n"
        "/ung <amount> - save your advance\n"
        "/ung <user_id> <amount> - admin saves advance for user\n"
        "/salary - your current month salary\n"
        "/salary <YYYY-MM> - admin monthly salary report\n"
        "/salary <YYYY-MM> <user_id> - salary report for one user\n"
        "/reset_today - reset your test shift today\n"
        "/reset_today <user_id> - reset a user's shift today\n"
        "\n"
        "Edit time:\n"
        "/edit_time <user_id> <YYYY-MM-DD> in <HH:MM>\n"
        "/edit_time <user_id> <YYYY-MM-DD> out <HH:MM>\n"
        "/edit_time <user_id> <YYYY-MM-DD> break <minutes>\n\n"
        "Examples:\n"
        "/edit_time 123456789 2026-06-09 in 08:30\n"
        "/edit_time 123456789 2026-06-09 out 17:45\n"
        "/edit_time 123456789 2026-06-09 break 60"
    )


async def users_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.effective_user or not update.message:
        return

    try:
        upsert_user(update.effective_user.id, get_full_name(update.effective_user))
        if not is_admin_user(update.effective_user.id):
            await update.message.reply_text("You do not have admin permission.")
            return

        users = list_users()
        if not users:
            await update.message.reply_text("No users saved yet.")
            return

        lines = [
            f"{row['full_name']} | {row['user_id']} | {row['role']}"
            for row in users
        ]
        await update.message.reply_text("Saved users:\n" + "\n".join(lines))
    except sqlite3.DatabaseError:
        logger.exception("Database error while listing users.")
        await update.message.reply_text("Database error. Please try again.")
    except TelegramError:
        logger.exception("Telegram error while listing users.")


async def today_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    try:
        upsert_user(update.effective_user.id, get_full_name(update.effective_user))
        if not is_admin_user(update.effective_user.id):
            await update.message.reply_text("You do not have admin permission.")
            return

        work_date = parse_work_date(context.args[0]) if context.args else today_local().isoformat()
        rows = list_timesheets_by_date(work_date)
        if not rows:
            await update.message.reply_text(f"No timesheets found for {work_date}.")
            return

        parts = [f"Timesheet report for {work_date}"]
        parts.extend(format_timesheet_line(row) for row in rows)
        await update.message.reply_text("\n\n".join(parts))
    except ValueError:
        await update.message.reply_text("Use: /today or /today <YYYY-MM-DD>")
    except sqlite3.DatabaseError:
        logger.exception("Database error while building today report.")
        await update.message.reply_text("Database error. Please try again.")
    except TelegramError:
        logger.exception("Telegram error while building today report.")


async def open_shifts_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.effective_user or not update.message:
        return

    try:
        upsert_user(update.effective_user.id, get_full_name(update.effective_user))
        if not is_admin_user(update.effective_user.id):
            await update.message.reply_text("You do not have admin permission.")
            return

        rows = list_open_timesheets()
        if not rows:
            await update.message.reply_text("No open shifts right now.")
            return

        lines = ["Open shifts:"]
        for row in rows:
            lines.append(
                f"{row['full_name']} ({row['user_id']}) | {row['date']} | "
                f"{row['status']} | in {format_time(row['in_time'])}"
            )
        await update.message.reply_text("\n".join(lines))
    except sqlite3.DatabaseError:
        logger.exception("Database error while listing open shifts.")
        await update.message.reply_text("Database error. Please try again.")
    except TelegramError:
        logger.exception("Telegram error while listing open shifts.")


async def backup_database(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.effective_user or not update.message:
        return

    try:
        upsert_user(update.effective_user.id, get_full_name(update.effective_user))
        if not is_admin_user(update.effective_user.id):
            await update.message.reply_text("You do not have admin permission.")
            return

        backup_dir = BASE_DIR / "backups"
        backup_dir.mkdir(exist_ok=True)
        stamp = now_local().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"time_tracker_{stamp}.db"
        shutil.copy2(DB_PATH, backup_path)
        await update.message.reply_text(f"Backup created:\n{backup_path.name}")
    except (OSError, sqlite3.DatabaseError):
        logger.exception("Database backup failed.")
        await update.message.reply_text("Backup failed. Please try again.")
    except TelegramError:
        logger.exception("Telegram error while creating backup.")


async def set_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    try:
        admin = update.effective_user
        upsert_user(admin.id, get_full_name(admin))
        if not is_admin_user(admin.id):
            await update.message.reply_text("You do not have admin permission.")
            return

        if len(context.args) != 2:
            await update.message.reply_text("Use: /set_rate <user_id> <amount_per_hour>")
            return

        target_user_id = int(context.args[0])
        target_user = get_user(target_user_id)
        if not target_user:
            await update.message.reply_text(f"User {target_user_id} is not saved yet.")
            return

        hourly_rate_cents = parse_money_to_cents(context.args[1])
        set_hourly_rate(target_user_id, hourly_rate_cents)
        await update.message.reply_text(
            f"Set hourly rate for {target_user['full_name']} to {format_money(hourly_rate_cents)}/h."
        )
    except ValueError:
        await update.message.reply_text("Invalid value. Example: /set_rate 6393446109 16")
    except sqlite3.DatabaseError:
        logger.exception("Database error while setting hourly rate.")
        await update.message.reply_text("Database error. Please try again.")
    except TelegramError:
        logger.exception("Telegram error while setting hourly rate.")


async def advance_salary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    try:
        user = update.effective_user
        upsert_user(user.id, get_full_name(user))

        if not context.args:
            await update.message.reply_text("Use: /ung <amount> or /ung <user_id> <amount>")
            return

        if len(context.args) >= 2 and context.args[0].lstrip("-").isdigit() and is_admin_user(user.id):
            target_user_id = int(context.args[0])
            amount_arg = context.args[1]
            note = " ".join(context.args[2:]) if len(context.args) > 2 else None
        else:
            target_user_id = user.id
            amount_arg = context.args[0]
            note = " ".join(context.args[1:]) if len(context.args) > 1 else None

        target_user = get_user(target_user_id)
        if not target_user:
            await update.message.reply_text(f"User {target_user_id} is not saved yet.")
            return

        amount_cents = parse_money_to_cents(amount_arg)
        add_advance(target_user_id, amount_cents, note)
        await update.message.reply_text(
            f"Advance saved for {target_user['full_name']}: {format_money(amount_cents)}."
        )
    except ValueError:
        await update.message.reply_text("Invalid amount. Example: /ung 100 or /ung 100$")
    except sqlite3.DatabaseError:
        logger.exception("Database error while adding advance.")
        await update.message.reply_text("Database error. Please try again.")
    except TelegramError:
        logger.exception("Telegram error while adding advance.")


async def salary_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    try:
        requester = update.effective_user
        upsert_user(requester.id, get_full_name(requester))

        month_prefix = today_local().strftime("%Y-%m")
        target_user_id: int | None = requester.id

        if context.args:
            month_prefix = parse_month(context.args[0])
        if len(context.args) >= 2:
            target_user_id = int(context.args[1])

        if target_user_id != requester.id and not is_admin_user(requester.id):
            await update.message.reply_text("You do not have admin permission.")
            return

        if is_admin_user(requester.id) and len(context.args) == 1:
            users = list_users_with_timesheets_by_month(month_prefix)
            if not users:
                await update.message.reply_text(f"No timesheets found for {month_prefix}.")
                return
            reports = [
                build_salary_report_for_user(int(row["user_id"]), month_prefix)
                for row in users
            ]
            await update.message.reply_text("\n\n----------------\n\n".join(reports))
            return

        await update.message.reply_text(build_salary_report_for_user(target_user_id, month_prefix))
    except ValueError:
        await update.message.reply_text("Use: /salary, /salary <YYYY-MM>, or /salary <YYYY-MM> <user_id>")
    except sqlite3.DatabaseError:
        logger.exception("Database error while building salary report.")
        await update.message.reply_text("Database error. Please try again.")
    except TelegramError:
        logger.exception("Telegram error while building salary report.")


async def edit_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    try:
        admin = update.effective_user
        upsert_user(admin.id, get_full_name(admin))

        if not is_admin_user(admin.id):
            await update.message.reply_text("Ban khong co quyen chinh gio.")
            return

        args = context.args
        if len(args) != 4:
            await update.message.reply_text(
                "Sai cu phap.\n"
                "Dung: /edit_time <user_id> <YYYY-MM-DD> <in|out|break> <gia_tri>"
            )
            return

        target_user_id = int(args[0])
        work_date = parse_work_date(args[1])
        field = args[2].strip().lower()
        raw_value = args[3].strip()
        timesheet = get_timesheet_by_user_date(target_user_id, work_date)

        if not timesheet:
            await update.message.reply_text(
                f"Khong tim thay bang cham cong cua user {target_user_id} ngay {work_date}."
            )
            return

        if field == "in":
            new_value = parse_time_for_date(work_date, raw_value)
            update_timesheet_field(timesheet["id"], "in_time", new_value)
            message = f"Da sua gio vao ca cua {timesheet['full_name']} thanh {raw_value}."
        elif field == "out":
            new_value = parse_time_for_date(work_date, raw_value)
            update_timesheet_field(timesheet["id"], "out_time", new_value)
            update_timesheet_field(timesheet["id"], "status", STATUS_FINISHED)
            message = f"Da sua gio tan ca cua {timesheet['full_name']} thanh {raw_value}."
        elif field == "break":
            break_minutes = int(raw_value)
            if break_minutes < 0:
                raise ValueError("Break minutes cannot be negative.")
            update_timesheet_field(timesheet["id"], "total_break_seconds", break_minutes * 60)
            message = f"Da sua tong nghi cua {timesheet['full_name']} thanh {break_minutes} phut."
        else:
            await update.message.reply_text("Truong can sua chi duoc la: in, out, break.")
            return

        updated_timesheet = get_timesheet_by_user_date(target_user_id, work_date)
        if updated_timesheet and updated_timesheet["in_time"] and updated_timesheet["out_time"]:
            work_seconds = calculate_work_seconds(updated_timesheet)
            message += f"\nTong gio lam moi: {format_work_duration(work_seconds)}."

        await update.message.reply_text(message)
    except ValueError:
        await update.message.reply_text(
            "Gia tri khong hop le. Ngay dung YYYY-MM-DD, gio dung HH:MM, break la so phut."
        )
    except sqlite3.DatabaseError:
        logger.exception("Database error while editing timesheet.")
        await update.message.reply_text("Co loi database. Vui long thu lai sau.")
    except TelegramError:
        logger.exception("Telegram error while editing timesheet.")


async def reset_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    try:
        admin = update.effective_user
        upsert_user(admin.id, get_full_name(admin))

        if not is_admin_user(admin.id):
            await update.message.reply_text("Ban khong co quyen reset ca.")
            return

        target_user_id = int(context.args[0]) if context.args else admin.id
        work_date = today_local().isoformat()
        deleted_count = delete_timesheets_by_user_date(target_user_id, work_date)

        if deleted_count:
            await update.message.reply_text(
                f"Da reset {deleted_count} ca cua user {target_user_id} ngay {work_date}."
            )
        else:
            await update.message.reply_text(
                f"Khong co ca nao cua user {target_user_id} ngay {work_date}."
            )
    except ValueError:
        await update.message.reply_text("Dung: /reset_today <user_id>")
    except sqlite3.DatabaseError:
        logger.exception("Database error while resetting today.")
        await update.message.reply_text("Co loi database. Vui long thu lai sau.")
    except TelegramError:
        logger.exception("Telegram error while resetting today.")


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return

    try:
        user = update.effective_user
        message_text = update.message.text or ""
        text_action_map = {
            "clock in": ACTION_IN,
            "start break": ACTION_BREAK,
            "end break": ACTION_RESUME,
            "clock out": ACTION_OUT,
        }
        action = text_action_map.get(message_text.strip().lower())
        if action:
            await process_time_action(update, context, action, user)
            return

        role = get_role_from_message(user.id, message_text)
        upsert_user(user.id, get_full_name(user), role)
        timesheet = get_today_timesheet(user.id)
        saved_user = get_user(user.id)
        saved_role = saved_user["role"] if saved_user else role

        if not timesheet:
            text = "Press Clock In to start your shift."
        elif timesheet["status"] == STATUS_WORKING:
            text = f"You are clocked in from {format_time(timesheet['in_time'])}."
        elif timesheet["status"] == STATUS_ON_BREAK:
            text = "You are on break. Press End Break when you return."
        else:
            work_seconds = calculate_work_seconds(timesheet)
            text = (
                "Your last shift is finished. Press Clock In to start another shift.\n"
                f"Last shift work time: {format_hhmm(work_seconds)}"
            )

        if message_text.strip().lower() == "admin" and saved_role != ROLE_ADMIN:
            text = (
                "This Telegram ID is not configured in ADMIN_CHAT_ID, so it is saved as EMPLOYEE."
            )

        await update.message.reply_text(text, reply_markup=build_keyboard(timesheet))
    except sqlite3.DatabaseError:
        logger.exception("Database error while handling text message.")
        await update.message.reply_text("Có lỗi database. Vui lòng thử lại sau.")
    except TelegramError:
        logger.exception("Telegram error while handling text message.")


# =========================
# Main
# =========================

async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Open time-tracking buttons"),
            BotCommand("myid", "Show your Telegram ID"),
            BotCommand("admin", "Show admin help"),
            BotCommand("users", "Admin: list users"),
            BotCommand("today", "Admin: timesheet report"),
            BotCommand("open_shifts", "Admin: open shifts"),
            BotCommand("backup", "Admin: backup database"),
            BotCommand("set_rate", "Admin: set hourly pay rate"),
            BotCommand("ung", "Record salary advance"),
            BotCommand("salary", "Salary report"),
            BotCommand("edit_time", "Admin: edit a timesheet"),
            BotCommand("reset_today", "Admin: reset today's shift"),
        ]
    )


def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing BOT_TOKEN in .env file.")

    init_db()
    ensure_configured_admins()

    application = Application.builder().token(token).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("myid", myid))
    application.add_handler(CommandHandler("admin", admin_help))
    application.add_handler(CommandHandler("users", users_report))
    application.add_handler(CommandHandler("today", today_report))
    application.add_handler(CommandHandler("open_shifts", open_shifts_report))
    application.add_handler(CommandHandler("backup", backup_database))
    application.add_handler(CommandHandler("set_rate", set_rate))
    application.add_handler(CommandHandler("ung", advance_salary))
    application.add_handler(CommandHandler("salary", salary_report))
    application.add_handler(CommandHandler("edit_time", edit_time))
    application.add_handler(CommandHandler("reset_today", reset_today))
    application.add_handler(CallbackQueryHandler(handle_button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    logger.info("Time-tracking bot is running...")
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
