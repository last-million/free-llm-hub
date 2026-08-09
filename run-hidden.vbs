' Calvoun Free LLM Hub - hidden launcher.
'
' Starts run.bat with NO console window (WScript.Shell Run, window style 0),
' so the hub never depends on an open terminal: closing any window can never
' kill it. Safe to run any number of times - run.bat refuses a second copy
' on a served port.
'
' Usage:
'   run-hidden.vbs              explicit user start: run.bat CLEARS the
'                               intentional-stop flag first, then launches
'                               (this is what the desktop shortcut calls)
'   run-hidden.vbs supervised   autostart / self-heal start: while the stop
'                               flag exists this is a no-op, so a dashboard
'                               stop stays stopped until the USER relaunches
'
' MEASURED 2026-08-09: a bare `call run.bat` here silently found nothing on a
' machine with NoDefaultCurrentDirectoryInExePath=1 set (a real Windows
' hardening setting some orgs/AV enable) -- that setting excludes the current
' directory from the search cmd.exe does for a bare executable name, so
' `call run.bat` failed with "not recognized" INSIDE the hidden cmd window
' nobody ever sees. wscript.exe itself still exited 0 regardless (sh.Run's
' fire-and-forget mode never propagates the inner command's real exit code),
' so autostart's own success reporting -- and Task Scheduler's LastTaskResult
' -- both looked completely healthy while the hub silently never started.
' Fix: call it by its FULL path (`here` is already computed) so this never
' depends on cmd.exe's current-directory search behavior at all.

Option Explicit

Dim sh, fso, here, supervised, batPath
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
batPath = here & "\run.bat"

supervised = False
If WScript.Arguments.Count > 0 Then
  supervised = (LCase(WScript.Arguments(0)) = "supervised")
End If

' 0 = hidden window, False = do not wait for exit
If supervised Then
  sh.Run "cmd /c set HUB_SUPERVISED=1 && call " & Chr(34) & batPath & Chr(34), 0, False
Else
  sh.Run "cmd /c call " & Chr(34) & batPath & Chr(34), 0, False
End If
