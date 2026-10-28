@echo off
rem =========================================================================
rem  Codex Overload Fix - one-click install, GATEWAY mode (sub2api etc.)
rem  For non-technical users: double-click this file. No terminal needed
rem  afterwards; you will only be asked for the gateway API key.
rem
rem  IT / the person distributing this zip: set GATEWAY_URL below to the
rem  company gateway before sending it out. Leave the placeholder to have
rem  the user paste it instead.
rem =========================================================================
setlocal
set "GATEWAY_URL=EDIT-THIS"
set "SCRIPT_DIR=%~dp0"

if "%GATEWAY_URL%"=="EDIT-THIS" (
    echo Where is the gateway? Paste the address IT gave you
    echo ^(for example http://127.0.0.1:8888^) and press Enter:
    set /p GATEWAY_URL="> "
    if "%GATEWAY_URL%"=="" (
        echo No gateway address given - exiting without changing anything.
        pause
        exit /b 1
    )
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%install.ps1" -Mode Gateway -GatewayUrl "%GATEWAY_URL%" -WriteConfig
echo.
echo ============================================================
echo  If you see "All done" above: fully quit Codex from the
echo  tray icon and start it again. That is the last step.
echo ============================================================
pause
