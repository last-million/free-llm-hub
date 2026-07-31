@echo off
rem Calvoun Free LLM Hub - one-command launcher (Windows)
rem Idempotent: creates a venv on first run, reuses it afterwards.
setlocal
cd /d "%~dp0"

if "%PORT%"=="" set "PORT=8787"

if defined FREE_LLM_HUB_CONFIG (
  for %%I in ("%FREE_LLM_HUB_CONFIG%") do set "STOP_MARKER=%%~dpIintentional-stop"
) else (
  set "STOP_MARKER=%USERPROFILE%\.free-llm-hub\intentional-stop"
)
if "%HUB_SUPERVISED%"=="1" if exist "%STOP_MARKER%" (
  echo [free-llm-hub] Intentionally stopped from the dashboard - supervisor restart skipped.
  echo                This is sticky: the self-heal task and logon launcher stay no-ops.
  echo                To start it again: run run.bat yourself, use the desktop shortcut,
  echo                or run "python app.py" - any of these clears the stop flag.
  exit /b 0
)
if not "%HUB_SUPERVISED%"=="1" if exist "%STOP_MARKER%" del /f /q "%STOP_MARKER%" >nul 2>nul

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

rem --- dependencies: install ONLY when they are actually missing or changed ---
rem This ran `pip install` on EVERY start, which is why a plain restart took
rem 60-200s of network round-trips before the hub bound its port -- painful, and
rem it makes the hub look hung. The stamp holds a hash of requirements.txt, so a
rem pinned-version bump still triggers a real install; nothing else does.
set "DEPS_OK="
python -c "import hashlib,os,sys;h=hashlib.sha256(open('requirements.txt','rb').read()).hexdigest();p=os.path.join('.venv','.deps-stamp');ok=os.path.exists(p) and open(p).read().strip()==h;__import__('flask');__import__('requests');sys.exit(0 if ok else 1)" >nul 2>nul
if not errorlevel 1 set "DEPS_OK=1"
if defined DEPS_OK (
  echo [free-llm-hub] Dependencies already installed - skipping pip.
) else (
  echo [free-llm-hub] Installing dependencies ^(flask, requests^)...
  pip install -q -r requirements.txt
  if not errorlevel 1 python -c "import hashlib,os;h=hashlib.sha256(open('requirements.txt','rb').read()).hexdigest();open(os.path.join('.venv','.deps-stamp'),'w').write(h)" >nul 2>nul
)

echo.
echo ==========================================================
echo   Calvoun Free LLM Hub is starting
echo   Dashboard:  http://127.0.0.1:%PORT%
echo ==========================================================
echo.
python app.py
