# One-click bot startup: single-instance guard, dated logs, four windows
# (bot, main monitor, prober, prober monitor). Run via START_BOT.bat.
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root

# LAUNCH MUTEX (2026-07-25 incident #4): two double-clicks ~1s apart both
# passed the process-count guard before either had spawned a python (a
# check-then-act race) and TWO full stacks quoted one account for 15 min.
# A named OS mutex makes the whole launch atomic: the second run refuses
# instantly, no matter how close the race.
$created = $false
$mutex = New-Object System.Threading.Mutex($true, "Global\combomaker_start_bot", [ref]$created)
if (-not $created) {
    Write-Host "REFUSING TO START - another START_BOT is already running right now." -ForegroundColor Red
    exit 1
}

# SINGLE-INSTANCE GUARD - the 2026-07-25 zombie double-stack (two supervisors +
# two bots on one account) caused a 429 rate storm and quote races. Never again.
# Only LIVE PYTHON processes refuse; dead cmd/powershell shell windows from a
# stopped stack (their command lines still name the bot) are swept, not blockers.
$matches_all = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match 'combomaker|fill_prober' -and $_.ProcessId -ne $PID }
$pythons = @($matches_all | Where-Object { $_.Name -match '^python' })
$shells = @($matches_all | Where-Object { $_.Name -match '^(cmd|powershell)' })
if ($pythons) {
    Write-Host "REFUSING TO START - the bot/prober is already running:" -ForegroundColor Red
    $pythons | ForEach-Object { Write-Host "  PID $($_.ProcessId): $($_.CommandLine)" }
    Write-Host "Run STOP_BOT.bat first." -ForegroundColor Red
    exit 1
}
if ($shells) {
    foreach ($s in $shells) { try { Stop-Process -Id $s.ProcessId -Force } catch {} }
    Write-Host "Closed $($shells.Count) stale window(s) from a previous run." -ForegroundColor Yellow
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

# POST-LAUNCH VERIFICATION (incident #4): prove exactly ONE bot came up.
# COUNT ROOTS ONLY: the venv python.exe is a launcher SHIM that spawns the
# real interpreter as a child with an IDENTICAL command line (proven
# empirically 2026-07-25 - one sleeper launch = 2 processes), so a naive
# count always reads 2 for one healthy bot. A root = a matched process
# whose parent is NOT itself in the matched set.
Start-Sleep -Seconds 6
$matched = @(Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match 'python' -and $_.CommandLine -match 'combomaker\.ops\.cli run' })
$ids = @($matched | ForEach-Object { $_.ProcessId })
$bots = @($matched | Where-Object { $ids -notcontains $_.ParentProcessId })
if ($bots.Count -eq 1) {
    Write-Host "VERIFIED: exactly one bot process (PID $($bots[0].ProcessId))." -ForegroundColor Green
    Write-Host "All four windows launched. Close this one freely." -ForegroundColor Green
} elseif ($bots.Count -eq 0) {
    Write-Host "WARNING: no bot process visible yet after 6s - check the MONITOR window for boot lines." -ForegroundColor Yellow
} else {
    Write-Host "DUPLICATE BOTS DETECTED ($($bots.Count)) - killing EVERYTHING. Run START_BOT.bat once, alone." -ForegroundColor Red
    Get-CimInstance Win32_Process |
        Where-Object { $_.Name -match 'python' -and ($_.CommandLine -match 'combomaker|fill_prober') } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch {} }
    exit 1
}
$mutex.ReleaseMutex()
