#Requires -Version 5.1
<#
.SYNOPSIS
    Install the Codex overload relay (universal edition) as a hidden,
    auto-restarting background process.

.DESCRIPTION
    Finds Python, writes hidden VBS launchers into %LOCALAPPDATA%\codex-overload-fix,
    registers a watchdog under HKCU\...\Run (no admin rights), then prints the
    exact config.toml block for the mode you chose.

    -Mode Direct   (default) the original layout: relay in front of chatgpt.com.
    -Mode Gateway  relay in front of an account-pool gateway (sub2api,
                   cockpit-tools, ...). -GatewayUrl is required, e.g.
                   http://127.0.0.1:8888 or https://sub2api.company.com.
                   A trailing /v1 is fine - the relay normalises it.

.PARAMETER Mode
    Direct or Gateway (default Direct).

.PARAMETER GatewayUrl
    The gateway's base URL. Required for -Mode Gateway.

.PARAMETER ApiKeyEnv
    Name of the env var Codex reads the gateway API key from
    (Gateway mode only, default SUB2API_API_KEY). The key itself is never
    stored by the installer.

.PARAMETER WriteConfig
    Edit %USERPROFILE%\.codex\config.toml automatically (backs it up first to
    config.toml.bak-codex-overload-fix). For non-technical users. Undo:
    python tools\configure_codex.py --restore

.PARAMETER ApiKey
    Gateway API key; with -WriteConfig it is written into config.toml for the
    user. If omitted in Gateway mode you are prompted (safe on the target
    machine; it does not end up in shell history).

.PARAMETER Port
    Local port for the relay. Default 8788 in Gateway mode, 8787 in Direct.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Mode Gateway -GatewayUrl http://127.0.0.1:8888 -WriteConfig
