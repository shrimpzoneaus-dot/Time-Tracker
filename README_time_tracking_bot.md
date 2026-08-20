# Telegram Time-tracking Bot

## Cai dat

```powershell
python -m pip install -r requirements.txt
```

## Cau hinh

Tao file `.env` trong thu muc nay:

```env
BOT_TOKEN=your_telegram_bot_token_here
ADMIN_CHAT_ID=your_admin_chat_id_here
APP_TIMEZONE=Australia/Sydney
```

## Chay bot

```powershell
python time_tracking_bot.py
```

Khi chay lan dau, bot tu tao database `time_tracker.db` voi bang `users` va `timesheets`.

## Chay nhanh tren Windows

Bam dup file:

```text
run_time_bot.bat
```

Admin mac dinh hien tai:

```text
<your_admin_telegram_id>
```

Lenh admin trong Telegram:

```text
/admin
/myid
/users
/today
/set_rate <user_id> <amount_per_hour>
/ung <amount>
/salary <YYYY-MM>
/edit_time <user_id> <YYYY-MM-DD> in <HH:MM>
/edit_time <user_id> <YYYY-MM-DD> out <HH:MM>
/edit_time <user_id> <YYYY-MM-DD> break <minutes>
/reset_today <user_id>
```

Vi du tinh luong:

```text
/set_rate <your_admin_telegram_id> 16
/ung 100$
/salary 2026-06 <your_admin_telegram_id>
```

Bot tinh:

```text
luong gross = tong gio lam trong thang * hourly rate
luong net = gross - tong tien ung trong thang
```

## Dashboard

Chay dashboard:

```powershell
python dashboard_time_tracker.py
```

Mo trinh duyet:

```text
http://127.0.0.1:5000
```

Dashboard co:

```text
- Xem bang cong theo ngay
- Sua gio in/out/break/status truc tiep
- Xem bang luong theo thang
- Set hourly rate
- Add advance
- Export Day CSV
- Export Salary CSV
```

Lenh admin bo sung trong Telegram:

```text
/open_shifts
/backup
```
