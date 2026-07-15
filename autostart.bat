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
rem The 5-minute task is safe to fire while the hub is healthy because run.bat
rem refuses to start a second copy on a served port - so it is a no-op then, and
rem a restart only when actually needed.
rem
rem NOTE: `schtasks /SC ONLOGON` is NOT used - it requires elevation, while
rem `/SC MINUTE` and the Startup folder do not. That is the whole reason for the
rem two-part design.
rem
rem Safe to re-run. Usage:
rem   autostart.bat            install (or refresh)
rem   autostart.bat remove     uninstall
rem   autostart.bat status     show current state
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "TASK=CalvounFreeLLMHub"
set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "LAUNCHER=%STARTUP%\CalvounFreeLLMHub.vbs"

if /i "%~1"=="remove" goto :remove
if /i "%~1"=="status" goto :status

rem --- 1. logon launcher (silent, no admin) ---
> "%LAUNCHER%" echo ' Calvoun Free LLM Hub - starts the local gateway at logon.
>> "%LAUNCHER%" echo ' Delete this file (or run autostart.bat remove) to disable.
>> "%LAUNCHER%" echo Set sh = CreateObject("WScript.Shell")
>> "%LAUNCHER%" echo sh.CurrentDirectory = "%HERE%"
>> "%LAUNCHER%" echo ' 0 = hidden window, False = do not wait
>> "%LAUNCHER%" echo sh.Run "cmd /c run.bat", 0, False
if not exist "%LAUNCHER%" (
  echo [autostart] ERROR: could not write to the Startup folder:
  echo             %STARTUP%
  exit /b 1
)
echo [autostart] Logon launcher installed.

rem --- 2. self-heal every 5 minutes (no admin) ---
schtasks /Delete /TN "%TASK%" /F >nul 2>nul
schtasks /Create /TN "%TASK%" /SC MINUTE /MO 5 /F ^
  /TR "cmd /c cd /d \"%HERE%\" && run.bat" >nul 2>nul
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
echo   Remove everything:   autostart.bat remove
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
