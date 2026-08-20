"""End-to-end tests over the real FastAPI app against a SQLite database."""

from __future__ import annotations

import os
from datetime import date, timedelta
from urllib.parse import unquote

import pytest

os.environ.setdefault("APP_TIMEZONE", "Australia/Sydney")
os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "hook-secret")
os.environ.setdefault("ADMIN_CHAT_ID", "999")
os.environ.setdefault("COOKIE_SECURE", "0")  # TestClient speaks plain http


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app import config
    from app.db import session as db_session

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
