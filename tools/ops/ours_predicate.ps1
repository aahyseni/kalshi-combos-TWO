# THE one definition of "ours" for the stop sweep / start guards (2026-07-31
# adversarial gate). The old predicate was a bare keyword net —
#   CommandLine -match 'combomaker|fill_prober|hang_watchdog|watch_main|watch_prober'
# — which kills ANY process whose command TEXT mentions those words: proven
# live on 2026-07-31 with a foreign decoy python ("python -c ... --tag-
# combomaker-notes" selected for kill), three OTHER-project bash shells whose
# command text mentioned the tree, and (at the 18:35 relight) live analysis
# shells. A process from another checkout (kct-reanchor) would die the same
# way. "Ours" must mean OUR LAUNCH SITES, not our name in someone's argv.
#
# WHY NOT ExecutablePath ALONE: the venv python.exe is a launcher SHIM whose
# child (the REAL interpreter, an identical command line) runs with
# ExecutablePath = the BASE interpreter (measured live 2026-07-31: bot child
# PID 25612 ExecutablePath C:\...\Python313\python.exe). An exe-path filter
# would spare the real bot — the 17:34 under-sweep class. The stable
# discriminator is the LAUNCH-SITE SIGNATURE: the exact argv shapes that
# start_all.ps1's Start-Process lines and supervisor_launch_cmd produce —
# kept in lockstep with those launch sites (both live in this directory's
# scripts + ops/quote_app.py; change a launch line, change the signature).
#
# Dot-source from a script whose tree root is two levels up:
#   . "$PSScriptRoot\ours_predicate.ps1"
# Exposes: $OursRoot and Test-CombomakerOurs (predicate over a Win32_Process
# row). Verified by tools/ops/prove_watchdog.py P7, which evaluates THIS file
# against the verbatim 17:34 process table plus foreign-decoy rows.

$OursRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$OursRootRe = [regex]::Escape($OursRoot)

# Our python entry points, as launched: the venv interpreter (relative
# ".venv\Scripts\python.exe" — our scripts Set-Location to the tree root
# before launching — or absolute under THIS tree, as supervisor_launch_cmd
# passes sys.executable) running one of OUR modules/scripts.
$OursVenvRe = '((^|[\s&"''])\.venv[\\/]Scripts[\\/]python\.exe|' + $OursRootRe + '\\\.venv\\Scripts\\python\.exe)'
$OursEntryRe = '(-m\s+combomaker\.|tools[\\/]ops[\\/]hang_watchdog\.py|tools[\\/]diagnostics[\\/]fill_prober\.py)'
# Our launcher windows: the exact titles start_all.ps1 sets, and the monitor
# scripts it starts by path.
$OursShellRe = '(title (BOT \(quote mode\)|FILL PROBER|HANG WATCHDOG)|tools[\\/]ops[\\/]watch_(main|prober)\.ps1)'
# Any absolute venv-interpreter path in the command line (used to detect a
# DIFFERENT checkout's absolute launch, which is never ours).
$AnyAbsVenvRe = '[A-Za-z]:[^"'']*?\.venv[\\/]Scripts[\\/]python\.exe'

function Test-CombomakerOurs {
    param($Proc)
    $cl = $Proc.CommandLine
    if (-not $cl) { return $false }
    if ($cl -match ($OursVenvRe + '\s+.*' + $OursEntryRe)) {
        # An absolute venv path from ANOTHER tree is never ours, even if the
        # rest of the argv looks like our launch (kct-reanchor protection).
        if (($cl -match $AnyAbsVenvRe) -and ($cl -notmatch $OursRootRe)) { return $false }
        return $true
    }
    return [bool]($cl -match $OursShellRe)
}
