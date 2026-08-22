"""End-to-end tests over the real FastAPI app against a SQLite database."""

from __future__ import annotations

import os
from datetime import date, timedelta
from urllib.parse import unquote

import pytest

# setenv, not setdefault: any other test module that imports something calling
# load_dotenv() would otherwise seed these from the real .env first and win.
TEST_ENV = {
    "APP_TIMEZONE": "Australia/Sydney",
    "SESSION_SECRET": "test-secret",
    "TELEGRAM_WEBHOOK_SECRET": "hook-secret",
    "ADMIN_CHAT_ID": "999",
    "COOKIE_SECURE": "0",  # TestClient speaks plain http
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import config
    from app.db import session as db_session

    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path.as_posix()}")
    config.get_settings.cache_clear()
    db_session.get_engine.cache_clear()
    db_session.get_sessionmaker.cache_clear()

    from fastapi.testclient import TestClient

    from app.db.models import Base
    from app.main import app

    Base.metadata.create_all(db_session.get_engine())

    with TestClient(app) as test_client:
        yield test_client

    config.get_settings.cache_clear()
    db_session.get_engine.cache_clear()
    db_session.get_sessionmaker.cache_clear()


def location(response) -> str:
    """Redirect target with its percent-encoding undone."""
    return unquote(response.headers["location"])


def sign_in(client, user_id: int, name: str) -> None:
    from app.db import repo
    from app.db.session import session_scope

    with session_scope() as session:
        repo.upsert_user(session, user_id, name)
        token = repo.create_magic_link(session, user_id)

    response = client.get(f"/auth/{token}", follow_redirects=False)
    assert response.status_code == 303
    client.cookies.update(response.cookies)


# --- auth ------------------------------------------------------------------


