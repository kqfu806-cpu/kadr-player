@echo off
REM Kurymdyk — автозапуск при включении ПК
setlocal EnableExtensions
echo Installing Kurymdyk autostart...

REM Путь к run.bat текущей папки
set "RUNBAT=%~dp0run.bat"
if not exist "%RUNBAT%" (
  echo ERROR: run.bat not found at %RUNBAT%
  pause
  exit /b 1
)

REM 1) Через папку Автозагрузка (Startup)
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
if not exist "%STARTUP%" mkdir "%STARTUP%" 2>nul
echo Copying to Startup folder...
copy /Y "%RUNBAT%" "%STARTUP%\Kurymdyk.bat" >nul
if not errorlevel 1 (
  echo  - Copied to %STARTUP%\Kurymdyk.bat
) else (
  echo  - Failed to copy to Startup
)

REM 2) Через реестр (надёжнее, работает даже если Startup отключён)
echo Adding registry key HKCU\Software\Microsoft\Windows\CurrentVersion\Run...
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v Kurymdyk /t REG_SZ /d "\"%RUNBAT%\" --silent" /f >nul
if not errorlevel 1 (
  echo  - Registry: HKCU\...\Run\Kurymdyk = "%RUNBAT%" --silent
) else (
  echo  - Failed to add registry key (try Run as Administrator)
)

echo.
echo Autostart installed. Kurymdyk will start silently on next reboot (run.bat --silent).
echo Browser will NOT open automatically in silent mode — open http://127.0.0.1:8000 manually or remove --silent from registry.
echo To remove: run remove_autostart.bat
pause
exit /b 0