#>
[CmdletBinding()]
param(
    [ValidateSet('Direct','Gateway')][string]$Mode = 'Direct',
    [string]$GatewayUrl = '',
    [string]$ApiKeyEnv = 'SUB2API_API_KEY',
    [string]$ApiKey = '',
    [switch]$WriteConfig,
    [int]$Port = 0,
    [string]$Proxy = '',
    [switch]$NoAutostart
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "  $msg" -ForegroundColor DarkGray }
function Write-Ok($msg)   { Write-Host "  [ok] $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "  [!!] $msg" -ForegroundColor Yellow }

if ($Port -eq 0) { $Port = if ($Mode -eq 'Gateway') { 8788 } else { 8787 } }

Write-Host ""
Write-Host "Codex Overload Fix - universal edition - installer" -ForegroundColor Cyan
Write-Host "--------------------------------------------------" -ForegroundColor DarkGray

if ($Mode -eq 'Gateway') {
    if (-not $GatewayUrl) {
        throw "-Mode Gateway requires -GatewayUrl, e.g. -GatewayUrl http://127.0.0.1:8888"
    }
    if ($GatewayUrl -notmatch '^https?://') {
        $GatewayUrl = "http://$GatewayUrl"
    }
    Write-Step "mode: gateway - relay in front of $GatewayUrl"
    # Refuse to sit on the gateway's own port.
    if ($GatewayUrl -match '^https?://[^/]+:(\d+)') {
        $gport = [int]$Matches[1]
        if ($gport -eq $Port) {
            throw "the relay port equals the gateway port ($Port); pass -Port <other port>"
        }
    }
} else {
    Write-Step 'mode: direct - relay in front of chatgpt.com (original layout)'
}

# ---------------------------------------------------------------- locations
$root     = Split-Path -Parent $PSScriptRoot
$relayPy  = Join-Path $root 'relay\relay.py'
$stateDir = Join-Path $env:LOCALAPPDATA 'codex-overload-fix'
$logPath  = Join-Path $stateDir 'relay.log'
$startVbs = Join-Path $stateDir 'start-relay.vbs'
$watchVbs = Join-Path $stateDir 'watch-relay.vbs'
$runKey   = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runName  = 'CodexOverloadRelay'

if (-not (Test-Path $relayPy)) {
    throw "relay.py not found at $relayPy - run this from the repository's scripts\ folder."
}
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

# ---------------------------------------------------------------- port clash
function Test-PortBusy([int]$p) {
    try {
        return [bool](Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction Stop)
    } catch {
        return [bool](netstat -ano | Select-String ":$p\s" | Select-String 'LISTENING')
    }
}
if (Test-PortBusy $Port) {
    $owner = (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
              Select-Object -First 1).OwningProcess
    $name = if ($owner) { (Get-Process -Id $owner -ErrorAction SilentlyContinue).ProcessName } else { 'unknown' }
    Write-Warn2 "port $Port is already in use by '$name'"
    Write-Warn2 "pass -Port <free port> and use the same port in the config snippet"
}

# ---------------------------------------------------------------- find python
Write-Step 'Looking for Python...'
$python = $null
foreach ($candidate in @('py -3', 'python', 'python3')) {
    $parts = $candidate.Split(' ')
    $exe = $parts[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $out = & $exe @($parts[1..($parts.Count - 1)] | Where-Object { $_ }) -c "import sys;print(sys.executable)" 2>$null
    } catch { continue }
    if (-not $out) { continue }
    $path = ($out | Select-Object -First 1).Trim()
    if (-not $path -or -not (Test-Path $path)) { continue }
    if ($path -match 'WindowsApps') { continue }
    $ver = & $path -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
    if (-not $ver) { continue }
    $major, $minor = $ver.Trim().Split('.')
    if ([int]$major -lt 3 -or ([int]$major -eq 3 -and [int]$minor -lt 9)) {
        Write-Warn2 "$path is Python $ver (need 3.9+)"
        continue
    }
    $python = $path
    Write-Ok "Python $ver at $python"
    break
}
if (-not $python) {
    throw 'No usable Python 3.9+ found. Install from https://python.org and re-run.'
}

# ---------------------------------------------------------------- build command
$argList = @(
    "--mode $Mode",
    "--port $Port",
    "--log `"$logPath`"",
    '--max-attempts 30',
    '--max-conn-attempts 3',
    '--conn-budget 15',
    '--backoff-base 1',
    '--backoff-max 8',
    '--total-budget 300',
    '--commit-deadline 120',
    '--max-concurrent 4',
    '--retries-per-sec 2'
)
if ($Mode -eq 'Gateway') {
    $argList += "--upstream `"$GatewayUrl`""
    $argList += '--cooling-latency 0.2'
}
if ($Proxy)   { $argList += "--proxy $Proxy" }
$innerCmd = '"' + $python + '" "' + $relayPy + '" ' + ($argList -join ' ')
$vbsCmd = $innerCmd.Replace('"', '""')

# ---------------------------------------------------------------- launcher
$startContent = @"
' Codex Overload Fix - hidden launcher. Generated by install.ps1, do not edit.
Set sh = CreateObject("WScript.Shell")
sh.Run "$vbsCmd", 0, False
"@
Set-Content -Path $startVbs -Value $startContent -Encoding ASCII
Write-Ok "launcher  $startVbs"

# ---------------------------------------------------------------- watchdog
$checkCmd = "cmd /c netstat -ano | findstr ""LISTENING"" | findstr "":$Port "" >nul 2>&1"
$watchContent = @"
' Codex Overload Fix - watchdog. Generated by install.ps1, do not edit.
' Runs forever with no window (wscript.exe is a GUI-subsystem host).
Set sh = CreateObject("WScript.Shell")
check = "$($checkCmd.Replace('"','""'))"
cmd   = "$vbsCmd"

Do
  rc = sh.Run(check, 0, True)
  If rc <> 0 Then
    sh.Run cmd, 0, True
    WScript.Sleep 3000
  Else
    WScript.Sleep 30000
  End If
Loop
"@
Set-Content -Path $watchVbs -Value $watchContent -Encoding ASCII
Write-Ok "watchdog  $watchVbs"

# ---------------------------------------------------------------- autostart
if (-not $NoAutostart) {
    $entry = 'wscript.exe "' + $watchVbs + '"'
    New-Item -Path $runKey -Force | Out-Null
    Set-ItemProperty -Path $runKey -Name $runName -Value $entry
    Write-Ok "registered $runName in HKCU Run"
}

# ---------------------------------------------------------------- start now
Write-Step 'Starting the relay...'
Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + $watchVbs + '"') -WindowStyle Hidden
Start-Sleep -Seconds 4
if (Test-PortBusy $Port) {
    Write-Ok "listening on 127.0.0.1:$Port"
} else {
    Write-Warn2 "not listening yet - check $logPath"
}

