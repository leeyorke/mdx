@echo off
rem =====================================================================
rem  MDX 4080 service RESTART  -  double-click to run
rem  Stops whatever is listening on port 4080, then starts the service
rem  again through the existing scheduled task "MDX-GoldenDict-API".
rem  It does NOT delete or recreate the scheduled task (that is what
rem  scheduled_task_install.cmd is for) - it only re-runs it, so the
rem  process comes back the same hidden-window way as at logon.
rem  Exit code: 0 = success, 1 = failure.  Log: data\restart_server.log
rem  Keep this file pure ASCII with CRLF line endings.
rem =====================================================================
setlocal
set LOG=%~dp0data\restart_server.log
if not exist "%~dp0data" mkdir "%~dp0data"
echo [%date% %time%] === restart started, args=[%*] === >> "%LOG%"

rem ---------- [1/3] stop whatever is on 4080 ----------
echo [1/3] Stopping the current 4080 service (if any)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0script\stop_server.ps1" >> "%LOG%" 2>&1

rem Wait for the port to be RELEASED before starting again. If an old
rem process somehow survives, starting now would fail to bind (the new
rem process exits silently) yet the port check below would still see the
rem old one listening and falsely report OK - so verify it is really gone.
echo     Waiting for port 4080 to be released...
set /a rel=0
:wait_release
netstat -ano | findstr /c:":4080 " | findstr /c:"LISTENING" >nul 2>&1
if errorlevel 1 goto released
ping -n 2 127.0.0.1 >nul
set /a rel+=1
if %rel% lss 10 goto wait_release

echo [!] Port 4080 is still held by another process after stopping.
echo     Something is still listening - close it manually, then retry.
echo     See which PID:  netstat -ano ^| findstr :4080
echo [%date% %time%] restart ABORTED: port 4080 still held >> "%LOG%"
pause
exit /b 1

:released
echo     Port 4080 is free.

rem ---------- [2/3] start the service again via the scheduled task ----------
rem  Uses the task's own action (autostart.vbs -> run_server.bat) so the
rem  window stays hidden, exactly like a real logon start. Requires the
rem  task to already exist (it does, after scheduled_task_install.cmd).
echo [2/3] Starting the service via task "MDX-GoldenDict-API"...
schtasks /Query /TN "MDX-GoldenDict-API" >nul 2>&1
if errorlevel 1 goto task_missing
schtasks /Run /TN "MDX-GoldenDict-API" >> "%LOG%" 2>&1
if errorlevel 1 goto run_failed

rem ---------- [3/3] wait for port 4080 to come up ----------
echo [3/3] Waiting for port 4080 to come up...
set /a tries=0
:wait_port
netstat -ano | findstr /c:":4080 " | findstr /c:"LISTENING" >nul 2>&1
if not errorlevel 1 goto port_up
ping -n 2 127.0.0.1 >nul
set /a tries+=1
if %tries% lss 20 goto wait_port

echo [!] Port 4080 did not come up - details in %LOG%
echo     Check the log:  data\server.log  and  data\restart_server.log
echo [%date% %time%] restart FAILED: port 4080 down >> "%LOG%"
pause
exit /b 1

:port_up
echo [OK] 4080 service restarted - running at http://127.0.0.1:4080
echo [%date% %time%] restart OK >> "%LOG%"
pause
exit /b 0

:task_missing
echo [!] Scheduled task "MDX-GoldenDict-API" not found.
echo     Run scheduled_task_install.cmd once to create it, then retry.
echo [%date% %time%] restart FAILED: task missing >> "%LOG%"
pause
exit /b 1

:run_failed
echo [!] schtasks /Run failed - details in %LOG%
echo [%date% %time%] restart FAILED: schtasks run >> "%LOG%"
pause
exit /b 1
