@echo off
REM Daily payroll backup out of Neon. Registered in Windows Task Scheduler as
REM "Time Tracker - daily Neon backup". Safe to double-click by hand as well.
REM
REM Output is APPENDED to backups\backup.log rather than shown, because a
REM scheduled run has no window to show it in. The exit code is passed through,
REM so Task Scheduler's "Last Run Result" column tells the truth: 0x0 means the
REM backup was taken AND verified, anything else means do not rely on it.
cd /d "%~dp0"
if not exist backups mkdir backups
echo. >> backups\backup.log
echo ---------- %DATE% %TIME% ---------- >> backups\backup.log
".venv\Scripts\python.exe" scripts\backup_neon.py >> backups\backup.log 2>&1
exit /b %ERRORLEVEL%
