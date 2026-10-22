@echo off
rem MDX auto-start task installer.
rem Runs as administrator (auto-relaunches itself elevated if not).
rem Task: MDX-GoldenDict-API, created from mdx-task.xml.
rem NOTE: re-running this script RESTARTS the service on purpose:
rem       deleting a task terminates its running instance, so step 3
rem       below starts the service again. Safe to re-run anytime.

fsutil dirty query %systemdrive% >nul 2>&1
if errorlevel 1 goto elevate

echo [1/3] Removing old task if present...
schtasks /Delete /TN "MDX-GoldenDict-API" /F >nul 2>&1

echo [2/3] Creating task from mdx-task.xml...
schtasks /Create /XML "%~dp0mdx-task.xml" /TN "MDX-GoldenDict-API" /F
if errorlevel 1 goto fail

echo [3/3] Starting the service now...
schtasks /Run /TN "MDX-GoldenDict-API" >nul 2>&1
set /a tries=0
:waitloop
timeout /t 1 /nobreak >nul
curl -sf http://127.0.0.1:4080/ >nul 2>&1
if not errorlevel 1 goto started
set /a tries+=1
if %tries% lss 10 goto waitloop
echo [!] Service did not answer on port 4080 - check data\server.log
goto end

:started
echo [OK] Task created AND service is up on http://127.0.0.1:4080
echo       Logon trigger fires at your next real logon (logout/login or reboot).
echo       stop: pwsh D:\myworkspace\mdx\script\stop_server.ps1
echo       log:   D:\myworkspace\mdx\data\server.log
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