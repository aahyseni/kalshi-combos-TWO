# One-click bot shutdown: kill supervisors FIRST (they respawn the bot), then
# bots and probers; verify; then offer a cancel-all sweep of resting quotes.
# Run via STOP_BOT.bat.
param([switch]$NoPrompt)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $root

$procs = Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match 'combomaker|fill_prober' -and $_.ProcessId -ne $PID }
if (-not $procs) {
    Write-Host "Nothing running (no combomaker/prober processes found)." -ForegroundColor Green
} else {
    $supervisors = @($procs | Where-Object { $_.CommandLine -match 'supervisor' })
    $rest = @($procs | Where-Object { $_.CommandLine -notmatch 'supervisor' })
    foreach ($p in $supervisors) {
        Write-Host "Stopping supervisor PID $($p.ProcessId)" -ForegroundColor Yellow
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {}
    }
    Start-Sleep -Milliseconds 500
    foreach ($p in $rest) {
        Write-Host "Stopping PID $($p.ProcessId): $($p.CommandLine.Substring(0, [Math]::Min(90, $p.CommandLine.Length)))" -ForegroundColor Yellow
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {}
    }
    Start-Sleep -Seconds 2
    $left = Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -match 'combomaker|fill_prober' }
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
