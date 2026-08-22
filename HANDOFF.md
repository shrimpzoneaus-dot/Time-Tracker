# Handoff — 2026-08-20, amended 2026-08-21

Read this first when picking the work back up.

## Where things stand in one line

The rebuilt app is **deployed and running** at
https://shrimpzone-timetracker.fly.dev against an **empty** Neon database. The
legacy Flask dashboard is **still the live system**, but the **legacy bot has
been stopped on purpose** — so nothing is currently recording attendance from
Telegram at all. Nothing has been cut over. No staff have been told anything.

## What is live and verified

| Thing | State |
|---|---|
| Fly app `shrimpzone-timetracker` | deployed, `syd`, machine `6834e10a0e5618`, health check 1/1 |
| Neon (project "Shrimp Zone", db `neondb`, branch `production`) | Postgres 18.6, ap-southeast-2, **empty** |
| Alembic | `0001_baseline` applied — all 6 tables created |
| Telegram bot on Fly | initialises correctly, **no webhook registered** so it receives nothing |
| Legacy bot | **STOPPED on purpose** (owner, 2026-08-21). No process running. Telegram attendance is NOT being recorded — do not assume a quiet day means a quiet day. Restart with `run_time_bot.bat`. |
| Legacy dashboard | still the source of truth, and **changed 2026-08-21** — see "Dashboard now updates itself" below |
| `time_tracker.db` | untouched by this project — 102 timesheets, 4 users, 6 advances. Newest row is **2026-08-20**; nothing since, which follows from the bot being stopped. |

Endpoint check at handoff time:

```
/healthz        200  {"ok":true}
/signin         200
/static/app.css 200
/               303 -> /signin
/admin          401
/tg/<wrong>     404
/tg/<correct>   500 on an empty body  (503 would mean the bot is disabled)
```

## Dashboard now updates itself (2026-08-21)

`dashboard_time_tracker.py` was a one-shot server-rendered page. It rendered
correctly on load and then **never asked the database again**, so a clock-in
punched on Telegram sat in SQLite, invisible, until someone hit Refresh. The bot
was never at fault — a fresh request always returned the right data.

The day and salary table bodies are now shared Jinja templates rendered by both
the full page and a new `/fragment/tables` endpoint, so the two cannot drift.
The page polls it every 10 s and swaps only what changed. Responses are marked
`no-store`.

⚠️ **Not a `<meta http-equiv="refresh">`, deliberately.** The day table has
inline in/out/break/status editors, and a periodic reload would wipe a half-typed
correction mid-keystroke. The swap is skipped while any field in the table is
focused or differs from its server value.

⚠️ **The tests skip under `.venv`.** The legacy dashboard is Flask on the system
Python; the rebuild's venv deliberately carries no Flask and must not grow one.
Run them directly instead — no pytest needed:

```powershell
python tests\test_dashboard_live_updates.py
```

Verified: 7/7 there, `62 passed, 1 skipped` under `.venv`, plus a headless-browser
run confirming the clock-in appeared with **one page-load event** (never
reloaded) and that in-progress typing survived a concurrent bot write.

## The rebuilt `/admin` console now updates itself too (2026-08-22)

It had the identical gap — its only `setInterval` was `clock.html`'s local
ticking timer, which advances a number the page already had rather than
fetching a new one. So the board said someone was on shift long after they
clocked out on Telegram.

`admin.html`'s "On shift now" section is now the shared partial
`_board.html`, rendered both on load and by a new **`GET /admin/fragment/board`**,
which the open console polls every 10 s (and immediately on regaining focus).
Both `/admin` and the fragment are marked `no-store`. The fragment is
admin-only, like the console: 401 signed out, 403 for an employee.

⚠️ **Only the board is polled, deliberately.** The week grid and the
exceptions list hold the inline edit forms and their `<details>` open state; a
swap would eat a half-typed clock-out correction. The board has no inputs, so
it needs no dirty-field guard — which is why it is the only thing swapped.

⚠️ The poll compares each response against **the previous response text**,
not against `board.innerHTML`. The browser normalises markup it has parsed, so
the innerHTML comparison never matches and every poll would rebuild the DOM.

Verified: 7 new tests, each watched failing first, and a real render
confirming the fragment body appears byte-identical inside the console.

## The staff clock page updates itself too (2026-08-22)

Same bug, different shape, and the worst of the three: the clock face baked
its status in at render and the script only ticked a local counter upward. An
employee who clocked out on Telegram watched the web page keep counting the
shift they had already ended — **still offering a Clock Out button**.

