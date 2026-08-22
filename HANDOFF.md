# Handoff — 2026-08-20, amended 2026-08-22

Read this first when picking the work back up.

## Where things stand in one line

The rebuilt app is **deployed and running** at
https://shrimpzone-timetracker.fly.dev against an **empty** Neon database.
Nothing has been cut over. No staff have been told anything.

🔴 **Cutover is BLOCKED.** The legacy bot was stopped on *this* machine on
2026-08-21, but a live bot is still polling the token **from another machine**
(proved 2026-08-22 — see the BLOCKER section). Telegram attendance is being
recorded into that machine's SQLite file, so the `time_tracker.db` here is a
stale copy and `migrate_sqlite_to_neon.py` would migrate the wrong data.
**Find that machine and retrieve its database before doing anything else.**

## What is live and verified

| Thing | State |
|---|---|
| Fly app `shrimpzone-timetracker` | deployed, `syd`, machine `6834e10a0e5618`, health check 1/1 |
| Neon (project "Shrimp Zone", db `neondb`, branch `production`) | Postgres 18.6, ap-southeast-2, **empty** |
| Alembic | `0001_baseline` applied — all 6 tables created |
| Telegram bot on Fly | initialises correctly, **no webhook registered** so it receives nothing |
| Legacy bot | 🔴 **RUNNING ON ANOTHER MACHINE** (proved 2026-08-22, see below). It was stopped on THIS machine on 2026-08-21; that is not the same thing. Telegram attendance IS being recorded — into that machine's SQLite file, not this one. **Do not start a local bot: two pollers on one token fight and drop updates.** |
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

## Backups (2026-08-22)

`scripts/backup_neon.py` takes a logical backup of the five payroll tables
(`users`, `rate_history`, `timesheets`, `advances`, `edit_log`) into a
timestamped SQLite file plus a JSON manifest, then **verifies it** - row counts
AND every employee-month pay figure, recomputed through the same payroll code
the admin console uses. A run that cannot verify exits non-zero and says so.

    .\.venv\Scripts\python.exe scriptsackup_neon.py                  # take + verify
    .\.venv\Scripts\python.exe scriptsackup_neon.py --verify <file>  # re-check an old one
    .\.venv\Scripts\python.exe scriptsackup_neon.py --restore <file> # load it back

**Scheduled**: Windows Task Scheduler task **"Time Tracker - daily Neon
backup"**, daily at 13:00, `StartWhenAvailable` so a missed run catches up when
the machine wakes. It runs `run_backup.bat`, which appends to
`backupsackup.log` and passes the exit code through - so Task Scheduler's
**Last Run Result** column is the honest signal: `0x0` means taken AND
verified. Remove it with
`Unregister-ScheduledTask -TaskName "Time Tracker - daily Neon backup"`.

⚠️ It only runs while this machine is on. That is the accepted trade-off of
the free option; a Fly scheduled machine was the alternative and was not taken.

⚠️ **Sessions are deliberately not backed up.** They are ephemeral login
tokens reissued through Telegram, and a file of live session tokens is a
liability. The manifest masks the password for the same reason.

⚠️ `connectable_url()` mirrors `app/config.py`: Neon hands out a bare
`postgresql://` URL, which SQLAlchemy reads as **psycopg2** - absent and
unwanted here. Any new entry point that opens `DATABASE_URL` must do the same
rewrite or it will work in tests and fail against the real database.

Backups land in `backups/`, which is gitignored. They are real pay data -
never commit one. Retention is unbounded; the files are small (tens of KB), so
this is fine for years, but nothing prunes them.

## 🔴 BLOCKER: a second bot is live on another machine (proved 2026-08-22)

**Do not cut over until the source-of-truth question below is answered.**

### The proof

With **zero** bot processes on this machine and five full minutes of silence
beforehand, a held `getUpdates` long-poll was **terminated after 5 seconds by
another getUpdates request**. An earlier run was terminated after 32 s.

That is conclusive in a way a plain 409 is not. A lingering registration from a
force-killed process can only *reject* a new request up front; it cannot
*issue* a fresh request 5 or 32 seconds later and take the slot away. Something
alive did that, and it is not on this machine.

