@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
python time_tracking_bot.py
pause
