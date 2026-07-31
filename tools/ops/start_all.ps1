# One-click bot startup: single-instance guard, dated logs, five windows
# (bot, main monitor, prober, prober monitor, hang watchdog). Run via
# START_BOT.bat.
#
# -Auto / -CallerPid (2026-07-31, the 45h frozen-log outage): the hang
# watchdog (tools/ops/hang_watchdog.py) relights a stalled tree through THIS
# script so every guard here (mutex, single-instance, KILL semantics) stays
# authoritative. -Auto means "no human at the keyboard": the human-gated KILL
# prompt becomes a refusal (exit 1 — the watchdog latches a halt receipt on
# that), watchdog state is NOT purged (its flap memory must survive its own
# relights), and no second watchdog window is spawned. -CallerPid exempts the
# calling watchdog from the single-instance guard.
param([switch]$Auto, [int]$CallerPid = 0)
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
    Where-Object { $_.CommandLine -match 'combomaker|fill_prober|hang_watchdog' -and
                   $_.ProcessId -ne $PID -and $_.ProcessId -ne $CallerPid }
$pythons = @($matches_all | Where-Object { $_.Name -match '^python' })
# In -Auto (a watchdog-initiated relight) the caller's own host cmd window
# also matches 'hang_watchdog' — it is alive and must not be swept as stale.
$shells = @($matches_all | Where-Object { $_.Name -match '^(cmd|powershell)' -and
    -not ($Auto -and $_.CommandLine -match 'hang_watchdog') })
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
# 2026-07-26: loop_progress.json joins the list - it is the second liveness
# signal (per-loop "working" ages, risk/progress.py) and a stale one reads as
# an instantly-stalled loop exactly the same way. The bot also rewrites it
# before launching the supervisor, so this is belt-and-braces.
# Captured BEFORE the purge: a heartbeat on disk proves the PREVIOUS run
# actually reached liveness at least once. That is the flap discriminator used
# by the KILL block below - no timer, no retry count, just "did the last
# relight produce a working bot".
$prevRunReachedLiveness = Test-Path "data\heartbeat.txt"
foreach ($hb in @("data\heartbeat.txt", "data\supervisor_heartbeat.txt", "data\loop_progress.json")) {
    if (Test-Path $hb) {
        Remove-Item -Force $hb
        Write-Host "Removed stale $hb (nothing was running)" -ForegroundColor Yellow
    }
}
# OPERATOR start = a fresh watchdog episode: clear its flap memory / halt latch
# (a human relight is exactly the "reviewed and ready" event the latch waits
# for). NEVER cleared in -Auto — the watchdog's own relights must not launder
# its restart budget.
if (-not $Auto) {
    foreach ($wf in @("data\watchdog_state.json")) {
        if (Test-Path $wf) {
            Remove-Item -Force $wf
            Write-Host "Cleared $wf (operator start = fresh watchdog episode)" -ForegroundColor Yellow
        }
    }
}
# A KILL file blocks startup BY DESIGN (real halt or supervisor emergency).
# Never silently delete it - show it and ask. The needs_reconcile marker is
# left alone: the bot reconciles against the exchange at boot and clears it
# itself.
#
# 2026-07-27 (machinery proportionality review): a MACHINE-written KILL no
# longer requires a human. Measured over the retained corpus: 8 supervisor
# emergency kills, ALL 8 with exchange_reachable=true, ALL 8 human-gated. The
# 07-27 kill fired at maintenance age=30.9s against a 30.5s bound - ONE tick
# over - and that same "unable to manage risk" bot then withdrew all 67 quotes
# in 218ms. The detector was right; the HUMAN LATCH is what turned a 0.5s
# overrun into 33 minutes of downtime.
#
# This is safe ONLY because needs_reconcile is untouched and is written
# unconditionally by the supervisor: an auto-relit bot still CANNOT quote until
# _startup_reconcile has enumerated and cancelled every leftover quote on the
# exchange and preflight gate 5 is green. Worst case per wasted cycle is the
# cancel-all it already performs.
#
# Flap guard with no invented number: we auto-clear only if the PREVIOUS run
# actually reached liveness (wrote a heartbeat). A run that died before ever
# beating means relighting is not producing a working bot - that is when a
# human should look.
$killIsMachineWritten = $false
if (Test-Path "KILL") {
    $killBody = (Get-Content "KILL" -Raw)
    # the only string supervisor._write_kill_file emits (supervisor.py:214)
    $killIsMachineWritten = $killBody.TrimStart().StartsWith("supervisor kill:")
}
if ((Test-Path "KILL") -and $killIsMachineWritten -and $prevRunReachedLiveness) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $archive = "data\KILL_$stamp.txt"
    Move-Item -Force "KILL" $archive
    Write-Host "Machine-written KILL auto-cleared (archived to $archive):" -ForegroundColor Yellow
    Get-Content $archive | ForEach-Object { Write-Host "   $_" -ForegroundColor Yellow }
    Write-Host "needs_reconcile remains - the bot re-proves the book against the exchange before quoting." -ForegroundColor Yellow
}
elseif (Test-Path "KILL") {
    if ($killIsMachineWritten -and -not $prevRunReachedLiveness) {
        Write-Host "FLAP GUARD: the previous run never reached liveness, so this KILL is NOT auto-cleared." -ForegroundColor Red
    }
    Write-Host "A KILL file is present - the bot refuses to start with it:" -ForegroundColor Red
    Get-Content "KILL" | ForEach-Object { Write-Host "   $_" -ForegroundColor Red }
    if ($Auto) {
        # No human at the keyboard: a KILL that is not auto-clearable (real
        # risk halt, or a machine KILL from a run that never reached liveness)
        # requires operator review. Refuse; the watchdog latches on this.
        Write-Host "AUTO MODE: KILL requires operator review - refusing to start." -ForegroundColor Red
        exit 1
    }
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

# 1) THE RELIGHTER, which spawns THE BOT (cli run, quote mode) as its CHILD.
#    The bot in turn spawns the safety supervisor as its OWN subprocess
#    (quote_app.supervisor_launch_cmd) - launching `-m combomaker.ops.supervisor`
#    standalone insta-kills on the missing heartbeat the not-yet-started bot
#    hasn't written (2026-07-25 launch failures #1 and #3; entrypoint verified
#    against the live process list).
#
#    NOT RUN UNDER THE RELIGHTER (2026-07-27 decision, reverted before it ever
#    went live). ops/relight.py was built to be the bot's PARENT and auto-restart
#    it after a lifecycle-class halt. Two findings killed it as a launcher:
#      1. The class it targets has fired ZERO times since the aeed109 breaker
#         rebuild (halt_metadata_change per run: 3 pre-rebuild, 0 in every run
#         since). It automates a problem we no longer have.
#      2. Adversarial gate F1: the boot-wedge branch returns without calling
#         proc.terminate(), so it ABANDONS A LIVE CHILD - the MONITOR window
#         says the bot is down and a human must check, while a parentless bot
#         quotes real money. Strictly worse than having no relighter.
#    The actual recovery need is already met, with no new process: the KILL
#    block above auto-clears a MACHINE-written KILL. relight.py stays in the
#    tree only for _write_halt_receipt, which quote_app imports; it is not a
#    launcher and must not become one again without closing F1.
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
    Write-Host "All windows launched. Close this one freely." -ForegroundColor Green
} elseif ($bots.Count -eq 0) {
    Write-Host "WARNING: no bot process visible yet after 6s - check the MONITOR window for boot lines." -ForegroundColor Yellow
} else {
    Write-Host "DUPLICATE BOTS DETECTED ($($bots.Count)) - killing EVERYTHING. Run START_BOT.bat once, alone." -ForegroundColor Red
    Get-CimInstance Win32_Process |
        Where-Object { $_.Name -match 'python' -and ($_.CommandLine -match 'combomaker|fill_prober') } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force } catch {} }
    exit 1
}

# 5) HANG WATCHDOG (2026-07-31, the 45h frozen-log outage): an EXTERNAL
#    supervisor that watches WORK (log + decision-flow advance), not PIDs.
#    Detects the exact 7/29 failure (PID alive, log frozen) and the boot-loop
#    failure (bot exits instantly at boot), stops/relights through THESE
#    scripts, and refuses to flap (halt receipt + stays down loud). Launched
#    LAST so the stack it certifies exists; only on operator starts — a
#    watchdog-initiated -Auto relight already has a watchdog (the caller).
#    It holds its own per-tree named mutex, so a stale duplicate refuses.
if (-not $Auto) {
    Start-Process cmd -ArgumentList "/k", "title HANG WATCHDOG && echo Watching WORK (log + store advance). Output tees to data\hang_watchdog.log && .venv\Scripts\python.exe tools\ops\hang_watchdog.py run"
    Write-Host "Hang watchdog launched (window: HANG WATCHDOG)." -ForegroundColor Green
}
$mutex.ReleaseMutex()
