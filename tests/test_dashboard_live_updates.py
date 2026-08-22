"""The legacy dashboard must reflect Telegram bot writes without a manual reload.

The bot and the dashboard are separate processes sharing one SQLite file. The
dashboard was a one-shot server-rendered page: correct on load, frozen after.
These tests pin the polling contract that replaced it.

The legacy dashboard is a Flask app on the system Python, while the rebuild's
.venv deliberately carries no Flask -- so this module skips under the rebuild
suite and is instead run directly, with no pytest needed:

    python tests\test_dashboard_live_updates.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import pytest
except ModuleNotFoundError:  # direct run on the system Python
    pytest = None
else:
    pytest.importorskip("flask", reason="legacy dashboard stack; run with the system Python")

USER_ID = 6393446109
RATE_CENTS = 1600


def bot_writes(db_path, sql: str, params: tuple) -> None:
    """Exactly how time_tracking_bot.py writes: its own connection, then commit."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def setup_dashboard():
    """A dashboard bound to an empty database holding one employee."""
    os.environ["APP_TIMEZONE"] = "Australia/Sydney"
    import dashboard_time_tracker as dash

    db_path = Path(tempfile.mkdtemp()) / "time_tracker.db"
    dash.DB_PATH = db_path
    dash.init_db()
    bot_writes(
        db_path,
        "INSERT INTO users (user_id, full_name, role, hourly_rate_cents) VALUES (?,?,?,?)",
        (USER_ID, "Test Employee", "EMPLOYEE", RATE_CENTS),
    )
    return db_path, dash.app.test_client()


def today_str() -> str:
    return datetime.now(ZoneInfo("Australia/Sydney")).date().isoformat()


def clock_in(db_path, today: str) -> None:
    bot_writes(
        db_path,
        "INSERT INTO timesheets (user_id, date, in_time, status, total_break_seconds) "
        "VALUES (?,?,?,?,?)",
        (USER_ID, today, f"{today} 09:15:00", "WORKING", 0),
    )


def clock_out(db_path, today: str) -> None:
    bot_writes(
        db_path,
        "UPDATE timesheets SET out_time = ?, status = ? WHERE user_id = ? AND date = ?",
        (f"{today} 17:15:00", "FINISHED", USER_ID, today),
    )


def test_page_can_update_itself():
    """Without this the page is correct on load and wrong for the rest of the day."""
    _db, client = setup_dashboard()
    html = client.get("/").get_data(as_text=True)
    assert "/fragment/tables" in html
    assert "setInterval" in html


def test_polling_never_clobbers_an_in_progress_edit():
    """The day table has inline in/out editors; a blind swap would eat keystrokes."""
    _db, client = setup_dashboard()
    html = client.get("/").get_data(as_text=True)
    assert "activeElement" in html
    assert "defaultValue" in html


def test_responses_are_not_cacheable():
    """A bot writes continuously, so a cached page is always a wrong page."""
    _db, client = setup_dashboard()
    for path in ("/", "/fragment/tables"):
        response = client.get(path)
        assert "no-store" in (response.headers.get("Cache-Control") or ""), path


def test_poll_picks_up_a_clock_in():
    db_path, client = setup_dashboard()
    today = today_str()

    before = client.get(f"/fragment/tables?date={today}").get_json()
    assert "09:15" not in before["day"]

    clock_in(db_path, today)

    after = client.get(f"/fragment/tables?date={today}").get_json()
    assert set(after) == {"day", "salary"}
    assert "09:15" in after["day"]


def test_poll_picks_up_a_clock_out_and_the_salary_follows():
    db_path, client = setup_dashboard()
    today = today_str()
    clock_in(db_path, today)
    clock_out(db_path, today)

    payload = client.get(f"/fragment/tables?date={today}&month={today[:7]}").get_json()
    assert "17:15" in payload["day"]
    assert "8h 0m" in payload["day"]
    assert "$128.00" in payload["salary"]  # 8h at $16/h


def test_fragment_and_full_page_cannot_drift():
    """Both render from the same template constants; this pins that they stay shared."""
    db_path, client = setup_dashboard()
    today = today_str()
    clock_in(db_path, today)

    fragment = client.get(f"/fragment/tables?date={today}").get_json()["day"]
    full = client.get(f"/?date={today}").get_data(as_text=True)
    assert fragment.strip() in full


def test_employee_names_are_still_escaped():
    """The fragments are injected with |safe, so escaping must happen upstream."""
    db_path, client = setup_dashboard()
    today = today_str()
    bot_writes(
        db_path,
        "INSERT INTO users (user_id, full_name, role, hourly_rate_cents) VALUES (?,?,?,?)",
        (1, "<script>alert(1)</script>", "EMPLOYEE", RATE_CENTS),
    )
    bot_writes(
        db_path,
        "INSERT INTO timesheets (user_id, date, in_time, status, total_break_seconds) "
        "VALUES (?,?,?,?,?)",
        (1, today, f"{today} 09:15:00", "WORKING", 0),
    )

    day = client.get(f"/fragment/tables?date={today}").get_json()["day"]
    assert "<script>alert(1)</script>" not in day
    assert "&lt;script&gt;" in day


if __name__ == "__main__":
    failed = []
    for name, func in sorted(globals().items()):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            func()
        except AssertionError as exc:
            failed.append(name)
            print(f"[FAIL] {name}: {exc}")
        else:
            print(f"[PASS] {name}")
    print()
    print(f"FAILED: {len(failed)}" if failed else "ALL CHECKS PASSED")
    sys.exit(1 if failed else 0)
