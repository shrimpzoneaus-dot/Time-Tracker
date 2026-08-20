"""Query layer.

Translates between ORM rows and the plain domain objects in app.domain, so the
state machine and the pay arithmetic never see SQLAlchemy and stay trivially
testable.
"""

from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.config import get_settings
from app.db.models import Advance, EditLog, RateHistory, Session, Timesheet, User
from app.domain import clock, payroll, shifts

ROLE_ADMIN = "ADMIN"
ROLE_EMPLOYEE = "EMPLOYEE"


# --- users -----------------------------------------------------------------


def upsert_user(session: OrmSession, user_id: int, full_name: str) -> User:
    user = session.get(User, user_id)
    if user is None:
        role = ROLE_ADMIN if user_id in get_settings().configured_admin_ids else ROLE_EMPLOYEE
        user = User(user_id=user_id, full_name=full_name, role=role, hourly_rate_cents=0)
        session.add(user)
        session.flush()
        # Every employee needs a rate period, even a zero one, or resolve_rate
        # has nothing to answer with.
        session.add(
            RateHistory(
                user_id=user_id,
                hourly_rate_cents=0,
                effective_from=date(1970, 1, 1),
                created_at=clock.now(),
            )
        )
    elif full_name and user.full_name != full_name:
        user.full_name = full_name
    return user


def list_users(session: OrmSession) -> list[User]:
    return list(session.scalars(select(User).order_by(User.full_name)))


def is_admin(user: User | None) -> bool:
    if user is None:
        return False
    return user.role == ROLE_ADMIN or user.user_id in get_settings().configured_admin_ids


# --- shifts ----------------------------------------------------------------


def to_domain(row: Timesheet) -> shifts.Shift:
    return shifts.Shift(
        id=row.id,
        user_id=row.user_id,
        date=row.date,
        status=row.status,
        in_time=row.in_time,
        out_time=row.out_time,
        break_start=row.break_start,
        total_break_seconds=row.total_break_seconds,
    )


def apply_domain(row: Timesheet, shift: shifts.Shift) -> Timesheet:
    row.date = shift.date
    row.status = shift.status
    row.in_time = shift.in_time
    row.out_time = shift.out_time
    row.break_start = shift.break_start
    row.total_break_seconds = shift.total_break_seconds
    return row


def open_shift_for(session: OrmSession, user_id: int) -> Timesheet | None:
    return session.scalars(
        select(Timesheet)
        .where(
            Timesheet.user_id == user_id,
            Timesheet.status.in_(shifts.OPEN_STATUSES),
            Timesheet.deleted_at.is_(None),
        )
        .order_by(Timesheet.id.desc())
    ).first()


def latest_shift_for(session: OrmSession, user_id: int) -> Timesheet | None:
    return session.scalars(
        select(Timesheet)
        .where(Timesheet.user_id == user_id, Timesheet.deleted_at.is_(None))
        .order_by(Timesheet.id.desc())
    ).first()


def create_shift(session: OrmSession, shift: shifts.Shift) -> Timesheet:
    row = apply_domain(Timesheet(user_id=shift.user_id), shift)
    session.add(row)
    session.flush()
    return row


def shifts_between(
    session: OrmSession, start: date, end: date, user_id: int | None = None
) -> list[Timesheet]:
    query = select(Timesheet).where(
        Timesheet.date >= start, Timesheet.date <= end, Timesheet.deleted_at.is_(None)
    )
    if user_id is not None:
        query = query.where(Timesheet.user_id == user_id)
    return list(session.scalars(query.order_by(Timesheet.date, Timesheet.id)))


def open_shifts(session: OrmSession) -> list[Timesheet]:
    return list(
        session.scalars(
            select(Timesheet)
            .where(Timesheet.status.in_(shifts.OPEN_STATUSES), Timesheet.deleted_at.is_(None))
            .order_by(Timesheet.in_time)
        )
    )


def soft_delete_shift(session: OrmSession, row: Timesheet, actor_id: int, reason: str) -> None:
    """Never a hard DELETE.

    /reset_today used to remove rows outright, which destroyed the only
    evidence of what a disputed shift originally said.
    """
    row.deleted_at = clock.now()
    log_edit(session, "timesheet", row.id, "deleted_at", None, row.deleted_at, actor_id, reason)


# --- rates and advances ----------------------------------------------------


