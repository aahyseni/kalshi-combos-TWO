# One-click bot startup: single-instance guard, dated logs, four windows
# (supervisor, main monitor, prober, prober monitor). Run via START_BOT.bat.
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root

# SINGLE-INSTANCE GUARD - the 2026-07-25 zombie double-stack (two supervisors +
# two bots on one account) caused a 429 rate storm and quote races. Never again.
$running = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match 'combomaker|fill_prober' }
if ($running) {
    Write-Host "REFUSING TO START - combomaker processes are already running:" -ForegroundColor Red
    $running | ForEach-Object { Write-Host "  PID $($_.ProcessId): $($_.CommandLine)" }
    Write-Host "Run STOP_BOT.bat first." -ForegroundColor Red
    exit 1
}

# PRE-START HYGIENE (2026-07-25: the first operator launch failed here).
# Nothing is running (guard above), so liveness files are stale leftovers:
# a stale heartbeat makes the supervisor declare "wedged" instantly and
# emergency-KILL before the bot even starts. Delete them.
foreach ($hb in @("data\heartbeat.txt", "data\supervisor_heartbeat.txt")) {
    if (Test-Path $hb) {
        Remove-Item -Force $hb
        Write-Host "Removed stale $hb (nothing was running)" -ForegroundColor Yellow
    }
}
# A KILL file blocks startup BY DESIGN (real halt or supervisor emergency).
# Never silently delete it - show it and ask. The needs_reconcile marker is
# left alone: the bot reconciles against the exchange at boot and clears it
# itself.
if (Test-Path "KILL") {
    Write-Host "A KILL file is present - the bot refuses to start with it:" -ForegroundColor Red
    Get-Content "KILL" | ForEach-Object { Write-Host "   $_" -ForegroundColor Red }
    $ans = Read-Host "Reviewed and ready to relight? Delete the KILL file and start? (y/n)"
    if ($ans -ne "y") {
        Write-Host "Leaving KILL in place. Not starting." -ForegroundColor Red
        exit 1
    }
    Remove-Item -Force "KILL"
    Write-Host "KILL file cleared - relighting." -ForegroundColor Yellow
}

$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$botLog = "data\live_$stamp.log"
$proberLog = "data\fill_prober_$stamp.log"
# Pointer file so READOUT.bat and log tooling know the active run's log.
Set-Content -Path "data\CURRENT_LOG.txt" -Value "$botLog`r`n$proberLog" -Encoding ascii

Write-Host "Starting bot stack (logs: $botLog / $proberLog)" -ForegroundColor Cyan

# 1) THE BOT (cli run, quote mode). It spawns the safety supervisor as its
#    OWN subprocess (quote_app.supervisor_launch_cmd) - launching
#    `-m combomaker.ops.supervisor` standalone insta-kills on the missing
#    heartbeat the not-yet-started bot hasn't written (2026-07-25 launch
#    failures #1 and #3; entrypoint verified against the live process list).
#    All output -> dated log, so this window is quiet BY DESIGN.
Start-Process cmd -ArgumentList "/k", "title BOT (quote mode) && echo Bot running. This window is quiet BY DESIGN - all output goes to $botLog && echo Watch the MONITOR window for live events. Closing THIS window kills the bot. && .venv\Scripts\python.exe -m combomaker.ops.cli run --env prod --mode quote --confirm-live --config config\prod-live-wc.local.yaml > $botLog 2>&1"

# 2) Main monitor (halts / fills / declines / waivers / errors).
Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-File", "tools\ops\watch_main.ps1", "-Log", $botLog

# 3) Fill prober (re-RFQs every new fill, reports richness vs other makers).
Start-Process cmd -ArgumentList "/k", "title FILL PROBER && .venv\Scripts\python.exe tools\diagnostics\fill_prober.py > $proberLog 2>&1"

# 4) Prober monitor.
Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-File", "tools\ops\watch_prober.ps1", "-Log", $proberLog

Write-Host "All four windows launched. Close this one freely." -ForegroundColor Green
