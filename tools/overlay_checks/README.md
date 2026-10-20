# Ручные проверки оверлея

Скрипты не входят в `pytest`-сьют (названия начинаются с `check_`), запускаются вручную.

| Скрипт | Что проверяет | Запуск |
|---|---|---|
| `check_overlay_buttons.py` | прозрачность для мыши, клики по кнопках → реальные POST, частота опроса, закрытие по ✕, автозакрытие при падении сервера | `.venv\Scripts\python tools\overlay_checks\check_overlay_buttons.py` (на Windows обычный запуск) |
| `check_overlay_processes.sh` | двойной запуск, частота запросов на реальных процессах, автозакрытие после остановки uvicorn | `bash tools/overlay_checks/check_overlay_processes.sh` (Linux) |
| `check_throttle.py` | кэш ответов Deezer/iTunes на 1 ч, пауза между запросами, лимит 50 артистов, повтор после 429 | `.venv\Scripts\python tools\overlay_checks\check_throttle.py` |

Перед запуском должен быть поднят сервер: `python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000`.
