@echo off
rem =====================================================================
rem  MDX auto-start INSTALLER  -  double-click to run
rem  Creates the scheduled task "MDX-GoldenDict-API" and starts the
rem  4080 service. Relaunches itself elevated if not administrator.
rem  Exit code: 0 = success, 1 = failure.  Log: data\install_task.log
rem  Keep this file pure ASCII with CRLF line endings.
rem =====================================================================
setlocal
set LOG=%~dp0data\install_task.log
if not exist "%~dp0data" mkdir "%~dp0data"
echo [%date% %time%] === install started, args=[%*] === >> "%LOG%"

set ELEVATED=0
if /i "%~1"=="/elevated" set ELEVATED=1

fsutil dirty query %systemdrive% >nul 2>&1
if errorlevel 1 goto need_elevate
goto do_work

rem ---------- not admin: relaunch elevated ----------
:need_elevate
echo [!] Not running as administrator - relaunching elevated...
echo [%date% %time%] not admin; relaunching elevated >> "%LOG%"
powershell -NoProfile -Command "try { Start-Process -FilePath cmd.exe -ArgumentList '/c','%~f0','/elevated' -Verb RunAs -Wait } catch { exit 1 }"
if errorlevel 1 goto elevate_failed
goto verify

:elevate_failed
echo [!] Could not relaunch as administrator.
echo     Option A: right-click this file - Run as administrator
echo     Option B: use the no-admin fallback below (Startup folder)
echo [%date% %time%] elevation relaunch FAILED >> "%LOG%"
set /p ans=Try the no-admin fallback now? [y/N]:
if /i "%ans%"=="y" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0script\install_autostart.ps1"
  echo [%date% %time%] fallback install_autostart invoked >> "%LOG%"
)
pause
exit /b 1

rem ---------- elevated: do the real work ----------
:do_work
echo [1/3] Removing old task if present...
schtasks /Delete /TN "MDX-GoldenDict-API" /F >> "%LOG%" 2>&1
echo [2/3] Creating task from mdx-task.xml...
schtasks /Create /XML "%~dp0script\mdx-task.xml" /TN "MDX-GoldenDict-API" /F >> "%LOG%" 2>&1
if errorlevel 1 goto create_failed
echo [3/3] Starting the service...
netstat -ano | findstr /c:":4080 " | findstr /c:"LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo     service already running - nothing to start
) else (
  schtasks /Run /TN "MDX-GoldenDict-API" >> "%LOG%" 2>&1
)
echo [%date% %time%] task created, service start requested >> "%LOG%"
if "%ELEVATED%"=="1" exit /b 0
goto verify

:create_failed
echo [!] schtasks /Create failed - details in %LOG%
echo [%date% %time%] schtasks create FAILED >> "%LOG%"
pause
exit /b 1

rem ---------- verify the real end state ----------
:verify
set /a tries=0
:wait_port
netstat -ano | findstr /c:":4080 " | findstr /c:"LISTENING" >nul 2>&1
if not errorlevel 1 goto port_up
ping -n 2 127.0.0.1 >nul
set /a tries+=1
if %tries% lss 20 goto wait_port
echo [!] Port 4080 did not come up - details in %LOG%
echo [%date% %time%] verify FAILED: port 4080 down >> "%LOG%"
pause
exit /b 1

:port_up
schtasks /Query /TN "MDX-GoldenDict-API" >nul 2>&1
if errorlevel 1 goto task_missing
echo [OK] Task "MDX-GoldenDict-API" created; service is up on http://127.0.0.1:4080
echo     It auto-starts at your next logon (logout/login or reboot).
echo [%date% %time%] install OK >> "%LOG%"
pause
exit /b 0

:task_missing
echo [!] The scheduled task is missing - details in %LOG%
echo [%date% %time%] verify FAILED: task missing >> "%LOG%"
pause
exit /b 1