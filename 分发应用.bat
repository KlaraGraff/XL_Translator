@echo off
setlocal
set "ROOT_DIR=%~dp0"
powershell -ExecutionPolicy Bypass -File "%ROOT_DIR%scripts\build_windows_package.ps1"
exit /b %ERRORLEVEL%
