@echo off
rem Calvoun Free LLM Hub - THE launcher (Windows). Double-click it.
rem
rem This is deliberately the ONLY .bat in the project root. There used to be a
rem second one (autostart.bat) sitting next to it, and two clickable files with
rem no way to tell which one starts the thing is a coin flip for anyone who did
rem not write them. Everything else is a subcommand of this file:
rem
rem   run.bat                    start the hub  <- what a double-click does
rem   run.bat restart            stop whatever is on the port, then start fresh
rem   run.bat autostart          also start it at logon, and self-heal
rem   run.bat autostart remove   undo that
rem   run.bat autostart status   show what is installed
rem
rem Idempotent: creates a venv on first run, reuses it afterwards. Installs
rem Python itself if the machine has none.
setlocal enabledelayedexpansion
cd /d "%~dp0"

if /i "%~1"=="autostart" (
  call "%~dp0scripts\autostart.bat" %2 %3
  exit /b %errorlevel%
)
if /i "%~1"=="help" goto :usage
if /i "%~1"=="/?" goto :usage
if /i "%~1"=="-h" goto :usage
if /i "%~1"=="--help" goto :usage
set "HUB_RESTART="
if /i "%~1"=="restart" set "HUB_RESTART=1"

if "%PORT%"=="" set "PORT=8787"

rem --- restart: stop whatever is serving PORT, then fall through to a normal start
rem Restarting by hand - kill whichever pid you happened to find, start a new one
rem - is exactly how you end up with several hubs alive at once. FOUND LIVE
rem 2026-08-06: four orphaned `python app.py` processes, only one actually owning
rem the port, so every check "passed" against whichever happened to answer. The
rem double-bind guard below only refuses a SECOND copy; it cannot clean up after a
rem manual kill that missed one. This asks the OS who owns the port rather than
rem matching on a process name, so it can never kill an unrelated python.
if defined HUB_RESTART (
  for /f "tokens=5" %%P in ('netstat -ano -p TCP 2^>nul ^| findstr /R /C:"LISTENING" ^| findstr /C:":%PORT% "') do (
    echo [free-llm-hub] Stopping the hub on port %PORT% ^(pid %%P^)...
    taskkill /F /PID %%P >nul 2>nul
  )
  rem Windows can hold the socket briefly after the process is gone, so the
  rem start below would otherwise race the release. ~2s, no extra dependency.
  ping -n 3 127.0.0.1 >nul 2>nul
)

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
    rem A live hub on the port IS proof setup already succeeded once -- this
    rem branch skips the python/venv checks below entirely, so without this
    rem call here the auto-persist step (see :maybe_autopersist) would never
    rem run for the single most common case: re-running/double-clicking
    rem run.bat while the hub is already up.
    call :maybe_autopersist
    exit /b 0
  )
)

rem --- find python, and INSTALL it if this machine has none -------------------
rem "Download it, run one file" only holds if the one file can also handle a
rem machine with no Python on it. Telling someone to go install Python first is
rem exactly the wall this script exists to remove.
call :find_python
if not defined PY (
  echo [free-llm-hub] Python was not found on this machine - installing it.
  echo                This is a one-time setup and needs no administrator rights.
  echo.
  call :install_python
  call :find_python
)
if not defined PY (
  echo.
  echo ERROR: could not install Python automatically.
  echo        Install it once from https://www.python.org/downloads/
  echo        ^(tick "Add python.exe to PATH"^), then run this file again.
  pause
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
  rem MEASURED: `-q` plus a slow/blocked network (corporate proxy, captive
  rem wifi, VPN) looks IDENTICAL to a genuine hang -- zero output, no way to
  rem tell "still working" from "stuck". Dropped -q so pip's own progress
  rem prints; --timeout bounds each connection attempt instead of silently
  rem trusting pip's default.
  echo [free-llm-hub] Installing dependencies ^(flask, requests^)...
  echo                This can take a minute on a slow network - progress prints below.
  echo                If nothing moves for several minutes, your network is likely
  echo                blocking it - check a proxy/firewall or try a different network.
  pip install --timeout 20 -r requirements.txt
  if not errorlevel 1 python -c "import hashlib,os;h=hashlib.sha256(open('requirements.txt','rb').read()).hexdigest();open(os.path.join('.venv','.deps-stamp'),'w').write(h)" >nul 2>nul
)

