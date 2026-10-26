@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Local Live Studio
if not exist "%~dp0runtime\python\python.exe" goto missing
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
"%~dp0runtime\python\python.exe" -u -B -X utf8 "%~dp0portable_entry.py" %*
if errorlevel 1 goto failed
exit /b 0
:missing
echo Chua giai nen day du goi portable. Hay giai nen toan bo ZIP vao mot thu muc.
:failed
echo.
echo Chay DIAGNOSE_LIVE.bat de kiem tra ket noi va xem log khoi dong.
echo Xem file data\startup.log trong thu muc ung dung.
pause
exit /b 1
