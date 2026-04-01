@echo off
setlocal

cd /d "%~dp0"

set "HOST=127.0.0.1"
set "PORT=8000"
set "APP_URL=http://%HOST%:%PORT%/"

echo.
echo === MonsieurAPP local launcher ===
echo Workspace: %CD%
echo URL: %APP_URL%
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo Python non trovato nel PATH.
  pause
  exit /b 1
)

echo Verifica dipendenze Python...
python -c "import fastapi, uvicorn, playwright, bs4, httpx, jinja2, multipart, recipe_scrapers, lxml" >nul 2>nul
if errorlevel 1 (
  echo Installazione dipendenze da requirements.txt...
  python -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Installazione dipendenze fallita.
    pause
    exit /b 1
  )
)

echo Verifica browser Playwright...
python -c "from pathlib import Path; import os; p=Path(os.getenv('USERPROFILE',''))/'.cache'/'ms-playwright'; raise SystemExit(0 if p.exists() else 1)" >nul 2>nul
if errorlevel 1 (
  echo Installazione browser Playwright...
  python -m playwright install chromium
  if errorlevel 1 (
    echo Installazione browser Playwright fallita.
    pause
    exit /b 1
  )
)

echo Controllo processi attivi sulla porta %PORT%...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do (
  echo Arresto processo PID %%P...
  taskkill /PID %%P /F >nul 2>nul
)

set "RELOAD=1"
set "PORT=%PORT%"
set "HEADLESS=1"

echo Avvio applicazione...
start "MonsieurAPP" cmd /k "cd /d ""%CD%"" && python main.py"

timeout /t 3 /nobreak >nul
start "" "%APP_URL%"

echo Applicazione avviata. Se era gia' in esecuzione, e' stata riavviata.
exit /b 0
