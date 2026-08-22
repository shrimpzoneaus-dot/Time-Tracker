"""Logical backup of the payroll data, and the restore that gets it back.

Why this exists: after cutover, Neon holds the ONLY copy of the payroll
history. The Free plan keeps six hours of history, which answers "we broke it
twenty minutes ago" and does not answer "nobody noticed for a day that a month
of timesheets went missing".

    python scripts/backup_neon.py                      # take one, verify it
    python scripts/backup_neon.py --verify backups/xyz.db
    python scripts/backup_neon.py --restore backups/xyz.db --target <url>

Design contract, matching migrate_sqlite_to_neon.py:

  * The source is read READ-ONLY. A backup run can never damage what it is
    backing up.
  * Nothing counts as a backup until it verifies -- row counts AND every
    employee-month pay figure, recomputed through the same payroll code the
    admin console uses. A backup you cannot trust is worse than none, because
    you stop looking for a better one.
  * Alembic owns the schema. Restore refuses a target it would have to create
    tables for, rather than becoming a second, unversioned source of DDL.
  * Sessions are NOT backed up. They are ephemeral login tokens reissued
    through Telegram, and a file of live session tokens on a disk is a
    liability, not an asset.

The whole module is engine-to-engine, so Neon is only ever a different URL and
the test suite exercises this same code SQLite -> SQLite.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Insertion order is foreign-key order: a timesheet cannot land before its
# user, and edit_log entries point at rows the earlier tables carry.
PAYROLL_TABLES = ("users", "rate_history", "timesheets", "advances", "edit_log")

# users.user_id is the Telegram id, supplied rather than generated, so it has
# no sequence to fix up after a restore.
SEQUENCE_TABLES = ("rate_history", "timesheets", "advances", "edit_log")

DEFAULT_BACKUP_DIR = Path("backups")


def _models() -> dict:
    from app.db.models import Advance, EditLog, RateHistory, Timesheet, User

    return {
        "users": User,
        "rate_history": RateHistory,
        "timesheets": Timesheet,
        "advances": Advance,
        "edit_log": EditLog,
    }


def force_ipv4(url: str) -> str:
    """Pin the connection to an A record.

    IPv6 to Neon times out from the owner's machine and IPv4 works, so a run
    that does not do this hangs until it happens to fall through. Postgres
    only; anything else is handed back untouched.
    """
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    if not parsed.drivername.startswith("postgresql") or not parsed.host:
        return url
    if "hostaddr" in parsed.query:
        return url
    try:
        address = socket.getaddrinfo(parsed.host, None, socket.AF_INET)[0][4][0]
    except OSError:
        return url  # let the driver try, and report its own error
    return parsed.update_query_dict({"hostaddr": address}).render_as_string(hide_password=False)


def connectable_url(url: str) -> str:
    """A URL this project can actually open.

    Mirrors app/config.py: a bare `postgresql://` URL makes SQLAlchemy reach
    for psycopg2, which is not installed and is not wanted -- the project uses
    psycopg 3. Neon hands out the bare form, and it is the bare form sitting in
    .env, so every entry point has to do this or it works in tests and fails
    against the real database.
    """
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return force_ipv4(url)


def safe_url(url: str) -> str:
    """The URL with its password masked, for printing and for the manifest."""
    from sqlalchemy.engine import make_url

    return make_url(url).render_as_string(hide_password=True)


def _engine(url: str):
    from sqlalchemy import create_engine

    return create_engine(connectable_url(url), future=True)


def _url_for(path: Path) -> str:
    return "sqlite+pysqlite:///" + Path(path).as_posix()


def _row_dicts(session, model) -> list[dict]:
    from sqlalchemy import inspect, select

    columns = [column.key for column in inspect(model).mapper.column_attrs]
    return [
        {column: getattr(row, column) for column in columns} for row in session.scalars(select(model))
    ]


def dump(source_url: str, out_path: Path) -> dict:
    """Copy every payroll table into a fresh SQLite file. Returns the manifest."""
    from sqlalchemy.orm import Session as OrmSession

    from app.db.models import Base

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()  # never append into a previous backup

    backup_engine = _engine(_url_for(out_path))
    # create_all is right HERE and wrong on Neon: this file is ours, created
    # empty in this process. Neon's schema belongs to Alembic.
    Base.metadata.create_all(backup_engine)

    rows_taken: dict[str, int] = {}
    with OrmSession(_engine(source_url)) as source, OrmSession(backup_engine) as backup:
        for name, model in _models().items():
            rows = _row_dicts(source, model)
            if rows:
                backup.bulk_insert_mappings(model, rows)
            rows_taken[name] = len(rows)
        backup.commit()

    manifest = {
        "taken_at": datetime.now(tz=timezone.utc).isoformat(),
        "source": safe_url(source_url),
        "tables": list(PAYROLL_TABLES),
        "rows": rows_taken,
    }
    out_path.with_suffix(".json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def payroll_totals(url: str) -> dict[str, tuple[int, int, int, int]]:
    """Every employee-month figure, recomputed from the rows in `url`.

    This is the check that matters. Row counts prove nothing about whether a
    backup would pay people correctly; these numbers are what an employee
    would dispute.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session as OrmSession

    from app.db.models import Advance, Timesheet, User
    from app.services import month_summary

    totals: dict[str, tuple[int, int, int, int]] = {}
    with OrmSession(_engine(url)) as session:
        for user_id in session.scalars(select(User.user_id).order_by(User.user_id)):
            months = {
                day.strftime("%Y-%m")
                for day in session.scalars(
                    select(Timesheet.date).where(Timesheet.user_id == user_id)
                )
            }
            months |= {
                day.strftime("%Y-%m")
                for day in session.scalars(
                    select(Advance.date).where(Advance.user_id == user_id)
                )
            }
            for month in sorted(months):
                summary = month_summary(session, user_id, month)
                totals[f"{user_id} {month}"] = (
                    summary.work_seconds,
                    summary.gross_cents,
                    summary.advance_cents,
                    summary.net_cents,
                )
    return totals


