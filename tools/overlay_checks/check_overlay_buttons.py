# -*- coding: utf-8 -*-
"""Тест оверлея: клики по кнопкам, частота запросов, автозакрытие, прозрачность для мыши.

Запуск (в песочнице Linux нужен offscreen/VNC-платформа Qt):
    LD_LIBRARY_PATH=/tmp QT_QPA_PLATFORM=vnc .venv/bin/python tests/manual/test_overlay_buttons.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "vnc")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

app = QApplication(sys.argv)
import overlay as ov

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name} {extra}")


print("=== 1. прозрачность для мыши и флаги ===")
w = ov.PlayerOverlay()
w.show()
app.processEvents()
check("окно НЕ прозрачно для мыши",
      not w.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
for name in ("cover", "extra", "title", "artist", "album"):
    widget = getattr(w, name)
    check(f"{name} НЕ прозрачен для мыши",
          not widget.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
for name in ("btn_prev", "btn_play", "btn_next", "btn_back", "btn_fwd", "close_btn"):
    button = getattr(w, name)
    check(f"{name}: enabled + курсор",
          button.isEnabled() and button.cursor().shape() == Qt.CursorShape.PointingHandCursor)

print("=== 2. клик по Play реально шлёт POST /api/player/play ===")
sent = []
real_open = ov.OPENER.open


def spy_open(req, timeout=None):
    url = getattr(req, "full_url", str(req))
    if url.startswith("http://127.0.0.1:8000/api/player/"):
        sent.append((url, getattr(req, "data", None)))
    return real_open(req, timeout=timeout)


ov.OPENER.open = spy_open
w.btn_play.click()
w.btn_next.click()
w.btn_prev.click()
deadline = time.time() + 3
while time.time() < deadline and len(sent) < 3:
    app.processEvents()
    time.sleep(0.05)
urls = [s[0] for s in sent]
check("POST /api/player/play отправлен", any(u.endswith("/play") for u in urls), urls)
check("POST /api/player/next отправлен", any(u.endswith("/next") for u in urls))
check("POST /api/player/prev отправлен", any(u.endswith("/prev") for u in urls))

print("=== 3. частота опроса: не больше 2 GET /api/now-playing в секунду ===")
counter = {"n": 0}


def counting_open(req, timeout=None):
    url = getattr(req, "full_url", str(req))
    if url.endswith("/api/now-playing"):
        counter["n"] += 1
    return real_open(req, timeout=timeout)


ov.OPENER.open = counting_open
start = time.time()
while time.time() - start < 3.0:
    app.processEvents()
    time.sleep(0.01)
elapsed = time.time() - start
rate = counter["n"] / elapsed
print(f"  запросов: {counter['n']} за {elapsed:.2f} c = {rate:.2f}/с")
check("частота <= 2.5 запроса/с", rate <= 2.5, f"({rate:.2f}/с)")
ov.OPENER.open = spy_open

print("=== 4. кнопка ✕ закрывает окно и останавливает поток ===")
w.close_btn.click()
app.processEvents()
check("окно закрыто", not w.isVisible())
check("таймер остановлен", not w.timer.isActive())
check("поток остановлен", not w.worker.isRunning())
w2 = ov.PlayerOverlay()
w2.show()
app.processEvents()
w2._quit()
app.processEvents()
check("_quit() закрывает окно", not w2.isVisible())
check("_quit() останавливает таймер", not w2.timer.isActive())

print("=== 5. автозакрытие при падении сервера (5 неудач = 2.5 c) ===")
w3 = ov.PlayerOverlay()
w3.show()
app.processEvents()
for _ in range(5):
    w3._on_poll_failed()
    app.processEvents()
check("окно закрыто после 5 ошибок", not w3.isVisible())

print("\nИТОГ:", "ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ")
sys.exit(0 if ok else 1)
