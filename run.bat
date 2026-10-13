@echo off
REM TASK2 stable: cd /d "%~dp0" + venv create/activate + pip + uvicorn, no splash/Edge
REM Kurymdyk — только сайт. Двойной клик → браузер http://127.0.0.1:8000
setlocal EnableExtensions
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set OLLAMA_NUM_GPU=1

if not exist "backend\main.py" (
  echo ERROR: backend\main.py not found. Run from the Kurymdyk folder.
  pause
  exit /b 1
)

REM venv — создать если нет
if not exist "venv\Scripts\activate.bat" (
  echo [1/3] Creating venv...
  python -m venv venv
  if errorlevel 1 (
    echo Failed to create venv. Try: python -m venv venv
    pause
    exit /b 1
  )
)

REM активировать venv
call "venv\Scripts\activate.bat"
if errorlevel 1 (
  echo Failed to activate venv
  pause
  exit /b 1
)

REM зависимости
echo [2/3] Installing dependencies...
python -m pip install --upgrade pip >nul 2>&1
pip install -r requirements.txt
if errorlevel 1 (
  echo pip install failed
  pause
  exit /b 1
)

REM === TASK1: автозапуск Ollama ===
where ollama >nul 2>&1
if errorlevel 1 (
  echo Ollama не установлена, ИИ-функции отключены
  goto :skip_ollama
)
curl -s http://127.0.0.1:11434/api/tags >nul 2>&1
if not errorlevel 1 (
  echo Ollama already running
  goto :skip_ollama
)
echo Starting Ollama...
start "Ollama" /min cmd /c "ollama serve"
timeout /t 5 /nobreak >nul
:skip_ollama

REM запуск — uvicorn + браузер по умолчанию
echo [3/3] Starting http://127.0.0.1:8000
echo Uvicorn running on http://127.0.0.1:8000
REM открыть браузер через 2 сек в фоне
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:8000"

python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
set "EC=%ERRORLEVEL%"
echo.
if not "%EC%"=="0" echo Exit code: %EC%
pause
exit /b %EC%
