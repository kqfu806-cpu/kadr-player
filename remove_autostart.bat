@echo off
REM Kurymdyk — удалить автозапуск
setlocal EnableExtensions
echo Removing Kurymdyk autostart...

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
if exist "%STARTUP%\Kurymdyk.bat" (
  del /F /Q "%STARTUP%\Kurymdyk.bat" >nul 2>&1
  echo  - Removed %STARTUP%\Kurymdyk.bat
) else (
  echo  - No file at %STARTUP%\Kurymdyk.bat
)
if exist "%STARTUP%\Kurymdyk.lnk" (
  del /F /Q "%STARTUP%\Kurymdyk.lnk" >nul 2>&1
  echo  - Removed Kurymdyk.lnk
)

echo Removing registry key...
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v Kurymdyk /f >nul 2>&1
if not errorlevel 1 (
  echo  - Registry key removed
) else (
  echo  - No registry key or failed to delete
)

echo.
echo Autostart removed.
pause
exit /b 0
