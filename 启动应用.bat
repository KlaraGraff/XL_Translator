@echo off
setlocal
set "ROOT_DIR=%~dp0"
set "ROOT_DIR_SAFE=%ROOT_DIR%"
if "%ROOT_DIR_SAFE:~-1%"=="\" set "ROOT_DIR_SAFE=%ROOT_DIR_SAFE:~0,-1%"
set "RUNNER_BAT=%ROOT_DIR%scripts\start_windows.bat"
set "SILENT_LAUNCHER_PS1=%ROOT_DIR%scripts\launch_silent_windows.ps1"
set "BOOTSTRAP_MARKER=%ROOT_DIR%.venv\.bootstrap_success"
set "VENV_PYTHON=%ROOT_DIR%.venv\Scripts\python.exe"
set "VENV_PYTHONW=%ROOT_DIR%.venv\Scripts\pythonw.exe"

if not exist "%RUNNER_BAT%" (
  echo.
  echo [ERROR] Launcher script not found: "%RUNNER_BAT%"
  pause
  exit /b 1
)

if not exist "%BOOTSTRAP_MARKER%" goto visible
if not exist "%VENV_PYTHON%" goto visible
if not exist "%VENV_PYTHONW%" goto visible
if not exist "%SILENT_LAUNCHER_PS1%" goto visible

powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%SILENT_LAUNCHER_PS1%" -PythonwPath "%VENV_PYTHONW%" -LauncherScript "%ROOT_DIR_SAFE%\scripts\launcher.py" -WorkingDirectory "%ROOT_DIR_SAFE%"
set "LAUNCH_EXIT=%ERRORLEVEL%"
if not "%LAUNCH_EXIT%"=="0" (
  echo.
  echo [WARN] Silent startup failed. Falling back to visible startup.
  goto visible
)

exit /b 0

:visible
call "%RUNNER_BAT%"
exit /b %ERRORLEVEL%
