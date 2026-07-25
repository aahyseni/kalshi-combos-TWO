@echo off
rem Plain-English read-out of tonight's declines from the CURRENT run's log.
cd /d %~dp0
if not exist data\CURRENT_LOG.txt (
    echo No data\CURRENT_LOG.txt - start the bot with START_BOT.bat first.
    pause
    exit /b 1
)
set /p BOTLOG=<data\CURRENT_LOG.txt
.venv\Scripts\python.exe tools\diagnostics\decline_readout.py %BOTLOG%
pause
