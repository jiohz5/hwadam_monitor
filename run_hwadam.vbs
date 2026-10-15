Option Explicit

Dim shell, files, base, entry, python, prefix, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
base = files.GetParentFolderName(WScript.ScriptFullName)
entry = files.BuildPath(base, "main.py")

If Not files.FileExists(entry) Then
    MsgBox "main.py was not found. Keep this launcher in the Hwadam program folder.", vbExclamation, "Hwadam Monitor"
    WScript.Quit 1
End If

' Match the existing batch launcher's Python preference, without cmd.exe.
python = shell.ExpandEnvironmentStrings("%USERPROFILE%\anaconda3\pythonw.exe")
prefix = ""
If Not files.FileExists(python) Then python = FindOnPath("pythonw.exe")
If python = "" Then
    python = FindOnPath("pyw.exe")
    prefix = " -3"
End If
If python = "" Then
    MsgBox "Python 3.10 or later (pythonw.exe / pyw.exe) was not found." & vbCrLf & _
           "Install Python, or use run_hwadam_console.bat to check the setup.", vbExclamation, "Hwadam Monitor"
    WScript.Quit 1
End If

shell.CurrentDirectory = base
command = Quote(python) & prefix & " -X utf8 " & Quote(entry)
On Error Resume Next
shell.Run command, 1, False
If Err.Number <> 0 Then
    MsgBox "Could not start Hwadam Monitor." & vbCrLf & Err.Description & vbCrLf & _
           "Use run_hwadam_console.bat for diagnostics.", vbExclamation, "Hwadam Monitor"
    WScript.Quit 1
End If
On Error GoTo 0

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function

Function FindOnPath(name)
    Dim folder, candidate
    FindOnPath = ""
    For Each folder In Split(shell.ExpandEnvironmentStrings("%PATH%"), ";")
        folder = Replace(Trim(folder), Chr(34), "")
        If folder <> "" Then
            candidate = files.BuildPath(folder, name)
            If files.FileExists(candidate) Then
                FindOnPath = candidate
                Exit Function
            End If
        End If
    Next
End Function
