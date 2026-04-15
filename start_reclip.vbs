Set sh = CreateObject("WScript.Shell")
ffmpegBin = "C:\Users\umuti\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin"
venvScripts = "C:\Users\umuti\projects\reclip\venv\Scripts"
sh.Environment("PROCESS").Item("PATH") = venvScripts & ";" & ffmpegBin & ";" & sh.Environment("PROCESS").Item("PATH")
sh.CurrentDirectory = "C:\Users\umuti\projects\reclip"

Set http = CreateObject("MSXML2.XMLHTTP")
running = False
On Error Resume Next
http.Open "GET", "http://localhost:8899/", False
http.Send
If Err.Number = 0 And http.Status = 200 Then running = True
On Error Goto 0

If Not running Then
    sh.Run """" & venvScripts & "\pythonw.exe"" app.py", 0, False
    WScript.Sleep 2000
End If

sh.Run "http://localhost:8899", 1, False
