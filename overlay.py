# -*- coding: utf-8 -*-
"""Оверлей «Dynamic Island» для Курымдык.

Отдельное приложение (PyQt6): frameless-окно поверх игр — Always-on-Top,
без рамки, без фокуса и без панели задач. Показывает текущий трек, прогресс
и даёт управление плеером (play/pause/next/prev/seek).

Архитектура:
  * GUI-поток ничего не блокирует: ни time.sleep(), ни сетевых запросов;
  * опрос состояния живёт в отдельном потоке PollWorker(QThread), GUI только
    слушает сигналы state_updated / cover_updated / poll_failed;
  * команды плеера уходят асинхронно через QNetworkAccessManager (событийная
    модель Qt — сетевого ожидания в GUI-потоке нет вовсе);
  * частота опроса фиксирована QTimer'ом (по умолчанию 1000 мс — 1 запрос/с,
    диапазон 500..1000 мс через KADR_OVERLAY_POLL_MS); в потоке стоит жёсткий
    пол 1 запрос/с, после клика по кнопке — один ускоренный опрос;
  * защита от двойного запуска — файловая блокировка cache/overlay.lock;
  * ВСЕ сообщения пишутся в cache/overlay.log (logging.FileHandler).

Почему окно НЕ наследуется от pyoverlaykit.Overlay:
  базовый класс кита держит собственное перетаскивание и сбрасывает его
  состояние только в mouseReleaseEvent. Любой ранний выход из нашего
  mouseReleaseEvent оставлял флаг «окно едет за курсором» — и дальше
  ЛЮБОЕ движение мыши над окном уводило его за курсором (GUI-поток
  захлёбывался, курсор «загрузка», клики не доходили). Поэтому здесь
  обычный QWidget + стандартная обработка событий Qt.

Данные: GET  http://127.0.0.1:8000/api/now-playing
Команды: POST http://127.0.0.1:8000/api/player/<play|pause|next|prev|seek>
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request

from PyQt6.QtCore import QObject, QPoint, QRectF, QThread, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkProxyFactory,
    QNetworkReply,
    QNetworkRequest,
)
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

BUILD = "2026-10-21-std-buttons"

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "overlay.log")
LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "overlay.lock")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "overlay_state.json")

# Системный прокси Windows не должен перехватывать обращения к 127.0.0.1
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# ...и для QNetworkAccessManager тоже (иначе POST /api/player/* уйдёт в прокси)
QNetworkProxyFactory.setUseSystemConfiguration(False)

# ---- глобальная горячая клавиша (Win32 RegisterHotKey, без хуков) ----
# Почему не «keyboard»: библиотека ставит глобальный low-level хук (LLKHF),
# он требует прав администратора и конфликтует с античитом игр (EAC/BattlEye).
# RegisterHotKey — штатный API Windows: без хуков, без админа, работает, когда
# фокус в игре (нужно для borderless-режима).
HOTKEY_ID = 0x0B01
HOTKEY_LABEL = "Ctrl+Shift+O"
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000
HOTKEY_MODIFIERS = MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT
HOTKEY_VK = ord("O")

# ---- логирование: ВСЁ пишется в cache/overlay.log (logging.FileHandler) ----
LOGGER = logging.getLogger("kadr.overlay")
LOGGER.setLevel(logging.INFO)
LOGGER.propagate = False


def _setup_logging() -> None:
    """Поднимаем FileHandler на cache/overlay.log (в консоль выводит print())."""
    if LOGGER.handlers:
        return
    formatter = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(formatter)
        LOGGER.addHandler(file_handler)
    except Exception:
        pass
    # в консоль выводит print() (в файле — FileHandler), чтобы не было дублей


_setup_logging()


def _log(message: str) -> None:
    """Пишем в cache/overlay.log (logging.FileHandler).

    В консоль дублируем явно через print() — так сообщение видно и в терминале,
    и в файле, и ничего не печатается дважды.
    """
    LOGGER.info(message)


def _save_position(x: int, y: int) -> None:
    """Запоминаем позицию островка — при следующем запуске он вернётся на место."""
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        payload = {
            "x": int(x),
            "y": int(y),
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as exc:
        _log(f"[overlay] позицию сохранить не удалось: {exc}")


def _load_position() -> tuple[int, int] | None:
    """Читаем сохранённую позицию; None — её нет или файл битый."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        x = int(data["x"])
        y = int(data["y"])
        return x, y
    except FileNotFoundError:
        return None
    except Exception as exc:
        _log(f"[overlay] сохранённую позицию прочитать не удалось: {exc}")
        return None


