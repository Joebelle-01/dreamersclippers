@echo off
setlocal
title DreamersClippers Studio (StreamClippers)
cd /d "%~dp0"

echo [DreamersClippers] Launching Studio...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-StreamClippers.ps1" %*

if errorlevel 1 (
    echo.
    echo [DreamersClippers] Process exited with an error.
    pause
)
