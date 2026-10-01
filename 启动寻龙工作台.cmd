@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Xunlong Workbench Launcher

rem Override the project location with XUNLONG_WORKBENCH_DIR.
set "APP_DIR=%XUNLONG_WORKBENCH_DIR%"
if not defined APP_DIR set "APP_DIR=%~dp0."

where powershell.exe >nul 2>&1
if errorlevel 1 (
    echo ERROR: Windows PowerShell was not found.
    pause
    exit /b 1
)

set "LAUNCHER=%APP_DIR%\launch_desktop.ps1"
if not exist "%LAUNCHER%" (
    echo ERROR: launch_desktop.ps1 was not found.
    echo Looked in: %APP_DIR%
    echo Run this launcher from the repository root, or set
    echo XUNLONG_WORKBENCH_DIR to the project folder.
    echo.
    pause
    exit /b 1
)

pushd "%APP_DIR%" >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%LAUNCHER%"
set "EXIT_CODE=%ERRORLEVEL%"
popd

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Startup failed with exit code %EXIT_CODE%. Recent log output:
    if exist "%APP_DIR%\data\launcher.error.log" (
        echo --- data\launcher.error.log ---
        powershell.exe -NoProfile -Command "Get-Content -LiteralPath '%APP_DIR%\data\launcher.error.log' -Tail 15"
    )
    if exist "%APP_DIR%\data\server.current.err.log" (
        echo --- data\server.current.err.log ---
        powershell.exe -NoProfile -Command "Get-Content -LiteralPath '%APP_DIR%\data\server.current.err.log' -Tail 15"
    )
    echo.
    pause
    exit /b %EXIT_CODE%
)

exit /b 0
