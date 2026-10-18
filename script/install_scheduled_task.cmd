@echo off
rem MDX auto-start task installer.
rem Runs as administrator (auto-relaunches itself elevated if not).
rem Task: MDX-GoldenDict-API, created from mdx-task.xml.

fsutil dirty query %systemdrive% >nul 2>&1
if errorlevel 1 goto elevate

echo [1/2] Removing old task if present...
schtasks /Delete /TN "MDX-GoldenDict-API" /F >nul 2>&1

echo [2/2] Creating task from mdx-task.xml...
schtasks /Create /XML "%~dp0mdx-task.xml" /TN "MDX-GoldenDict-API" /F
if errorlevel 1 goto fail

echo.
echo [OK] Task "MDX-GoldenDict-API" created. It runs at your logon:
echo       logon -^> wscript D:\myworkspace\mdx\script\autostart.vbs -^> hidden server on port 4080
echo       log: D:\myworkspace\mdx\data\server.log
echo       stop: pwsh D:\myworkspace\mdx\script\stop_server.ps1
echo       optional: delete MDX-GoldenDict-API.lnk from the Startup folder (shell:startup)
goto end

:elevate
echo [!] Not running as administrator. Relaunching elevated...
powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
exit /b 1

:fail
echo [!] Task creation failed - please report the error above.

:end
echo.
pause