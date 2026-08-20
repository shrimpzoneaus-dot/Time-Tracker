# Move Time Tracker Bot To A New PC

## Files to copy

Copy the whole folder:

```text
time_tracker_transfer
```

It contains:

```text
time_tracking_bot.py
dashboard_time_tracker.py
time_tracker.db
requirements.txt
README_time_tracking_bot.md
run_time_bot.bat
run_dashboard.bat
.env.example
```

## Setup on the new PC

Open PowerShell in the copied folder:

```powershell
cd "PATH_TO_COPIED_FOLDER"
python -m pip install -r requirements.txt
copy .env.example .env
```

Open `.env` and fill:

```env
BOT_TOKEN=your_telegram_bot_token_here
ADMIN_CHAT_ID=your_admin_telegram_id_here
APP_TIMEZONE=Australia/Sydney
```

## Run bot

```powershell
python time_tracking_bot.py
```

Or double-click:

```text
run_time_bot.bat
```

## Run dashboard

```powershell
python dashboard_time_tracker.py
```

Then open:

```text
http://127.0.0.1:5000
```

Or double-click:

```text
run_dashboard.bat
```

## Important

The history is stored in:

```text
time_tracker.db
```

Do not delete this file if you want to keep employee timesheets, salary rates, advances, and history.
