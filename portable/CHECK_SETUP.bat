@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0runtime\python\python.exe" goto missing
"%~dp0runtime\python\python.exe" -B -X utf8 "%~dp0portable_entry.py" --check --verify
set "check_result=%errorlevel%"
echo.
pause
exit /b %check_result%
:missing
echo Chua giai nen day du goi portable. Hay giai nen toan bo ZIP vao mot thu muc.
pause
exit /b 1