def _say(message: str) -> None:
    """Сообщение в консоль (print) и в cache/overlay.log (_log)."""
    try:
        print(message, flush=True)
    except Exception:
        pass  # под pythonw stdout может быть None
    LOGGER.info(message)


# ---------- базовый класс ----------
# Раньше оверлей наследовался от PyOverlayKit.Overlay. У кита своя реализация
# перетаскивания, которая сбрасывалась только в mouseReleaseEvent; наш ранний
# выход из mouseReleaseEvent ломал этот сброс — окно начинало «уезжать» за
# курсором, GUI-поток захлёбывался и клики не доходили. Работаем на голом QWidget.
HAS_KIT = False

# ---------- настройки ----------
HOST = "127.0.0.1"
PORT = 8000
BASE = f"http://{HOST}:{PORT}"
API_STATE = f"{BASE}/api/now-playing"
API_CMD = f"{BASE}/api/player"

def _env_int(name: str, default: int, low: int, high: int) -> int:
    """Число из переменной окружения с жёсткими границами (неверное значение → default)."""
    try:
        value = int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


COLLAPSED_W, COLLAPSED_H = 400, 84
GHOST_OPACITY = 0.4          # свёрнутый оверлей — полупрозрачный («призрак»)
SOLID_OPACITY = 1.0          # под курсором / раскрытый / в фокусе — яркий
EXPANDED_W, EXPANDED_H = 400, 400
BOTTOM_MARGIN = 40          # отступ от нижнего края экрана, px
# опрос состояния: 1 GET /api/now-playing в секунду (можно переопределить: 500..1000 мс)
POLL_MS = _env_int("KADR_OVERLAY_POLL_MS", 1000, 500, 1000)
FAST_POLL_MS = 250          # один ускоренный опрос сразу после команды пользователя
POLL_MIN_INTERVAL = 1.0     # абсолютный пол: не чаще 1 GET /api/now-playing в секунду
MAX_POLL_FAILURES = 5       # 5 неудачных опросов подряд (5 c) — закрываем оверлей
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
        self._last_cover = ""
        self._last_poll = 0.0
        self._force_poll = False

    # --- вызовы из GUI-потока (потокобезопасные) ---
    def request_poll(self, force: bool = False) -> None:
        """Просит один опрос состояния (вызывается из QTimer в GUI).

        force=True — единовременный опрос по команде пользователя:
        он обходит абсолютный пол, но не меняет регулярную частоту.
        """
        if force:
            self._force_poll = True
        self._poll_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._poll_event.set()

    # --- работа в отдельном потоке ---
    def run(self) -> None:  # noqa: D102
        while not self._stop_event.is_set():
            # ждём сигнала от GUI-таймера (1000 мс) или команды остановки
            self._poll_event.wait(timeout=0.2)
            if self._stop_event.is_set():
                break
            if not self._poll_event.is_set():
                continue
            self._poll_event.clear()
            self._do_poll()

    def _do_poll(self) -> None:
        # Абсолютный пол: регулярных GET /api/now-playing не больше одного в секунду,
        # сколько бы раз GUI ни дёрнул request_poll(). Единственное исключение —
        # один форсированный опрос сразу после команды пользователя.
        now = time.monotonic()
        if not self._force_poll:
            wait = POLL_MIN_INTERVAL - (now - self._last_poll)
            if wait > 0:
                time.sleep(wait)
        self._force_poll = False
        self._last_poll = time.monotonic()
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


