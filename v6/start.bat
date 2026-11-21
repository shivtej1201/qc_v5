@echo off
setlocal
REM QC V6 - start on Windows (double-click, or run .\start.bat in PowerShell / Command Prompt).
REM Needs: Python 3.11+ (python.org, tick "Add python.exe to PATH") and PostgreSQL 16 (postgresql.org/download/windows).
REM Node is NOT needed: the web app is already built.
cd /d "%~dp0"

REM ---- find Python
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY ( where py >nul 2>nul && set "PY=py -3" )
if not defined PY (
  echo Python was not found. Install Python 3.11 or newer from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during installation. Then run start.bat again.
  pause & exit /b 1
)

REM ---- database address (the database itself is created automatically)
if not "%QC6_DATABASE_URL%"=="" goto have_db
echo.
echo QC V6 keeps its data in PostgreSQL. Enter the password you chose for the "postgres" user
echo when you installed PostgreSQL. The "qc6" database is created for you.
set /p "QC6_DB_PASSWORD=PostgreSQL password: "
set "QC6_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/qc6"
:have_db

REM ---- first start: virtual environment, packages, Chromium
if exist .venv\Scripts\python.exe goto have_venv
echo Setting up QC V6 for the first time - this takes a few minutes...
%PY% -m venv .venv
if errorlevel 1 (echo Could not create the virtual environment. & pause & exit /b 1)
.venv\Scripts\python -m pip install -q --upgrade pip
.venv\Scripts\python -m pip install -q -r requirements.txt
if errorlevel 1 (echo Installing packages failed. Check your internet connection. & rmdir /s /q .venv & pause & exit /b 1)
:have_venv
if exist .venv\.chromium-ok goto have_chromium
.venv\Scripts\python -m playwright install chromium
if errorlevel 1 (echo WARNING: Chromium could not be downloaded. Page checks work; real-visitor checks and journeys need it.) else (type nul > .venv\.chromium-ok)
:have_chromium

if "%QC6_DATA_DIR%"=="" set "QC6_DATA_DIR=%CD%\var"
.venv\Scripts\python -m qc6.db.migrate
if errorlevel 1 (
  echo.
  echo Could not reach PostgreSQL. Check that PostgreSQL is running and the password is right,
  echo then run start.bat again.
  pause & exit /b 1
)

start "QC V6 worker" .venv\Scripts\python -m qc6.workers.runner
echo.
echo QC V6 is running: open http://127.0.0.1:8003 in your browser. Close this window to stop it.
start "" http://127.0.0.1:8003
.venv\Scripts\python -m uvicorn qc6.api.app:app --host 127.0.0.1 --port 8003
