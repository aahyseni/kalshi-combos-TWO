@echo off
rem One-click start: supervisor + main monitor + fill prober + prober monitor.
rem Refuses to start if any combomaker process is already running (STOP_BOT first).
cd /d %~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File tools\ops\start_all.ps1
pause
