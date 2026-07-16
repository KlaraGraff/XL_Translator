@echo off
setlocal
set "ROOT_DIR=%~dp0.."
if not defined TRANSLATOR_APP_DATA_DIR set "TRANSLATOR_APP_DATA_DIR=%ROOT_DIR%\.runtime\tauri-dev-app-data"
cd /d "%ROOT_DIR%\src-tauri"
call "%ROOT_DIR%\ui\node_modules\.bin\tauri.cmd" dev
exit /b %ERRORLEVEL%
