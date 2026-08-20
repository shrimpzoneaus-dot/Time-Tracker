# Time Tracker — Fly + Neon re-architecture and UI redesign

Date: 2026-08-20
Status: IMPLEMENTED (application + migration). Not yet deployed; see §12.
Repo: `Time-Tracker` (github.com/shrimpzoneaus-dot/Time-Tracker)

## 1. Context

The Time Tracker is live and paying real people. As of 2026-08-20 the SQLite
database holds:

- 4 users — 2 `ADMIN` (one on an $18.00/h rate), 2 `EMPLOYEE` ($16.00/h, $18.00/h)
- 103 timesheets spanning 2026-06-01 → 2026-08-20 (41 June, 37 July, 25 August)
- 6 advances, 2026-06-10 → 2026-07-03
- 2 shifts in an open state — **both stale, neither is anyone currently working**
  (see §13)
- Shift 19 (a 16.5 h hand-entered overnight row) was **deleted on owner
  instruction 2026-08-20**; June for user 8865482786 went $1,008.83 → $711.83.
  Backup: `time_tracker_backup_20260820_delete_shift19.db`.

It runs as two Python processes against a single local SQLite file:

| File | Lines | Role |
|---|---|---|
| `time_tracking_bot.py` | 1,288 | python-telegram-bot v20, polling. Employee clock in/break/resume/out via inline buttons; admin commands. |
| `dashboard_time_tracker.py` | 897 | Flask on `127.0.0.1:5000`, single inline template. Admin-only day grid, timesheet editing, salary, CSV/XLSX export. |

The database lives on one PC. `MIGRATE_TO_NEW_PC.md` exists solely because of
that, and the working directory has accumulated three backup/old copies of the
`.db` file.

## 2. Goals

1. Run on the SZ arsenal: Fly.io (`syd`) + Neon Postgres, matching the
   conventions already established by `shrimpzone-app` and `shrimpzone-concierge`.
2. Give employees a conventional web time-clock: one big clock in/out control,
   a live shift timer, their own history — while keeping the Telegram bot
   working exactly as they know it.
3. Preserve every existing row and reproduce every existing salary figure to
   the cent.
4. Fix the defects that currently mispay people.

## 3. Non-goals

Explicitly out of scope. Each would need its own brainstorm.

- GPS / geofenced clock-in
- Leave, roster or availability management
- An approval workflow for timesheets (audit log yes, approvals no)
- Multi-organisation / multi-tenant support
- **Any award-interpretation engine** — overtime loadings, penalty rates,
  casual loading, weekend/public-holiday rates. If these staff are covered by
  an AU modern award, that is a legal question to scope deliberately, not to
  infer. The system records hours; it does not interpret them.

## 4. Decisions taken (owner, 2026-08-20)

| Decision | Choice | Consequence |
|---|---|---|
| Clock-in surface | Web app for staff **and** Telegram retained | Domain logic must have exactly one owner that both call |
| Language | Stay Python — FastAPI + SQLAlchemy + Alembic | Proven payroll logic ported, not reimplemented. Alembic plays Prisma's role. |
| Staff auth | Telegram one-tap sign-in | `users.user_id` stays the Telegram ID PK — **zero identity remapping** |
| Cutover | Close open shifts, then proceed | Real clock-out times supplied by owner at cutover; never invented |
| Scope | Tiers 0 + 1 + 2 in one pass | Money bugs, safety, and UI all ship together |

## 5. Architecture

One Fly app, one process, two surfaces, one domain core.

```
shrimpzone-timetracker/
  Dockerfile
  fly.toml                      # app = "shrimpzone-timetracker", primary_region = "syd"
  alembic.ini
  alembic/versions/             # versioned migrations ONLY
  app/
    config.py                   # env, settings
    domain/
      clock.py                  # timezone, parsing, formatting, business-date rules
      shifts.py                 # clock_in / start_break / end_break / clock_out + guards
      payroll.py                # hours -> gross -> advances -> net (Decimal)
    db/
      models.py                 # SQLAlchemy models
      repo.py                   # query layer
    web/
      auth.py                   # Telegram sign-in tokens + sessions
      routes_staff.py           # clock screen, my timesheets, my pay
      routes_admin.py           # live board, week grid, exceptions, payroll, exports
      templates/  static/
    bot/
      handlers.py               # commands + inline buttons -> domain/
      webhook.py                # secret-path update receiver
  scripts/
    migrate_sqlite_to_neon.py   # one-way, verifying
  tests/
```

