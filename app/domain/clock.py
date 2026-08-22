"""Time handling for the tracker.

Every naive local string the legacy SQLite database holds is interpreted in
APP_TIMEZONE. Everything this module hands back is timezone-aware, so the
"which day is 02:00 on" question can never be answered by accident again.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DB_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_TIMEZONE = "Australia/Sydney"


def get_timezone() -> ZoneInfo:
    return ZoneInfo(os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE))


def now() -> datetime:
    return datetime.now(tz=get_timezone())


def today() -> date:
    return now().date()


def business_date(moment: datetime) -> date:
    """The work day a moment belongs to.

    Deliberately the local calendar date, matching how the legacy schema fills
    timesheets.date. A shift that runs past midnight keeps the date it started
    on; only out_time crosses over.
    """
    return to_local(moment).date()


def to_local(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=get_timezone())
    return moment.astimezone(get_timezone())


def to_utc(moment: datetime) -> datetime:
    return to_local(moment).astimezone(ZoneInfo("UTC"))


def parse_legacy(value: str | None) -> datetime | None:
    """Read a naive 'YYYY-MM-DD HH:MM:SS' string as local time.

    Used by the migration and the parity tests. New code never writes this
    format.
    """
    if not value:
        return None
    parsed = datetime.strptime(value.strip(), DB_DATETIME_FORMAT)
    return parsed.replace(tzinfo=get_timezone())


def format_legacy(moment: datetime) -> str:
    return to_local(moment).strftime(DB_DATETIME_FORMAT)


def parse_clock_time(work_date: date, raw_time: str, day_offset: int = 0) -> datetime:
    """Combine a work date with an HH:MM entered by an admin.

    day_offset exists because the legacy helper of the same name did not: it
    forced every time onto the shift's start date, so editing a 22:00-02:00
    shift silently moved the clock-out back twelve hours and the shift was
    paid as zero. Callers editing a clock-out that crossed midnight pass
    day_offset=1.
    """
    parsed_time = datetime.strptime(raw_time.strip(), "%H:%M").time()
    stamp = datetime.combine(work_date + timedelta(days=day_offset), parsed_time)
    return stamp.replace(tzinfo=get_timezone())


def format_time(moment: datetime | None) -> str:
    if moment is None:
        return "-"
    return to_local(moment).strftime("%H:%M")


def format_hhmm(total_seconds: int) -> str:
    total_seconds = max(0, int(total_seconds))
    return f"{total_seconds // 3600:02d}:{(total_seconds % 3600) // 60:02d}"


def format_duration(total_seconds: int) -> str:
    total_seconds = max(0, int(total_seconds))
    hours, minutes = total_seconds // 3600, (total_seconds % 3600) // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def month_bounds(month: str) -> tuple[date, date]:
    """'YYYY-MM' -> (first day, last day) inclusive."""
    first = datetime.strptime(month, "%Y-%m").date()
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    return first, next_first - timedelta(days=1)
