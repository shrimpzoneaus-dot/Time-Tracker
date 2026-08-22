"""The admin console: live board, week grid, exceptions, payroll."""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session as OrmSession

from app.db import repo
from app.db.models import Timesheet, User
from app.db.session import get_session
from app.domain import clock, payroll, shifts
from app.services import exceptions, on_shift_now, payroll_for_month
from app.web import auth
from app.web.templating import templates

router = APIRouter(prefix="/admin")

# The bot writes continuously, so a cached console is always a wrong console.
NO_STORE = {"Cache-Control": "no-store"}


def board_context(session: OrmSession, now) -> dict:
    """Everything _board.html needs, shared by the console and its poll."""
    return {
        "board": on_shift_now(session, as_of=now),
        "format_time": clock.format_time,
        "format_duration": clock.format_duration,
    }


@router.get("", response_class=HTMLResponse)
def console(
    request: Request,
    week_of: str | None = None,
    admin: User = Depends(auth.require_admin),
    session: OrmSession = Depends(get_session),
):
    now = clock.now()
    anchor = date.fromisoformat(week_of) if week_of else now.date()
    monday = anchor - timedelta(days=anchor.weekday())
    sunday = monday + timedelta(days=6)

    rows = repo.shifts_between(session, monday, sunday)
    grid: dict[int, dict[date, list]] = {}
    for row in rows:
        shift = repo.to_domain(row)
        try:
            worked = shifts.worked_seconds(shift, as_of=now)
        except shifts.NegativeDurationError:
            worked = None
        grid.setdefault(row.user_id, {}).setdefault(row.date, []).append(
            {"row": row, "shift": shift, "worked_seconds": worked}
        )

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            **board_context(session, now),
            "admin": admin,
            "now": now,
            "users": repo.list_users(session),
            "grid": grid,
            "days": [monday + timedelta(days=i) for i in range(7)],
            "monday": monday,
            "prev_week": (monday - timedelta(days=7)).isoformat(),
            "next_week": (monday + timedelta(days=7)).isoformat(),
            "exceptions": exceptions(session, monday - timedelta(days=90), sunday, as_of=now),
            "format_money": payroll.format_money,
            "message": request.query_params.get("msg"),
            "error": request.query_params.get("error"),
        },
        headers=NO_STORE,
    )


@router.get("/fragment/board", response_class=HTMLResponse)
def board_fragment(
    request: Request,
    admin: User = Depends(auth.require_admin),
    session: OrmSession = Depends(get_session),
):
    """The live board alone, re-rendered for the open console to poll.

    Deliberately only the board. The week grid and the exceptions list carry
    the inline edit forms, and swapping those under an admin would wipe a
    half-typed clock-out correction mid-keystroke.
    """
    return templates.TemplateResponse(
        request, "_board.html", board_context(session, clock.now()), headers=NO_STORE
    )


@router.post("/timesheet/{timesheet_id}")
def edit_timesheet(
    timesheet_id: int,
    in_time: str = Form(""),
    out_time: str = Form(""),
    out_next_day: str = Form(""),
    break_minutes: int = Form(0),
    status: str = Form(shifts.STATUS_FINISHED),
    reason: str = Form(""),
    admin: User = Depends(auth.require_admin),
    session: OrmSession = Depends(get_session),
):
    """Edit a shift.

    out_next_day is the fix for the defect that silently unpaid overnight
    shifts: the legacy form forced the clock-out onto the shift's START date,
    so 22:00-02:00 became out < in and the shift paid zero.
    """
    row = session.get(Timesheet, timesheet_id)
    if row is None:
        return RedirectResponse("/admin?error=Shift not found", status_code=303)

    before = repo.to_domain(row)
    try:
        new_in = clock.parse_clock_time(row.date, in_time) if in_time.strip() else None
        new_out = (
            clock.parse_clock_time(row.date, out_time, day_offset=1 if out_next_day else 0)
            if out_time.strip()
            else None
        )
    except ValueError:
        return RedirectResponse("/admin?error=Times must look like HH:MM", status_code=303)

    if new_in and new_out and new_out <= new_in:
        return RedirectResponse(
            "/admin?error=Clock-out must be after clock-in. Tick 'next day' if the shift "
            "crossed midnight.",
            status_code=303,
        )

    for field, old, new in (
        ("in_time", before.in_time, new_in),
        ("out_time", before.out_time, new_out),
        ("total_break_seconds", before.total_break_seconds, max(0, break_minutes) * 60),
        ("status", before.status, status),
    ):
        if old != new:
            repo.log_edit(
                session, "timesheet", row.id, field, old, new, admin.user_id, reason or None
            )

    row.in_time = new_in
    row.out_time = new_out
    row.total_break_seconds = max(0, break_minutes) * 60
    row.status = status

    return RedirectResponse("/admin?msg=Shift updated", status_code=303)


