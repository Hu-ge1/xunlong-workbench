Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
projectRoot = fso.GetParentFolderName(WScript.ScriptFullName)
launcher = fso.BuildPath(projectRoot, "launch_desktop.ps1")
If Not fso.FileExists(launcher) Then
    MsgBox "Xunlong Workbench launcher was not found:" & vbCrLf & launcher, vbCritical, "Xunlong Workbench"
    WScript.Quit 1
End If
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & launcher & """", 0, False
