@echo off
rem =====================================================================
rem  MDX auto-start UNINSTALLER  -  double-click to run
rem  Stops the 4080 service and deletes the scheduled task
rem  "MDX-GoldenDict-API". Relaunches itself elevated if not admin.
rem  Exit code: 0 = success, 1 = failure.  Log: data\install_task.log
rem  Keep this file pure ASCII with CRLF line endings.
rem =====================================================================
setlocal
set LOG=%~dp0data\install_task.log
if not exist "%~dp0data" mkdir "%~dp0data"
echo [%date% %time%] === uninstall started, args=[%*] === >> "%LOG%"

set ELEVATED=0
if /i "%~1"=="/elevated" set ELEVATED=1

fsutil dirty query %systemdrive% >nul 2>&1
if errorlevel 1 goto need_elevate
goto do_work

rem ---------- not admin: relaunch elevated ----------
:need_elevate
echo [!] Not running as administrator - relaunching elevated...
powershell -NoProfile -Command "try { Start-Process -FilePath cmd.exe -ArgumentList '/c','%~f0','/elevated' -Verb RunAs -Wait } catch { exit 1 }"
if errorlevel 1 goto elevate_failed
goto verify

:elevate_failed
echo [!] Could not relaunch as administrator.
echo     Right-click this file and choose "Run as administrator".
echo [%date% %time%] elevation relaunch FAILED >> "%LOG%"
pause
exit /b 1

rem ---------- elevated: do the real work ----------
:do_work
echo [1/2] Stopping the 4080 service...
call :stop_4080
echo [2/2] Deleting the scheduled task...
schtasks /Query /TN "MDX-GoldenDict-API" >nul 2>&1
if errorlevel 1 (
  echo     task not present - nothing to delete
) else (
  schtasks /Delete /TN "MDX-GoldenDict-API" /F >> "%LOG%" 2>&1
  if errorlevel 1 goto delete_failed
  echo     task deleted
)
echo [%date% %time%] service stopped, task deleted >> "%LOG%"
if "%ELEVATED%"=="1" exit /b 0
goto verify

:delete_failed
echo [!] schtasks /Delete failed - details in %LOG%
echo [%date% %time%] schtasks delete FAILED >> "%LOG%"
pause
exit /b 1

rem ---------- helper: kill whatever listens on 4080 ----------
:stop_4080
set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /c:":4080 " ^| findstr /c:"LISTENING"') do (
  set FOUND=1
  echo     stopping listener PID %%a
  taskkill /f /pid %%a >nul 2>&1
)
if "%FOUND%"=="0" echo     4080 is not running
ping -n 3 127.0.0.1 >nul
goto :eof

rem ---------- verify the real end state ----------
:verify
set /a tries=0
:wait_clear
netstat -ano | findstr /c:":4080 " >nul 2>&1
if errorlevel 1 goto all_clear
ping -n 2 127.0.0.1 >nul
set /a tries+=1
if %tries% lss 10 goto wait_clear
rem 10s later: distinguish "still listening" (fail) from client TIME_WAIT (ok)
netstat -ano | findstr /c:":4080 " | findstr /c:"LISTENING" >nul 2>&1
if not errorlevel 1 goto still_listening
echo [OK] Service stopped; nothing is LISTENING on 4080.
echo     Leftover lines are old client-side TIME_WAIT entries (PID 0,
echo     not the service); they clear on their own within ~4 minutes:
netstat -ano | findstr /c:":4080 "
set /p ans=Wait here until they clear? [y/N]:
if /i "%ans%"=="y" goto wait_timewait
echo [%date% %time%] uninstall OK (client TIME_WAIT remnants only) >> "%LOG%"
pause
exit /b 0

:wait_timewait
set /a tries=0
:wait_clear2
netstat -ano | findstr /c:":4080 " >nul 2>&1
if errorlevel 1 goto all_clear
ping -n 3 127.0.0.1 >nul
set /a tries+=1
if %tries% lss 160 goto wait_clear2
echo [!] Still not clear after ~4 minutes - please check manually.
echo [%date% %time%] uninstall FAILED: 4080 lines persist >> "%LOG%"
pause
exit /b 1

:all_clear
schtasks /Query /TN "MDX-GoldenDict-API" >nul 2>&1
if not errorlevel 1 goto task_left
echo [OK] Task deleted and netstat shows nothing on 4080.
echo [%date% %time%] uninstall OK >> "%LOG%"
pause
exit /b 0

:still_listening
echo [!] Something is still LISTENING on 4080:
netstat -ano | findstr /c:":4080 " | findstr /c:"LISTENING"
echo [%date% %time%] uninstall FAILED: still listening >> "%LOG%"
pause
exit /b 1

:task_left
echo [!] The scheduled task still exists.
echo [%date% %time%] uninstall FAILED: task still exists >> "%LOG%"
pause
exit /b 1