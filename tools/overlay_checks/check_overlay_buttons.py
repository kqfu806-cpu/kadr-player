# -*- coding: utf-8 -*-
"""Тест оверлея: раскладка кнопок, клики, частота запросов, автозакрытие.

Запуск (в песочнице Linux нужен offscreen/VNC-платформа Qt):
    LD_LIBRARY_PATH=/tmp QT_QPA_PLATFORM=vnc .venv/bin/python tools/overlay_checks/check_overlay_buttons.py

Скрипт сам поднимает заглушку API на 127.0.0.1:8000 (если порт свободен),
поэтому сервер приложения запускать не обязательно.
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("QT_QPA_PLATFORM", "vnc")

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QHBoxLayout

POSTS = []


class _StubHandler(BaseHTTPRequestHandler):
    """Заглушка сервера: отдаёт состояние и принимает команды плеера."""

    def log_message(self, *args):
        pass

    def _send(self, payload, code=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/now-playing"):
            self._send({
                "ok": True, "playing": True, "position": 12.0, "duration": 100.0,
                "at": time.time(),
                "track": {"id": "1", "title": "Тест", "artist": "Проверка",
                          "album": "Альбом", "cover": ""},
            })
        else:
            self._send({"ok": True})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        POSTS.append(self.path)
        self._send({"ok": True})


def _start_stub():
    """Поднимает заглушку API на 127.0.0.1:8000 (если порт свободен)."""
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 8000), _StubHandler)
    except OSError:
        return None  # порт занят — значит, сервер приложения уже работает
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


STUB = _start_stub()

app = QApplication(sys.argv)
import overlay as ov

ok = True


def check(name, cond, extra=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name} {extra}")


def find_transport(layout, wanted):
    """Ищет горизонтальную раскладку, в которой лежат нужные кнопки."""
    for i in range(layout.count()):
        item = layout.itemAt(i)
        widget = item.widget()
        if isinstance(layout, QHBoxLayout) and widget is not None:
            if widget in wanted:
                return layout
        if item.layout() is not None:
            found = find_transport(item.layout(), wanted)
            if found is not None:
                return found
    return None


print("=== 0. раскладка транспорта: [⏮][▶][⏭] отдельной строкой ===")
w = ov.PlayerOverlay()
w.show()
app.processEvents()

buttons = [w.btn_prev, w.btn_play, w.btn_next]
transport = find_transport(w.layout(), buttons)
check("транспорт собран в QHBoxLayout", transport is not None)
if transport is not None:
    check("отступ между кнопками = 10 px", transport.spacing() == 10, f"({transport.spacing()})")
    check("в раскладке ровно три кнопки", transport.count() == 3, f"({transport.count()})")

for name, symbol in (("btn_prev", "⏮"), ("btn_play", "▶"), ("btn_next", "⏭")):
    button = getattr(w, name)
    check(f"{name}: символ {symbol}", button.text() == symbol, f"({button.text()!r})")
    check(f"{name}: размер 32x32",
          button.width() == 32 and button.height() == 32,
          f"({button.width()}x{button.height()})")
    check(f"{name}: НЕ прозрачен для мыши",
          not button.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
    check(f"{name}: фокус StrongFocus",
          button.focusPolicy() == Qt.FocusPolicy.StrongFocus)

geoms = [(b.text(), b.geometry()) for b in buttons]
for i in range(len(geoms) - 1):
    (t1, g1), (t2, g2) = geoms[i], geoms[i + 1]
    check(f"{t1} и {t2} не пересекаются", not g1.intersects(g2), f"({g1} / {g2})")
    gap = g2.left() - g1.right()
    check(f"зазор {t1}→{t2} ≈ 10 px", 9 <= gap <= 12, f"({gap})")

for name in ("btn_prev", "btn_play", "btn_next"):
    button = getattr(w, name)
    check(f"{name} не перекрывает обложку",
          not button.geometry().intersects(w.cover.geometry()),
          f"({button.geometry()} / обложка {w.cover.geometry()})")
    check(f"{name} внутри окна",
          w.rect().contains(button.geometry()),
          f"({button.geometry()} / окно {w.rect()})")
    hit = w.childAt(button.geometry().center())
    check(f"{name} — верхний виджет в этой точке (клик дойдёт до кнопки)",
          hit is button, f"({hit})")

check("высота свёрнутого окна >= 80 px", w.height() >= 80, f"({w.height()})")
check("ширина окна достаточна для кнопок", w.width() >= 360, f"({w.width()})")

print("=== 1. прозрачность для мыши и флаги ===")
check("окно НЕ прозрачно для мыши",
      not w.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
for name in ("cover", "title", "artist"):
    widget = getattr(w, name)
    check(f"{name} прозрачен для мыши (клик доходит до окна)",
          widget.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
for name in ("btn_prev", "btn_play", "btn_next", "btn_back", "btn_fwd", "close_btn"):
    button = getattr(w, name)
    check(f"{name}: enabled + курсор + не прозрачен",
          button.isEnabled()
          and button.cursor().shape() == Qt.CursorShape.PointingHandCursor
          and not button.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))

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

print("=== 2b. настоящий клик мышью по кнопке (проверка попадания) ===")
sent.clear()
QTest.mouseClick(w.btn_next, Qt.MouseButton.LeftButton)
deadline = time.time() + 3
while time.time() < deadline and not sent:
    app.processEvents()
    time.sleep(0.05)
check("клик мышью по кнопке «следующий» дошёл до сервера",
      any(u.endswith("/next") for u in [s[0] for s in sent]),
      [s[0] for s in sent])
check("сервер получил команду через HTTP",
      "/api/player/next" in POSTS, POSTS)

print("=== 2c. клик по кнопке, когда её событие перехвачено (свой hit-test) ===")
sent.clear()
# имитируем «прозрачный виджет поверх кнопки»: Qt отдаст клик окну, а не кнопке
w.btn_next.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
center = w.btn_next.mapToGlobal(QPoint(w.btn_next.width() // 2, w.btn_next.height() // 2))
origin = w.mapToGlobal(QPoint(0, 0))
local = QPointF(center.x() - origin.x(), center.y() - origin.y())
press = QMouseEvent(QEvent.Type.MouseButtonPress, local, QPointF(center),
                    Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier)
release = QMouseEvent(QEvent.Type.MouseButtonRelease, local, QPointF(center),
                      Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                      Qt.KeyboardModifier.NoModifier)
was_expanded = w._expanded
QApplication.sendEvent(w, press)
QApplication.sendEvent(w, release)
deadline = time.time() + 3
while time.time() < deadline and not sent:
    app.processEvents()
    time.sleep(0.05)
check("свой hit-test всё равно отправил команду «следующий»",
      any(u.endswith("/next") for u in [s[0] for s in sent]),
      [s[0] for s in sent])
check("оверлей не развернулся при клике по кнопке", w._expanded == was_expanded)
w.btn_next.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)

print("=== 3. частота опроса: не больше 1 GET /api/now-playing в секунду ===")
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
check("частота <= 1.5 запроса/с", rate <= 1.5, f"({rate:.2f}/с)")
check("таймер опроса в диапазоне 500..1000 мс",
      500 <= ov.POLL_MS <= 1000, f"({ov.POLL_MS} мс)")
check("абсолютный пол между запросами = 1 c", ov.POLL_MIN_INTERVAL >= 1.0,
      f"({ov.POLL_MIN_INTERVAL} c)")
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

print("=== 5. автозакрытие при падении сервера (5 неудачных опросов) ===")
w3 = ov.PlayerOverlay()
w3.show()
app.processEvents()
for _ in range(5):
    w3._on_poll_failed()
    app.processEvents()
check("окно закрыто после 5 ошибок", not w3.isVisible())

print("\nИТОГ:", "ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ")
sys.exit(0 if ok else 1)
