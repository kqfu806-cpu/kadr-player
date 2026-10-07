@echo off
REM Opens Kurymdyk in a chromeless window (Edge/Chrome --app).
setlocal EnableExtensions
cd /d "%~dp0"
set PYTHONUTF8=1

REM If launch.py is already running, still invoke it -- the mutex inside
REM launch.py opens the existing window and will not start a second uvicorn.

if exist "launch.py" (
  if exist "venv\Scripts\pythonw.exe" (
    start "" "venv\Scripts\pythonw.exe" launch.py --app
    exit /b 0
  )
  if exist "venv\Scripts\python.exe" (
    start "" "venv\Scripts\python.exe" launch.py --app
    exit /b 0
  )
)

REM No launch.py: start server only if port 8000 is free
netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul 2>&1
if errorlevel 1 (
  if exist "venv\Scripts\python.exe" (
    start "Kurymdyk server" /MIN "venv\Scripts\python.exe" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
  ) else (
    start "Kurymdyk server" cmd /c run.bat
  )
  timeout /t 3 /nobreak >nul
)

set "URL=http://127.0.0.1:8000"
set "EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
set "EDGE2=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
set "CHROME2=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
set "PROFILE=%cd%\cache\edge-app"

powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'edge-app' }) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 exit /b 0

if exist "%EDGE%"  start "" "%EDGE%"  --app=%URL% --user-data-dir="%PROFILE%" & exit /b 0
if exist "%EDGE2%" start "" "%EDGE2%" --app=%URL% --user-data-dir="%PROFILE%" & exit /b 0
if exist "%CHROME%"  start "" "%CHROME%"  --app=%URL% --user-data-dir="%PROFILE%" & exit /b 0
if exist "%CHROME2%" start "" "%CHROME2%" --app=%URL% --user-data-dir="%PROFILE%" & exit /b 0

start "" %URL%
exit /b 0
