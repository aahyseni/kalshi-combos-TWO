# Live-log monitor: halts, fills, declines, waivers, errors.
# Launched by START_BOT.bat in its own window; pass the log path as -Log.
param([Parameter(Mandatory = $true)][string]$Log)

$host.UI.RawUI.WindowTitle = "MONITOR (main) - $Log"
# Boot events included (2026-07-25: four silent windows read as "nothing is
# going on" — the monitor must SHOW the bot coming up, then go quiet-unless-
# eventful).
$boot = 'supervisor_starting|supervisor_launched|supervisor_heartbeat_wedged|supervisor_emergency_kill|kill_file_present|needs_reconcile_marker|quote_app_starting|prod_preflight_green|metadata_cache_loaded|ws_connected|communications_subscribed|exposure_rehydrated|rehydrated_legs_armed|startup_reconciled|account_standing'
$pattern = "halted|kill_switch|quote_executed_msg|fill_recovery_late|fill_recovery_quote_cancelled|fills_ledger_missing|fills_ledger_sweep_summary|hard_trip|give_back|Traceback|CRITICAL|preflight_fail|supervisor_killed|ACCUMULATED|`"phase`": `"decline`"|waiver_granted|lastlook_waiver_retry|HALT|$boot"

Write-Host "Watching $Log" -ForegroundColor Cyan
Write-Host "You should see BOOT lines (supervisor_starting, preflight, rehydrated...) within ~60s." -ForegroundColor Cyan
Write-Host "After boot this stays quiet until something happens: fills GREEN, declines YELLOW, halts/errors RED." -ForegroundColor Cyan
while (-not (Test-Path $Log)) { Start-Sleep -Seconds 1 }
Get-Content -Path $Log -Wait -Tail 200 | Select-String -Pattern $pattern | ForEach-Object {
    $line = $_.Line
    $color = "Gray"
    if ($line -match 'quote_executed_msg') { $color = "Green" }
    elseif ($line -match '"phase": "decline"|ACCUMULATED') { $color = "Yellow" }
    elseif ($line -match 'halted|kill_switch|hard_trip|HALT|CRITICAL|Traceback|supervisor_killed|supervisor_emergency_kill|supervisor_heartbeat_wedged|preflight_fail|kill_file_present') { $color = "Red" }
    elseif ($line -match 'waiver_granted|lastlook_waiver_retry') { $color = "Cyan" }
    elseif ($line -match 'supervisor_starting|supervisor_launched|quote_app_starting|prod_preflight_green|metadata_cache_loaded|ws_connected|communications_subscribed|exposure_rehydrated|rehydrated_legs_armed|startup_reconciled|account_standing') { $color = "White" }
    Write-Host $line -ForegroundColor $color
}