### 5.1 One process, webhook not polling

FastAPI serves the web app and receives Telegram updates on a secret path
(`/tg/<TELEGRAM_WEBHOOK_SECRET>`), verified against the
`X-Telegram-Bot-Api-Secret-Token` header. One deploy, one connection pool, no
second machine kept awake by long-polling.

### 5.2 Fly configuration

Following `shrimpzone-concierge/fly.toml`:

- `primary_region = "syd"`
- `auto_stop_machines = "off"`, `min_machines_running = 1` — a 2–6 s cold start
  when someone taps *Clock In* reads as broken
- `release_command = "alembic upgrade head"` — the Alembic equivalent of
  `prisma migrate deploy`. **Never** an auto-generate-and-apply step. The
  concierge `fly.toml` documents at length why destructive DDL on every deploy
  nearly cost that app its production data; the same rule binds here.
- **Its own Neon database.** Not shared with any other SZ app, for the reason
  stated in that same file: a shared DB lets either app's deploy drop the
  other's tables.

Secrets via `fly secrets`: `DATABASE_URL` (Neon pooled endpoint), `BOT_TOKEN`,
`TELEGRAM_WEBHOOK_SECRET`, `SESSION_SECRET`, `APP_TIMEZONE=Australia/Sydney`.

## 6. Data model

**Schema changes are additive only.** The four existing tables keep every
column, type and row.

### 6.1 Unchanged

```
users       (user_id PK, full_name, role, hourly_rate_cents)
timesheets  (id PK, user_id FK, date, in_time, out_time,
             break_start, total_break_seconds, status)
advances    (id PK, user_id FK, date, amount_cents, note, created_at)
```

`users.hourly_rate_cents` is **retained** even though `rate_history` supersedes
it for calculation — it stays as the "current rate" convenience field and keeps
the rollback path trivial.

### 6.2 Added

```
rate_history (id PK, user_id FK, hourly_rate_cents, effective_from DATE,
              created_at, created_by)
edit_log     (id PK, entity, entity_id, field, old_value, new_value,
              changed_by, changed_at, reason)
sessions     (token PK, user_id FK, issued_at, expires_at, revoked_at)
```

### 6.3 Timestamps

`in_time` / `out_time` / `break_start` / `created_at` become `timestamptz`,
converted from naive Australia/Sydney local time. This is unambiguous for all
existing data: 2026-06-01 → 2026-08-20 falls entirely within AEST (UTC+10) with
no DST transition, so no wall-clock time in the current dataset is ambiguous or
non-existent. Future rows are stored as aware UTC at write time, which removes
the problem permanently.

`timesheets.date` stays a local business date (`DATE`) — it is the shift's
"work day", not a timestamp, and the reports group on it.

## 7. Migration — the intactness guarantee

`scripts/migrate_sqlite_to_neon.py` is one-way and refuses to run twice against
a non-empty target.

1. Open `time_tracker.db` **read-only** (`file:...?mode=ro`). The file is never
   written to and remains the rollback artifact.
2. Copy `users`, `timesheets`, `advances` verbatim, preserving primary keys and
   `id` values; reset Postgres sequences past the max id.
3. Seed `rate_history`: one row per user, `hourly_rate_cents` = their current
   rate, `effective_from = 1970-01-01`. Every historical shift therefore prices
   at exactly the rate that produces today's figures.
4. Verify, and abort on any mismatch:
   - row counts per table
   - per-user, per-month `SUM(work_seconds)`, `gross_cents`, `advance_cents`,
     `net_cents` computed from the new schema vs. the same computed from the
     legacy SQLite code path
   - every `in_time`/`out_time` round-trips back to the identical local
     wall-clock string
5. Print a diff report. Non-zero exit on any discrepancy.

## 8. Tier 0 — defects to fix

### 8.1 Overnight shift corruption on edit (money-losing)

`dashboard_time_tracker.py:713` rebuilds both times with
`parse_time_for_date(row["date"], ...)`, forcing `out_time` onto the shift's
**start** date. The inline edit form (`:530`) posts `in_time` and `out_time` on
every submit, so editing only the break minutes still round-trips both. A
22:00→02:00 shift becomes 22:00→02:00 *same day*: `out_time < in_time`.

