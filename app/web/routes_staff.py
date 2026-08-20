"""The employee clock screen."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session as OrmSession

from app.config import get_settings
from app.db import repo
from app.db.models import User
from app.db.session import get_session
from app.domain import clock, payroll, shifts
from app.domain.strings import t
from app.services import current_state, month_summary, perform
from app.web import auth
from app.web.templating import templates

router = APIRouter()


@router.get("/signin", response_class=HTMLResponse)
def signin(request: Request):
    return templates.TemplateResponse(request, "signin.html", {"t": _translator()})


@router.get("/auth/{token}")
def consume_magic_link(token: str, session: OrmSession = Depends(get_session)):
    """Burn the link, hand out a session cookie."""
    session_token = repo.exchange_magic_link(session, token)
    if session_token is None:
        return RedirectResponse("/signin?expired=1", status_code=303)

    response = RedirectResponse("/", status_code=303)
    auth.set_session_cookie(response, session_token, get_settings().session_days)
    return response


@router.post("/signout")
def signout(request: Request, session: OrmSession = Depends(get_session)):
    token = request.cookies.get(auth.COOKIE_NAME)
    if token:
        repo.revoke_session(session, token)
    response = RedirectResponse("/signin", status_code=303)
    auth.clear_session_cookie(response)
    return response


@router.get("/", response_class=HTMLResponse)
def clock_screen(
    request: Request,
    user: User | None = Depends(auth.current_user),
    session: OrmSession = Depends(get_session),
):
    if user is None:
        return RedirectResponse("/signin", status_code=303)

    now = clock.now()
    state = current_state(session, user.user_id, as_of=now)
    summary = month_summary(session, user.user_id, now.strftime("%Y-%m"))

    return templates.TemplateResponse(
        request,
        "clock.html",
        {
            "t": _translator(),
            "user": user,
            "state": state,
            "summary": summary,
            "now": now,
            "is_admin": repo.is_admin(user),
            "format_time": clock.format_time,
            "format_duration": clock.format_duration,
            "format_hhmm": clock.format_hhmm,
            "format_money": payroll.format_money,
            "message": request.query_params.get("msg"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/clock/{action}")
def clock_action(
    action: str,
    user: User = Depends(auth.require_user),
    session: OrmSession = Depends(get_session),
):
    try:
        result = perform(session, user.user_id, action)
    except shifts.ShiftError as error:
        return RedirectResponse(f"/?error={error}", status_code=303)

    message = t(result.message_key, get_settings().language, **result.params)
    return RedirectResponse(f"/?msg={message}", status_code=303)


@router.get("/me/shifts", response_class=HTMLResponse)
def my_shifts(
    request: Request,
    month: str | None = None,
    user: User = Depends(auth.require_user),
    session: OrmSession = Depends(get_session),
):
    month = month or clock.now().strftime("%Y-%m")
    start, end = clock.month_bounds(month)
    rows = repo.shifts_between(session, start, end, user_id=user.user_id)

    entries = []
    for row in rows:
        shift = repo.to_domain(row)
        try:
            worked = shifts.worked_seconds(shift)
        except shifts.NegativeDurationError:
            worked = None
        entries.append({"row": row, "shift": shift, "worked_seconds": worked})

    return templates.TemplateResponse(
        request,
        "shifts.html",
        {
            "t": _translator(),
            "user": user,
            "month": month,
            "entries": entries,
            "summary": month_summary(session, user.user_id, month),
            "is_admin": repo.is_admin(user),
            "format_time": clock.format_time,
            "format_duration": clock.format_duration,
            "format_money": payroll.format_money,
        },
    )


def _translator():
    language = get_settings().language

    def translate(key: str, **kwargs) -> str:
        return t(key, language, **kwargs)

    return translate
