@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PROJECT_ROOT=%%~fI"

set "PYTHON_EXE="
set "PYTHON_ARGS="
if defined PRODUCT_TRANSLATE_BOOTSTRAP_PYTHON (
  if exist "%PRODUCT_TRANSLATE_BOOTSTRAP_PYTHON%" set "PYTHON_EXE=%PRODUCT_TRANSLATE_BOOTSTRAP_PYTHON%"
)

if not defined PYTHON_EXE (
  if exist "%PROJECT_ROOT%\runtime\python\python.exe" set "PYTHON_EXE=%PROJECT_ROOT%\runtime\python\python.exe"
)

if not defined PYTHON_EXE (
  where py >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_EXE=py"
    set "PYTHON_ARGS=-3"
  )
)

if not defined PYTHON_EXE (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON_EXE=python"
)

if not defined PYTHON_EXE (
  echo.
  echo [ERROR] Python 3.10+ was not found on this Windows machine.
  echo [ERROR] Install Python 3.10+ or set PRODUCT_TRANSLATE_BOOTSTRAP_PYTHON.
  pause
  exit /b 1
)

"%PYTHON_EXE%" %PYTHON_ARGS% "%PROJECT_ROOT%\scripts\launcher.py"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] Launch failed with exit code: %EXIT_CODE%
  pause
)

exit /b %EXIT_CODE%
