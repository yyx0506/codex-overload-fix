@echo off
rem =========================================================================
rem  Codex Overload Fix - one-click full rollback, for non-technical users.
rem  Double-click this file to undo everything the installer did:
rem    1. stops the relay, kills the watchdog, removes the HKCU Run entry
rem    2. restores config.toml from the pre-install backup
rem  Afterwards you can simply delete this folder.
rem =========================================================================
setlocal
set "SCRIPT_DIR=%~dp0"
set "CFG=%SCRIPT_DIR%..\tools\configure_codex.py"

echo == 1/2 stopping the relay and removing autostart ==
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%uninstall.ps1" -Purge

echo.
echo == 2/2 restoring config.toml from the pre-install backup ==
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%CFG%" --restore
) else (
    python "%CFG%" --restore
)

echo.
echo ============================================================
echo  Rollback complete. You can now delete this folder -
echo  nothing else is left behind.
echo ============================================================
pause
