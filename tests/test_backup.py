"""Neon holds the only copy of the payroll data once cutover happens.

The Free plan keeps six hours of history, which answers "we broke it twenty
minutes ago" and does not answer "nobody noticed for a day that a month of
timesheets went missing". These tests pin a logical backup that closes that
gap and -- more importantly -- pin that it can be restored, because an
untested restore path is the standard way backups fail.

Everything here runs SQLite -> SQLite. The script is engine-to-engine on
purpose, so Neon is only ever a different URL and this suite exercises the
real code rather than a stand-in.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import backup_neon  # noqa: E402

SYDNEY = "Australia/Sydney"


@pytest.fixture(autouse=True)
def timezone(monkeypatch):
    monkeypatch.setenv("APP_TIMEZONE", SYDNEY)


def url_for(path: Path) -> str:
    return "sqlite+pysqlite:///" + path.as_posix()


def build(path: Path):
    from sqlalchemy import create_engine

    from app.db.models import Base

    engine = create_engine(url_for(path), future=True)
    Base.metadata.create_all(engine)
    return engine


def seed(path: Path) -> None:
    """A source shaped like the live database: two employees, a raise, closed
    shifts, an advance, an audit trail, and one live session token."""
    from sqlalchemy.orm import Session as OrmSession

    from app.db.models import Advance, EditLog, RateHistory, Session, Timesheet, User
    from app.domain import clock

    engine = build(path)
    with OrmSession(engine) as session:
        for user_id, name, rate in ((111, "Employee One", 2000), (222, "Employee Two", 1600)):
            session.add(
                User(user_id=user_id, full_name=name, role="EMPLOYEE", hourly_rate_cents=rate)
            )
            session.add(
                RateHistory(
                    user_id=user_id,
                    hourly_rate_cents=rate,
                    effective_from=date(1970, 1, 1),
                    created_at=clock.now(),
                )
            )
        # A raise, so a backup that dropped rate_history would silently
        # re-price every past month instead of failing loudly.
        session.add(
            RateHistory(
                user_id=111,
                hourly_rate_cents=2500,
                effective_from=date(2026, 7, 1),
                created_at=clock.now(),
            )
        )

        for day in (1, 2, 3):
            for user_id in (111, 222):
                start = datetime(2026, 7, day, 9, 0, tzinfo=clock.get_timezone())
                session.add(
                    Timesheet(
                        user_id=user_id,
                        date=date(2026, 7, day),
                        status="FINISHED",
                        in_time=start,
                        out_time=start + timedelta(hours=8),
                        total_break_seconds=1800,
                    )
                )

        session.add(
            Advance(
                user_id=111,
                date=date(2026, 7, 5),
                amount_cents=10000,
                note="advance",
                created_at=clock.now(),
            )
        )
        session.add(
            EditLog(
                entity="timesheet",
                entity_id=1,
                field="out_time",
                old_value="a",
                new_value="b",
                changed_by=999,
                changed_at=clock.now(),
                reason="fixed a typo",
            )
        )
        session.add(
            Session(
                token="a-live-session-token",
                user_id=111,
                issued_at=clock.now(),
                expires_at=clock.now() + timedelta(days=30),
            )
        )
        session.commit()


def counts(url: str) -> dict:
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import Session as OrmSession

    from app.db.models import Advance, EditLog, RateHistory, Session, Timesheet, User

    models = {
        "users": User,
        "rate_history": RateHistory,
        "timesheets": Timesheet,
        "advances": Advance,
        "edit_log": EditLog,
        "sessions": Session,
    }
    engine = create_engine(url, future=True)
    with OrmSession(engine) as session:
        return {
            name: session.scalar(select(func.count()).select_from(model))
            for name, model in models.items()
        }


@pytest.fixture
def source(tmp_path) -> Path:
    path = tmp_path / "source.db"
    seed(path)
    return path


# --- taking a backup -------------------------------------------------------


def test_the_backup_carries_every_payroll_table(source, tmp_path):
    out = tmp_path / "backup.db"
    backup_neon.dump(url_for(source), out)

    before, after = counts(url_for(source)), counts(url_for(out))
    for table in backup_neon.PAYROLL_TABLES:
        assert after[table] == before[table] > 0, table


def test_the_backup_leaves_live_session_tokens_behind(source, tmp_path):
    """A file of valid session tokens sitting on a disk is a liability, not an
    asset -- and sessions are reissued through Telegram anyway."""
    out = tmp_path / "backup.db"
    backup_neon.dump(url_for(source), out)

    assert counts(url_for(source))["sessions"] == 1
    assert counts(url_for(out))["sessions"] == 0
    assert "a-live-session-token" not in out.read_bytes().decode("latin-1")


def test_taking_a_backup_never_writes_to_the_source(source, tmp_path):
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    backup_neon.dump(url_for(source), tmp_path / "backup.db")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest


def test_the_manifest_records_what_was_taken(source, tmp_path):
    out = tmp_path / "backup.db"
    manifest = backup_neon.dump(url_for(source), out)

    written = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert written == manifest
    assert manifest["rows"]["timesheets"] == 6
    assert manifest["tables"] == list(backup_neon.PAYROLL_TABLES)
    datetime.fromisoformat(manifest["taken_at"])  # parses, so it is a real stamp


# --- proving the backup is worth keeping -----------------------------------


def test_a_good_backup_verifies_clean(source, tmp_path):
    out = tmp_path / "backup.db"
    backup_neon.dump(url_for(source), out)
    assert backup_neon.verify(url_for(source), out) == []


def test_verify_catches_a_backup_that_lost_a_shift(source, tmp_path):
    from sqlalchemy import create_engine, text

    out = tmp_path / "backup.db"
    backup_neon.dump(url_for(source), out)
    with create_engine(url_for(out), future=True).begin() as conn:
        conn.execute(text("DELETE FROM timesheets WHERE id = 1"))

    failures = backup_neon.verify(url_for(source), out)
    assert any("timesheets" in failure for failure in failures)


def test_verify_catches_a_backup_whose_pay_figures_moved(source, tmp_path):
    """Row counts alone would pass this one. The figure is what matters."""
    from sqlalchemy import create_engine, text

    out = tmp_path / "backup.db"
    backup_neon.dump(url_for(source), out)
    with create_engine(url_for(out), future=True).begin() as conn:
        conn.execute(text("UPDATE timesheets SET total_break_seconds = 7200 WHERE id = 1"))

    failures = backup_neon.verify(url_for(source), out)
    assert any("2026-07" in failure and "111" in failure for failure in failures)


# --- getting the data back -------------------------------------------------


def test_restore_refuses_a_target_that_already_holds_timesheets(source, tmp_path):
    out = tmp_path / "backup.db"
    backup_neon.dump(url_for(source), out)
    occupied = tmp_path / "occupied.db"
    seed(occupied)

    with pytest.raises(SystemExit, match="already holds"):
        backup_neon.restore(out, url_for(occupied))


def test_restore_refuses_a_target_with_no_schema(source, tmp_path):
    """Alembic owns the schema. A restore that created tables itself would be
    a second, unversioned source of DDL truth."""
    out = tmp_path / "backup.db"
    backup_neon.dump(url_for(source), out)

    with pytest.raises(SystemExit, match="alembic upgrade head"):
        backup_neon.restore(out, url_for(tmp_path / "empty.db"))


# --- handling the connection string ----------------------------------------


def test_the_manifest_never_records_the_password():
    """The manifest sits next to the backup and gets copied around with it.
    Neon's connection string is the whole key to the payroll history."""
    masked = backup_neon.safe_url(
        "postgresql+psycopg://neon_user:sup3r-s3cret@ep-x.ap-southeast-2.aws.neon.tech/neondb"
    )
    assert "sup3r-s3cret" not in masked
    assert "ep-x.ap-southeast-2.aws.neon.tech" in masked  # still says which host


def test_a_bare_postgres_url_gets_the_psycopg_driver():
    """The .env holds a bare postgresql:// URL, which SQLAlchemy reads as
    psycopg2 -- a driver this project does not have and never will. app/config
    rewrites it; anything else connecting to the same URL has to as well, or it
    works in tests and fails against the real Neon."""
    connectable = backup_neon.connectable_url("postgresql://u:p@localhost/neondb")
    assert connectable.startswith("postgresql+psycopg://")


def test_a_sqlite_url_is_left_alone():
    url = "sqlite+pysqlite:///C:/tmp/whatever.db"
    assert backup_neon.connectable_url(url) == url


def test_a_full_round_trip_puts_every_row_and_every_figure_back(source, tmp_path):
    """The test this whole script exists for."""
    out = tmp_path / "backup.db"
    backup_neon.dump(url_for(source), out)

    target = tmp_path / "target.db"
    build(target)  # stands in for `alembic upgrade head`
    backup_neon.restore(out, url_for(target))

    for table in backup_neon.PAYROLL_TABLES:
        assert counts(url_for(target))[table] == counts(url_for(source))[table], table
    assert backup_neon.payroll_totals(url_for(target)) == backup_neon.payroll_totals(
        url_for(source)
    )
