@echo off
rem Calvoun Free LLM Hub - install/remove autostart (Windows)
rem
rem WHY: the hub is a foreground process in a console window. Close the window,
rem log out, or reboot and it is gone - and then every CLI pointed at it
rem silently loses the free fleet (or falls back to a paid path).
rem
rem TWO mechanisms, deliberately, and NEITHER needs admin rights (verified):
rem   1. a silent launcher in the per-user Startup folder -> starts at logon,
rem      immediately, with no console window flashing up.
rem   2. a 5-minute Scheduled Task -> SELF-HEAL: if the hub ever dies mid-session
rem      it comes back within 5 minutes instead of silently staying dead.
rem Both go through run-hidden.vbs (supervised), so nothing ever shows a
rem console window - and closing a terminal can never kill the hub.
rem The 5-minute task is safe to fire while the hub is healthy because run.bat
rem refuses to start a second copy on a served port - so it is a no-op then, and
rem a restart only when actually needed. It ALSO stays a no-op after a
rem dashboard stop: supervised runs refuse while the intentional-stop flag
rem exists, so the self-heal never resurrects a hub the user stopped. Only an
rem explicit user action (desktop shortcut, plain run.bat, python app.py)
rem clears that flag.
rem
rem NOTE: `schtasks /SC ONLOGON` is NOT used - it requires elevation, while
rem `/SC MINUTE` and the Startup folder do not. That is the whole reason for the
rem two-part design.
rem
rem Safe to re-run. Usage:
rem   run.bat autostart            install (or refresh)
rem   run.bat autostart remove     uninstall
rem   run.bat autostart status     show current state
setlocal EnableDelayedExpansion
rem This lives in scripts\ so the project root holds exactly ONE clickable .bat
rem (run.bat). Everything it writes -- the logon launcher, the scheduled task --
rem points at the ROOT, so HERE has to climb out of this folder. Normally you
rem reach it as `run.bat autostart`; running it directly still works.
cd /d "%~dp0.."

set "TASK=CalvounFreeLLMHub"
set "HERE=%CD%"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LAUNCHER=%STARTUP%\CalvounFreeLLMHub.vbs"

if /i "%~1"=="remove" goto :remove
if /i "%~1"=="status" goto :status

rem --- 1. logon launcher (silent, no admin) ---
> "%LAUNCHER%" echo ' Calvoun Free LLM Hub - starts the local gateway at logon.
>> "%LAUNCHER%" echo ' Delete this file (or run run.bat autostart remove) to disable.
>> "%LAUNCHER%" echo Set sh = CreateObject("WScript.Shell")
>> "%LAUNCHER%" echo ' run-hidden.vbs supervised: hidden window, no-op while the
>> "%LAUNCHER%" echo ' intentional-stop flag exists (a dashboard stop stays stopped).
>> "%LAUNCHER%" echo sh.Run "wscript.exe ""%HERE%\run-hidden.vbs"" supervised", 0, False
if not exist "%LAUNCHER%" (
  echo [autostart] ERROR: could not write to the Startup folder:
  echo             %STARTUP%
  exit /b 1
)
echo [autostart] Logon launcher installed.

rem --- 2. self-heal every 5 minutes (no admin, hidden) ---
schtasks /Delete /TN "%TASK%" /F >nul 2>nul
schtasks /Create /TN "%TASK%" /SC MINUTE /MO 5 /F ^
  /TR "wscript.exe \"%HERE%\run-hidden.vbs\" supervised" >nul 2>nul
if errorlevel 1 (
  echo [autostart] NOTE: the 5-minute self-heal task could not be created.
  echo             The hub will still start at logon - it just will not come
  echo             back automatically if it crashes mid-session.
) else (
  echo [autostart] Self-heal task installed ^(checks every 5 min^).
)

echo.
echo   Installed. The hub starts at logon and recovers within 5 min if it dies.
echo   Dashboard: http://127.0.0.1:8787
echo.
echo   Start it right now:  schtasks /Run /TN "%TASK%"
echo   Remove everything:   run.bat autostart remove
goto :eof

:remove
if exist "%LAUNCHER%" (
  del /f /q "%LAUNCHER%"
  echo [autostart] Logon launcher removed.
) else (
  echo [autostart] No logon launcher was installed.
)
schtasks /Delete /TN "%TASK%" /F >nul 2>nul
if errorlevel 1 (
  echo [autostart] No self-heal task was registered.
) else (
  echo [autostart] Self-heal task removed.
)
echo [autostart] Done. A hub running right now is left alone.
goto :eof

:status
if exist "%LAUNCHER%" (echo [autostart] Logon launcher: INSTALLED) else (echo [autostart] Logon launcher: not installed)
schtasks /Query /TN "%TASK%" /FO LIST 2>nul | findstr /C:"TaskName" /C:"Status" /C:"Next Run Time"
if errorlevel 1 echo [autostart] Self-heal task: not installed
goto :eof
