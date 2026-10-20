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
# шпион на _send_command: проверяем, что команда уходит из обработчика кнопки
sent_cmds = []
real_send = w._send_command


def spy_send(command, value=None):
    sent_cmds.append(command)
    return real_send(command, value)


w._send_command = spy_send
w.btn_play.click()
w.btn_next.click()
w.btn_prev.click()
app.processEvents()
check("обработчик Play отправил команду", "play" in sent_cmds or "pause" in sent_cmds, sent_cmds)
check("обработчик Next отправил команду", "next" in sent_cmds, sent_cmds)
check("обработчик Prev отправил команду", "prev" in sent_cmds, sent_cmds)

if STUB is not None:
    deadline = time.time() + 3
    while time.time() < deadline and len(POSTS) < 3:
        app.processEvents()
        time.sleep(0.05)
    check("сервер получил POST /api/player/play", "/api/player/play" in POSTS, POSTS)
    check("сервер получил POST /api/player/next", "/api/player/next" in POSTS, POSTS)
    check("сервер получил POST /api/player/prev", "/api/player/prev" in POSTS, POSTS)
else:
    print("  [SKIP] реальный сервер на 8000 занят — проверяем только отправку команды")

print("=== 2b. настоящий клик мышью по кнопке (проверка попадания) ===")
sent_cmds.clear()
POSTS.clear()
QTest.mouseClick(w.btn_next, Qt.MouseButton.LeftButton)
deadline = time.time() + 3
while time.time() < deadline and not sent_cmds:
    app.processEvents()
    time.sleep(0.05)
check("клик мышью по кнопке «следующий» дошёл до обработчика",
      "next" in sent_cmds, sent_cmds)
was_expanded = w._expanded
QTest.mouseClick(w.btn_prev, Qt.MouseButton.LeftButton)
app.processEvents()
check("клик по кнопке НЕ разворачивает оверлей (стандартный Qt)",
      w._expanded == was_expanded)
check("обработчик Prev сработал по клику мышью", "prev" in sent_cmds, sent_cmds)
if STUB is not None:
    deadline = time.time() + 3
    while time.time() < deadline and not POSTS:
        app.processEvents()
        time.sleep(0.05)
    check("сервер получил команду через HTTP",
          "/api/player/next" in POSTS, POSTS)

