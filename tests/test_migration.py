"""Migration transform and verification, on a synthetic database.

Runs everywhere, including where the production file is absent. The live
dry-run is the other half of the proof.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import migrate_sqlite_to_neon as migration  # noqa: E402

from app.domain import clock  # noqa: E402

SCHEMA = """
CREATE TABLE users (
    user_id INTEGER PRIMARY KEY, full_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'EMPLOYEE', hourly_rate_cents INTEGER NOT NULL DEFAULT 0);
CREATE TABLE timesheets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, date DATE NOT NULL,
    in_time DATETIME, out_time DATETIME, break_start DATETIME,
    total_break_seconds INTEGER DEFAULT 0, status TEXT NOT NULL);
CREATE TABLE advances (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, date DATE NOT NULL,
    amount_cents INTEGER NOT NULL, note TEXT, created_at DATETIME NOT NULL);
"""


@pytest.fixture(autouse=True)
def _fixed_timezone():
    os.environ["APP_TIMEZONE"] = "Australia/Sydney"
    yield
    os.environ.pop("APP_TIMEZONE", None)


@pytest.fixture
def legacy_db(tmp_path) -> Path:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO users VALUES (555, 'Test Employee', 'EMPLOYEE', 1800)")
    conn.executemany(
        "INSERT INTO timesheets (id, user_id, date, in_time, out_time, break_start,"
        " total_break_seconds, status) VALUES (?,?,?,?,?,?,?,?)",
        [
            (1, 555, "2026-06-09", "2026-06-09 08:00:00", "2026-06-10 00:30:00", None, 0,
             "FINISHED"),
            (2, 555, "2026-06-11", "2026-06-11 09:00:00", "2026-06-11 17:00:00", None, 1800,
             "FINISHED"),
            (3, 555, "2026-06-12", "2026-06-12 09:00:00", None, None, 0, "WORKING"),
        ],
    )
    conn.execute(
        "INSERT INTO advances (id, user_id, date, amount_cents, note, created_at)"
        " VALUES (1, 555, '2026-06-15', 10000, 'test', '2026-06-15 10:00:00')"
    )
    conn.commit()
    conn.close()
    return path


def test_transform_preserves_every_row_and_key(legacy_db):
    data = migration.read_legacy(legacy_db)
    plan = migration.transform(data)

    assert [r["id"] for r in plan.timesheets] == [1, 2, 3]
    assert [r["id"] for r in plan.advances] == [1]
    assert len(plan.users) == 1


def test_rate_history_is_seeded_from_1970_so_history_does_not_reprice(legacy_db):
    plan = migration.transform(migration.read_legacy(legacy_db))

    assert len(plan.rate_history) == 1
    assert plan.rate_history[0]["effective_from"] == date(1970, 1, 1)
    assert plan.rate_history[0]["hourly_rate_cents"] == 1800


def test_timestamps_become_utc_but_round_trip_to_the_same_wall_clock(legacy_db):
    data = migration.read_legacy(legacy_db)
    plan = migration.transform(data)

    overnight = plan.timesheets[0]
    assert overnight["in_time"].utcoffset().total_seconds() == 0  # stored as UTC
    # June is AEST (UTC+10), so 08:00 local is 22:00 UTC the previous day.
    assert overnight["in_time"].isoformat() == "2026-06-08T22:00:00+00:00"
    assert clock.format_legacy(overnight["in_time"]) == "2026-06-09 08:00:00"
    assert clock.format_legacy(overnight["out_time"]) == "2026-06-10 00:30:00"


def test_nulls_stay_null(legacy_db):
    plan = migration.transform(migration.read_legacy(legacy_db))
    open_shift = plan.timesheets[2]
    assert open_shift["out_time"] is None
    assert open_shift["break_start"] is None


def test_verification_passes_on_clean_data(legacy_db):
    data = migration.read_legacy(legacy_db)
    failures, _ = migration.verify(data, migration.transform(data))
    assert failures == []


def test_verification_reports_the_open_shift_as_a_warning_not_a_failure(legacy_db):
    data = migration.read_legacy(legacy_db)
    failures, warnings = migration.verify(data, migration.transform(data))
    assert failures == []
    assert any("missing clock-out" in w for w in warnings)


def test_verification_fails_when_a_row_goes_missing(legacy_db):
    data = migration.read_legacy(legacy_db)
    plan = migration.transform(data)
    plan.timesheets.pop()

    failures, _ = migration.verify(data, plan)
    assert any("rows in" in f for f in failures)


def test_source_database_is_opened_read_only(legacy_db):
    """The legacy file is the rollback artifact; nothing may write to it."""
    before = legacy_db.read_bytes()
    migration.verify(
        migration.read_legacy(legacy_db), migration.transform(migration.read_legacy(legacy_db))
    )
    assert legacy_db.read_bytes() == before