The clock face and the month figures are now the shared partial
`_clockface.html`, rendered on load and by **`GET /fragment/clock`**, polled
every 10 s. The action buttons live inside the partial deliberately: a
clock-out taken on Telegram has to take the Clock Out button away with it.

⚠️ **`require_user`, not `current_user`.** The page redirects a signed-out
visitor to `/signin`; the fragment returns **401** instead, so a poll whose
session expired is told to stop rather than handed a sign-in page to swap into
the clock face.

⚠️ **The swap re-seeds the local ticker** (`startTimer()` after every swap).
Without it the ticker holds the `#timer` element the swap just discarded, and
the visible timer freezes. `data-worked` ticks every second, so the fragment
differs on every poll and the page does swap every 10 s — that is deliberate,
it keeps the local timer pinned to server truth.

⚠️ **The ticker paints immediately on (re)start.** The server renders the
timer as `HH:MM`; waiting for the first 1 s interval dropped the seconds off
the display for a beat after every swap.

⚠️ **The swap is skipped while a button inside the panel has focus**, so a
tap in flight is never pulled out from under a finger. There are no text
inputs in this panel, so it needs no dirty-field guard beyond that.

⚠️ **The client-side JS is reviewed, not machine-verified** — this machine has
no playwright or selenium, in either interpreter. The server contract (status,
buttons, figures, 401, `no-store`, no drift) is covered by tests; the swap and
re-seed logic is not. Worth a browser check before cutover.

`/me/shifts` is deliberately left alone: it lists closed shifts for a month,
so it has nothing live to go stale.

Verified: `78 passed, 1 skipped` under `.venv` (9 more, each watched failing
first), the legacy suite still 7/7, and a real render showing the fragment go
from `clockface working` + Clock Out + `data-running="1"` to
`clockface finished` + Clock In + `data-running="0"` after a Telegram
clock-out.

## Git

Branch **`rebuild/domain-core-and-migration`**, pushed, **not merged to `main`**.

```
22b673e fix(dashboard): the web page never asked the database again
bb9dfbe fix(tests): test_web no longer depends on being imported first
2699c4a feat(scripts): guarded target reset; smoke test passed on the live app
ad59576 docs: handoff — deployed, verified, awaiting smoke test and cutover
1e1253e fix(main): a broken BOT_TOKEN no longer takes the web app down
a0d9115 fix(deps): declare python-multipart — the image could not boot without it
05f42e3 feat(app): FastAPI clock screen + admin console, Telegram sign-in, Fly/Neon deploy
faa1c60 feat(core): tested shift/payroll domain, verifying SQLite->Neon migration
```

78 passing + 1 skipped: `.\.venv\Scripts\python.exe -m pytest -q`
(the skip is the legacy-dashboard module above; run it on the system Python)

## Secrets

All five are set on Fly. **Fly cannot show a secret value back to you** — only a
digest. The local `.env` is therefore the only readable copy of
`TELEGRAM_WEBHOOK_SECRET` and `SESSION_SECRET`. It is gitignored. **Do not lose
it**; losing the webhook secret means regenerating it and re-running
`setWebhook`.

## THE NEXT STEP

Nothing is blocked. The next action is a **smoke test on the empty database**,
which is completely safe — the live system is not involved.

Because no webhook is registered, the bot cannot hand out sign-in links yet. So
mint one directly:

```powershell
cd "C:\Users\tthan\Downloads\time_tracker_transfer"
# (ask Claude to run this — it reads DATABASE_URL from .env, creates a user and
#  prints a one-tap sign-in URL for the deployed app)
```

Open that link on a phone, clock in, take a break, clock out, then look at
`/admin`. That exercises the whole stack against Neon without touching
anything live.

### Smoke test: DONE (2026-08-20)

A full clock cycle was run against the live deployment and passed:

```
not clocked in -> on shift -> on break -> on shift -> not clocked in
```

The admin console, week grid, payroll page and CSV export all render, and the
CSV totalled correctly.

⚠️ **Smoke testing writes rows into the target database**, and the migration
refuses a target that already holds timesheets — so those rows would block
cutover. Neon was cleared afterwards and is empty again. If you smoke test
again, clear it before migrating:

```powershell
.\.venv\Scripts\python.exe scripts\reset_target_db.py         # show what is there
.\.venv\Scripts\python.exe scripts\reset_target_db.py --yes   # delete it
```

It refuses to touch a target that looks like real payroll history (more than 10
timesheets, or any advance at all).

## Then, in order

