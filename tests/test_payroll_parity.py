"""Proof that the rewrite pays people exactly what the old code pays them.

Reads the production SQLite file READ-ONLY and recomputes every employee-month
two ways: once with the legacy arithmetic lifted verbatim out of
time_tracking_bot.py / dashboard_time_tracker.py, once through app.domain.
Any discrepancy fails the suite and blocks the migration.

The database is gitignored, so the test skips cleanly where the file is absent.
"""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from datetime import date
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import pytest

from app.domain import clock, payroll, shifts

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB = REPO_ROOT / "time_tracker.db"

pytestmark = pytest.mark.skipif(
    not LIVE_DB.exists(), reason="production time_tracker.db not present"
)


@pytest.fixture(scope="module", autouse=True)
def _fixed_timezone():
    previous = os.environ.get("APP_TIMEZONE")
    os.environ["APP_TIMEZONE"] = "Australia/Sydney"
    yield
    if previous is None:
        os.environ.pop("APP_TIMEZONE", None)
    else:
        os.environ["APP_TIMEZONE"] = previous


@pytest.fixture(scope="module")
def rows():
    # mode=ro cannot write, cannot create, cannot journal. The live file is
    # never touched by this suite.
    conn = sqlite3.connect(f"file:{LIVE_DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield {
            "users": conn.execute("SELECT * FROM users").fetchall(),
            "timesheets": conn.execute("SELECT * FROM timesheets").fetchall(),
            "advances": conn.execute("SELECT * FROM advances").fetchall(),
        }
    finally:
        conn.close()


# --- legacy arithmetic, copied verbatim from the shipped code ---------------


def legacy_work_seconds(row) -> int:
    if not row["in_time"] or not row["out_time"]:
        return 0
    in_time = clock.parse_legacy(row["in_time"])
    out_time = clock.parse_legacy(row["out_time"])
    break_seconds = int(row["total_break_seconds"] or 0)
    return max(0, int((out_time - in_time).total_seconds()) - break_seconds)


def legacy_gross_cents(total_work_seconds: int, rate: int) -> int:
    return round(total_work_seconds * rate / 3600)


# --- the new path ----------------------------------------------------------


def domain_work_seconds(row) -> int:
    if not row["in_time"] or not row["out_time"]:
        return 0
    shift = shifts.Shift(
        id=row["id"],
        user_id=row["user_id"],
        date=date.fromisoformat(row["date"]),
        status=row["status"],
        in_time=clock.parse_legacy(row["in_time"]),
        out_time=clock.parse_legacy(row["out_time"]),
        total_break_seconds=int(row["total_break_seconds"] or 0),
    )
    return shifts.worked_seconds(shift)


def months_of(rows) -> set[str]:
    return {row["date"][:7] for row in rows["timesheets"]}


def test_dataset_is_the_one_we_designed_against(rows):
    """Guards the assumptions in the design doc."""
    assert len(rows["users"]) >= 1
    assert len(rows["timesheets"]) >= 1
    assert months_of(rows), "no months to compare"


def test_per_shift_work_seconds_match(rows):
    mismatches = []
    for row in rows["timesheets"]:
        legacy = legacy_work_seconds(row)
        try:
            new = domain_work_seconds(row)
        except shifts.NegativeDurationError as error:
            # A shift the old code silently paid as zero. Report it loudly -
            # this is exactly the class of row the max(0, ...) clamp hid.
            mismatches.append(f"shift {row['id']}: legacy={legacy}s, domain raised: {error}")
            continue
        if legacy != new:
            mismatches.append(f"shift {row['id']}: legacy={legacy}s new={new}s")
    assert not mismatches, "\n".join(mismatches)


def test_monthly_gross_advances_and_net_match(rows, capsys):
    users = {row["user_id"]: row for row in rows["users"]}
    seconds = defaultdict(int)
    shift_dates = defaultdict(list)

    for row in rows["timesheets"]:
        key = (row["user_id"], row["date"][:7])
        worked = legacy_work_seconds(row)
        seconds[key] += worked
        shift_dates[key].append((date.fromisoformat(row["date"]), worked))

    advances = defaultdict(int)
    for row in rows["advances"]:
        advances[(row["user_id"], row["date"][:7])] += int(row["amount_cents"] or 0)

    differences = []
    half_cent_months = []
    for key in sorted(set(seconds) | set(advances)):
        user_id, month = key
        rate = int(users[user_id]["hourly_rate_cents"] or 0) if user_id in users else 0

        legacy_gross = legacy_gross_cents(seconds[key], rate)
        legacy_net = legacy_gross - advances[key]

        # rate_history as the migration seeds it: one period, dated 1970-01-01
        periods = [payroll.RatePeriod(hourly_rate_cents=rate, effective_from=date(1970, 1, 1))]
        total_seconds, new_gross = payroll.gross_for_shifts(shift_dates[key], periods)
        new_net = payroll.net_cents(new_gross, advances[key])

        # Hours must match exactly. There is no acceptable difference here.
        if total_seconds != seconds[key]:
            differences.append(f"{user_id} {month}: seconds {seconds[key]} -> {total_seconds}")

        # Money may differ ONLY where the exact value lands on a half cent, and
        # then only by +1: the legacy code used round(), which is banker's
        # rounding, so an exact .5 went to the nearest EVEN cent. We round half
        # up, which resolves the half cent in the employee's favour. Any other
        # difference is a real regression.
        exact = (Decimal(total_seconds) * Decimal(rate)) / Decimal(3600)
        is_half_cent = exact == exact.to_integral_value(rounding=ROUND_DOWN) + Decimal("0.5")
        delta = new_gross - legacy_gross

        if delta != 0:
            if is_half_cent and delta == 1:
                half_cent_months.append(
                    f"{user_id} {month}: {payroll.format_money(legacy_gross)} -> "
                    f"{payroll.format_money(new_gross)} (exact {exact}c)"
                )
            else:
                differences.append(
                    f"{user_id} {month}: gross {payroll.format_money(legacy_gross)} -> "
                    f"{payroll.format_money(new_gross)} (exact {exact}c, delta {delta})"
                )

        if (new_net - legacy_net) != delta:
            differences.append(
                f"{user_id} {month}: net moved independently of gross "
                f"({legacy_net} -> {new_net})"
            )

    with capsys.disabled():
        print(f"\n  Half-cent roundings (legacy banker's -> half-up): {len(half_cent_months)}")
        for line in half_cent_months:
            print(f"    {line}")

    assert not differences, "PAYROLL WOULD CHANGE:\n" + "\n".join(differences)


def test_report_exceptions_present_in_live_data(rows, capsys):
    """Not an assertion - an inventory of rows the admin will need to fix."""
    as_of = clock.now()
    found = []
    for row in rows["timesheets"]:
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
            found.append(f"  shift {row['id']} user {row['user_id']} {row['date']}: {problem}")

    with capsys.disabled():
        print(f"\n  Live-data exceptions: {len(found)}")
        for line in found:
            print(line)
