"""Use cases shared by the web app and the Telegram bot.

Neither surface is allowed its own copy of these. The whole reason the legacy
code drifted - two salary implementations, two copies of parse_time_for_date -
is that there was no layer here for them to share.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session as OrmSession

from app.db import repo
from app.db.models import Timesheet, User
from app.domain import clock, payroll, shifts

ACTION_IN = "in"
ACTION_BREAK = "break"
ACTION_RESUME = "resume"
ACTION_OUT = "out"


@dataclass
class ClockState:
    status: str
    shift: shifts.Shift | None
    worked_seconds: int
    break_seconds: int
    started_at: datetime | None

    @property
    def is_working(self) -> bool:
        return self.status == shifts.STATUS_WORKING

    @property
    def is_on_break(self) -> bool:
        return self.status == shifts.STATUS_ON_BREAK

    @property
    def is_off(self) -> bool:
        return self.shift is None or self.status == shifts.STATUS_FINISHED

    def available_actions(self) -> list[str]:
        if self.is_working:
            return [ACTION_BREAK, ACTION_OUT]
        if self.is_on_break:
            return [ACTION_RESUME]
        return [ACTION_IN]


@dataclass
class ActionResult:
    message_key: str
    params: dict
    state: ClockState


def current_state(session: OrmSession, user_id: int, as_of: datetime | None = None) -> ClockState:
    as_of = as_of or clock.now()
    row = repo.open_shift_for(session, user_id) or repo.latest_shift_for(session, user_id)

    if row is None:
        return ClockState(shifts.STATUS_FINISHED, None, 0, 0, None)

    shift = repo.to_domain(row)
    try:
        worked = shifts.worked_seconds(shift, as_of=as_of)
    except shifts.NegativeDurationError:
        worked = 0

    return ClockState(
        status=shift.status,
        shift=shift,
        worked_seconds=worked,
        break_seconds=shift.total_break_seconds,
        started_at=shift.in_time,
    )


def perform(
    session: OrmSession, user_id: int, action: str, as_of: datetime | None = None
) -> ActionResult:
    """Run a clock action. Raises shifts.ShiftError if the rules forbid it."""
    as_of = as_of or clock.now()
    row = repo.open_shift_for(session, user_id)
    shift = repo.to_domain(row) if row else None

    if action == ACTION_IN:
        created = shifts.clock_in(user_id, as_of, open_shift=shift)
        repo.create_shift(session, created)
        return _result(
            session, user_id, "clocked_in_at", {"time": clock.format_time(as_of)}, as_of
        )

    if shift is None:
        raise shifts.ShiftError("You have not clocked in yet.")

    if action == ACTION_BREAK:
        repo.apply_domain(row, shifts.start_break(shift, as_of))
        return _result(session, user_id, "break_started", {}, as_of)

    if action == ACTION_RESUME:
        repo.apply_domain(row, shifts.end_break(shift, as_of))
        return _result(session, user_id, "break_ended", {}, as_of)

    if action == ACTION_OUT:
        finished = shifts.clock_out(shift, as_of)
        repo.apply_domain(row, finished)
        return _result(
            session,
            user_id,
            "clocked_out_at",
            {
                "time": clock.format_time(as_of),
                "duration": clock.format_duration(shifts.worked_seconds(finished)),
            },
            as_of,
        )

    raise shifts.ShiftError(f"Unknown action: {action}")


def _result(
    session: OrmSession, user_id: int, key: str, params: dict, as_of: datetime
) -> ActionResult:
    session.flush()
    return ActionResult(key, params, current_state(session, user_id, as_of))


# --- reporting -------------------------------------------------------------


@dataclass
class MonthSummary:
    user_id: int
    full_name: str
    month: str
    work_seconds: int = 0
    gross_cents: int = 0
    advance_cents: int = 0
    net_cents: int = 0
    shift_count: int = 0
    problem_shifts: list[tuple[int, str]] = None

    def __post_init__(self):
        self.problem_shifts = self.problem_shifts or []


def month_summary(session: OrmSession, user_id: int, month: str) -> MonthSummary:
    start, end = clock.month_bounds(month)
    user = session.get(User, user_id)
    rows = repo.shifts_between(session, start, end, user_id=user_id)
    advances = repo.advances_between(session, start, end, user_id=user_id)

    entries: list[tuple[date, int]] = []
    problems: list[tuple[int, str]] = []

    for row in rows:
        shift = repo.to_domain(row)
        if shift.in_time is None or shift.out_time is None:
            problems.append((row.id, "incomplete shift"))
            continue
        try:
            entries.append((shift.date, shifts.worked_seconds(shift)))
        except shifts.NegativeDurationError as error:
            problems.append((row.id, str(error)))

    seconds, gross = payroll.gross_for_shifts(entries, repo.rate_periods(session, user_id))
    advance_total = sum(a.amount_cents for a in advances)

    return MonthSummary(
        user_id=user_id,
        full_name=user.full_name if user else str(user_id),
        month=month,
        work_seconds=seconds,
        gross_cents=gross,
        advance_cents=advance_total,
        net_cents=payroll.net_cents(gross, advance_total),
        shift_count=len(entries),
        problem_shifts=problems,
    )


def payroll_for_month(session: OrmSession, month: str) -> list[MonthSummary]:
    return [month_summary(session, user.user_id, month) for user in repo.list_users(session)]


def on_shift_now(session: OrmSession, as_of: datetime | None = None) -> list[dict]:
    as_of = as_of or clock.now()
    board = []
    for row in repo.open_shifts(session):
        shift = repo.to_domain(row)
        try:
            worked = shifts.worked_seconds(shift, as_of=as_of)
        except shifts.NegativeDurationError:
            worked = 0
        board.append(
            {
                "shift": shift,
                "user": row.user,
                "worked_seconds": worked,
                "stale": shift.date < clock.business_date(as_of),
            }
        )
    return board


def exceptions(session: OrmSession, start: date, end: date, as_of: datetime | None = None):
    as_of = as_of or clock.now()
    found = []
    for row in repo.shifts_between(session, start, end):
        problems = shifts.exceptions_for(repo.to_domain(row), as_of)
        if problems:
            found.append({"row": row, "shift": repo.to_domain(row), "problems": problems})
    return found
