"""Sign-in, resolved from a Telegram-issued link.

There are no passwords and no new accounts. The bot hands out a short-lived
single-use link; opening it burns that link and sets a long-lived session
cookie. Identity is the Telegram user id, which is already the primary key of
every timesheet and advance, so nothing had to be remapped.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session as OrmSession

from app.config import get_settings
from app.db import repo
from app.db.models import User
from app.db.session import get_session

COOKIE_NAME = "tt_session"


def current_user(request: Request, session: OrmSession = Depends(get_session)) -> User | None:
    return repo.user_for_session(session, request.cookies.get(COOKIE_NAME))


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in through Telegram."
        )
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    """The admin console edits clock times and pay rates.

    The legacy dashboard had no check at all, which was survivable only
    because it was bound to 127.0.0.1. On Fly it is on the public internet.
    """
    if not repo.is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins only.")
    return user


def set_session_cookie(response, token: str, max_age_days: int) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max_age_days * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME)
