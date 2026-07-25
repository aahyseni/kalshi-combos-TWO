@echo off
rem P(book) shadow read-out on the CURRENT run's log (run ~4:00 PM ET on the
rem pregame sample before arming the steer).
cd /d %~dp0
if not exist data\CURRENT_LOG.txt (
    echo No data\CURRENT_LOG.txt - start the bot with START_BOT.bat first,
    echo or run: .venv\Scripts\python.exe tools\diagnostics\pbook_shadow_readout.py data\^<logfile^>
    pause
    exit /b 1
)
set /p BOTLOG=<data\CURRENT_LOG.txt
echo Reading %BOTLOG% ...
.venv\Scripts\python.exe tools\diagnostics\pbook_shadow_readout.py %BOTLOG%
pause
