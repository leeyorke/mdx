' ─────────────────────────────────────────────────────────────────────
'  MDX Dictionary API + GoldenDict watcher: hidden auto-start at logon
'
'  Called by the shortcut MDX-GoldenDict-API.lnk in the Startup folder.
'  Install/rebuild shortcut : pwsh script/install_autostart.ps1
'  Stop the server         : pwsh script/stop_server.ps1
'  Log file                : data\server.log
'
'  NOTE: keep this file ASCII-only. Windows Script Host reads .vbs with
'  the system ANSI codepage, so non-ASCII comments break compilation.
' ─────────────────────────────────────────────────────────────────────
Option Explicit

Dim sh, fso, bat
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

bat = "D:\myworkspace\mdx\script\run_server.bat"

' Exit silently if the project was moved or deleted
If Not fso.FileExists(bat) Then
    WScript.Quit
End If

' Window style 0 = hidden; False = do not wait
sh.Run "cmd /c " & bat, 0, False
