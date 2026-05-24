@echo off
setlocal
set "ROOT_DIR=%~dp0"
set "RUNNER_BAT=%ROOT_DIR%scripts\start_windows.bat"

if not exist "%RUNNER_BAT%" (
  echo.
  echo [ERROR] Launcher script not found: "%RUNNER_BAT%"
  pause
  exit /b 1
)

call "%RUNNER_BAT%"
exit /b %ERRORLEVEL%
