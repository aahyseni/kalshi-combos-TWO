@echo off
rem One-click stop: kills supervisors first (they respawn the bot), then bots
rem and probers, then offers a cancel-all sweep of resting quotes.
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File tools\ops\stop_all.ps1
pause
