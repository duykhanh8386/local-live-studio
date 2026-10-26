@echo off
setlocal
cd /d "%~dp0"
python -c "import fastapi, uvicorn, playwright.sync_api" >nul 2>nul
if errorlevel 1 (
  python -m pip install -r requirements.txt
  if errorlevel 1 goto failed
)
python app.py %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo Khong khoi dong duoc. Hay cai Python 3.12 va xem README.md.
pause
exit /b 1
