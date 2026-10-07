@echo off
REM ASCII-only: cmd.exe corrupts UTF-8 Cyrillic in .bat files.
setlocal EnableExtensions
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set OLLAMA_NUM_GPU=1

if not exist "backend\main.py" (
  echo ERROR: backend\main.py not found. Run this from the Kurymdyk folder.
  pause
  exit /b 1
)

REM Prefer "python" (3.13 on this PC) over "py -3" (often stuck on 3.9).
set "PY="
call :try python
if defined PY goto :go
call :try py -3.13
if defined PY goto :go
call :try py -3.12
if defined PY goto :go
call :try py -3.11
if defined PY goto :go
call :try py -3.10
if defined PY goto :go
call :try python3
if defined PY goto :go
call :try py -3
if defined PY goto :go

echo.
echo  Python 3.10+ not found on PATH.
echo  Install from https://www.python.org/downloads/
echo  Enable "Add python.exe to PATH".
echo.
echo  If Python 3.13 already opens in PowerShell, run:
echo     python launch.py
echo.
pause
exit /b 1

:go
echo Using: %PY%
%PY% "launch.py" %*
set "EC=%ERRORLEVEL%"
echo.
if not "%EC%"=="0" echo Exit code: %EC%
pause
exit /b %EC%

:try
%* -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%*"
exit /b 0
