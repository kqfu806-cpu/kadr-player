# -*- coding: utf-8 -*-
"""Оверлей «Dynamic Island» для Курымдык.

Отдельное приложение (PyQt6 + pyoverlaykit): frameless-окно поверх игр —
Always-on-Top, без рамки, без фокуса и без панели задач. Показывает текущий
трек, прогресс и даёт управление плеером (play/pause/next/prev/seek).

Архитектура:
  * GUI-поток ничего не блокирует: ни time.sleep(), ни сетевых запросов;
  * HTTP живёт в отдельном потоке PollWorker(QThread), GUI только слушает
    сигналы state_updated / cover_updated / poll_failed;
  * частота опроса фиксирована QTimer'ом в 500 мс (не чаще 2 запросов/с);
  * защита от двойного запуска — файловая блокировка cache/overlay.lock.

Данные: GET  http://127.0.0.1:8000/api/now-playing
Команды: POST http://127.0.0.1:8000/api/player/<play|pause|next|prev|seek>
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request

from PyQt6.QtCore import QObject, QPoint, QRectF, QThread, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "overlay.log")
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "overlay.lock")

# Системный прокси Windows не должен перехватывать обращения к 127.0.0.1
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _log(message: str) -> None:
    """Пишем в консоль и в cache/overlay.log — запускается через pythonw, консоли нет."""
    print(message, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


# ---------- киты: pyoverlaykit даёт базовый класс оверлея ----------
try:
    from PyOverlayKit.overlay import Overlay as _KitOverlay

    HAS_KIT = True
except Exception as _kit_error:  # pragma: no cover - страховка, если нет pyqtgraph
    _KitOverlay = QWidget
    HAS_KIT = False
    _log(f"[overlay] PyOverlayKit недоступен ({_kit_error}) — работаем на голом Qt")

# ---------- настройки ----------
HOST = "127.0.0.1"
PORT = 8000
BASE = f"http://{HOST}:{PORT}"
API_STATE = f"{BASE}/api/now-playing"
API_CMD = f"{BASE}/api/player"

COLLAPSED_W, COLLAPSED_H = 320, 80
EXPANDED_W, EXPANDED_H = 320, 400
BOTTOM_MARGIN = 40          # отступ от нижнего края экрана, px
POLL_MS = 500               # период опроса состояния плеера (2 запроса/с максимум)
MAX_POLL_FAILURES = 5       # 5 неудачных опросов подряд (2.5 c) — закрываем оверлей
HTTP_TIMEOUT = 2.0

ACCENT = "#ff6b9d"
TEXT = "#ffffff"
MUTED = "#9aa0aa"
CARD_RADIUS = 24            # скругление «жидкого стекла»
COVER_RADIUS = 12           # скругление обложки
GLASS_TOP = QColor(44, 44, 66, 205)      # rgba(44,44,66,0.80)
GLASS_MID = QColor(20, 20, 30, 191)      # rgba(20,20,30,0.75)
GLASS_BOTTOM = QColor(12, 12, 20, 205)   # rgba(12,12,20,0.80)
NEON_BORDER = QColor(255, 107, 157, 102)  # rgba(255,107,157,0.4)

# шрифты: обычный текст + шрифт символов (⏮ ⏸ ⏭)
UI_FAMILIES = ["Segoe UI", "Tahoma", "Arial"]
GLYPH_FAMILIES = ["Segoe UI Symbol", "Segoe UI", "Arial Unicode MS"]


def _font(size: int, glyph: bool = False) -> QFont:
    font = QFont()
    font.setFamilies(GLYPH_FAMILIES if glyph else UI_FAMILIES)
    font.setPointSize(size)
    return font


def _fmt(seconds: float) -> str:
    try:
        sec = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        sec = 0
    return f"{sec // 60}:{sec % 60:02d}"


class _InstanceLock:
    """Файловая блокировка: второй экземпляр оверлея не запускается.

    Блокировку держит открытый дескриптор — она снимается сама, когда
    процесс завершается (в том числе аварийно).
    """

    def __init__(self, path: str):
        self.path = path
        self._fh = None

    def acquire(self) -> tuple[bool, str]:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self._fh = open(self.path, "a+", encoding="utf-8")
        except OSError:
            return True, ""  # не смогли создать файл — не блокируем запуск
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False, self._read_pid()
        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(str(os.getpid()))
            self._fh.flush()
        except OSError:
            pass
        return True, ""

    def _read_pid(self) -> str:
        try:
            self._fh.seek(0)
            return self._fh.read().strip() or "?"
        except OSError:
            return "?"

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None


class PollWorker(QThread):
    """Весь HTTP — здесь, GUI-поток не блокируется ни на миллисекунду.

    Сигналы:
      state_updated(dict) — новое состояние плеера
      cover_updated(bytes) — байты обложки
      poll_failed()        — сервер не ответил
    """

    state_updated = pyqtSignal(dict)
    cover_updated = pyqtSignal(bytes)
    poll_failed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop_event = threading.Event()
        self._poll_event = threading.Event()
        self._commands: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._last_cover = ""

    # --- вызовы из GUI-потока (потокобезопасные) ---
    def request_poll(self) -> None:
        """Просит один опрос состояния (вызывается из QTimer в GUI)."""
        self._poll_event.set()

    def send_command(self, command: str, value=None) -> None:
        """Кладёт команду в очередь — она уйдёт из этого потока."""
        self._commands.put((command, value))

    def stop(self) -> None:
        self._stop_event.set()
        self._poll_event.set()

    # --- работа в отдельном потоке ---
    def run(self) -> None:  # noqa: D102
        while not self._stop_event.is_set():
            self._drain_commands()
            # ждём сигнала от GUI-таймера (500 мс) или команды остановки
            self._poll_event.wait(timeout=0.2)
            if self._stop_event.is_set():
                break
            if not self._poll_event.is_set():
                continue
            self._poll_event.clear()
            self._do_poll()
        self._drain_commands()

    def _drain_commands(self) -> None:
        while True:
            try:
                command, value = self._commands.get_nowait()
            except queue.Empty:
                return
            self._send_command(command, value)

    def _do_poll(self) -> None:
        try:
            with OPENER.open(API_STATE, timeout=HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            self.poll_failed.emit()
            return
        if not isinstance(data, dict):
            self.poll_failed.emit()
            return
        self.state_updated.emit(data)
        cover = str((data.get("track") or {}).get("cover") or "")
        if cover and cover != self._last_cover:
            self._last_cover = cover
            try:
                with OPENER.open(cover, timeout=3) as resp:
                    self.cover_updated.emit(resp.read())
            except Exception:
                pass

    def _send_command(self, command: str, value=None) -> None:
        body = b"{}"
        if command == "seek" and value is not None:
            try:
                body = json.dumps({"position": float(value)}).encode("utf-8")
            except (TypeError, ValueError):
                body = b"{}"
        try:
            request = urllib.request.Request(
                f"{API_CMD}/{command}",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with OPENER.open(request, timeout=3):
                pass
        except Exception:
            pass


class ElidedLabel(QLabel):
    """QLabel, который сам обрезает текст многоточием."""

    def __init__(self, text: str = "", size: int = 14, color: str = TEXT, parent=None):
        super().__init__(parent)
        self._full = ""
        self.setFont(_font(size))
        self.setStyleSheet(f"color: {color}; background: transparent;")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt API)
        self._full = text or ""
        self._apply()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._apply()

    def _apply(self) -> None:
        fm = QFontMetrics(self.font())
        width = max(20, self.width())
        super().setText(fm.elidedText(self._full, Qt.TextElideMode.ElideRight, width))


class PlayerOverlay(_KitOverlay):
    """Островок: свёрнут 320x80, по клику разворачивается до 320x400."""

    def __init__(self):
        super().__init__()

        # --- окно: без рамки, всегда сверху, вне панели задач, без фокуса ---
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        # БАЗОВЫЙ КЛАСС КИТА СТАВИТ WA_TransparentForMouseEvents (клики проходят сквозь!)
        # → жёстко выключаем, иначе кнопки не получают события мыши
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        try:
            self.set_close_button_visibility(False)
        except Exception:
            pass

        self.setWindowTitle("Курымдык — оверлей")
        self.setFixedSize(COLLAPSED_W, COLLAPSED_H)

        self._expanded = False
        self._press_pos: QPoint | None = None
        self._drag_offset = QPoint(0, 0)
        self._dragging = False
        self._user_moved = False
        self._cover_url = ""
        self._last_state: dict = {}
        self._poll_failures = 0
        self._poll_pending = False

        self._build_ui()

        # --- сеть: отдельный поток, GUI не блокируется ---
        self.worker = PollWorker(self)
        self.worker.state_updated.connect(self._on_state)
        self.worker.cover_updated.connect(self._on_cover)
        self.worker.poll_failed.connect(self._on_poll_failed)
        self.worker.start()
        # страховка: поток гасится, даже если окно уничтожили без closeEvent
        self.destroyed.connect(lambda *_: self.worker.stop())

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(POLL_MS)

        self._place()
        self._make_topmost()

    # ---------------- интерфейс ----------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 34, 10)  # справа место под кнопку ✕
        outer.setSpacing(6)

        # --- строка островка: обложка + текст/прогресс + транспорт ---
        row = QHBoxLayout()
        row.setSpacing(10)

        self.cover = QLabel(self)
        self.cover.setFixedSize(60, 60)
        self.cover.setStyleSheet(f"background: transparent; border-radius: {COVER_RADIUS}px;")
        row.addWidget(self.cover, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        self.title = ElidedLabel("Курымдык", 10, TEXT, self)
        self.artist = ElidedLabel("нет трека", 8, MUTED, self)
        for label in (self.title, self.artist):
            # лёгкая тень под текстом — «стекло» поверх фона
            shadow = QGraphicsDropShadowEffect(self)
            shadow.setBlurRadius(6)
            shadow.setOffset(0, 1)
            shadow.setColor(QColor(0, 0, 0, 170))
            label.setGraphicsEffect(shadow)
        text_col.addWidget(self.title)
        text_col.addWidget(self.artist)

        # тонкий прогресс-бар (виден и в свёрнутом, и в раскрытом виде)
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.setStyleSheet(
            "QProgressBar{background:rgba(255,255,255,0.15);border:none;border-radius:2px;}"
            f"QProgressBar::chunk{{background:{ACCENT};border-radius:2px;}}"
        )
        text_col.addWidget(self.progress)
        row.addLayout(text_col, 1)

        self.btn_prev = self._button("⏮", "Предыдущий", self, glyph=True)
        self.btn_play = self._button("▶", "Играть / пауза", self, glyph=True)
        self.btn_next = self._button("⏭", "Следующий", self, glyph=True)
        self.btn_play.setFixedSize(34, 34)
        self.btn_prev.setFixedSize(30, 30)
        self.btn_next.setFixedSize(30, 30)
        for button in (self.btn_prev, self.btn_play, self.btn_next):
            self._round_button(button)

        self.btn_prev.clicked.connect(lambda: self._command("prev"))
        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_next.clicked.connect(lambda: self._command("next"))

        outer.addLayout(row)

        # --- блок раскрытого состояния ---
        self.extra = QWidget(self)
        extra_layout = QVBoxLayout(self.extra)
        extra_layout.setContentsMargins(0, 4, 0, 0)
        extra_layout.setSpacing(8)

        self.album = ElidedLabel("", 8, MUTED, self.extra)
        extra_layout.addWidget(self.album)
        extra_layout.addStretch(1)  # время и перемотка прижаты к низу карточки

        times = QHBoxLayout()
        self.time_now = QLabel("0:00", self.extra)
        self.time_all = QLabel("0:00", self.extra)
        for label in (self.time_now, self.time_all):
            label.setStyleSheet(f"color: {MUTED}; font-size: 10px; background: transparent;")
        times.addWidget(self.time_now)
        times.addStretch(1)
        times.addWidget(self.time_all)
        extra_layout.addLayout(times)

        seek_row = QHBoxLayout()
        seek_row.setSpacing(8)
        self.btn_back = self._button("−10", "Назад на 10 секунд", self.extra)
        self.btn_fwd = self._button("+10", "Вперёд на 10 секунд", self.extra)
        self.btn_back.clicked.connect(lambda: self._seek_relative(-10))
        self.btn_fwd.clicked.connect(lambda: self._seek_relative(10))
        for button in (self.btn_back, self.btn_fwd):
            seek_row.addWidget(button, 1)
        extra_layout.addLayout(seek_row)

        outer.addWidget(self.extra)
        self.extra.setVisible(False)

        # --- кнопка закрытия в правом верхнем углу ---
        self.close_btn = QPushButton("✕", self)
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.setToolTip("Закрыть оверлей")
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_btn.setEnabled(True)
        self.close_btn.setStyleSheet(
            "QPushButton{background-color:rgba(255,255,255,0.06);color:rgba(255,255,255,0.30);"
            "border:none;border-radius:10px;font-size:11px;}"
            "QPushButton:hover{background-color:rgba(255,107,157,0.30);color:#ffffff;}"
            "QPushButton:pressed{background-color:rgba(255,107,157,0.50);}"
        )
        self.close_btn.clicked.connect(self._quit)
        self._position_close_button()

    def _button(self, text: str, tooltip: str, parent, glyph: bool = False) -> QPushButton:
        button = QPushButton(text, parent)
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setEnabled(True)
        if glyph:
            button.setFont(_font(11, glyph=True))
        button.setStyleSheet(
            "QPushButton{background-color:rgba(255,255,255,0.10);color:#ffffff;"
            "border:1px solid rgba(255,255,255,0.08);border-radius:17px;font-size:13px;}"
            "QPushButton:hover{background-color:rgba(255,107,157,0.30);"
            "border:1px solid rgba(255,107,157,0.55);}"
            "QPushButton:pressed{background-color:rgba(255,107,157,0.50);}"
        )
        return button

    def _round_button(self, button: QPushButton) -> None:
        """Круглая кнопка: радиус = половина размера (иначе Qt рисует квадрат)."""
        radius = max(1, button.height() // 2)
        style = button.styleSheet().replace("border-radius:17px", f"border-radius:{radius}px")
        button.setStyleSheet(style)

    def _position_close_button(self) -> None:
        if not hasattr(self, "close_btn"):
            return
        self.close_btn.move(self.width() - self.close_btn.width() - 6, 6)
        self.close_btn.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._position_close_button()

    def showEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().showEvent(event)
        # страховка: базовый класс кита мог включить «прозрачность для мыши»
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        if self.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents):
            _log("[overlay] ВНИМАНИЕ: окно всё ещё прозрачно для мыши — кнопки не сработают")
        self._position_close_button()

    # ---------------- отрисовка: «жидкое стекло» ----------------
    def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)

        # 1) мягкая тень под стеклом
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(12, 0, -1):
            offset = i * 0.7
            shadow = QRectF(
                rect.x() - offset,
                rect.y() - offset + 3,
                rect.width() + 2 * offset,
                rect.height() + 2 * offset,
            )
            path = QPainterPath()
            path.addRoundedRect(shadow, CARD_RADIUS + offset, CARD_RADIUS + offset)
            painter.setBrush(QColor(0, 0, 0, 5))
            painter.drawPath(path)

        # 2) корпус: полупрозрачный градиент + неоновая рамка
        path = QPainterPath()
        path.addRoundedRect(rect, CARD_RADIUS, CARD_RADIUS)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0.0, GLASS_TOP)
        gradient.setColorAt(0.5, GLASS_MID)
        gradient.setColorAt(1.0, GLASS_BOTTOM)
        painter.setBrush(gradient)
        painter.setPen(QPen(NEON_BORDER, 1.0))
        painter.drawPath(path)

        # 3) верхний блик — блик стекла
        painter.setPen(Qt.PenStyle.NoPen)
        gloss = QRectF(
            rect.x() + CARD_RADIUS * 0.7,
            rect.y() + 1.5,
            rect.width() - CARD_RADIUS * 1.4,
            min(rect.height() * 0.45, 36.0),
        )
        gloss_path = QPainterPath()
        gloss_path.addRoundedRect(gloss, CARD_RADIUS * 0.6, CARD_RADIUS * 0.6)
        painter.setBrush(QColor(255, 255, 255, 14))
        painter.drawPath(gloss_path)
        painter.end()

    # ---------------- геометрия ----------------
    def _place(self) -> None:
        """Снизу по центру экрана, отступ BOTTOM_MARGIN от нижнего края."""
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        x = geo.center().x() - self.width() // 2
        y = geo.bottom() - self.height() - BOTTOM_MARGIN
        self.move(int(x), int(y))

    def set_expanded(self, expanded: bool) -> None:
        old_height = self.height()
        self._expanded = bool(expanded)
        if self._expanded:
            self.setFixedSize(EXPANDED_W, EXPANDED_H)
            self.extra.setVisible(True)
        else:
            self.setFixedSize(COLLAPSED_W, COLLAPSED_H)
            self.extra.setVisible(False)
        if self._user_moved:
            # пользователь перетащил островок — держим нижний край на месте
            self.move(self.x(), self.y() - (self.height() - old_height))
        else:
            self._place()
        self._make_topmost()

    def toggle_expanded(self) -> None:
        self.set_expanded(not self._expanded)

    # ---------------- клики, перетаскивание ----------------
    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.globalPosition().toPoint()
            self._drag_offset = self._press_pos - self.pos()
            self._dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._press_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint() - self._press_pos
            if not self._dragging and delta.manhattanLength() > 6:
                self._dragging = True
            if self._dragging:
                self._user_moved = True
                self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.button() == Qt.MouseButton.LeftButton:
            moved = 0
            if self._press_pos is not None:
                moved = (event.globalPosition().toPoint() - self._press_pos).manhattanLength()
            self._press_pos = None
            was_dragging = self._dragging
            self._dragging = False
            if not was_dragging and moved <= 6:
                self.toggle_expanded()
        super().mouseReleaseEvent(event)

    # ---------------- закрытие ----------------
    def _quit(self) -> None:
        """Кнопка ✕: полностью закрываем приложение."""
        _log("[overlay] закрыт кнопкой")
        self.timer.stop()
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # гасим таймер и фоновый поток — иначе процесс не завершится
        self.timer.stop()
        try:
            self.worker.stop()
            self.worker.wait(2000)
        except Exception:
            pass
        super().closeEvent(event)

    # ---------------- Win32: Always-on-Top + AOD ----------------
    def _make_topmost(self) -> None:
        if os.name != "nt":
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TRANSPARENT = 0x00000020  # клики проходят сквозь — нам НЕ нужно
            HWND_TOPMOST = -1
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd,
                GWL_EXSTYLE,
                (style | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
                & ~WS_EX_TRANSPARENT,
            )
            user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE,
            )
        except Exception as exc:
            _log(f"[overlay] не удалось поднять окно в Always-on-Top: {exc}")

    # ---------------- данные ----------------
    def _tick(self) -> None:
        """Единственный источник запросов: QTimer, 500 мс, без блокировок."""
        if self._poll_pending:
            return
        self._poll_pending = True
        self.worker.request_poll()

    def _on_poll_failed(self) -> None:
        """Сервер не ответил: счётчик неудач, после 5 — закрываемся."""
        self._poll_pending = False
        self._poll_failures += 1
        if self._poll_failures >= MAX_POLL_FAILURES:
            _log(
                f"[overlay] сервер не отвечает {self._poll_failures} раз подряд — закрываю оверлей "
                "(запустить снова: run_overlay.bat)"
            )
            self._quit()

    def _on_state(self, data: dict) -> None:
        self._poll_pending = False
        self._poll_failures = 0
        self._last_state = data or {}
        track = self._last_state.get("track") or {}
        playing = bool(self._last_state.get("playing"))

        title = str(track.get("title") or "")
        artist = str(track.get("artist") or "")
        album = str(track.get("album") or "")
        self.title.setText(title or "Курымдык")
        self.artist.setText(artist or "нет трека")
        self.album.setText(album)
        self.btn_play.setText("⏸" if playing else "▶")
        self.btn_play.setFont(_font(11, glyph=True))

        cover = str(track.get("cover") or "")
        if cover != self._cover_url:
            self._cover_url = cover
            if not cover:
                self._on_cover(b"")

        try:
            duration = float(self._last_state.get("duration") or track.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        try:
            position = float(self._last_state.get("position") or 0)
        except (TypeError, ValueError):
            position = 0.0
        if duration > 0:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(max(0, min(1000, position / duration * 1000))))
        else:
            self.progress.setRange(0, 0)
        self.time_now.setText(_fmt(position))
        self.time_all.setText(_fmt(duration))

    def _on_cover(self, data: bytes) -> None:
        if not data:
            self.cover.clear()
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        # скруглённая обложка 60x60 (radius 12)
        rounded = QPixmap(60, 60)
        rounded.fill(Qt.GlobalColor.transparent)
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, 60, 60), COVER_RADIUS, COVER_RADIUS)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, pixmap.scaled(
            60, 60, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        ))
        painter.end()
        self.cover.setPixmap(rounded)

    # ---------------- команды ----------------
    def _command(self, command: str, value=None) -> None:
        """Команда уходит из фонового потока — GUI не блокируется."""
        try:
            self.worker.send_command(command, value)
        except Exception as exc:
            _log(f"[overlay] команда {command} не отправлена: {exc}")

    def _toggle_play(self) -> None:
        self._command("play" if not self._last_state.get("playing") else "pause")

    def _seek_relative(self, offset: float) -> None:
        try:
            position = float(self._last_state.get("position") or 0) + offset
        except (TypeError, ValueError):
            position = 0.0
        self._command("seek", max(0.0, position))


def main() -> int:
    lock = _InstanceLock(LOCK_FILE)
    acquired, other_pid = lock.acquire()
    if not acquired:
        _log(f"[overlay] уже запущен другой экземпляр (pid {other_pid}) — выходим")
        print(f"[overlay] Уже запущен другой экземпляр оверлея (PID {other_pid}).")
        return 0

    app = QApplication(sys.argv)
    app.setApplicationName("Курымдык — оверлей")
    app.setQuitOnLastWindowClosed(True)

    overlay = PlayerOverlay()
    overlay.show()
    # окно не должно забирать фокус у игры
    overlay.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    overlay.raise_()
    # при выходе из приложения гасим поток, иначе Qt ругается на живой QThread
    app.aboutToQuit.connect(overlay.close)

    _log(f"[overlay] запущен: pyoverlaykit={'да' if HAS_KIT else 'нет'}")
    _log(f"[overlay] состояние: {API_STATE} (опрос {POLL_MS} мс)")
    _log(f"[overlay] команды:   {API_CMD}/<play|pause|next|prev|seek>")
    _log(f"[overlay] лог:       {LOG_FILE}")
    try:
        return app.exec()
    finally:
        try:
            overlay.worker.stop()
            overlay.worker.wait(2000)
        except Exception:
            pass
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
