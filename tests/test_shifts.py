"""The shift state machine, including the defects found in the live data."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain import clock, shifts

SYD = ZoneInfo("Australia/Sydney")


@pytest.fixture(autouse=True)
def _fixed_timezone():
    os.environ["APP_TIMEZONE"] = "Australia/Sydney"
    yield
    os.environ.pop("APP_TIMEZONE", None)


def at(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day} {hhmm}").replace(tzinfo=SYD)


def test_clock_in_creates_a_working_shift_dated_today():
    shift = shifts.clock_in(1, at("2026-08-20", "09:00"))
    assert shift.status == shifts.STATUS_WORKING
    assert shift.date == date(2026, 8, 20)
    assert shift.total_break_seconds == 0


def test_cannot_clock_in_twice():
    open_shift = shifts.clock_in(1, at("2026-08-20", "09:00"))
    with pytest.raises(shifts.ShiftError, match="already clocked in"):
        shifts.clock_in(1, at("2026-08-20", "10:00"), open_shift=open_shift)


def test_break_requires_being_clocked_in():
    finished = shifts.clock_out(
        shifts.clock_in(1, at("2026-08-20", "09:00")), at("2026-08-20", "17:00")
    )
    with pytest.raises(shifts.ShiftError, match="not currently clocked in"):
        shifts.start_break(finished, at("2026-08-20", "18:00"))


def test_cannot_clock_out_while_on_break():
    shift = shifts.start_break(
        shifts.clock_in(1, at("2026-08-20", "09:00")), at("2026-08-20", "12:00")
    )
    with pytest.raises(shifts.ShiftError, match="end your break"):
        shifts.clock_out(shift, at("2026-08-20", "17:00"))


def test_break_is_deducted_from_paid_time():
    shift = shifts.clock_in(1, at("2026-08-20", "09:00"))
    shift = shifts.start_break(shift, at("2026-08-20", "12:00"))
    shift = shifts.end_break(shift, at("2026-08-20", "12:30"))
    shift = shifts.clock_out(shift, at("2026-08-20", "17:00"))

    assert shift.total_break_seconds == 30 * 60
    assert shifts.worked_seconds(shift) == int(7.5 * 3600)


def test_running_break_counts_live_without_being_committed():
    """The employee screen's live timer must not tick while on break."""
    shift = shifts.clock_in(1, at("2026-08-20", "09:00"))
    shift = shifts.start_break(shift, at("2026-08-20", "12:00"))

    assert shifts.worked_seconds(shift, as_of=at("2026-08-20", "12:00")) == 3 * 3600
    assert shifts.worked_seconds(shift, as_of=at("2026-08-20", "12:45")) == 3 * 3600
    assert shift.total_break_seconds == 0  # nothing persisted until end_break


# --- the overnight defect --------------------------------------------------


def test_overnight_shift_is_paid_across_midnight():
    """Live shift 19: 2026-06-09 08:00 -> 2026-06-10 00:30 = 16.5h."""
    shift = shifts.clock_in(1, at("2026-06-09", "08:00"))
    shift = shifts.clock_out(shift, at("2026-06-10", "00:30"))

    assert shift.date == date(2026, 6, 9)
    assert shifts.crosses_midnight(shift)
    assert shifts.worked_seconds(shift) == int(16.5 * 3600)


def test_clock_out_before_clock_in_is_refused_not_clamped():
    """The exact corruption dashboard_time_tracker.py:713 produces.

    The legacy path wrapped this in max(0, ...) and silently paid zero.
    """
    shift = shifts.clock_in(1, at("2026-06-09", "08:00"))
    with pytest.raises(shifts.NegativeDurationError):
        shifts.clock_out(shift, at("2026-06-09", "00:30"))


def test_negative_duration_raises_rather_than_paying_zero():
    corrupted = shifts.Shift(
        id=19,
        user_id=1,
        date=date(2026, 6, 9),
        status=shifts.STATUS_FINISHED,
        in_time=at("2026-06-09", "08:00"),
        out_time=at("2026-06-09", "00:30"),  # date forced back by the old editor
    )
    with pytest.raises(shifts.NegativeDurationError, match="negative duration"):
        shifts.worked_seconds(corrupted)


def test_parse_clock_time_can_place_a_clock_out_on_the_next_day():
    """The fix: the caller says which day the clock-out belongs to."""
    same_day = clock.parse_clock_time(date(2026, 6, 9), "00:30")
    next_day = clock.parse_clock_time(date(2026, 6, 9), "00:30", day_offset=1)

    assert same_day.date() == date(2026, 6, 9)
    assert next_day.date() == date(2026, 6, 10)


# --- exceptions view -------------------------------------------------------


def test_stale_open_shift_is_flagged():
    """Live shift 47: WORKING since 2026-07-05, never closed."""
    shift = shifts.Shift(
        id=47,
        user_id=1,
        date=date(2026, 7, 5),
        status=shifts.STATUS_WORKING,
        in_time=at("2026-07-05", "09:15"),
        total_break_seconds=10228,
    )
    problems = shifts.exceptions_for(shift, at("2026-08-20", "14:00"))
    assert any("missing clock-out" in p for p in problems)


def test_finished_without_clock_out_is_flagged():
    """Live shift 16."""
    shift = shifts.Shift(
        id=16,
        user_id=1,
        date=date(2026, 6, 10),
        status=shifts.STATUS_FINISHED,
        in_time=at("2026-06-10", "13:30"),
        out_time=None,
    )
    problems = shifts.exceptions_for(shift, at("2026-08-20", "14:00"))
    # Exactly one: measuring a missing clock-out against the wall clock would
    # also report "longer than 16 hours", which is noise, not a second fault.
    assert problems == ["Marked finished but has no clock-out time"]


def test_clean_shift_has_no_exceptions():
    shift = shifts.clock_out(
        shifts.clock_in(1, at("2026-08-20", "09:00")), at("2026-08-20", "17:00")
    )
    assert shifts.exceptions_for(shift, at("2026-08-20", "18:00")) == []
