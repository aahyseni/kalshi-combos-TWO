# Fill-prober monitor: richness of every new fill vs the best competing maker.
# Launched by START_BOT.bat in its own window; pass the prober log path as -Log.
param([Parameter(Mandatory = $true)][string]$Log)

$host.UI.RawUI.WindowTitle = "MONITOR (prober) - $Log"
Write-Host "Watching $Log for RICHNESS / FAILED / no-competing lines" -ForegroundColor Cyan
while (-not (Test-Path $Log)) { Start-Sleep -Seconds 1 }
Get-Content -Path $Log -Wait -Tail 50 | Select-String -Pattern 'RICHNESS|FAILED|no competing' | ForEach-Object {
    $line = $_.Line
    if ($line -match 'FAILED') { Write-Host $line -ForegroundColor Red }
    elseif ($line -match 'RICHNESS \+') { Write-Host $line -ForegroundColor Green }
    else { Write-Host $line -ForegroundColor Yellow }
}
