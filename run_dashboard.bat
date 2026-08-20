@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt
python dashboard_time_tracker.py
pause
