@echo off
REM Оверлей «Dynamic Island» для Курымдык (PyQt6 + pyoverlaykit) — окно поверх игр.
REM Запуск: run_overlay.bat            (сразу)
REM        run_overlay.bat 12         (через 12 секунд, пока поднимается сервер)
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
  echo ERROR: venv не найден. Сначала запусти run.bat.
  pause
  exit /b 1
)

set "PYW=venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=venv\Scripts\python.exe"

if not "%~1"=="" timeout /t %~1 /nobreak >nul

start "" "%PYW%" "%~dp0overlay.py"
echo Оверлей запущен: %~dp0overlay.py
exit /b 0
