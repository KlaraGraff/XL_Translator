@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "PROJECT_ROOT=%%~fI"
set "VENV_PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
set "BOOTSTRAP_MARKER=%PROJECT_ROOT%\.venv\.bootstrap_success"

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

"%PYTHON_EXE%" %PYTHON_ARGS% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] Python 3.10+ is required.
  pause
  exit /b 1
)

if not exist "%VENV_PYTHON%" (
  echo [INFO] Creating project virtual environment for the native app...
  "%PYTHON_EXE%" %PYTHON_ARGS% -m venv "%PROJECT_ROOT%\.venv"
  if errorlevel 1 (
    echo.
    echo [ERROR] Failed to create project virtual environment.
    pause
    exit /b 1
  )
)

if not exist "%BOOTSTRAP_MARKER%" goto install_deps
"%VENV_PYTHON%" -c "import PySide6, anthropic, dashscope, docx, httpx, loguru, openai, openpyxl, psutil, pydantic, tenacity, xlrd, zhipuai" >nul 2>nul
if errorlevel 1 goto install_deps
goto deps_ready

:install_deps
  echo [INFO] Installing native app dependencies...
  "%VENV_PYTHON%" -m pip install -r "%PROJECT_ROOT%\requirements.txt"
  if errorlevel 1 (
    echo.
    echo [ERROR] Failed to install native app dependencies.
    pause
    exit /b 1
  )

:deps_ready

if not exist "%PROJECT_ROOT%\.venv" mkdir "%PROJECT_ROOT%\.venv"
type nul > "%BOOTSTRAP_MARKER%"

"%VENV_PYTHON%" "%PROJECT_ROOT%\scripts\launch_native.py"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo [ERROR] Launch failed with exit code: %EXIT_CODE%
  pause
)

exit /b %EXIT_CODE%
