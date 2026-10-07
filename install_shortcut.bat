@echo off
REM ASCII-only. Creates a Desktop shortcut to start_app.bat with the glass icon.
setlocal EnableExtensions
cd /d "%~dp0"

set "TARGET=%~dp0start_app.bat"
set "ICON=%~dp0frontend\icons\kadr.ico"
set "DESKTOP=%USERPROFILE%\Desktop"
set "STARTMENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs"
set "LNK=%DESKTOP%\Kurymdyk.lnk"
set "LNK2=%STARTMENU%\Kurymdyk.lnk"

if not exist "%TARGET%" (
  echo start_app.bat not found
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell; ^
   $s = $ws.CreateShortcut($env:LNK); ^
   $s.TargetPath = $env:TARGET; ^
   $s.WorkingDirectory = (Split-Path $env:TARGET); ^
   if (Test-Path $env:ICON) { $s.IconLocation = $env:ICON }; ^
   $s.WindowStyle = 7; ^
  $s.Description = 'Kurymdyk local music player'; ^
   $s.Save(); ^
   Write-Host ('Desktop: ' + $env:LNK); ^
   $s2 = $ws.CreateShortcut($env:LNK2); ^
   $s2.TargetPath = $env:TARGET; ^
   $s2.WorkingDirectory = (Split-Path $env:TARGET); ^
   if (Test-Path $env:ICON) { $s2.IconLocation = $env:ICON }; ^
  $s2.Description = 'Kurymdyk local music player'; ^
   $s2.Save(); ^
   Write-Host ('Start Menu: ' + $env:LNK2)"

if errorlevel 1 (
  echo Failed to create shortcut
  pause
  exit /b 1
)

echo.
echo Desktop shortcut created: %LNK%
echo Double-click it after the server is running, or it will start the server itself.
echo.
pause
