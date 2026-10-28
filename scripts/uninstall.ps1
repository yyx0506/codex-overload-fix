#Requires -Version 5.1
<#
.SYNOPSIS
    Stop the relay and remove its autostart entry.

.DESCRIPTION
    Removes the HKCU Run entry, terminates the watchdog and relay processes,
    and optionally deletes the generated launchers and logs.

    It does NOT touch your Codex config.toml - you remove the provider block
    yourself so you can see exactly what changed.

.PARAMETER Purge
    Also delete %LOCALAPPDATA%\codex-overload-fix (launchers and logs).
#>
[CmdletBinding()]
param([switch]$Purge)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "  $msg" -ForegroundColor DarkGray }
function Write-Ok($msg)   { Write-Host "  [ok] $msg" -ForegroundColor Green }

Write-Host ""
Write-Host "Codex Overload Fix - uninstaller" -ForegroundColor Cyan
Write-Host "-------------------------------" -ForegroundColor DarkGray

$stateDir = Join-Path $env:LOCALAPPDATA 'codex-overload-fix'
$runKey   = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runName  = 'CodexOverloadRelay'

try {
    $existing = Get-ItemProperty -Path $runKey -Name $runName -ErrorAction Stop
    if ($existing) {
        Remove-ItemProperty -Path $runKey -Name $runName
        Write-Ok "removed $runName from HKCU Run"
    }
} catch {
    Write-Step 'no autostart entry found'
}

$killed = 0
Get-CimInstance Win32_Process -Filter "Name = 'wscript.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*watch-relay.vbs*' } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $killed++
    }

Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*relay.py*' } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $killed++
    }

if ($killed -gt 0) {
    Write-Ok "stopped $killed process(es)"
} else {
    Write-Step 'nothing was running'
}

if ($Purge) {
    if (Test-Path $stateDir) {
        Remove-Item -Recurse -Force $stateDir
        Write-Ok "deleted $stateDir"
    }
}

Write-Host ""
Write-Host "If the installer was run with -WriteConfig, also restore the config:" -ForegroundColor Yellow
Write-Host "  python tools\configure_codex.py --restore" -ForegroundColor White
Write-Host ""
Write-Host "Otherwise edit $env:USERPROFILE\.codex\config.toml yourself:" -ForegroundColor Yellow
Write-Host "  1. set  model_provider  back to the provider you used before"
Write-Host "     (direct mode: the default; gateway mode: your gateway's own provider,"
Write-Host "      cockpit-tools users usually `"codex_local_access`")"
Write-Host "  2. delete the [model_providers.openai-retry] or"
Write-Host "     [model_providers.gateway-retry] block the installer added"
Write-Host ""
Write-Host "Check what is selected now:  python tools\relayctl.py path" -ForegroundColor DarkGray
Write-Host ""
