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

Option Explicit

Dim sh, fso, here, supervised
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here

supervised = False
If WScript.Arguments.Count > 0 Then
  supervised = (LCase(WScript.Arguments(0)) = "supervised")
End If

' 0 = hidden window, False = do not wait for exit
If supervised Then
  sh.Run "cmd /c set HUB_SUPERVISED=1 && call run.bat", 0, False
Else
  sh.Run "cmd /c call run.bat", 0, False
End If
