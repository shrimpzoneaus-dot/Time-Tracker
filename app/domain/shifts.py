"""The shift state machine.

This is the single owner of "what may happen to a shift and when". The web
routes and the Telegram bot both call these functions; neither is allowed its
own copy of the rules, which is how the two salary implementations and the two
copies of parse_time_for_date came about in the first place.

Every function is pure: it takes a Shift and returns a new Shift. Persistence
is the caller's problem.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime

from app.domain import clock

STATUS_WORKING = "WORKING"
STATUS_ON_BREAK = "ON_BREAK"
STATUS_FINISHED = "FINISHED"

OPEN_STATUSES = (STATUS_WORKING, STATUS_ON_BREAK)

# A shift longer than this is almost certainly a forgotten clock-out rather
# than a real day's work. Surfaced in the admin Exceptions view, never
# silently altered.
IMPLAUSIBLE_SHIFT_SECONDS = 16 * 3600


class ShiftError(Exception):
    """A transition the rules forbid. Safe to show to the person who tried."""


class NegativeDurationError(ShiftError):
    """out_time is not after in_time.

    The legacy code clamped this to zero with max(0, ...), which turned a
    corrupted overnight shift into an unpaid one with no warning anywhere.
    It raises now, and the admin Exceptions view catches it.
    """


@dataclass(frozen=True)
class Shift:
    user_id: int
    date: date
    status: str
    in_time: datetime | None = None
    out_time: datetime | None = None
    break_start: datetime | None = None
    total_break_seconds: int = 0
    id: int | None = None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


def clock_in(user_id: int, at: datetime, open_shift: Shift | None = None) -> Shift:
    if open_shift is not None and open_shift.is_open:
        raise ShiftError("You are already clocked in.")
    return Shift(
        user_id=user_id,
        date=clock.business_date(at),
        status=STATUS_WORKING,
        in_time=at,
        total_break_seconds=0,
    )


def start_break(shift: Shift, at: datetime) -> Shift:
    if shift.status != STATUS_WORKING:
        raise ShiftError("You are not currently clocked in.")
    if at < shift.in_time:
        raise ShiftError("A break cannot start before the shift does.")
    return replace(shift, status=STATUS_ON_BREAK, break_start=at)


def end_break(shift: Shift, at: datetime) -> Shift:
    if shift.status != STATUS_ON_BREAK or shift.break_start is None:
        raise ShiftError("You are not currently on break.")
    if at < shift.break_start:
        raise ShiftError("A break cannot end before it starts.")
    elapsed = int((at - shift.break_start).total_seconds())
    return replace(
        shift,
        status=STATUS_WORKING,
        break_start=None,
        total_break_seconds=shift.total_break_seconds + elapsed,
    )


def clock_out(shift: Shift, at: datetime) -> Shift:
    if shift.status == STATUS_ON_BREAK:
        raise ShiftError("Please end your break before clocking out.")
    if shift.status != STATUS_WORKING:
        raise ShiftError("You have not clocked in yet.")
    if shift.in_time is None or at <= shift.in_time:
        raise NegativeDurationError("Clock-out must be after clock-in.")
    finished = replace(shift, status=STATUS_FINISHED, out_time=at)
    # Force the arithmetic now so an impossible shift is refused at the point
    # of creation rather than discovered weeks later on a payslip.
    worked_seconds(finished)
    return finished


def worked_seconds(shift: Shift, as_of: datetime | None = None) -> int:
    """Paid seconds: elapsed time minus breaks.

    For an open shift, pass as_of to get the live figure - that is what drives
    the running timer on the employee screen.
    """
    if shift.in_time is None:
        return 0

    end = shift.out_time or as_of
    if end is None:
        return 0

    elapsed = int((end - shift.in_time).total_seconds())
    breaks = shift.total_break_seconds + _running_break_seconds(shift, as_of)
    worked = elapsed - breaks

    if worked < 0:
        raise NegativeDurationError(
            f"Shift {shift.id or '(new)'} on {shift.date} for user "
            f"{shift.user_id} has a negative duration "
            f"({elapsed}s elapsed, {breaks}s break)."
        )
    return worked


def _running_break_seconds(shift: Shift, as_of: datetime | None) -> int:
    if shift.status != STATUS_ON_BREAK or shift.break_start is None or as_of is None:
        return 0
    return max(0, int((as_of - shift.break_start).total_seconds()))


def crosses_midnight(shift: Shift) -> bool:
    if shift.in_time is None or shift.out_time is None:
        return False
    return clock.to_local(shift.in_time).date() != clock.to_local(shift.out_time).date()


def exceptions_for(shift: Shift, as_of: datetime) -> list[str]:
    """Why this shift needs a human to look at it. Empty list means fine."""
    found: list[str] = []

    if shift.status in OPEN_STATUSES and shift.date < clock.business_date(as_of):
        found.append("Still open from an earlier day - missing clock-out")

    if shift.status == STATUS_FINISHED and shift.out_time is None:
        # Measuring this shift against the clock would report a nonsense
        # duration, so report the hole itself and stop.
        found.append("Marked finished but has no clock-out time")
        return found

    if crosses_midnight(shift):
        found.append("Crosses midnight - check the clock-out date is right")

    try:
        if worked_seconds(shift, as_of) > IMPLAUSIBLE_SHIFT_SECONDS:
            found.append("Longer than 16 hours - likely a forgotten clock-out")
    except NegativeDurationError as error:
        found.append(str(error))

    return found
