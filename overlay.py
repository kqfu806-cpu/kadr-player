# -*- coding: utf-8 -*-
"""Оверлей «Dynamic Island» для Курымдык.

Отдельное приложение (PyQt6 + pyoverlaykit): frameless-окно поверх игр —
Always-on-Top, без рамки, без фокуса и без панели задач. Показывает текущий
трек, прогресс и даёт управление плеером (play/pause/next/prev/seek).

Данные: GET  http://127.0.0.1:8000/api/now-playing   (опрос каждые 500 мс)
Команды: POST http://127.0.0.1:8000/api/player/<play|pause|next|prev|seek>
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
import urllib.error
import urllib.request

from PyQt6.QtCore import QObject, QPoint, QRectF, QRunnable, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "overlay.log")


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
POLL_MS = 500               # период опроса состояния плеера

ACCENT = "#ff6b9d"
TEXT = "#ffffff"
MUTED = "#9aa0aa"
BG = QColor(20, 20, 30, 191)          # rgba(20, 20, 30, 0.75)
BORDER = QColor(255, 107, 157, 190)   # тонкая неоновая рамка
RADIUS = 20

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


class _Signals(QObject):
    state = pyqtSignal(dict)
    cover = pyqtSignal(bytes)
    failed = pyqtSignal()


class _PollTask(QRunnable):
    """Фоновый опрос состояния плеера (не блокирует GUI)."""

    def __init__(self, signals: _Signals, last_cover: str):
        super().__init__()
        self.signals = signals
        self.last_cover = last_cover

    def run(self) -> None:
        try:
            with urllib.request.urlopen(API_STATE, timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            # важно: сигнал ошибки всегда эмитится, иначе опрос встанет
            self.signals.failed.emit()
            return
        if not isinstance(data, dict):
            self.signals.failed.emit()
            return
        self.signals.state.emit(data)
        cover = str((data.get("track") or {}).get("cover") or "")
        if cover and cover != self.last_cover:
            try:
                with urllib.request.urlopen(cover, timeout=3) as resp:
                    self.signals.cover.emit(resp.read())
            except Exception:
                pass


class _CommandTask(QRunnable):
    """Отправка команды плееру (POST /api/player/<command>)."""

    def __init__(self, command: str, value=None):
        super().__init__()
        self.command = command
        self.value = value

    def run(self) -> None:
        body = b"{}"
        if self.command == "seek" and self.value is not None:
            try:
                body = json.dumps({"position": float(self.value)}).encode("utf-8")
            except (TypeError, ValueError):
                body = b"{}"
        try:
            request = urllib.request.Request(
                f"{API_CMD}/{self.command}",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=3):
                pass
        except Exception:
            pass


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
        # базовый класс кита ставит WA_TransparentForMouseEvents — нам нужны кнопки
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        try:
            self.set_close_button_visibility(False)
        except Exception:
            pass

        self.setWindowTitle("Курымдык — оверлей")
        self.setFixedSize(COLLAPSED_W, COLLAPSED_H)

        self._expanded = False
        self._press_pos: QPoint | None = None
        self._cover_url = ""
        self._last_state: dict = {}
        self._poll_busy = False

        self._build_ui()

        self.signals = _Signals()
        self.signals.state.connect(self._on_state)
        self.signals.cover.connect(self._on_cover)
        self.signals.failed.connect(self._on_poll_failed)

        self.pool = QThreadPool.globalInstance()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(POLL_MS)

        self._place()
        self._make_topmost()

    # ---------------- интерфейс ----------------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(6)

        # --- строка островка: обложка + текст/прогресс + транспорт ---
        row = QHBoxLayout()
        row.setSpacing(10)

        self.cover = QLabel(self)
        self.cover.setFixedSize(60, 60)
        self.cover.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.cover.setStyleSheet("background: transparent;")
        row.addWidget(self.cover, 0, Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        self.title = ElidedLabel("Курымдык", 10, TEXT, self)
        self.artist = ElidedLabel("нет трека", 8, MUTED, self)
        for label in (self.title, self.artist):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        text_col.addWidget(self.title)
        text_col.addWidget(self.artist)

        # тонкий прогресс-бар (виден и в свёрнутом, и в раскрытом виде)
        self.progress = QProgressBar(self)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
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
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)

        self.btn_prev.clicked.connect(lambda: self._command("prev"))
        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_next.clicked.connect(lambda: self._command("next"))

        outer.addLayout(row)

        # --- блок раскрытого состояния ---
        self.extra = QWidget(self)
        self.extra.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        extra_layout = QVBoxLayout(self.extra)
        extra_layout.setContentsMargins(0, 4, 0, 0)
        extra_layout.setSpacing(8)

        self.album = ElidedLabel("", 8, MUTED, self.extra)
        self.album.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        extra_layout.addWidget(self.album)
        extra_layout.addStretch(1)  # время и перемотка прижаты к низу карточки

        times = QHBoxLayout()
        self.time_now = QLabel("0:00", self.extra)
        self.time_all = QLabel("0:00", self.extra)
        for label in (self.time_now, self.time_all):
            label.setStyleSheet(f"color: {MUTED}; font-size: 10px; background: transparent;")
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
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

    def _button(self, text: str, tooltip: str, parent, glyph: bool = False) -> QPushButton:
        button = QPushButton(text, parent)
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if glyph:
            button.setFont(_font(11, glyph=True))
        button.setStyleSheet(
            "QPushButton{background:rgba(255,255,255,0.10);color:#ffffff;border:none;"
            "border-radius:15px;font-size:13px;}"
            "QPushButton:hover{background:rgba(255,107,157,0.35);}"
            "QPushButton:pressed{background:rgba(255,107,157,0.55);}"
        )
        return button

    # ---------------- отрисовка ----------------
    def paintEvent(self, event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(1.5, 1.5, self.width() - 3, self.height() - 3)

        # мягкая тень
        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(8, 0, -1):
            shadow = QRectF(
                rect.x() - i, rect.y() - i + 3, rect.width() + 2 * i, rect.height() + 2 * i
            )
            path = QPainterPath()
            path.addRoundedRect(shadow, RADIUS + i, RADIUS + i)
            painter.setBrush(QColor(0, 0, 0, 6))
            painter.drawPath(path)

        # корпус: полупрозрачный фон + неоновая рамка
        path = QPainterPath()
        path.addRoundedRect(rect, RADIUS, RADIUS)
        painter.setBrush(BG)
        painter.setPen(QPen(BORDER, 1.0))
        painter.drawPath(path)
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
        self._expanded = bool(expanded)
        if self._expanded:
            self.setFixedSize(EXPANDED_W, EXPANDED_H)
            self.extra.setVisible(True)
        else:
            self.setFixedSize(COLLAPSED_W, COLLAPSED_H)
            self.extra.setVisible(False)
        self._place()
        self._make_topmost()

    def toggle_expanded(self) -> None:
        self.set_expanded(not self._expanded)

    # ---------------- клики и перетаскивание ----------------
    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.globalPosition().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt API)
        moved = 0
        if self._press_pos is not None:
            moved = (event.globalPosition().toPoint() - self._press_pos).manhattanLength()
            self._press_pos = None
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton and moved <= 6:
            self.toggle_expanded()

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
            HWND_TOPMOST = -1
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(
                hwnd,
                GWL_EXSTYLE,
                style | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
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
    def _poll(self) -> None:
        if self._poll_busy:
            return
        self._poll_busy = True
        try:
            self.pool.start(_PollTask(self.signals, self._cover_url))
        except Exception:
            self._poll_busy = False

    def _on_poll_failed(self) -> None:
        """Сервер не ответил — снимаем блокировку, чтобы опрос продолжился."""
        self._poll_busy = False

    def _on_state(self, data: dict) -> None:
        self._poll_busy = False
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
        # скруглённая обложка 60x60
        rounded = QPixmap(60, 60)
        rounded.fill(Qt.GlobalColor.transparent)
        painter = QPainter(rounded)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, 60, 60), 12, 12)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, pixmap.scaled(
            60, 60, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        ))
        painter.end()
        self.cover.setPixmap(rounded)

    # ---------------- команды ----------------
    def _command(self, command: str, value=None) -> None:
        try:
            self.pool.start(_CommandTask(command, value))
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
    app = QApplication(sys.argv)
    app.setApplicationName("Курымдык — оверлей")
    app.setQuitOnLastWindowClosed(True)

    overlay = PlayerOverlay()
    overlay.show()
    # окно не должно забирать фокус у игры
    overlay.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    overlay.raise_()

    _log(f"[overlay] запущен: pyoverlaykit={'да' if HAS_KIT else 'нет'}")
    _log(f"[overlay] состояние: {API_STATE}")
    _log(f"[overlay] команды:   {API_CMD}/<play|pause|next|prev|seek>")
    _log(f"[overlay] лог:       {LOG_FILE}")
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