print("=== 2c. РЕГРЕСС: окно не должно «уезжать» за курсором ===")
# Сценарий, который ломал всё раньше: нажали на пустом месте островка,
# отпустили над кнопкой → состояние перетаскивания не сбрасывалось →
# ЛЮБОЕ движение мыши над окном уводило его за курсором (GUI зависал).
origin = w.mapToGlobal(QPoint(0, 0))
background = QPointF(20, 20)          # пустое место островка
button_center = w.btn_next.mapToGlobal(
    QPoint(w.btn_next.width() // 2, w.btn_next.height() // 2))
btn_local = QPointF(button_center.x() - origin.x(), button_center.y() - origin.y())
press = QMouseEvent(QEvent.Type.MouseButtonPress, background, QPointF(background),
                    Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier)
release = QMouseEvent(QEvent.Type.MouseButtonRelease, btn_local, QPointF(button_center),
                      Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                      Qt.KeyboardModifier.NoModifier)
QApplication.sendEvent(w, press)
QApplication.sendEvent(w, release)
app.processEvents()
check("после отпускания состояние перетаскивания сброшено", w._press_pos is None)
# клик по пустому месту штатно переключает раскладку (высота 84 -> 400),
# поэтому позицию считаем ПОСЛЕ отпускания
pos_after_click = w.pos()
# теперь просто навёдываем мышь (кнопка не зажата) — окно обязано остаться на месте
for step in range(1, 11):
    move = QMouseEvent(QEvent.Type.MouseMove, QPointF(20 + step * 15, 20),
                       QPointF(20 + step * 15, 20), Qt.MouseButton.NoButton,
                       Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(w, move)
    app.processEvents()
check("окно не уехало за курсором при наведении", w.pos() == pos_after_click,
      f"({pos_after_click} → {w.pos()})")
check("перетаскивание не началось", w._dragging is False)

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

print("=== 4. кнопка ✕ закрывает окно и останавливает поток ===")
close_started = time.time()
w.close_btn.click()
app.processEvents()
close_time = time.time() - close_started
check("закрытие быстрое (не блокирует GUI на секунды)", close_time < 1.5,
      f"({close_time:.2f} c)")
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

print("=== 6. запоминание позиции окна (cache/overlay_state.json) ===")
# тест не должен трогать настоящий файл пользователя — подменяем путь
import tempfile

_real_state_file = ov.STATE_FILE
_tmp_state = os.path.join(tempfile.gettempdir(), "kadr_overlay_state_test.json")
if os.path.exists(_tmp_state):
    os.remove(_tmp_state)
ov.STATE_FILE = _tmp_state

w4 = ov.PlayerOverlay()
w4.show()
app.processEvents()
w4.move(777, 555)
w4._save_current_position()
check("файл состояния создан", os.path.exists(_tmp_state), _tmp_state)
if os.path.exists(_tmp_state):
    with open(_tmp_state, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    check("в файле сохранены координаты", data.get("x") == 777 and data.get("y") == 555, data)
    check("есть метка времени", bool(data.get("saved_at")), data)

# новый оверлей должен вернуться на сохранённое место
w5 = ov.PlayerOverlay()
w5.show()
app.processEvents()
check("позиция восстановлена при запуске", (w5.x(), w5.y()) == (777, 555),
      f"({w5.x()}, {w5.y()})")

# позиция вне экрана игнорируется (после смены монитора)
ov.STATE_FILE = _tmp_state
with open(_tmp_state, "w", encoding="utf-8") as fh:
    json.dump({"x": -99999, "y": -99999, "saved_at": "test"}, fh)
w6 = ov.PlayerOverlay()
w6.show()
app.processEvents()
check("несуществующая позиция не ломает запуск", w6.isVisible())
check("при несуществующей позиции оверлей на экране",
      w6.geometry().intersects(app.primaryScreen().availableGeometry()),
      f"{w6.geometry()}")
ov.STATE_FILE = _real_state_file
w4.close()
w5.close()
w6.close()
app.processEvents()

print("=== 7. режим «сквозь» (Ctrl+Shift+O) ===")
w7 = ov.PlayerOverlay()
w7.show()
app.processEvents()
w7.set_click_through(True)
app.processEvents()
check("режим «сквозь» включён", w7._click_through is True)
check("окно прозрачно для мыши (Qt)", w7.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
w7.set_click_through(False)
app.processEvents()
check("режим «сквозь» выключен", w7._click_through is False)
check("окно снова кликабельно (Qt)", not w7.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
w7.toggle_click_through()
check("toggle_click_through переключает режим", w7._click_through is True)
w7.toggle_click_through()
check("повторный toggle выключает режим", w7._click_through is False)
check("горячая клавиша объявлена как Ctrl+Shift+O", ov.HOTKEY_LABEL == "Ctrl+Shift+O", ov.HOTKEY_LABEL)
w7.close()
app.processEvents()

print("=== 8. режим «призрака» (полупрозрачность) ===")
w8 = ov.PlayerOverlay()
w8.show()
app.processEvents()
check("свёрнутый оверлей полупрозрачный (0.4)",
      abs(w8.windowOpacity() - ov.GHOST_OPACITY) < 0.01, f"({w8.windowOpacity()})")
w8._hovered = True
w8._apply_opacity()
check("под курсором оверлей яркий (1.0)",
      abs(w8.windowOpacity() - ov.SOLID_OPACITY) < 0.01, f"({w8.windowOpacity()})")
w8._hovered = False
w8._apply_opacity()
check("после ухода курсора снова полупрозрачный",
      abs(w8.windowOpacity() - ov.GHOST_OPACITY) < 0.01, f"({w8.windowOpacity()})")
w8.set_expanded(True)
app.processEvents()
check("раскрытый оверлей яркий (1.0)",
      abs(w8.windowOpacity() - ov.SOLID_OPACITY) < 0.01, f"({w8.windowOpacity()})")
w8.set_expanded(False)
app.processEvents()
check("свёрнутый оверлей снова полупрозрачный",
      abs(w8.windowOpacity() - ov.GHOST_OPACITY) < 0.01, f"({w8.windowOpacity()})")
w8.set_click_through(True)
w8._apply_opacity()
check("в режиме «сквозь» оверлей остаётся полупрозрачным",
      abs(w8.windowOpacity() - ov.GHOST_OPACITY) < 0.01, f"({w8.windowOpacity()})")
w8.set_click_through(False)
w8.close()
app.processEvents()

print("\nИТОГ:", "ВСЁ ОК" if ok else "ЕСТЬ ОШИБКИ")
sys.exit(0 if ok else 1)