The bot's `/edit_time` command shares the defect: `time_tracking_bot.py:506`
has its own copy of `parse_time_for_date` with the same date-forcing behaviour,
so `/edit_time <user> <date> out 02:00` corrupts an overnight shift the same
way. Both call sites are fixed, which is itself an argument for the single
`domain/clock.py`.

**Fix:** the admin edit form takes a full date+time for `out_time`, defaulting
to the shift date and offering next-day; `/edit_time` accepts an optional
`+1d` suffix. `clock_out` and both edit paths reject `out_time <= in_time` with
an explicit validation error.

### 8.2 `max(0, ...)` hides 8.1

`calculate_work_seconds` clamps negatives to zero, so a corrupted shift reports
0 hours — unpaid, silently, with no warning on any screen.

**Fix:** the domain function raises on a negative duration. Callers surface it
as an exception row in the admin *Exceptions* view rather than a silent zero.
The migration reports any existing rows that would raise.

### 8.3 Retroactive rate changes

Salary is `hours × users.hourly_rate_cents` — the *current* rate. A raise
silently re-prices every past month; June's payslip cannot be reproduced.

**Fix:** `payroll.py` resolves the rate per shift from `rate_history` by
`effective_from`. Seeded at 1970-01-01, all existing figures are unchanged;
only future rate changes are dated.

### 8.4 Two salary implementations

`build_salary_report_for_user` (bot) and `build_salary_rows` (dashboard).
Verified 2026-08-20 to agree on gross and net — both compute
`round(total_work_seconds × rate / 3600)`, summing seconds across the month and
rounding once. They diverge only in the shift *count* (the dashboard tallies
incomplete shifts; the bot lists them as incomplete).

**Fix:** one implementation in `payroll.py`; both surfaces call it. The
month-level single rounding is preserved deliberately — it is what currently
produces the correct figures, and the parity test pins it.

### 8.5 Float money

`parse_money_to_cents` routes through `float`. Low severity at whole-dollar
advances, free to fix.

**Fix:** `Decimal` with `ROUND_HALF_UP`.

## 9. Tier 1 — safety

- **Admin auth.** The dashboard currently has none; its POST routes edit clock
  times and pay rates. Survivable on `127.0.0.1`, unacceptable on the public
  internet. Admin routes require a session whose `users.role = ADMIN`.
- **Audit log.** Every mutation of a clock time, break, status, rate or advance
  writes to `edit_log` with actor, before and after. This is the entire
  evidence base if an employee disputes their hours.
- **Soft delete.** `/reset_today` currently `DELETE`s rows
  (`delete_timesheets_by_user_date`). It becomes a soft delete recorded in
  `edit_log`.
- **Backups.** Neon point-in-time recovery replaces the `/backup` command and
  the `time_tracker_backup_*.db` sprawl. `/backup` remains as an on-demand
  export.
- **Secret hygiene.** `.env` stays gitignored (verified: it was never
  committed). Note that the repo is **public** and `.env.example` plus the
  README carry a real Telegram admin ID (`6393446109`); recommend removing it
  from both.
- **Dependency cleanup.** `MetaTrader5` is in `requirements.txt`, is imported
  nowhere, and is a Windows-only wheel — both `.bat` launchers run
  `pip install -r requirements.txt` on every start, so it breaks a clean setup.
  Removed.

## 10. Tier 2 — UI/UX

The visual design is to be produced through the **huashu-design** skill, which
requires three direction drafts for owner selection before any build. This
section specifies structure and behaviour, not visual style.

Roles are additive, not exclusive. `6393446109` is an `ADMIN` who also carries
an $18.00/h rate and clocks real shifts, so an admin sees **both** the employee
clock screen and the admin console — the console is an addition to their view,
never a replacement for it.

### 10.1 Employee screen (mobile-first)

- One large state-aware primary control: **Clock In** → **Start Break** /
  **Clock Out** → **End Break**. The state machine and its guards are the same
  ones the bot enforces, because both call `domain/shifts.py`.
- Live running timer for the current shift, with break time excluded and shown
  separately.
- Today at a glance: in time, break total, elapsed.
- My history: recent shifts, this month's hours, month-to-date gross, advances
  taken, net.
- No editing. Corrections are requested, not self-served.

### 10.2 Admin console

- **On shift now** — live board of who is clocked in, who is on break, how long.
- **Week grid** — replaces the current one-day-at-a-time view.
- **Exceptions** — missing clock-outs, shifts crossing midnight, implausibly
  long shifts, and any row that would raise under 8.2. This is the view that
  would have caught the overnight bug.
