@echo off
rem YouTube summary bot - laptop mode (Task Scheduler runs this file)
rem Secrets are read from secrets.local.json in this folder. Never write keys here.
cd /d "%~dp0"
python yt_bot.py >> yt_bot.log 2>&1
