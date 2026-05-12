Set sh = CreateObject("WScript.Shell")
ffmpegBin = "C:\Users\umuti\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin"
venvScripts = "C:\Users\umuti\projects\reclip\venv\Scripts"
sh.Environment("PROCESS").Item("PATH") = venvScripts & ";" & ffmpegBin & ";" & sh.Environment("PROCESS").Item("PATH")
sh.CurrentDirectory = "C:\Users\umuti\projects\reclip"

' Eski reclip process'lerini oldur (kod degisiklikleri yansisin)
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("SELECT ProcessId, ExecutablePath FROM Win32_Process WHERE Name='pythonw.exe' OR Name='python.exe'")
For Each p In procs
    If Not IsNull(p.ExecutablePath) Then
        If InStr(LCase(p.ExecutablePath), "\reclip\venv\") > 0 Then
            On Error Resume Next
            p.Terminate()
            On Error Goto 0
        End If
    End If
Next

' Port serbest kalmasini bekle
WScript.Sleep 800

' Yeni server'i baslat
sh.Run """" & venvScripts & "\pythonw.exe"" app.py", 0, False

' Server ayaga kalkana kadar bekle (max 10 sn)
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