# ---------------------------------------------------------------- config
$configPath = Join-Path $env:USERPROFILE '.codex\config.toml'
$configurePy = Join-Path $root 'tools\configure_codex.py'

if ($WriteConfig) {
    if ($Mode -eq 'Gateway' -and -not $ApiKey) {
        $ApiKey = Read-Host 'Gateway API key (paste, or Enter to skip and configure later)'
    }
    $cfgArgs = @('--config', $configPath, '--mode', $Mode.ToLower(), '--port', "$Port")
    if ($ApiKey) { $cfgArgs += @('--api-key', $ApiKey) }
    Write-Ok "editing $configPath (backup kept alongside)"
    & $python $configurePy @cfgArgs
    if ($LASTEXITCODE -eq 0) {
        Write-Host ""
        Write-Host "All done. Fully quit and restart Codex (tray icon -> Quit, not just close)." -ForegroundColor Yellow
        Write-Host "Everything the installer changed is reversible:" -ForegroundColor Cyan
        Write-Host "  relay:     powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1"
        Write-Host "  config:    python tools\configure_codex.py --restore"
        Write-Host ""
        Write-Host "Verify the relay is in the path:  python tools\relayctl.py path" -ForegroundColor DarkGray
        Write-Host "Log:     $logPath" -ForegroundColor DarkGray
        exit 0
    }
    Write-Warn2 "config write failed - paste the snippet below by hand"
}

Write-Host ""
Write-Host "Add this to $configPath" -ForegroundColor Cyan
Write-Host "----------------------------------------" -ForegroundColor DarkGray

if ($Mode -eq 'Gateway') {
    # If a cockpit-tools provider block already exists, its bearer token and
    # headers can be carried over so account rotation and session affinity
    # keep working unchanged.
    $existing = $null
    if (Test-Path $configPath) {
        $text = Get-Content -Raw -Path $configPath
        $m = [regex]::Match($text,
            '(?ms)^\[model_providers\.codex_local_access\](.*?)(?=^\[|\Z)')
        if ($m.Success) { $existing = $m.Groups[1].Value }
    }
    $snippet = @"
model_provider = "gateway-retry"

[model_providers.gateway-retry]
name = "Gateway (retry)"
base_url = "http://127.0.0.1:$Port/v1"
wire_api = "responses"
env_key = "$ApiKeyEnv"
stream_max_retries = 20
request_max_retries = 10
"@
    if ($existing) {
        $carry = @()
        foreach ($line in ($existing -split "`n")) {
            $t = $line.Trim()
            if ($t -match '^(experimental_bearer_token|http_headers)\s*=') { $carry += $t }
        }
        if ($carry.Count -gt 0) {
            # An explicit token overrides the env var, so drop env_key.
            $snippet = $snippet -replace "(?m)^env_key = .*\r?\n?", ""
            $snippet += "`n" + ($carry -join "`n")
            Write-Step "carried the token/headers over from [model_providers.codex_local_access]"
        }
    }
    Write-Host $snippet -ForegroundColor White
    Write-Host "----------------------------------------" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "The installer never stores your key. Put it in the environment" -ForegroundColor Yellow
    Write-Host "so Codex can read it, then restart the terminal and Codex:"         -ForegroundColor Yellow
    Write-Host "  [Environment]::SetEnvironmentVariable('$ApiKeyEnv','sk-...','User')" -ForegroundColor White
} else {
    $snippet = @"
model_provider = "openai-retry"

[model_providers.openai-retry]
name = "OpenAI (retry)"
base_url = "http://127.0.0.1:$Port"
wire_api = "responses"
requires_openai_auth = true
stream_max_retries = 20
request_max_retries = 10
"@
    Write-Host $snippet -ForegroundColor White
    Write-Host "----------------------------------------" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Keep a backup of config.toml before editing." -ForegroundColor Yellow
Write-Host "Then fully quit and restart Codex (tray icon -> Quit, not just close)." -ForegroundColor Yellow
Write-Host ""
Write-Host "Verify the relay is actually in the path:" -ForegroundColor Cyan
Write-Host "  python tools\relayctl.py path" -ForegroundColor White
Write-Host "  python tools\relayctl.py status" -ForegroundColor White
Write-Host ""
Write-Host "Log:     $logPath" -ForegroundColor DarkGray
Write-Host "Stop:    powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1" -ForegroundColor DarkGray
Write-Host ""
