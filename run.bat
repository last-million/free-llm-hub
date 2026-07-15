@echo off
rem Calvoun Free LLM Hub - one-command launcher (Windows)
rem Idempotent: creates a venv on first run, reuses it afterwards.
setlocal
cd /d "%~dp0"

if "%PORT%"=="" set "PORT=8787"

rem --- refuse to double-bind -------------------------------------------------
rem Werkzeug sets SO_REUSEADDR, so Windows lets a SECOND process bind a port
rem that is already served. You then get two hubs alive at once and requests
rem land on whichever won - typically the OLD one, so code changes look like
rem they "didn't take effect" and any check you run passes against a stale
rem process. Cheaper to refuse than to debug. Set HUB_FORCE=1 to override.
if not defined HUB_FORCE (
  netstat -ano -p TCP | findstr /R /C:"LISTENING" | findstr /C:":%PORT% " >nul 2>nul
  if not errorlevel 1 (
    echo [free-llm-hub] Port %PORT% is already being served - not starting a second copy.
    echo                Dashboard: http://127.0.0.1:%PORT%
    echo                Restart it instead, or set HUB_FORCE=1 to override.
    exit /b 0
  )
)

rem --- find python ---
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo ERROR: Python 3.9+ not found. Install it from https://www.python.org/downloads/
  exit /b 1
)

rem --- venv (create once, reuse forever) ---
if not exist ".venv\Scripts\python.exe" (
  echo [free-llm-hub] Creating virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo ERROR: failed to create the virtual environment.
    exit /b 1
  )
)

call ".venv\Scripts\activate.bat"

echo [free-llm-hub] Installing dependencies (flask, requests)...
pip install -q -r requirements.txt

echo.
echo ==========================================================
echo   Calvoun Free LLM Hub is starting
echo   Dashboard:  http://127.0.0.1:%PORT%
echo ==========================================================
echo.
python app.py