def _counts(url: str) -> dict[str, int]:
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session as OrmSession

    with OrmSession(_engine(url)) as session:
        return {
            name: session.scalar(select(func.count()).select_from(model))
            for name, model in _models().items()
        }


def verify(source_url: str, backup_path: Path) -> list[str]:
    """Everything wrong with this backup, as a list. Empty means keep it."""
    from app.domain.payroll import format_money

    backup_url = _url_for(backup_path)
    failures: list[str] = []

    source_counts, backup_counts = _counts(source_url), _counts(backup_url)
    for table in PAYROLL_TABLES:
        if source_counts[table] != backup_counts[table]:
            failures.append(
                f"{table}: source has {source_counts[table]} rows, "
                f"backup has {backup_counts[table]}"
            )

    source_totals, backup_totals = payroll_totals(source_url), payroll_totals(backup_url)
    for key in sorted(set(source_totals) | set(backup_totals)):
        expected, actual = source_totals.get(key), backup_totals.get(key)
        if expected == actual:
            continue
        if expected is None or actual is None:
            failures.append(f"{key}: present in one copy only")
            continue
        failures.append(
            f"{key}: gross {format_money(expected[1])} in the source, "
            f"{format_money(actual[1])} in the backup"
        )
    return failures


def restore(backup_path: Path, target_url: str) -> dict[str, int]:
    """Load a backup into a target that Alembic has already prepared."""
    from sqlalchemy import func, inspect, select, text
    from sqlalchemy.orm import Session as OrmSession

    from app.db.models import Timesheet

    engine = _engine(target_url)
    missing = set(PAYROLL_TABLES) - set(inspect(engine).get_table_names())
    if missing:
        raise SystemExit(
            f"Target is missing tables: {sorted(missing)}. Run `alembic upgrade head` first."
        )

    restored: dict[str, int] = {}
    with OrmSession(_engine(_url_for(backup_path))) as backup, OrmSession(engine) as target:
        existing = target.scalar(select(func.count()).select_from(Timesheet))
        if existing:
            raise SystemExit(
                f"Refusing to restore: target already holds {existing} timesheets. "
                "Clear it deliberately first -- a restore is not a merge."
            )

        for name, model in _models().items():
            rows = _row_dicts(backup, model)
            if rows:
                target.bulk_insert_mappings(model, rows)
            restored[name] = len(rows)

        # bulk_insert_mappings supplies explicit ids, which leaves Postgres
        # sequences at 1 and the next insert colliding on the primary key.
        if engine.dialect.name == "postgresql":
            for table in SEQUENCE_TABLES:
                if restored[table]:
                    highest = target.scalar(select(func.max(_models()[table].id)))
                    target.execute(
                        text(
                            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                            ":next_id, false)"
                        ),
                        {"next_id": highest + 1},
                    )
        target.commit()
    return restored


def _timestamped_name() -> str:
    return "time_tracker_" + datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".db"


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ModuleNotFoundError:
        pass

    parser = argparse.ArgumentParser(description="Back up and restore the payroll data.")
    parser.add_argument("--source", default=os.getenv("DATABASE_URL"), help="database to read")
    parser.add_argument("--out", type=Path, default=None, help="backup file to write")
    parser.add_argument("--verify", type=Path, help="verify an existing backup and stop")
    parser.add_argument("--restore", type=Path, help="load a backup into --target")
    parser.add_argument("--target", help="where --restore writes (defaults to --source)")
    args = parser.parse_args()

    if not args.source:
        raise SystemExit("No DATABASE_URL. Put it in .env or pass --source.")

    if args.restore:
        target = args.target or args.source
        print(f"Restoring {args.restore} -> {safe_url(target)}")
        for table, count in restore(args.restore, target).items():
            print(f"  {table:<14} {count}")
        return 0

    if args.verify:
        failures = verify(args.source, args.verify)
        for failure in failures:
            print(f"  FAIL {failure}")
        print("Backup verified." if not failures else f"{len(failures)} problem(s).")
        return 1 if failures else 0

    out = args.out or DEFAULT_BACKUP_DIR / _timestamped_name()
    print(f"Source: {safe_url(args.source)}")
    manifest = dump(args.source, out)
    for table, count in manifest["rows"].items():
        print(f"  {table:<14} {count}")

    failures = verify(args.source, out)
    if failures:
        for failure in failures:
            print(f"  FAIL {failure}")
        print(f"\nBackup at {out} did NOT verify. Do not rely on it.")
        return 1

    print(f"\nVerified. Backup at {out}, manifest at {out.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
