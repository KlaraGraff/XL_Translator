@echo off
setlocal
set "ROOT_DIR=%~dp0"
call "%ROOT_DIR%scripts\start_tauri_windows.bat"
exit /b %ERRORLEVEL%
