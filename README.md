# Time Tracker

Telegram time clock and payroll for Shrimp Zone staff. Employees clock in,
break, resume and clock out; admins edit timesheets, set hourly rates, record
advances and pull monthly salary reports.

**Status: mid-rebuild.** The legacy app is still the one in production. The new
core is landing alongside it and nothing has been cut over yet.

## Layout

| Path | State | What it is |
|---|---|---|
| `time_tracking_bot.py` | **live** | Telegram bot (polling), SQLite |
| `dashboard_time_tracker.py` | **live** | Flask admin dashboard on `127.0.0.1:5000` |
| `time_tracker.db` | **live** | SQLite. Gitignored. The only copy of the payroll history. |
| `app/domain/` | new | Shift state machine and pay arithmetic — the single owner of both |
| `app/db/models.py` | new | SQLAlchemy schema for Postgres |
| `scripts/migrate_sqlite_to_neon.py` | new | One-way, self-verifying SQLite → Postgres migration |
| `tests/` | new | Including a parity suite that pins payroll against the real data |
| `docs/superpowers/specs/` | new | The design this rebuild follows |

## Running the legacy app

```powershell
python -m pip install -r requirements.txt
copy .env.example .env    # then fill BOT_TOKEN and ADMIN_CHAT_ID
python time_tracking_bot.py
python dashboard_time_tracker.py   # http://127.0.0.1:5000
```

## Running the tests

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pytest
.\.venv\Scripts\python.exe -m pytest -q
```

The parity suite reads `time_tracker.db` **read-only** and skips if it is
absent. It recomputes every employee-month with the legacy arithmetic and with
the new domain code and fails on any difference it cannot account for.

## Checking the migration without running it

```powershell
.\.venv\Scripts\python.exe scripts\migrate_sqlite_to_neon.py --dry-run
```

Verifies row counts, primary keys, timestamp round-tripping and every
employee-month total, then reports the shifts an admin needs to look at.
Writes nothing. Drop `--dry-run` and set `DATABASE_URL` to load Postgres; it
refuses to run against a target that already holds timesheets.

## Where the payroll history lives

`time_tracker.db` holds every timesheet, rate and advance, and it is
gitignored. Do not delete it. After cutover it remains the rollback artifact
and must be kept.

## Design

See [`docs/superpowers/specs/2026-08-20-time-tracker-fly-neon-design.md`](docs/superpowers/specs/2026-08-20-time-tracker-fly-neon-design.md)
for the target architecture (Fly.io + Neon), the defects being fixed, and the
cutover plan.

Vietnamese notes for the legacy bot are in `README_time_tracking_bot.md`.