call :maybe_autopersist

if "%HUB_DETACHED%"=="1" (
  echo.
  echo ==========================================================
  echo   Calvoun Free LLM Hub is starting
  echo   Dashboard:  http://127.0.0.1:%PORT%
  echo ==========================================================
  echo.
  python app.py
  exit /b %errorlevel%
)

rem --- relaunch hidden/detached, then let THIS visible window exit -----------
rem MEASURED 2026-08-09: closing the visible window running `python app.py`
rem in the foreground killed the hub instantly. Autostart's self-heal task
rem (see :maybe_autopersist) then revived it -- but only within its own
rem 5-minute check interval, so closing the window still meant real downtime.
rem Reuse run-hidden.vbs -- the SAME hidden/detached mechanism the logon
rem launcher and self-heal already use, already tested -- in its NON-
rem supervised mode (explicit start: clears the intentional-stop flag,
rem what someone running this by hand wants, unlike the supervised mode
rem schtasks itself calls). HUB_DETACHED=1 travels with it through normal
rem Windows child-process environment inheritance (this run.bat -> wscript
rem -> cmd -> the relaunched run.bat), so that copy takes the branch above
rem instead of trying to detach a second time.
set "HUB_DETACHED=1"
echo [free-llm-hub] Starting in the background - this window is safe to close now.
echo                Dashboard: http://127.0.0.1:%PORT%
start "" wscript.exe "%~dp0run-hidden.vbs"
rem Give it a moment to actually bind the port before this window vanishes,
rem so someone watching does not see the dashboard fail to load for a beat
rem and worry it did not actually start.
ping -n 3 127.0.0.1 >nul 2>nul
exit /b 0


rem ===========================================================================
rem  Helpers
rem ===========================================================================

:maybe_autopersist
rem First successful setup ever: auto-persist, so a closed window or a reboot
rem never silently drops the hub again for someone who has no reason to know
rem "run.bat autostart" exists. Once only -- a marker in the state dir
rem (survives a venv rebuild, matches the STOP_MARKER convention above), and
rem fully best-effort: never blocks or slows the actual start, never touches
rem ~/.free-llm-hub/'s config or history, and stays silent instead of adding
rem noise to every single plain start. Called from TWO places: right before
rem the final `python app.py` launch, and from the "port already served"
rem branch (a live hub IS proof setup already succeeded, and that branch
rem skips the python/venv checks entirely -- without this second call site,
rem the single most common case, re-running run.bat while the hub is already
rem up, would never trigger it).
if defined FREE_LLM_HUB_CONFIG (
  for %%I in ("%FREE_LLM_HUB_CONFIG%") do set "AUTOSTART_MARKER=%%~dpIautostart-auto-installed"
) else (
  set "AUTOSTART_MARKER=%USERPROFILE%\.free-llm-hub\autostart-auto-installed"
)
if not exist "%AUTOSTART_MARKER%" (
  call "%~dp0scripts\autostart.bat" >nul 2>nul
  if not errorlevel 1 (
    for %%I in ("%AUTOSTART_MARKER%") do if not exist "%%~dpI" mkdir "%%~dpI" >nul 2>nul
    >"%AUTOSTART_MARKER%" echo installed automatically on first successful start -- delete this file to let run.bat try again, or run "run.bat autostart remove" to fully uninstall it
  )
)
exit /b 0

:usage
echo Calvoun Free LLM Hub
echo.
echo   run.bat                    start the hub ^(this is what a double-click does^)
echo   run.bat restart            stop whatever is on the port, then start fresh
echo   run.bat autostart          also start it at logon, and self-heal
echo   run.bat autostart remove   undo that
echo   run.bat autostart status   show what is installed
echo.
exit /b 0