def rate_periods(session: OrmSession, user_id: int) -> list[payroll.RatePeriod]:
    rows = session.scalars(
        select(RateHistory)
        .where(RateHistory.user_id == user_id)
        .order_by(RateHistory.effective_from)
    )
    return [payroll.RatePeriod(r.hourly_rate_cents, r.effective_from) for r in rows]


def set_rate(
    session: OrmSession,
    user_id: int,
    hourly_rate_cents: int,
    actor_id: int,
    effective_from: date | None = None,
) -> None:
    user = session.get(User, user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found.")

    effective_from = effective_from or clock.today()
    previous = user.hourly_rate_cents

    session.add(
        RateHistory(
            user_id=user_id,
            hourly_rate_cents=hourly_rate_cents,
            effective_from=effective_from,
            created_at=clock.now(),
            created_by=actor_id,
        )
    )
    # Kept in step as the "current rate" the admin screens display; pay itself
    # is resolved from rate_history.
    user.hourly_rate_cents = hourly_rate_cents
    log_edit(
        session,
        "user",
        user_id,
        "hourly_rate_cents",
        previous,
        hourly_rate_cents,
        actor_id,
        f"effective {effective_from}",
    )


def add_advance(
    session: OrmSession, user_id: int, amount_cents: int, actor_id: int, note: str | None = None
) -> Advance:
    advance = Advance(
        user_id=user_id,
        date=clock.today(),
        amount_cents=amount_cents,
        note=note,
        created_at=clock.now(),
    )
    session.add(advance)
    session.flush()
    log_edit(session, "advance", advance.id, "amount_cents", None, amount_cents, actor_id, note)
    return advance


def advances_between(
    session: OrmSession, start: date, end: date, user_id: int | None = None
) -> list[Advance]:
    query = select(Advance).where(
        Advance.date >= start, Advance.date <= end, Advance.deleted_at.is_(None)
    )
    if user_id is not None:
        query = query.where(Advance.user_id == user_id)
    return list(session.scalars(query.order_by(Advance.date)))


# --- audit -----------------------------------------------------------------


def log_edit(
    session: OrmSession,
    entity: str,
    entity_id: int,
    field: str,
    old_value,
    new_value,
    changed_by: int,
    reason: str | None = None,
) -> None:
    session.add(
        EditLog(
            entity=entity,
            entity_id=entity_id,
            field=field,
            old_value=None if old_value is None else str(old_value),
            new_value=None if new_value is None else str(new_value),
            changed_by=changed_by,
            changed_at=clock.now(),
            reason=reason,
        )
    )


def edit_history(session: OrmSession, entity: str, entity_id: int) -> list[EditLog]:
    return list(
        session.scalars(
            select(EditLog)
            .where(EditLog.entity == entity, EditLog.entity_id == entity_id)
            .order_by(EditLog.changed_at.desc())
        )
    )


# --- sessions --------------------------------------------------------------


def create_magic_link(session: OrmSession, user_id: int) -> str:
    """Short-lived, single-use token delivered over Telegram."""
    token = secrets.token_urlsafe(32)
    now = clock.now()
    session.add(
        Session(
            token=token,
            user_id=user_id,
            issued_at=now,
            expires_at=now + timedelta(minutes=get_settings().magic_link_minutes),
        )
    )
    return token


def exchange_magic_link(session: OrmSession, token: str) -> str | None:
    """Burn the link, hand back a long-lived session token.

    Rotating rather than reusing means a link sitting in Telegram history is
    worthless once it has been opened.
    """
    row = _live_session(session, token)
    if row is None:
        return None

    row.revoked_at = clock.now()
    now = clock.now()
    new_token = secrets.token_urlsafe(32)
    session.add(
        Session(
            token=new_token,
            user_id=row.user_id,
            issued_at=now,
            expires_at=now + timedelta(days=get_settings().session_days),
        )
    )
    return new_token


def user_for_session(session: OrmSession, token: str | None) -> User | None:
    if not token:
        return None
    row = _live_session(session, token)
    return session.get(User, row.user_id) if row else None


def revoke_session(session: OrmSession, token: str) -> None:
    row = session.get(Session, token)
    if row and row.revoked_at is None:
        row.revoked_at = clock.now()


def _live_session(session: OrmSession, token: str) -> Session | None:
    row = session.get(Session, token)
    if row is None or row.revoked_at is not None:
        return None
    expires_at = row.expires_at
    if expires_at.tzinfo is None:  # SQLite round-trips naive datetimes
        expires_at = clock.to_local(expires_at)
    return None if expires_at < clock.now() else row
