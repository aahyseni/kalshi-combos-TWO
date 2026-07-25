# Live-log monitor: halts, fills, declines, waivers, errors.
# Launched by START_BOT.bat in its own window; pass the log path as -Log.
param([Parameter(Mandatory = $true)][string]$Log)

$host.UI.RawUI.WindowTitle = "MONITOR (main) - $Log"
$pattern = 'halted|kill_switch|quote_executed_msg|fill_recovery_late|fill_recovery_quote_cancelled|fills_ledger_missing|fills_ledger_sweep_summary|hard_trip|give_back|Traceback|CRITICAL|preflight_fail|supervisor_killed|ACCUMULATED|"phase": "decline"|waiver_granted|lastlook_waiver_retry|HALT'

Write-Host "Watching $Log for: halts / fills / declines / waivers / errors" -ForegroundColor Cyan
while (-not (Test-Path $Log)) { Start-Sleep -Seconds 1 }
Get-Content -Path $Log -Wait -Tail 200 | Select-String -Pattern $pattern | ForEach-Object {
    $line = $_.Line
    $color = "Gray"
    if ($line -match 'quote_executed_msg') { $color = "Green" }
    elseif ($line -match '"phase": "decline"|ACCUMULATED') { $color = "Yellow" }
    elseif ($line -match 'halted|kill_switch|hard_trip|HALT|CRITICAL|Traceback|supervisor_killed|preflight_fail') { $color = "Red" }
    elseif ($line -match 'waiver_granted|lastlook_waiver_retry') { $color = "Cyan" }
    Write-Host $line -ForegroundColor $color
}