Also ruled out, individually: the Fly app (`fly logs` shows only `/healthz`,
and `build_application` sets `.updater(None)`, so it is structurally incapable
of polling), the local uvicorn processes (same code path), and a duplicate
local process (`bot.log` carries exactly one startup line; the two PIDs are the
Windows Store Python shim and its child).

### What it means for cutover

**That machine's SQLite file is the real payroll record.** `time_tracker.db`
here has been frozen at 102 timesheets, newest **2026-08-20**, which is
evidence that attendance has been going somewhere else, not evidence that none
was recorded.

`scripts/migrate_sqlite_to_neon.py` reads `time_tracker.db` **in this
directory**. Running it as-is would migrate the stale copy and silently discard
every shift recorded on the other machine since 2026-08-20, including any pay
owed. The three "stale open shifts" may also already be settled over there.

### The database lineage — NOT a three-month split brain

An earlier version of this section claimed the two copies had diverged since
June. The file forensics say otherwise, and the gap is small:

| File | Content | What it is |
|---|---|---|
| `time_tracker_local_old_20260820_135726.db` | 19 shifts, 3 users, 06-01 → **06-11** (mtime 11 Jun) | THIS machine's own database. It stopped recording on 11 June. |
| `time_tracker.db` | 102 shifts, 4 users, 06-01 → **08-20** | The OTHER machine's database, copied here **2026-08-20 13:57**, when the local one was renamed aside. |

So the local file already contains the other machine's history through
2026-08-20. Comparing the two, only **two** shifts exist solely in the old
local copy, and both are accounted for: shift 19 (the 16.5 h overnight row
deleted on owner instruction 2026-08-20) and a dangling never-closed shift for
`6393446109` at 2026-06-11 19:08. Nothing was lost in the swap.

**The real gap is therefore only 2026-08-20 → now** — whatever the other
machine has recorded since the copy was taken. Retrieve that file, confirm it
is a superset of this one, then use it. Keep `time_tracker.db` as it stands as
the fallback until that check passes; do not delete it.

### Who to ask

`ADMIN_CHAT_ID=6393446109,298764295` — two admins:

| Telegram id | Name | Role | Shifts here |
|---|---|---|---|
| `6393446109` | V Kai | ADMIN, $18.00/h | **65** (2026-06-01 → 08-20) |
| `298764295` | Thanh Nguyen | ADMIN, $0.00/h | 2, both on 2026-06-10 (setup tests) |
| `8865482786` | Tyler Dao | EMPLOYEE, $18.00/h | 30 |
| `7725821590` | Kayn Tran | EMPLOYEE, $16.00/h | 5 |

This machine belongs to Thanh (`THANH` / `tthan`), who has 2 test shifts. The
heavy user and the other admin is **V Kai** — the most likely owner of the
machine still polling. The transfer package that seeded this folder arrived by
Discord on 2026-06-10, built elsewhere the evening before, which fits.

### ⚠️ Never probe a running bot with getUpdates

Calling `getUpdates` to "check whether the bot is polling" **is itself a
competing poller** — it kicks the real bot off its long-poll, and this bot
registers no error handler, so the conflict surfaces as an unhandled exception.
`getWebhookInfo` is safe. `getUpdates` is not.

An instantaneous probe is also a **bad detector**: a rival in backoff holds the
slot only briefly every ~35 s, so 7 of 8 snapshot probes returned 200 and
produced a confident, wrong "there is no second bot". **Use a held long-poll
(`timeout=60`) with every local bot stopped** — it catches anything that polls
during the whole window.

⚠️ On 2026-08-22 roughly 40 minutes went into starting local bots and probing,
all of it competing with the live remote instance. Clock-ins attempted in that
window may have hit a bot that was mid-backoff. Do not repeat it: diagnose with
one long-poll, with nothing local running.

## Open decisions for the owner

1. ~~**Neon Free plan gives 6 hours of history retention.**~~ **SETTLED
   2026-08-22** - see "Backups" below. Not a `pg_dump`: this machine has no
   PostgreSQL install at all. Upgrading the Neon plan is still open as a
   separate question about point-in-time recovery; the data-loss gap is closed.
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