1. **Settle the three stale shifts** in the LEGACY database, while the old
   dashboard is still what you use daily. Real clock-out times needed — these
   decide someone's pay and must not be invented:
   - shift **47**, user `8865482786`, `WORKING` since **2026-07-05 09:15** — 46
     days open, currently unpaid
   - shift **86**, user `6393446109` (you), `ON_BREAK` since **2026-08-06 18:21**
   - shift **16**, user `298764295`, `FINISHED` with no clock-out; pays 0 hours,
     but that user's rate is $0 so no money rides on it
2. **Fix the Neon backup gap** — see Open decisions.
3. **Cutover** — see below.
4. **Merge the branch to `main`.**
5. **Move the repo** out of `Downloads\` to `OneDrive\Documents\GitHub\Time-Tracker`,
   alongside the other four. Deliberately NOT done yet: the live bot runs from
   this directory and moving it mid-flight would break staff clock-outs.

## Cutover procedure

Order matters — Telegram will not allow polling and a webhook at the same time.

1. Pick a moment when nobody is on shift.
2. Stop the local bot (close the `run_time_bot.bat` window).
3. Run the migration:
   ```powershell
   .\.venv\Scripts\python.exe scripts\migrate_sqlite_to_neon.py
   ```
   It re-verifies row counts, primary keys, timestamps and every employee-month
   total before writing, and exits non-zero rather than loading anything
   questionable. Reads the source read-only.
4. Register the webhook (secret is in `.env`):
   ```
   https://api.telegram.org/bot<TOKEN>/setWebhook
     ?url=https://shrimpzone-timetracker.fly.dev/tg/<TELEGRAM_WEBHOOK_SECRET>
     &secret_token=<TELEGRAM_WEBHOOK_SECRET>
   ```
5. Tell staff to send `/start` and tap **Open my timesheet**.

**Rollback at any point:** call `deleteWebhook`, restart `run_time_bot.bat`.
You are exactly back where you are today — nothing in this project ever writes
to `time_tracker.db`.

## Open decisions for the owner

1. **Neon Free plan gives 6 hours of history retention.** Thin once Neon is the
   only copy of the payroll data. Either upgrade the plan or add a scheduled
   `pg_dump`. Should be settled BEFORE cutover, not after.
2. **The UI has no visual identity yet.** What ships now is a deliberately
   restrained functional stylesheet. Three design directions are still owed for
   an owner pick (via the huashu-design process) — not started.
3. **Award coverage.** Assumed none: the system records hours, it does not price
   overtime or penalty rates. If these staff are award-covered, that is a
   separate piece of work and a legal question, not a coding one.
4. **Half-cent rounding.** One historical figure changes by one cent:
   `6393446109`, July 2026, $1,555.48 -> $1,555.49. The legacy `round()` was
   banker's rounding and sent an exact half cent to the even cent; the rebuild
   rounds half up, in the employee's favour. Deliberate. The parity test asserts
   this is the ONLY class of difference that can occur.

## Already done to the live database

**Shift 19 was deleted on owner instruction 2026-08-20** — a hand-entered 16.5 h
overnight row (2026-06-09 08:00 -> 06-10 00:30). June for user `8865482786` went
from $1,008.83 to $711.83. Backup: `time_tracker_backup_20260820_delete_shift19.db`,
row dump: `deleted_shift_19_20260820_delete_shift19.log`. Both gitignored.
Restore with `copy time_tracker_backup_20260820_delete_shift19.db time_tracker.db`.

## Gotchas learned the hard way this session

- **Placeholders in shell commands got pasted verbatim, twice** (`REAL_PASSWORD`,
  `your_real_bot_token`), each costing a failed deploy. Put values in `.env` and
  read them from there instead of typing them on a command line.
- **PowerShell double quotes interpolate `$`.** A password containing `$` is
  silently corrupted with no error. Use single quotes: `fly secrets set 'K=v'`.
- **`python-multipart` is required but never imported by name.** FastAPI needs it
  for `Form(...)`. It looks unused; it is not. Without it the image raises at
  import and the machine reboots in a loop. Verify dependency changes by
  installing `requirements.txt` into a clean venv and importing `app.main` —
  that is what the Dockerfile does.
- **IPv6 to Neon times out from this machine**, IPv4 works. Anything connecting
  locally may hang until it falls through. Pass `hostaddr` to force IPv4.
- **A machine that crash-looped stays `stopped`** (restart policy `no`). After
  fixing the cause, `fly machine start <id>` — a successful `fly deploy` alone
  may not bring it back.
- **Fly secrets are write-only.** Plan for it.
- **`shrimpzone-web` had a concurrent Claude session running during this work**,
  committing to `main` and leaving files dirty. Its Stop hook blocked this
  session repeatedly over changes that were not ours. Avoid running two sessions
  in one repo.