- **Payroll** — monthly per-employee hours, gross, advances, net; CSV and XLSX
  export retained.
- Rate and advance management, with every change logged.

## 11. Testing

- `tests/test_payroll_parity.py` — **the critical artifact.** For all 4 users ×
  3 months, assert the new implementation reproduces the figures the legacy
  code produces from the real dataset. A fixture copy of the production SQLite
  file is the input.
- `tests/test_shifts.py` — the state machine: double clock-in, break without
  clock-in, clock-out while on break, resume without break, overnight shift,
  and the `out_time <= in_time` rejection.
- `tests/test_migration.py` — round-trip on a fixture DB, including timezone
  conversion.
- `tests/test_auth.py` — token issue, expiry, revocation, admin-route rejection
  for non-admin sessions.

## 12. Cutover

1. Build and deploy to Fly with the Neon DB empty; smoke-test with a throwaway
   account. The live bot keeps running locally throughout.
2. Owner picks the window. Owner supplies real clock-out times for any open
   shifts — **these are never invented**.
3. Stop the local bot. Run the migration. Read the verification report.
4. Point the Telegram webhook at Fly (`setWebhook`).
5. Send each staff member their one-tap sign-in link.
6. `time_tracker.db` is retained untouched as the rollback. Rollback is: delete
   the webhook, restart the local bot.

## 13. Findings from the live data (2026-08-20)

Produced by `tests/test_payroll_parity.py` and
`scripts/migrate_sqlite_to_neon.py --dry-run` against the production file.

### 13.1 Parity holds, with one deliberate one-cent change

Every shift's worked seconds and every employee-month's gross, advances and
net reconcile between the legacy arithmetic and `app.domain` — **except one**:

```
6393446109  2026-07:  $1,555.48 -> $1,555.49   (exact 155548.5c)
```

311,097 seconds at $18.00/h is exactly 155,548.5 cents. The legacy code used
`round()`, which is banker's rounding, so an exact half cent went to the
nearest **even** cent. `payroll.gross_cents` rounds **half up**, resolving the
half cent in the employee's favour.

This is a decision, not a defect: banker's-rounding-on-a-float was an artefact
of `round()`, not a payroll policy. The parity test does not assert "identical"
— it asserts that a difference may occur **only** at an exact half cent and
**only** by +1 cent, so any other drift still fails the suite.

### 13.2 Four rows need an admin

| id | user | date | Problem |
|---|---|---|---|
| 47 | 8865482786 | 2026-07-05 09:15 | `WORKING`, never clocked out — open 46 days |
| 86 | 6393446109 | 2026-08-06 18:21 | `ON_BREAK`, never resumed — open 14 days |
| 16 | 298764295 | 2026-06-10 13:30 | `FINISHED` with no `out_time`; pays as 0 hours |
| 19 | 8865482786 | 2026-06-09 → 06-10 00:30 | Genuine 16.5 h overnight shift, stored correctly |

Shift 19 is the live example of §8.1's blast radius. It is currently correct
and paying 16.5 h × $18.00 = $297.00. The moment anyone opens that row in the
dashboard and presses Save — even only to adjust the break — `out_time` snaps
back to 9 June and the shift pays **$0.00**. Until the fix ships, that row
should not be edited.

The migration carries all four across exactly as found. They are warnings, not
blockers: correcting them is a payroll decision for the owner, and inventing
times is not an option.

### 13.3 Consequence for cutover

There is nobody to "log out" — no employee is mid-shift. §12 step 2 is
therefore about supplying real clock-out times for shifts **47 and 86**, both
of which are weeks old, rather than interrupting anyone working.

## 14. Open items for owner

1. **Repo location** — currently `C:\Users\tthan\Downloads\time_tracker_transfer`.
   Recommend moving to `OneDrive\Documents\GitHub\Time-Tracker` alongside the
   other four SZ repos. Owner to confirm before any move.
2. **Fly app name** — proposed `shrimpzone-timetracker`.
3. **Public repo** — confirm it should stay public, and confirm removal of the
   hard-coded admin Telegram ID from `.env.example` and the README.
4. **Award coverage** — see §3. Confirm whether overtime/penalty rates apply.
5. **UI language** — the current bot mixes English with Vietnamese error
   strings ("Có lỗi database..."). Confirm the target: English throughout,
   Vietnamese throughout, or both.
