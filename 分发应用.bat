@echo off
setlocal
set "ROOT_DIR=%~dp0"
set "PYTHON_EXE="
set "PYTHON_ARGS="

if exist "%ROOT_DIR%.venv\Scripts\python.exe" set "PYTHON_EXE=%ROOT_DIR%.venv\Scripts\python.exe"
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
  echo [ERROR] Python was not found.
  pause
  exit /b 1
)

echo ================================================
echo   Translator Windows Distribution Build
echo ================================================
echo.

"%PYTHON_EXE%" %PYTHON_ARGS% "%ROOT_DIR%scripts\build_distribution.py" --platform windows --zip --version-zip
if errorlevel 1 (
  echo.
  echo [ERROR] Distribution build failed. Check the output above.
  pause
  exit /b 1
)

echo.
echo [INFO] Opening the dist folder...
explorer "%ROOT_DIR%dist"

echo.
echo Done. Press any key to close this window...
pause >nul
