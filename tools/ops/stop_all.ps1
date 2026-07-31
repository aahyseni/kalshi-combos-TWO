# One-click bot shutdown: kill supervisors AND THE RELIGHTER first (they are the
# processes that can bring the bot back), then bots and probers; verify; then
# offer a cancel-all sweep of resting quotes.
# Run via STOP_BOT.bat.
#
# WHY THE RELIGHTER GOES IN THE FIRST GROUP (2026-07-27): it is the bot's PARENT
# and the only process that restarts it. Killed in the same pass as the bot it
# would refuse anyway (an operator stop force-kills the child, which leaves no
# halt receipt => default-deny => escalate), but killing it FIRST means the
# operator never sees a spurious relight_terminal_escalation line on the way out.
# -KeepPid (2026-07-31): the hang watchdog stops a hung tree through THIS
# script before relighting it; the pid it passes is its own, so the stop
# does not kill the process performing the recovery. An operator stop passes
# nothing and the watchdog dies in the FIRST group (it too can resurrect the
# bot, so it goes down before the bot does).
param([switch]$NoPrompt, [int]$KeepPid = 0)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root

# FULL SWEEP PREDICATE (2026-07-31 watchdog-relight blockage): includes the
# monitor windows (watch_main/watch_prober) so this stop pass is the COMPLETE
# stack teardown — bot, probers, monitors, watchdog — with exactly one carve-out:
# a watchdog-initiated stop (-KeepPid) keeps the watchdog's own process TREE
# (its python AND its cmd host / venv shim all match 'hang_watchdog') alive to
# perform the relight.
# OURS-ONLY (2026-07-31 adversarial gate): membership is the LAUNCH-SITE
# signature in ours_predicate.ps1, never a bare keyword — the old keyword net
# was proven live to select a foreign decoy python and other projects' shells
# whose command TEXT merely mentioned the tree. Full derivation + the
# executed-proof coverage (prove_watchdog P7) live in that file.
. "$PSScriptRoot\ours_predicate.ps1"
$procs = Get-CimInstance Win32_Process |
    Where-Object { (Test-CombomakerOurs $_) -and
                   $_.ProcessId -ne $PID -and $_.ProcessId -ne $KeepPid -and
                   -not ($KeepPid -ne 0 -and $_.CommandLine -match 'hang_watchdog') }
if (-not $procs) {
    Write-Host "Nothing running (no combomaker/prober/watchdog processes found)." -ForegroundColor Green
} else {
    $supervisors = @($procs | Where-Object { $_.CommandLine -match 'supervisor|relight|hang_watchdog' })
    $rest = @($procs | Where-Object { $_.CommandLine -notmatch 'supervisor|relight|hang_watchdog' })
    foreach ($p in $supervisors) {
        Write-Host "Stopping supervisor/relighter PID $($p.ProcessId)" -ForegroundColor Yellow
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {}
    }
    Start-Sleep -Milliseconds 500
    foreach ($p in $rest) {
        Write-Host "Stopping PID $($p.ProcessId): $($p.CommandLine.Substring(0, [Math]::Min(90, $p.CommandLine.Length)))" -ForegroundColor Yellow
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {}
    }
    Start-Sleep -Seconds 2
    # Same predicate as the kill list: a watchdog that survived an OPERATOR
    # stop is exactly the process that could relight against operator intent,
    # so it must be flagged here; a watchdog-initiated stop (KeepPid) keeps
    # its own process alive by design.
    $left = Get-CimInstance Win32_Process |
        Where-Object { (Test-CombomakerOurs $_) -and
                       $_.ProcessId -ne $PID -and $_.ProcessId -ne $KeepPid -and
                       -not ($KeepPid -ne 0 -and $_.CommandLine -match 'hang_watchdog') }
    if ($left) {
        Write-Host "STILL RUNNING (kill by hand):" -ForegroundColor Red
        $left | ForEach-Object { Write-Host "  PID $($_.ProcessId): $($_.CommandLine)" }
        exit 1
    }
    Write-Host "All combomaker/prober processes stopped." -ForegroundColor Green
}

# ORPHANED POOL WORKERS (2026-07-25: four multiprocessing spawn workers from a
# force-killed morning stack idled for 2h). A spawn_main python whose PARENT
# is dead is an orphan of a killed bot - safe to reap. Workers with a live
# parent (any other app's) are left alone.
$workers = Get-CimInstance Win32_Process |
    Where-Object { $_.Name -match '^python' -and $_.CommandLine -match 'multiprocessing\.spawn' }
foreach ($w in $workers) {
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($w.ParentProcessId)" -ErrorAction SilentlyContinue
    if (-not $parent) {
        try { Stop-Process -Id $w.ProcessId -Force; Write-Host "Reaped orphaned pool worker PID $($w.ProcessId)" -ForegroundColor Yellow } catch {}
    }
}

# Resting quotes from a hard kill lapse on their own TTL; a cancel-all clears
# them immediately instead.
if (-not $NoPrompt) {
    $ans = Read-Host "Cancel ALL resting quotes on the exchange now? (y/n)"
    if ($ans -eq "y") {
        .venv\Scripts\python.exe -m combomaker.ops.cli cancel-all --env prod
    }
}