@router.post("/timesheet/{timesheet_id}/delete")
def delete_timesheet(
    timesheet_id: int,
    reason: str = Form(""),
    admin: User = Depends(auth.require_admin),
    session: OrmSession = Depends(get_session),
):
    row = session.get(Timesheet, timesheet_id)
    if row is None:
        return RedirectResponse("/admin?error=Shift not found", status_code=303)
    repo.soft_delete_shift(session, row, admin.user_id, reason or "removed by admin")
    return RedirectResponse("/admin?msg=Shift removed (recoverable)", status_code=303)


@router.post("/rate")
def set_rate(
    user_id: int = Form(...),
    amount: str = Form(...),
    effective_from: str = Form(""),
    admin: User = Depends(auth.require_admin),
    session: OrmSession = Depends(get_session),
):
    try:
        cents = payroll.parse_money_to_cents(amount)
    except payroll.MoneyError as error:
        return RedirectResponse(f"/admin?error={error}", status_code=303)

    repo.set_rate(
        session,
        user_id,
        cents,
        admin.user_id,
        effective_from=date.fromisoformat(effective_from) if effective_from else None,
    )
    return RedirectResponse("/admin?msg=Rate saved", status_code=303)


@router.post("/advance")
def add_advance(
    user_id: int = Form(...),
    amount: str = Form(...),
    note: str = Form(""),
    admin: User = Depends(auth.require_admin),
    session: OrmSession = Depends(get_session),
):
    try:
        cents = payroll.parse_money_to_cents(amount)
    except payroll.MoneyError as error:
        return RedirectResponse(f"/admin?error={error}", status_code=303)

    repo.add_advance(session, user_id, cents, admin.user_id, note or None)
    return RedirectResponse("/admin?msg=Advance recorded", status_code=303)


@router.get("/payroll", response_class=HTMLResponse)
def payroll_view(
    request: Request,
    month: str | None = None,
    admin: User = Depends(auth.require_admin),
    session: OrmSession = Depends(get_session),
):
    month = month or clock.now().strftime("%Y-%m")
    return templates.TemplateResponse(
        request,
        "payroll.html",
        {
            "admin": admin,
            "month": month,
            "summaries": payroll_for_month(session, month),
            "format_duration": clock.format_duration,
            "format_money": payroll.format_money,
        },
    )


@router.get("/payroll.csv")
def payroll_csv(
    month: str | None = None,
    admin: User = Depends(auth.require_admin),
    session: OrmSession = Depends(get_session),
):
    month = month or clock.now().strftime("%Y-%m")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["User", "ID", "Month", "Hours", "Shifts", "Gross", "Advances", "Net"])
    for row in payroll_for_month(session, month):
        writer.writerow(
            [
                row.full_name,
                row.user_id,
                row.month,
                f"{row.work_seconds / 3600:.2f}",
                row.shift_count,
                f"{row.gross_cents / 100:.2f}",
                f"{row.advance_cents / 100:.2f}",
                f"{row.net_cents / 100:.2f}",
            ]
        )
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="payroll-{month}.csv"'},
    )
