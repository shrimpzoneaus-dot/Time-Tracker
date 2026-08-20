@echo off
cd /d "%~dp0"
start "Time Tracker Dashboard" "%~dp0run_dashboard.bat"
start "Time Tracker Telegram Bot" "%~dp0run_time_bot.bat"
