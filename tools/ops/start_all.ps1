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

$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$botLog = "data\live_$stamp.log"
$proberLog = "data\fill_prober_$stamp.log"
# Pointer file so READOUT.bat and log tooling know the active run's log.
Set-Content -Path "data\CURRENT_LOG.txt" -Value "$botLog`r`n$proberLog" -Encoding ascii

Write-Host "Starting bot stack (logs: $botLog / $proberLog)" -ForegroundColor Cyan

# 1) Supervisor (spawns + respawns the quote app). All output -> dated log.
Start-Process cmd -ArgumentList "/k", "title BOT supervisor && .venv\Scripts\python.exe -m combomaker.ops.supervisor --env prod --config config\prod-live-wc.local.yaml > $botLog 2>&1"

# 2) Main monitor (halts / fills / declines / waivers / errors).
Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-File", "tools\ops\watch_main.ps1", "-Log", $botLog

# 3) Fill prober (re-RFQs every new fill, reports richness vs other makers).
Start-Process cmd -ArgumentList "/k", "title FILL PROBER && .venv\Scripts\python.exe tools\diagnostics\fill_prober.py > $proberLog 2>&1"

# 4) Prober monitor.
Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-File", "tools\ops\watch_prober.ps1", "-Log", $proberLog

Write-Host "All four windows launched. Close this one freely." -ForegroundColor Green
