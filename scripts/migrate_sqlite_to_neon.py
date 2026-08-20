"""One-way SQLite -> Postgres migration for the Time Tracker.

Design contract: the legacy file is opened READ-ONLY and is never written to,
so it remains the rollback artifact. Nothing is committed to Postgres unless
every verification passes.

    python scripts/migrate_sqlite_to_neon.py --dry-run
    DATABASE_URL=postgresql+psycopg://... python scripts/migrate_sqlite_to_neon.py

Exit code is non-zero on any discrepancy.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domain import clock, payroll, shifts  # noqa: E402

SEED_RATE_DATE = date(1970, 1, 1)


@dataclass
class LegacyData:
    users: list[dict]
    timesheets: list[dict]
    advances: list[dict]


@dataclass
class MigrationPlan:
    users: list[dict] = field(default_factory=list)
    timesheets: list[dict] = field(default_factory=list)
    advances: list[dict] = field(default_factory=list)
    rate_history: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def read_legacy(db_path: Path) -> LegacyData:
    if not db_path.exists():
        raise SystemExit(f"Source database not found: {db_path}")

    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return LegacyData(
            users=[dict(r) for r in conn.execute("SELECT * FROM users")],
            timesheets=[dict(r) for r in conn.execute("SELECT * FROM timesheets ORDER BY id")],
            advances=[dict(r) for r in conn.execute("SELECT * FROM advances ORDER BY id")],
        )
    finally:
        conn.close()


def transform(data: LegacyData, migrated_at: datetime | None = None) -> MigrationPlan:
    migrated_at = migrated_at or clock.now()
    plan = MigrationPlan()

    for row in data.users:
        plan.users.append(
            {
                "user_id": int(row["user_id"]),
                "full_name": row["full_name"],
                "role": row["role"] or "EMPLOYEE",
                "hourly_rate_cents": int(row["hourly_rate_cents"] or 0),
            }
        )
        # One seeded period from 1970 means every historical shift prices at
        # exactly the rate that produces today's figures.
        plan.rate_history.append(
            {
                "user_id": int(row["user_id"]),
                "hourly_rate_cents": int(row["hourly_rate_cents"] or 0),
                "effective_from": SEED_RATE_DATE,
                "created_at": clock.to_utc(migrated_at),
                "created_by": None,
            }
        )

    for row in data.timesheets:
        plan.timesheets.append(
            {
                "id": int(row["id"]),
                "user_id": int(row["user_id"]),
                "date": date.fromisoformat(row["date"]),
                "in_time": _utc(row["in_time"]),
                "out_time": _utc(row["out_time"]),
                "break_start": _utc(row["break_start"]),
                "total_break_seconds": int(row["total_break_seconds"] or 0),
                "status": row["status"],
                "deleted_at": None,
            }
        )

    for row in data.advances:
        plan.advances.append(
            {
                "id": int(row["id"]),
                "user_id": int(row["user_id"]),
                "date": date.fromisoformat(row["date"]),
                "amount_cents": int(row["amount_cents"]),
                "note": row["note"],
                "created_at": _utc(row["created_at"]) or clock.to_utc(migrated_at),
                "deleted_at": None,
            }
        )

    return plan


def _utc(value: str | None) -> datetime | None:
    moment = clock.parse_legacy(value)
    return clock.to_utc(moment) if moment else None


def verify(data: LegacyData, plan: MigrationPlan) -> tuple[list[str], list[str]]:
    """Returns (failures, warnings). A non-empty failures list aborts."""
    failures: list[str] = []
    warnings: list[str] = []

    # 1. Nothing lost, nothing invented.
    for name, before, after in (
        ("users", data.users, plan.users),
        ("timesheets", data.timesheets, plan.timesheets),
        ("advances", data.advances, plan.advances),
    ):
        if len(before) != len(after):
            failures.append(f"{name}: {len(before)} rows in, {len(after)} rows out")

    if len(plan.rate_history) != len(plan.users):
        failures.append("rate_history must have exactly one seeded row per user")

    # 2. Primary keys preserved verbatim.
    for name, before, after in (
        ("timesheets", data.timesheets, plan.timesheets),
        ("advances", data.advances, plan.advances),
    ):
        if [r["id"] for r in before] != [r["id"] for r in after]:
            failures.append(f"{name}: primary keys were not preserved")

    # 3. Every timestamp round-trips to the identical local wall clock.
    for before, after in zip(data.timesheets, plan.timesheets):
        for column in ("in_time", "out_time", "break_start"):
            original = before[column]
            converted = after[column]
            if original is None:
                if converted is not None:
                    failures.append(f"timesheet {before['id']}.{column}: null became a value")
                continue
            restored = clock.format_legacy(converted)
            if restored != original.strip():
                failures.append(
                    f"timesheet {before['id']}.{column}: {original!r} -> {restored!r}"
                )

    # 4. Every employee-month pays the same, to the cent.
    failures.extend(_verify_payroll(data, plan, warnings))

    # 5. Rows an admin will need to look at. Never a blocker - the migration
    #    carries the data across exactly as found, warts included.
    as_of = clock.now()
    for row in data.timesheets:
        shift = shifts.Shift(
            id=row["id"],
            user_id=row["user_id"],
            date=date.fromisoformat(row["date"]),
            status=row["status"],
            in_time=clock.parse_legacy(row["in_time"]),
            out_time=clock.parse_legacy(row["out_time"]),
            break_start=clock.parse_legacy(row["break_start"]),
            total_break_seconds=int(row["total_break_seconds"] or 0),
        )
        for problem in shifts.exceptions_for(shift, as_of):
            warnings.append(f"shift {row['id']} ({row['date']}, user {row['user_id']}): {problem}")

    return failures, warnings


def _verify_payroll(data: LegacyData, plan: MigrationPlan, warnings: list[str]) -> list[str]:
    failures: list[str] = []
    rates = {u["user_id"]: int(u["hourly_rate_cents"] or 0) for u in data.users}

    buckets: dict[tuple[int, str], list[tuple[date, int]]] = {}
    for row in data.timesheets:
        key = (row["user_id"], row["date"][:7])
        buckets.setdefault(key, []).append(
            (date.fromisoformat(row["date"]), _legacy_work_seconds(row))
        )

    for (user_id, month), entries in sorted(buckets.items()):
        rate = rates.get(user_id, 0)
        total_seconds = sum(seconds for _, seconds in entries)

        legacy_gross = round(total_seconds * rate / 3600)
        periods = [payroll.RatePeriod(rate, SEED_RATE_DATE)]
        _, new_gross = payroll.gross_for_shifts(entries, periods)

        delta = new_gross - legacy_gross
        if delta == 0:
            continue

        exact = (Decimal(total_seconds) * Decimal(rate)) / Decimal(3600)
        if delta == 1 and exact == exact.to_integral_value(rounding="ROUND_DOWN") + Decimal("0.5"):
            # The legacy round() is banker's rounding, so an exact half cent
            # went to the nearest EVEN cent. We round half up, in the
            # employee's favour. Reported, not silently applied.
            warnings.append(
                f"user {user_id} {month}: gross {payroll.format_money(legacy_gross)} -> "
                f"{payroll.format_money(new_gross)} (exact half cent, rounded up)"
            )
        else:
            failures.append(
                f"user {user_id} {month}: gross would change "
                f"{payroll.format_money(legacy_gross)} -> {payroll.format_money(new_gross)}"
            )

    return failures


def _legacy_work_seconds(row: dict) -> int:
    if not row["in_time"] or not row["out_time"]:
        return 0
    in_time = clock.parse_legacy(row["in_time"])
    out_time = clock.parse_legacy(row["out_time"])
    return max(0, int((out_time - in_time).total_seconds()) - int(row["total_break_seconds"] or 0))


def load(plan: MigrationPlan, database_url: str) -> None:
    from sqlalchemy import create_engine, func, inspect, select
    from sqlalchemy.orm import Session as OrmSession

    from app.db.models import Advance, RateHistory, Timesheet, User

    engine = create_engine(database_url, future=True)

    # Alembic owns the schema. This script only moves data. create_all() here
    # would be a second, unversioned source of DDL truth - the exact mistake
    # documented in shrimpzone-concierge/fly.toml.
    missing = {"users", "timesheets", "advances", "rate_history"} - set(
        inspect(engine).get_table_names()
    )
    if missing:
        raise SystemExit(
            f"Target is missing tables: {sorted(missing)}. Run `alembic upgrade head` first."
        )

    with OrmSession(engine) as session:
        existing = session.scalar(select(func.count()).select_from(Timesheet))
        if existing:
            raise SystemExit(
                f"Refusing to run: target already holds {existing} timesheets. "
                "This migration is one-way and single-use."
            )

        session.bulk_insert_mappings(User, plan.users)
        session.bulk_insert_mappings(RateHistory, plan.rate_history)
        session.bulk_insert_mappings(Timesheet, plan.timesheets)
        session.bulk_insert_mappings(Advance, plan.advances)

        # bulk_insert_mappings supplies explicit ids, which leaves Postgres
        # sequences at 1 and the next insert colliding on the primary key.
        # SQLite has no sequences, so a local rehearsal skips this.
        if engine.dialect.name == "postgresql":
            for table, rows in (("timesheets", plan.timesheets), ("advances", plan.advances)):
                if rows:
                    session.execute(
                        _reset_sequence_sql(table), {"next_id": max(r["id"] for r in rows) + 1}
                    )
        else:
            print(f"({engine.dialect.name}: skipping sequence reset - Postgres only)")

        session.commit()


def _reset_sequence_sql(table: str):
    from sqlalchemy import text

    return text(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), :next_id, false)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="time_tracker.db", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="verify only, write nothing")
    args = parser.parse_args()

    data = read_legacy(args.source)
    plan = transform(data)
    failures, warnings = verify(data, plan)

    print(f"Source     : {args.source}")
    print(
        f"Rows       : {len(plan.users)} users, {len(plan.timesheets)} timesheets, "
        f"{len(plan.advances)} advances, {len(plan.rate_history)} seeded rates"
    )

    if warnings:
        print(f"\nWarnings ({len(warnings)}) - carried across as-is, for an admin to review:")
        for line in warnings:
            print(f"  - {line}")

    if failures:
        print(f"\nFAILED ({len(failures)}):")
        for line in failures:
            print(f"  - {line}")
        print("\nNothing was written.")
        return 1

    print("\nVerification passed: row counts, primary keys, timestamps and every")
    print("employee-month total reconcile against the legacy arithmetic.")

    if args.dry_run:
        print("Dry run - nothing written.")
        return 0

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("\nDATABASE_URL is not set. Re-run with it, or pass --dry-run.")
        return 1

    load(plan, database_url)
    print(f"Loaded into {database_url.split('@')[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
