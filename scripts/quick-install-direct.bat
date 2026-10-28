@echo off
rem =========================================================================
rem  Codex Overload Fix - one-click install, DIRECT mode (your own account).
rem  For non-technical users: double-click this file. No key needed - Codex
rem  keeps using the ChatGPT login you already have.
rem =========================================================================
setlocal
set "SCRIPT_DIR=%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%install.ps1" -WriteConfig
echo.
echo ============================================================
echo  If you see "All done" above: fully quit Codex from the
echo  tray icon and start it again. That is the last step.
echo ============================================================
pause