:find_python
rem Sets PY to a working launcher, or leaves it empty. Checks PATH first, then
rem the per-user install location - a fresh install lands there but does not
rem reach THIS already-running shell's PATH, so looking only at PATH would make
rem a successful install look like a failed one.
set "PY="
where python >nul 2>nul && set "PY=python"
if defined PY (
  rem The Microsoft Store stub is named python.exe, answers `where`, and does
  rem nothing but open the Store. Prove the interpreter actually runs.
  python -c "import sys" >nul 2>nul || set "PY="
)
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
  if defined PY py -3 -c "import sys" >nul 2>nul || set "PY="
)
if not defined PY (
  for /f "delims=" %%P in ('dir /b /o-n "%LOCALAPPDATA%\Programs\Python\Python3*" 2^>nul') do (
    if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\%%P\python.exe" (
      set "PY=%LOCALAPPDATA%\Programs\Python\%%P\python.exe"
    )
  )
)
exit /b 0

:install_python
rem Two ways, cheapest first. winget ships with Windows 10 21H2+ and Windows 11
rem and handles the download, the hash check and the PATH entry itself.
where winget >nul 2>nul
if not errorlevel 1 (
  echo [free-llm-hub] Installing Python via winget...
  rem MEASURED: winget's first-ever run on a machine can pop an invisible
  rem source-agreement/UAC prompt even with --silent --accept-*-agreements --
  rem cmd.exe has no native way to bound how long a child process runs, so a
  rem stuck prompt used to hang this whole script forever with zero output.
  rem Start-Process + WaitForExit(ms) gives it a real 2-minute cap; -NoNewWindow
  rem keeps winget's own progress visible in this console instead of hiding it.
  rem Past the cap we kill it and fall through to the direct installer below.
  rem One line, deliberately -- see the autostart.bat comment on the same bug:
  rem this file is LF-only on disk, and cmd.exe's `^` continuation formally
  rem needs CRLF, so a caret split here is not reliable.
  powershell -NoProfile -Command "$p = $null; try { $p = Start-Process winget -ArgumentList 'install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements' -PassThru -NoNewWindow } catch {}; if (-not $p -or -not $p.WaitForExit(120000)) { if ($p) { try { $p.Kill() } catch {} }; Write-Host '[free-llm-hub] winget did not finish - falling back to the direct installer.'; exit 1 } else { exit $p.ExitCode }"
  call :find_python
  if defined PY exit /b 0
  echo [free-llm-hub] winget did not produce a working Python - trying the direct installer instead.
)

rem Fallback: the official installer, per-user and unattended. InstallAllUsers=0
rem is what keeps this from needing administrator rights.
set "PYVER=3.12.8"
set "PYARCH=amd64"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "PYARCH=arm64"
set "PYEXE=%TEMP%\python-%PYVER%-%PYARCH%.exe"
echo [free-llm-hub] Downloading Python %PYVER% ^(%PYARCH%^)...
rem MEASURED: Invoke-WebRequest has no timeout by default, so a blocked or
rem very slow network (proxy, firewall, captive wifi) hung here indefinitely
rem with nothing on screen. -TimeoutSec bounds it; the catch turns a real
rem failure into a visible message instead of a silently-missing .exe.
rem One line, deliberately -- same LF-only/caret-continuation bug as above.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/%PYVER%/python-%PYVER%-%PYARCH%.exe' -OutFile '%PYEXE%' -UseBasicParsing -TimeoutSec 60 } catch { Write-Host ('[free-llm-hub] Download failed: ' + $_.Exception.Message); exit 1 }"
if not exist "%PYEXE%" (
  echo [free-llm-hub] Could not download Python - check your internet connection/firewall.
  exit /b 1
)
echo [free-llm-hub] Installing Python %PYVER%...
"%PYEXE%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=1 >nul 2>nul
del /f /q "%PYEXE%" >nul 2>nul
exit /b 0
