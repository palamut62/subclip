Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
ffmpegBin = "C:\Users\umuti\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin"
venvScripts = scriptDir & "\venv\Scripts"

pathVal = sh.Environment("PROCESS").Item("PATH")
If fso.FolderExists(venvScripts) Then
    pathVal = venvScripts & ";" & pathVal
End If
If fso.FolderExists(ffmpegBin) Then
    pathVal = ffmpegBin & ";" & pathVal
End If
sh.Environment("PROCESS").Item("PATH") = pathVal
sh.CurrentDirectory = scriptDir

' Bu proje venv'i ile calisan eski python process'lerini oldur
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("SELECT ProcessId, ExecutablePath FROM Win32_Process WHERE Name='pythonw.exe' OR Name='python.exe'")
needle = LCase(scriptDir & "\venv\")
For Each p In procs
    If Not IsNull(p.ExecutablePath) Then
        If InStr(LCase(p.ExecutablePath), needle) > 0 Then
            On Error Resume Next
            p.Terminate()
            On Error Goto 0
        End If
    End If
Next

WScript.Sleep 800

sh.Run """" & venvScripts & "\pythonw.exe"" app.py", 0, False

ready = False
For i = 1 To 20
    WScript.Sleep 500
    Set http = CreateObject("MSXML2.XMLHTTP")
    On Error Resume Next
    http.Open "GET", "http://localhost:8899/", False
    http.Send
    If Err.Number = 0 And http.Status = 200 Then
        ready = True
        Err.Clear
        Exit For
    End If
    Err.Clear
    On Error Goto 0
Next

sh.Run "http://localhost:8899", 1, False