def test_signed_out_visitor_is_sent_to_sign_in(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/signin"


def test_magic_link_signs_you_in(client):
    sign_in(client, 111, "Employee One")
    page = client.get("/")
    assert page.status_code == 200
    assert "Employee One" in page.text


def test_magic_link_is_single_use(client):
    from app.db import repo
    from app.db.session import session_scope

    with session_scope() as session:
        repo.upsert_user(session, 111, "Employee One")
        token = repo.create_magic_link(session, 111)

    assert client.get(f"/auth/{token}", follow_redirects=False).status_code == 303
    second = client.get(f"/auth/{token}", follow_redirects=False)
    assert second.headers["location"] == "/signin?expired=1"


def test_expired_magic_link_is_refused(client):
    from app.db.models import Session
    from app.db.session import session_scope
    from app.db import repo
    from app.domain import clock

    with session_scope() as session:
        repo.upsert_user(session, 111, "Employee One")
        token = repo.create_magic_link(session, 111)
        session.flush()
        session.get(Session, token).expires_at = clock.now() - timedelta(minutes=1)

    response = client.get(f"/auth/{token}", follow_redirects=False)
    assert response.headers["location"] == "/signin?expired=1"


# --- clocking --------------------------------------------------------------


def test_clock_in_then_break_then_out(client):
    sign_in(client, 111, "Employee One")

    assert "Clock In" in client.get("/").text

    client.post("/clock/in", follow_redirects=False)
    page = client.get("/").text
    assert "Start Break" in page and "Clock Out" in page

    client.post("/clock/break", follow_redirects=False)
    assert "End Break" in client.get("/").text

    client.post("/clock/resume", follow_redirects=False)
    client.post("/clock/out", follow_redirects=False)
    assert "Clock In" in client.get("/").text


def test_double_clock_in_is_refused_with_a_message(client):
    sign_in(client, 111, "Employee One")
    client.post("/clock/in", follow_redirects=False)

    response = client.post("/clock/in", follow_redirects=False)
    assert "already clocked in" in location(response)


def test_clock_out_while_on_break_is_refused(client):
    sign_in(client, 111, "Employee One")
    client.post("/clock/in", follow_redirects=False)
    client.post("/clock/break", follow_redirects=False)

    response = client.post("/clock/out", follow_redirects=False)
    assert "end your break" in location(response)


# --- authorisation ---------------------------------------------------------


def test_employee_cannot_reach_the_admin_console(client):
    sign_in(client, 111, "Employee One")
    assert client.get("/admin").status_code == 403


def test_employee_cannot_set_a_pay_rate(client):
    sign_in(client, 111, "Employee One")
    response = client.post("/admin/rate", data={"user_id": 111, "amount": "99.00"})
    assert response.status_code == 403


def test_admin_reaches_the_console(client):
    sign_in(client, 999, "The Boss")  # 999 is in ADMIN_CHAT_ID
    page = client.get("/admin")
    assert page.status_code == 200
    assert "On shift now" in page.text


# --- the overnight defect, through the real editor -------------------------


def make_overnight_shift(user_id: int) -> int:
    """A shift like live id 19: 08:00 -> 00:30 the next day."""
    from app.db import repo
    from app.db.models import Timesheet
    from app.db.session import session_scope
    from app.domain import clock, shifts

    with session_scope() as session:
        repo.upsert_user(session, user_id, "Night Owl")
        row = Timesheet(
            user_id=user_id,
            date=date(2026, 6, 9),
            status=shifts.STATUS_FINISHED,
            in_time=clock.parse_clock_time(date(2026, 6, 9), "08:00"),
            out_time=clock.parse_clock_time(date(2026, 6, 9), "00:30", day_offset=1),
            total_break_seconds=0,
        )
        session.add(row)
        session.flush()
        return row.id


def test_editing_only_the_break_no_longer_destroys_an_overnight_shift(client):
    """The exact regression: the legacy form resubmitted both times and forced
    out_time onto the shift's start date, so the shift silently paid zero."""
    from app.db.models import Timesheet
    from app.db.session import session_scope
    from app.db import repo
    from app.domain import shifts

    sign_in(client, 999, "The Boss")
    shift_id = make_overnight_shift(111)

    response = client.post(
        f"/admin/timesheet/{shift_id}",
        data={
            "in_time": "08:00",
            "out_time": "00:30",
            "out_next_day": "1",
            "break_minutes": "30",
            "status": "FINISHED",
            "reason": "adjust break",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with session_scope() as session:
        shift = repo.to_domain(session.get(Timesheet, shift_id))
        assert shifts.crosses_midnight(shift)
        assert shifts.worked_seconds(shift) == int(16.0 * 3600)  # 16.5h less 30m break


def test_editor_refuses_a_clock_out_before_clock_in(client):
    sign_in(client, 999, "The Boss")
    shift_id = make_overnight_shift(111)

    response = client.post(
        f"/admin/timesheet/{shift_id}",
        data={
            "in_time": "08:00",
            "out_time": "00:30",
            "out_next_day": "",  # the legacy behaviour
            "break_minutes": "0",
            "status": "FINISHED",
        },
        follow_redirects=False,
    )
    assert "Clock-out must be after clock-in" in location(response)


def test_every_edit_is_written_to_the_audit_log(client):
    from app.db import repo
    from app.db.session import session_scope

    sign_in(client, 999, "The Boss")
    shift_id = make_overnight_shift(111)

    client.post(
        f"/admin/timesheet/{shift_id}",
        data={
            "in_time": "09:00",
            "out_time": "00:30",
            "out_next_day": "1",
            "break_minutes": "0",
            "status": "FINISHED",
            "reason": "started late",
        },
        follow_redirects=False,
    )

    with session_scope() as session:
        history = repo.edit_history(session, "timesheet", shift_id)

    assert [entry.field for entry in history] == ["in_time"]
    assert history[0].changed_by == 999
    assert history[0].reason == "started late"


def test_removing_a_shift_is_a_soft_delete(client):
    from app.db.models import Timesheet
    from app.db.session import session_scope

    sign_in(client, 999, "The Boss")
    shift_id = make_overnight_shift(111)

    client.post(
        f"/admin/timesheet/{shift_id}/delete",
        data={"reason": "duplicate"},
        follow_redirects=False,
    )

    with session_scope() as session:
        row = session.get(Timesheet, shift_id)
        assert row is not None  # the row survives
        assert row.deleted_at is not None


# --- rates -----------------------------------------------------------------


def test_a_raise_does_not_reprice_last_month(client):
    from app.db.session import session_scope
    from app.services import month_summary

    sign_in(client, 999, "The Boss")
    make_overnight_shift(111)  # 16.5h on 2026-06-09

    client.post(
        "/admin/rate",
        data={"user_id": "111", "amount": "18.00", "effective_from": "2026-06-01"},
        follow_redirects=False,
    )
    with session_scope() as session:
        june_before = month_summary(session, 111, "2026-06").gross_cents

    client.post(
        "/admin/rate",
        data={"user_id": "111", "amount": "25.00", "effective_from": "2026-08-01"},
        follow_redirects=False,
    )
    with session_scope() as session:
        june_after = month_summary(session, 111, "2026-06").gross_cents

    assert june_before == int(16.5 * 1800)
    assert june_after == june_before


# --- webhook ---------------------------------------------------------------


def test_webhook_rejects_a_wrong_secret(client):
    assert client.post("/tg/not-the-secret", json={}).status_code == 404


def test_webhook_requires_the_header_as_well_as_the_path(client):
    assert client.post("/tg/hook-secret", json={}).status_code == 404


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_a_broken_bot_token_does_not_take_down_the_web_app(tmp_path, monkeypatch):
    """Staff clocking in on the web do not need Telegram to be working.

    A placeholder BOT_TOKEN used to raise InvalidToken inside lifespan and
    crash startup, rebooting the machine in a loop.
    """
    from app import config
    from app.db import session as db_session

    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{(tmp_path / 'b.db').as_posix()}")
    monkeypatch.setenv("BOT_TOKEN", "your_real_bot_token")
    config.get_settings.cache_clear()
    db_session.get_engine.cache_clear()
    db_session.get_sessionmaker.cache_clear()

    from fastapi.testclient import TestClient

    from app.db.models import Base
    from app.main import app

    Base.metadata.create_all(db_session.get_engine())

    with TestClient(app) as c:
        assert c.get("/healthz").json() == {"ok": True}
        assert c.get("/signin").status_code == 200
        assert app.state.bot is None  # bot disabled, web app alive

    config.get_settings.cache_clear()
    db_session.get_engine.cache_clear()
    db_session.get_sessionmaker.cache_clear()


# --- the admin console must not go stale -----------------------------------
#
# The legacy dashboard rendered once and never asked the database again, so a
# clock-in punched on Telegram was invisible until someone hit Refresh. The
# rebuilt console shipped with exactly the same gap: its only setInterval is
# clock.html's local ticking timer, which advances a number it already had
# rather than fetching a new one. These tests pin the polling contract.

BOARD_FRAGMENT = "/admin/fragment/board"


def clock_in_on_telegram(user_id: int, name: str, minutes_ago: int = 0) -> int:
    """A shift opened by the bot process, which the console never learns about."""
    from app.db import repo
    from app.db.models import Timesheet
    from app.db.session import session_scope
    from app.domain import clock, shifts

    started = clock.now() - timedelta(minutes=minutes_ago)
    with session_scope() as session:
        repo.upsert_user(session, user_id, name)
        row = Timesheet(
            user_id=user_id,
            date=clock.business_date(started),
            status=shifts.STATUS_WORKING,
            in_time=started,
            total_break_seconds=0,
        )
        session.add(row)
        session.flush()
        return row.id


def clock_out_on_telegram(shift_id: int) -> None:
    from app.db.models import Timesheet
    from app.db.session import session_scope
    from app.domain import clock, shifts

    with session_scope() as session:
        row = session.get(Timesheet, shift_id)
        row.out_time = clock.now()
        row.status = shifts.STATUS_FINISHED


def test_the_console_can_update_itself(client):
    """Without this the board is right on load and wrong for the rest of the day."""
    sign_in(client, 999, "The Boss")
    page = client.get("/admin").text
    assert BOARD_FRAGMENT in page
    assert "setInterval" in page


def test_the_board_fragment_is_admin_only(client):
    """It carries every open shift and every employee name; /admin is 403 for a
    reason and a second door onto the same data must be locked the same way."""
    assert client.get(BOARD_FRAGMENT).status_code == 401
    sign_in(client, 111, "Employee One")
    assert client.get(BOARD_FRAGMENT).status_code == 403


def test_the_poll_picks_up_a_clock_in(client):
    sign_in(client, 999, "The Boss")

    before = client.get(BOARD_FRAGMENT)
    assert before.status_code == 200
    assert "Night Owl" not in before.text

    clock_in_on_telegram(111, "Night Owl")

    after = client.get(BOARD_FRAGMENT)
    assert "Night Owl" in after.text


def test_the_poll_picks_up_a_clock_out(client):
    sign_in(client, 999, "The Boss")
    shift_id = clock_in_on_telegram(111, "Night Owl")
    assert "Night Owl" in client.get(BOARD_FRAGMENT).text

    clock_out_on_telegram(shift_id)

    assert "Night Owl" not in client.get(BOARD_FRAGMENT).text


def test_the_console_and_its_fragment_are_not_cacheable(client):
    """The bot writes continuously, so a cached console is always a wrong one."""
    sign_in(client, 999, "The Boss")
    for path in ("/admin", BOARD_FRAGMENT):
        response = client.get(path)
        assert "no-store" in (response.headers.get("cache-control") or ""), path


def test_the_fragment_and_the_console_cannot_drift(client):
    """Both must render from one template, or the polled board slowly stops
    matching the board you saw on load."""
    sign_in(client, 999, "The Boss")
    clock_in_on_telegram(111, "Night Owl")

    fragment = client.get(BOARD_FRAGMENT).text
    assert fragment.strip() in client.get("/admin").text


def test_employee_names_are_escaped_in_the_fragment(client):
    """The fragment is swapped in as markup, so escaping must happen server-side."""
    sign_in(client, 999, "The Boss")
    clock_in_on_telegram(111, "<script>alert(1)</script>")

    fragment = client.get(BOARD_FRAGMENT).text
    assert "<script>alert(1)</script>" not in fragment
    assert "&lt;script&gt;" in fragment


# --- the staff clock page must not go stale --------------------------------
#
# The same bug as the console had, in a different shape. The clock face bakes
# its status in at render and its script only ticks a local counter upward, so
# an employee who clocks out on Telegram watches the web page keep counting the
# shift they already ended -- and the buttons it offers are the wrong ones.

CLOCK_FRAGMENT = "/fragment/clock"


def start_break_on_telegram(shift_id: int) -> None:
    from app.db.models import Timesheet
    from app.db.session import session_scope
    from app.domain import clock, shifts

    with session_scope() as session:
        row = session.get(Timesheet, shift_id)
        row.status = shifts.STATUS_ON_BREAK
        row.break_start = clock.now()


def set_rate_for(user_id: int, cents: int) -> None:
    from app.db import repo
    from app.db.session import session_scope

    with session_scope() as session:
        repo.set_rate(session, user_id, cents, 999)


def test_the_clock_page_can_update_itself(client):
    sign_in(client, 111, "Employee One")
    page = client.get("/").text
    assert CLOCK_FRAGMENT in page
    assert "setInterval" in page


def test_the_clock_fragment_needs_a_signed_in_user(client):
    """401, not the 303 to /signin that the page itself returns: a poll must be
    told to stop, not handed a sign-in page to swap into the clock face."""
    assert client.get(CLOCK_FRAGMENT, follow_redirects=False).status_code == 401


def test_the_clock_fragment_shows_only_your_own_state(client):
    sign_in(client, 111, "Employee One")
    clock_in_on_telegram(222, "Somebody Else")

    fragment = client.get(CLOCK_FRAGMENT).text
    assert "not clocked in" in fragment
    assert "Somebody Else" not in fragment


def test_the_poll_sees_a_clock_out_made_on_telegram(client):
    """The bug in one test: the page kept counting a shift that had ended."""
    sign_in(client, 111, "Employee One")
    shift_id = clock_in_on_telegram(111, "Employee One", minutes_ago=120)

    on_shift = client.get(CLOCK_FRAGMENT).text
    assert "on shift" in on_shift
    assert "Clock Out" in on_shift

    clock_out_on_telegram(shift_id)

    after = client.get(CLOCK_FRAGMENT).text
    assert "not clocked in" in after
    assert "Clock In" in after
    assert "Clock Out" not in after


def test_the_poll_sees_a_break_started_on_telegram(client):
    sign_in(client, 111, "Employee One")
    shift_id = clock_in_on_telegram(111, "Employee One", minutes_ago=60)
    start_break_on_telegram(shift_id)

    fragment = client.get(CLOCK_FRAGMENT).text
    assert "on break" in fragment
    assert "End Break" in fragment


def test_the_month_figures_follow_a_clock_out(client):
    """Pay only lands in the month total once the shift is closed, so the
    figures are exactly as stale as the clock face was."""
    sign_in(client, 111, "Employee One")
    set_rate_for(111, 2000)  # $20.00/h
    shift_id = clock_in_on_telegram(111, "Employee One", minutes_ago=120)

    assert "$0.00" in client.get(CLOCK_FRAGMENT).text

    clock_out_on_telegram(shift_id)

    after = client.get(CLOCK_FRAGMENT).text
    assert "2h" in after
    assert "$40.00" in after


def test_the_clock_page_and_its_fragment_are_not_cacheable(client):
    sign_in(client, 111, "Employee One")
    for path in ("/", CLOCK_FRAGMENT):
        response = client.get(path)
        assert "no-store" in (response.headers.get("cache-control") or ""), path


def test_the_clock_fragment_and_the_page_cannot_drift(client):
    sign_in(client, 111, "Employee One")
    clock_in_on_telegram(111, "Employee One", minutes_ago=30)

    fragment = client.get(CLOCK_FRAGMENT).text
    assert fragment.strip() in client.get("/").text


def test_the_swapped_clock_face_carries_the_server_figure(client):
    """The local ticker counts up from data-worked. After a swap it must be
    re-seeded from the new one, or it carries on from the number it had."""
    sign_in(client, 111, "Employee One")
    clock_in_on_telegram(111, "Employee One", minutes_ago=30)

    fragment = client.get(CLOCK_FRAGMENT).text
    assert 'data-worked="' in fragment
    assert 'data-running="1"' in fragment
    assert "startTimer" in client.get("/").text