class PlayerOverlay(QWidget):
    """Островок: свёрнут 400x84, по клику разворачивается до 400x400."""

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
        # окно и все кнопки ОБЯЗАНЫ получать мышь (иначе клики проходят сквозь)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)

        self.setWindowTitle("Курымдык — оверлей")
        self.setFixedSize(COLLAPSED_W, COLLAPSED_H)

        self._expanded = False
        self._press_pos: QPoint | None = None
        self._drag_offset = QPoint(0, 0)
        self._dragging = False
        self._user_moved = False
        self._cover_url = ""
        self._hovered = False
        self._click_through = False
        self._last_state: dict = {}
        self._poll_failures = 0
        self._poll_pending = False

        self._build_ui()

        # --- сеть: команды уходят асинхронно, GUI-поток не ждёт сеть ---
        # QNetworkAccessManager работает по событийной модели Qt: post() мгновенно
        # возвращает QNetworkReply, блокировки нет.
        self.network_manager = QNetworkAccessManager(self)

        # --- глобальная горячая клавиша: Ctrl+Shift+O — режим «сквозь» ---
        self._hotkey_registered = False
        self._register_hotkey()

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

        # одиночный ускоренный опрос после команды пользователя (частоту не меняет)
        self._fast_poll_timer = QTimer(self)
        self._fast_poll_timer.setSingleShot(True)
        self._fast_poll_timer.timeout.connect(lambda: self._tick(True))

        self._place()
        self._restore_position()
        self._make_topmost()
        self._apply_opacity()
        self._log_layout_diagnostics()

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
        # обложка не должна перехватывать клики (иначе не работает перетаскивание/сворачивание)
        self.cover.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
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
            # текст не перехватывает мышь — клик по островку сворачивает/разворачивает его
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
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

        # --- транспорт: отдельный горизонтальный ряд [⏮][▶][⏭] справа от текста ---
        # Раньше кнопки создавались, но не добавлялись ни в одну раскладку —
        # они лежали друг на друге в левом верхнем углу поверх обложки.
        transport = QHBoxLayout()
        transport.setSpacing(10)
        transport.setContentsMargins(0, 0, 0, 0)

        self.btn_prev = self._button("⏮", "Предыдущий", self, glyph=True)
        self.btn_play = self._button("▶", "Играть / пауза", self, glyph=True)
        self.btn_next = self._button("⏭", "Следующий", self, glyph=True)
        for button in (self.btn_prev, self.btn_play, self.btn_next):
            button.setFixedSize(32, 32)
            # кнопка обязана получать мышь и клавиатуру
            button.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self._round_button(button)
            transport.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)

        # стандартная обработка Qt: клик по QPushButton обрабатывает сама кнопка,
        # событие до окна не доходит — никаких своих hit-test'ов не нужно
        self.btn_prev.clicked.connect(self.on_prev_clicked)
        self.btn_play.clicked.connect(self.on_play_clicked)
        self.btn_next.clicked.connect(self.on_next_clicked)

        row.addLayout(transport, 0)

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
        self.close_btn.clicked.connect(self.on_close_clicked)
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

    def _log_layout_diagnostics(self) -> None:
        """Пишем в cache/overlay.log раскладку — видно, какая сборка запущена."""
        try:
            hit = self.childAt(self.btn_play.geometry().center())
            _say(f"[overlay] сборка {BUILD}")
            _log(
                f"[overlay] окно {self.width()}x{self.height()}, "
                f"кнопки: prev={self.btn_prev.geometry()} "
                f"play={self.btn_play.geometry()} next={self.btn_next.geometry()}"
            )
            _log(
                f"[overlay] виджет под центром Play: "
                f"{'это кнопка Play' if hit is self.btn_play else hit} "
                f"(прозрачность окна для мыши: "
                f"{self.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)})"
            )
        except Exception as exc:
            _log(f"[overlay] диагностика раскладки не удалась: {exc}")

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

    def _restore_position(self) -> None:
        """Возвращаем островок на сохранённое место (если оно ещё на экране)."""
        saved = _load_position()
        if saved is None:
            return
        x, y = saved
        if not self._position_on_screen(x, y):
            _log(
                f"[overlay] сохранённая позиция ({x}, {y}) вне экрана — "
                "ставлю оверлей по умолчанию"
            )
            return
        self.move(x, y)
        self._user_moved = True
        _log(f"[overlay] позиция восстановлена: ({x}, {y})")

    def _position_on_screen(self, x: int, y: int) -> bool:
        """Точка хотя бы на одном экране? (после смены монитора — нет)."""
        try:
            screens = QApplication.screens()
        except Exception:
            screens = []
        for screen in screens:
            geo = screen.availableGeometry()
            if geo.contains(x + 1, y + 1):
                return True
        return False

    def _save_current_position(self) -> None:
        """Сохраняем текущие координаты островка."""
        try:
            _save_position(self.x(), self.y())
        except Exception as exc:
            _log(f"[overlay] позицию сохранить не удалось: {exc}")

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
        # раскрытый оверлей — всегда яркий, свёрнутый — «призрак»
        self._apply_opacity()

    def toggle_expanded(self) -> None:
        self.set_expanded(not self._expanded)

    # ---------------- клики, перетаскивание ----------------
    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Нажатие на саму кнопку сюда не попадает — её обрабатывает QPushButton.
        # Сюда приходит только нажатие на «пустое» место островка.
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.globalPosition().toPoint()
            self._drag_offset = self._press_pos - self.pos()
            self._dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # Перетаскивание — ТОЛЬКО при зажатой левой кнопке.
        # Если кнопку отпустили (или окно потеряло фокус) — состояние сбрасываем,
        # иначе окно может «уехать» за курсором.
        if self._press_pos is not None:
            if not (event.buttons() & Qt.MouseButton.LeftButton):
                self._press_pos = None
                self._dragging = False
            else:
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
            was_dragging = self._dragging
            self._press_pos = None
            self._dragging = False
            if was_dragging:
                # пользователь перетащил островок — запоминаем новое место
                self._save_current_position()
            elif moved <= 6:
                # клик по пустому месту островка — свернуть/развернуть
                self.toggle_expanded()
        super().mouseReleaseEvent(event)

    # ---------------- режим «сквозь» (click-through) ----------------
    def _register_hotkey(self) -> None:
        """Регистрируем глобальную горячую клавишу Ctrl+Shift+O."""
        if os.name != "nt":
            return
        try:
            user32 = ctypes.windll.user32
            if user32.RegisterHotKey(None, HOTKEY_ID, HOTKEY_MODIFIERS, HOTKEY_VK):
                self._hotkey_registered = True
                _log(f"[overlay] горячая клавиша {HOTKEY_LABEL} включена (режим «сквозь»)")
            else:
                _log(
                    f"[overlay] не удалось занять {HOTKEY_LABEL} — "
                    "возможно, клавиша уже занята другой программой"
                )
        except Exception as exc:
            _log(f"[overlay] горячую клавишу зарегистрировать не удалось: {exc}")

    def _unregister_hotkey(self) -> None:
        if os.name != "nt" or not self._hotkey_registered:
            return
        try:
            ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
        except Exception:
            pass
        self._hotkey_registered = False

    def nativeEvent(self, event_type, message):  # noqa: N802 (Qt API)
        """Ловим WM_HOTKEY — сообщение приходит прямо в окно, без фокуса."""
        if os.name == "nt" and bytes(event_type) == b"windows_generic_MSG":
            try:
                import ctypes.wintypes

                msg = ctypes.wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.toggle_click_through()
                    return (True, 0)
            except Exception as exc:
                _log(f"[overlay] ошибка обработки горячей клавиши: {exc}")
        return super().nativeEvent(event_type, message)

    def toggle_click_through(self) -> None:
        """Переключаем режим «сквозь»: клики проходят в игру."""
        self.set_click_through(not self._click_through)

    def set_click_through(self, enabled: bool) -> None:
        """Включает/выключает прозрачность окна для мыши.

        Два уровня:
          * WA_TransparentForMouseEvents — уровень Qt (окно не обрабатывает мышь);
          * WS_EX_TRANSPARENT — уровень Win32 (клики проходят в игру, даже если
            игра в фокусе). Без него borderless-игра не получит клик.
        """
        self._click_through = bool(enabled)
        self.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, self._click_through
        )
        self._apply_win32_style()
        self._apply_opacity()
        if self._click_through:
            _say(f"[overlay] режим «сквозь» ВКЛ ({HOTKEY_LABEL}) — клики уходят в игру")
        else:
            _say(f"[overlay] режим «сквозь» ВЫКЛ ({HOTKEY_LABEL}) — оверлей снова кликабелен")

    # ---------------- режим «призрака» ----------------
    def _apply_opacity(self) -> None:
        """Прозрачность окна: призрак в свёрнутом виде, яркость под курсором.

        В режиме «сквозь» навести мышь нельзя — оставляем полупрозрачным,
        чтобы оверлей не мешал игре и был виден.
        """
        if self._click_through:
            target = GHOST_OPACITY
        elif self._expanded or self._hovered:
            target = SOLID_OPACITY
        else:
            target = GHOST_OPACITY
        try:
            self.setWindowOpacity(target)
        except Exception as exc:
            _log(f"[overlay] прозрачность применить не удалось: {exc}")

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt API)
        """Курсор над оверлеем — делаем его непрозрачным."""
        self._hovered = True
        self._apply_opacity()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt API)
        """Курсор ушёл — снова «призрак» (если оверлей свёрнут)."""
        self._hovered = False
        self._apply_opacity()
        super().leaveEvent(event)

    # ---------------- закрытие ----------------
    def _quit(self) -> None:
        """Кнопка ✕: полностью закрываем приложение."""
        _log("[overlay] закрыт кнопкой")
        self._stop_timers()
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _stop_timers(self) -> None:
        """Гасим все таймеры окна (основной + ускоренный опрос)."""
        for name in ("timer", "_fast_poll_timer"):
            timer = getattr(self, name, None)
            if timer is None:
                continue
            try:
                timer.stop()
            except Exception:
                pass

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        # гасим таймер и фоновый поток — иначе процесс не завершится
        self._save_current_position()
        self._unregister_hotkey()
        self._stop_timers()
        try:
            self.worker.stop()
            if not self.worker.wait(800):
                self.worker.terminate()
                self.worker.wait(300)
        except Exception:
            pass
        super().closeEvent(event)

    # ---------------- Win32: Always-on-Top + AOD ----------------
    def _apply_win32_style(self) -> None:
        """Always-on-Top + (по режиму «сквозь») WS_EX_TRANSPARENT.

        WS_EX_TRANSPARENT на уровне Win32 нужен для borderless-игры: окно
        остаётся видимым, но клики проходят насквозь, в игру.
        """
        if os.name != "nt":
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TRANSPARENT = 0x00000020
            HWND_TOPMOST = -1
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            if self._click_through:
                style |= WS_EX_TRANSPARENT
            else:
                style &= ~WS_EX_TRANSPARENT
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
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
            _log(f"[overlay] не удалось применить стиль окна: {exc}")

    def _make_topmost(self) -> None:
        self._apply_win32_style()

    # ---------------- данные ----------------
    def _tick(self, force: bool = False) -> None:
        """Единственный источник запросов: QTimer, 1000 мс, без блокировок."""
        if self._poll_pending and not force:
            return
        self._poll_pending = True
        self.worker.request_poll(force=force)

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
    def on_play_clicked(self) -> None:
        """Кнопка ▶/⏸: играть или пауза."""
        print("[overlay] Кнопка Play нажата!")
        _log("[overlay] Кнопка Play нажата!")
        action = "pause" if self._last_state.get("playing") else "play"
        self._send_command(action)

    def on_next_clicked(self) -> None:
        """Кнопка ⏭: следующий трек."""
        print("[overlay] Кнопка Next нажата!")
        _log("[overlay] Кнопка Next нажата!")
        self._send_command("next")

    def on_prev_clicked(self) -> None:
        """Кнопка ⏮: предыдущий трек."""
        print("[overlay] Кнопка Prev нажата!")
        _log("[overlay] Кнопка Prev нажата!")
        self._send_command("prev")

    def on_close_clicked(self) -> None:
        """Кнопка ✕: закрыть оверлей."""
        print("[overlay] Кнопка Close нажата!")
        _log("[overlay] Кнопка Close нажата!")
        self._quit()

    def _send_command(self, command: str, value=None) -> None:
        """POST /api/player/<command> — асинхронно, GUI-поток не ждёт сеть.

        QNetworkAccessManager работает по событийной модели Qt: post() сразу
        возвращает QNetworkReply, блокирующего вызова (requests.post и т.п.)
        в обработчике кнопки нет.
        """
        url = f"{API_CMD}/{command}"
        body = b"{}"
        if command == "seek" and value is not None:
            try:
                body = json.dumps({"position": float(value)}).encode("utf-8")
            except (TypeError, ValueError):
                body = b"{}"
        request = QNetworkRequest(QUrl(url))
        request.setHeader(
            QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json"
        )
        print(f"[overlay] Отправлен POST на сервер: {url}")
        _log(f"[overlay] Отправлен POST на сервер: {url}")
        try:
            reply = self.network_manager.post(request, body)
        except Exception as exc:
            _log(f"[overlay] команда {command} не отправлена: {exc}")
            return
        reply.finished.connect(lambda r=reply, c=command: self._on_command_finished(r, c))
        # после команды сразу обновляем карточку — один дополнительный запрос
        self._schedule_fast_poll()

    def _on_command_finished(self, reply, command: str) -> None:
        """Ответ сервера на команду пришёл — пишем код в лог."""
        status = "?"
        try:
            status = reply.attribute(
                QNetworkRequest.Attribute.HttpStatusCodeAttribute
            )
        except Exception:
            pass
        try:
            error = reply.error()
            if error != QNetworkReply.NetworkError.NoError:
                status = f"{status} (ошибка: {error.name})"
        except Exception:
            pass
        _log(f"[overlay] POST /api/player/{command} → {status}")
        try:
            reply.deleteLater()
        except Exception:
            pass

    def _schedule_fast_poll(self) -> None:
        """Один ускоренный опрос состояния (после клика по кнопке)."""
        timer = getattr(self, "_fast_poll_timer", None)
        if timer is None:
            return
        try:
            timer.start(FAST_POLL_MS)
        except Exception:
            pass

    def _seek_relative(self, offset: float) -> None:
        try:
            position = float(self._last_state.get("position") or 0) + offset
        except (TypeError, ValueError):
            position = 0.0
        self._send_command("seek", max(0.0, position))


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

    _say(f"[overlay] запущен: сборка {BUILD} (базовый класс — QWidget, кнопки — стандартные QPushButton)")
    _say(f"[overlay] состояние: {API_STATE} (опрос {POLL_MS} мс)")
    _say(f"[overlay] команды:   {API_CMD}/<play|pause|next|prev|seek>")
    _say(f"[overlay] лог:       {LOG_FILE}")
    _say(f"[overlay] позиция:   {STATE_FILE}")
    _say(f"[overlay] горячая клавиша: {HOTKEY_LABEL} — режим «сквозь» (клики в игру)")
    try:
        return app.exec()
    finally:
        try:
            overlay.worker.stop()
            if not overlay.worker.wait(800):
                overlay.worker.terminate()
                overlay.worker.wait(300)
        except Exception:
            pass
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
