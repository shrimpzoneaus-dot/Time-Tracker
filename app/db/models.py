"""SQLAlchemy models.

The four legacy tables keep their names, columns and semantics exactly; only
the timestamp type changes (naive local text -> timestamptz). Everything new
is additive, so a migrated database still answers every question the old one
did.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    # This is the Telegram user id, and it stays that way: it is what every
    # timesheet and advance already references, and what web sign-in resolves
    # to. No identity remapping anywhere in the migration.
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="EMPLOYEE")

    # Retained deliberately even though rate_history is now authoritative for
    # pay: it is the "current rate" convenience field the admin screens show,
    # and keeping it makes a rollback to the legacy code trivial.
    hourly_rate_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    timesheets: Mapped[list["Timesheet"]] = relationship(back_populates="user")
    rates: Mapped[list["RateHistory"]] = relationship(back_populates="user")


class Timesheet(Base):
    __tablename__ = "timesheets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.user_id"), nullable=False
    )

    # The local business date the shift belongs to. A shift that runs past
    # midnight keeps the date it started on; out_time carries the real instant.
    date: Mapped[date] = mapped_column(Date, nullable=False)

    in_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    out_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    break_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_break_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False)

    # Additive: /reset_today used to DELETE rows outright. It soft-deletes now,
    # so a disputed correction is still answerable.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="timesheets")

    __table_args__ = (Index("idx_timesheets_user_date", "user_id", "date"),)


class Advance(Base):
    __tablename__ = "advances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.user_id"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("idx_advances_user_date", "user_id", "date"),)


class RateHistory(Base):
    """Dated pay rates.

    Fixes the retroactive-raise defect: pay used to be hours times the
    employee's CURRENT rate, so any raise silently re-priced every past month
    and an issued payslip could not be reproduced. Migration seeds one row per
    employee at 1970-01-01 with their present rate, which leaves every existing
    figure untouched.
    """

    __tablename__ = "rate_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.user_id"), nullable=False
    )
    hourly_rate_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger)

    user: Mapped[User] = relationship(back_populates="rates")

    __table_args__ = (Index("idx_rate_history_user_from", "user_id", "effective_from"),)


class EditLog(Base):
    """Who changed which figure, when, and from what.

    If an employee ever disputes their hours, this table is the entire
    argument. Nothing that touches a clock time, break, status, rate or advance
    is allowed to skip it.
    """

    __tablename__ = "edit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    field: Mapped[str] = mapped_column(String(64), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("idx_edit_log_entity", "entity", "entity_id"),)


class Session(Base):
    """Web sign-in issued through Telegram."""

    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.user_id"), nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("idx_sessions_user", "user_id"),)
