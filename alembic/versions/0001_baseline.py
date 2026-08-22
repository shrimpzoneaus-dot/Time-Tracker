"""Baseline schema.

The four legacy tables (users, timesheets, advances) reproduced exactly as the
SQLite database holds them, plus the three additive tables the rebuild needs.

IMPORTANT for an existing database: if the target already carries the legacy
tables, stamp this revision instead of running it -

    DATABASE_URL=<prod> alembic stamp 0001_baseline

Running it against a populated database will fail on "table already exists",
and that failure is the safe outcome. Do not work around it by dropping
anything.

Revision ID: 0001_baseline
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False, server_default="EMPLOYEE"),
        sa.Column("hourly_rate_cents", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "timesheets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("in_time", sa.DateTime(timezone=True)),
        sa.Column("out_time", sa.DateTime(timezone=True)),
        sa.Column("break_start", sa.DateTime(timezone=True)),
        sa.Column("total_break_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_timesheets_user_date", "timesheets", ["user_id", "date"])

    op.create_table(
        "advances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_advances_user_date", "advances", ["user_id", "date"])

    op.create_table(
        "rate_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("hourly_rate_cents", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.BigInteger()),
    )
    op.create_index("idx_rate_history_user_from", "rate_history", ["user_id", "effective_from"])

    op.create_table(
        "edit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entity", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.BigInteger(), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("old_value", sa.Text()),
        sa.Column("new_value", sa.Text()),
        sa.Column("changed_by", sa.BigInteger(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text()),
    )
    op.create_index("idx_edit_log_entity", "edit_log", ["entity", "entity_id"])

    op.create_table(
        "sessions",
        sa.Column("token", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("idx_sessions_user", "sessions", ["user_id"])


def downgrade() -> None:
    # Deliberately does not drop users, timesheets or advances. This revision
    # is the point below which the payroll history lives, and an accidental
    # `alembic downgrade base` must not be able to delete it.
    op.drop_table("sessions")
    op.drop_table("edit_log")
    op.drop_table("rate_history")
