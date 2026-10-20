# -*- coding: utf-8 -*-
"""Абсолютный троттлинг «шумных» эндпоинтов + диагностика спамера.

Зачем это нужно
----------------
Браузер и оверлей опрашивают сервер постоянно. Даже с клиентским
троттлингом (1 запрос в секунду) любой другой клиент — старый кэш
app.js в service worker, лишняя вкладка, зависший скрипт — способен
завалить сервер тысячами запросов в секунду.

Здесь стоит защита с трёх сторон:

1. ``RequestGuard`` — жёсткий предел «не чаще N секунд на клиента»
   для горячих эндпоинтов. Лишние обновления состояния отбрасываются
   (клиенту возвращается 200, чтобы он не повторял запрос).
2. ``HotAccessLogFilter`` — не даёт uvicorn писать тысячи строк
   access-лога: вместо этого раз в 10 секунд печатается одна
   сводная строка «сколько запросов от кого».
3. Диагностика — ``GET /api/diagnostics/requests`` показывает, кто
   именно шлёт спам (адрес клиента + User-Agent + частота).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from typing import Any, Iterable

# Эндпоинты, которые клиенты опрашивают постоянно.
HOT_PATHS: tuple[str, ...] = ("/api/now-playing", "/api/player/state")

DEFAULT_MIN_INTERVAL = 1.0     # не чаще 1 обновления состояния в секунду
DEFAULT_WINDOW = 10.0          # окно подсчёта частоты, секунды
DEFAULT_WARN_RATE = 3.0        # выше этого (запросов/с) считаем спамом


class RequestGuard:
    """Счётчик и троттлинг запросов по паре (путь, клиент)."""

    def __init__(
        self,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        window: float = DEFAULT_WINDOW,
        warn_rate: float = DEFAULT_WARN_RATE,
    ) -> None:
        self.min_interval = min_interval
        self.window = window
        self.warn_rate = warn_rate
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._last_allowed: dict[tuple[str, str], float] = {}
        self._dropped = 0
        self._total = 0

    # ---------- служебное ----------
    @staticmethod
    def client_key(request: Any) -> str:
        """Ключ клиента: адрес + начало User-Agent (кто шлёт запрос).

        Порт не включаем: каждое соединение открывает новый порт, и без
        этого один и тот же клиент распадался бы на сотни «разных».
        """
        host = "?"
        try:
            host = request.client.host if request.client else "?"
        except Exception:
            pass
        agent = ""
        try:
            agent = (request.headers.get("user-agent") or "")[:48]
        except Exception:
            pass
        return f"{host} [{agent or 'нет UA'}]"

    @staticmethod
    def normalize_client(client: str) -> str:
        """Убираем порт: 127.0.0.1:52344 → 127.0.0.1 (для группировки)."""
        text = str(client or "").strip()
        if not text:
            return "?"
        if text.startswith("["):  # IPv6 [::1]:port
            head, _, tail = text.partition("]")
            return head + "]" if not tail else head + "]"
        if text.count(":") == 1:
            return text.split(":")[0]
        return text

    def _prune(self, now: float) -> None:
        for key in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window * 3]:
            self._hits.pop(key, None)
            self._last_allowed.pop(key, None)

    # ---------- основное ----------
    def note(self, path: str, client: str, now: float | None = None) -> None:
        """Просто посчитать запрос (без решения о пропуске)."""
        now = time.monotonic() if now is None else now
        key = (path, client)
        queue = self._hits[key]
        while queue and now - queue[0] > self.window:
            queue.popleft()
        queue.append(now)
        self._total += 1

    def check(self, path: str, client: str, now: float | None = None) -> bool:
        """True — запрос можно обрабатывать, False — лишний (дропаем)."""
        now = time.monotonic() if now is None else now
        key = (path, client)
        last = self._last_allowed.get(key)
        if last is not None and now - last < self.min_interval:
            self._dropped += 1
            return False
        self._last_allowed[key] = now
        return True

    def allow(self, path: str, client: str, now: float | None = None) -> bool:
        """Посчитать запрос и сразу решить, пропускать ли его."""
        now = time.monotonic() if now is None else now
        self.note(path, client, now)
        return self.check(path, client, now)

    def count(self, path: str, client: str, now: float | None = None) -> int:
        """Сколько раз клиент дёрнул эндпоинт за окно."""
        now = time.monotonic() if now is None else now
        queue = self._hits.get((path, client)) or deque()
        return sum(1 for t in queue if now - t <= self.window)

    def rate(self, path: str, client: str, now: float | None = None) -> float:
        """Средняя частота запросов в секунду за окно."""
        return self.count(path, client, now) / self.window

    def spammers(self, now: float | None = None) -> list[dict[str, Any]]:
        """Клиенты, которые шлют заметно больше нормы."""
        now = time.monotonic() if now is None else now
        self._prune(now)
        out: list[dict[str, Any]] = []
        for (path, client) in self._hits:
            rate = self.rate(path, client, now)
            if rate > self.warn_rate:
                out.append({
                    "path": path,
                    "client": client,
                    "rate": round(rate, 2),
                    "hits": self.count(path, client, now),
                })
        return sorted(out, key=lambda item: -item["rate"])

    def stats(self) -> dict[str, Any]:
        now = time.monotonic()
        self._prune(now)
        clients = []
        for (path, client) in self._hits:
            clients.append({
                "path": path,
                "client": client,
                "rate": round(self.rate(path, client, now), 2),
                "hits": self.count(path, client, now),
            })
        clients.sort(key=lambda item: -item["rate"])
        return {
            "ok": True,
            "min_interval": self.min_interval,
            "window": self.window,
            "total": self._total,
            "dropped": self._dropped,
            "clients": clients,
            "spammers": self.spammers(now),
        }

    def reset(self) -> None:
        self._hits.clear()
        self._last_allowed.clear()
        self._dropped = 0
        self._total = 0


class HotAccessLogFilter(logging.Filter):
    """Сводит тысячи строк access-лога в одну сводку.

    uvicorn пишет по строке на каждый запрос. Для горячих эндпоинтов
    эти строки и есть «тысячи запросов в секунду» в логе. Фильтр их
    гасит и копит счётчики, а ``take_summary()`` отдаёт накопленное.
    """

    def __init__(self, guard: RequestGuard, hot_paths: Iterable[str] = HOT_PATHS) -> None:
        super().__init__()
        self.guard = guard
        self.hot_paths = tuple(hot_paths)
        self._summary: dict[tuple[str, str], int] = defaultdict(int)
        self._suppressed = 0

    @staticmethod
    def _parse(record: logging.LogRecord) -> tuple[str, str]:
        """Достаём (клиент, путь) из записи access-лога uvicorn.

        Формат аргументов менялся между версиями uvicorn:
          * 0.54+:  (client, method, path, http_version, status)
          * старый: (client, 'GET /path HTTP/1.1', status)
        """
        client = "?"
        path = ""
        args = getattr(record, "args", None)
        if isinstance(args, tuple) and len(args) >= 4:
            client = str(args[0])
            path = str(args[2])
        elif isinstance(args, tuple) and len(args) >= 2:
            client = str(args[0])
            request_line = str(args[1])
            parts = request_line.split()
            if len(parts) >= 2:
                path = parts[1]
        if not path:
            path = str(getattr(record, "path", "") or "")
        if not client or client == "None":
            client = "?"
        return RequestGuard.normalize_client(client), path

    def filter(self, record: logging.LogRecord) -> bool:
        client, path = self._parse(record)
        if path not in self.hot_paths:
            return True
        self._suppressed += 1
        self._summary[(path, client)] += 1
        self.guard.note(path, client)
        return False  # строку в лог не пропускаем

    def take_summary(self) -> list[tuple[str, str, int]]:
        """Забрать накопленную сводку и обнулить счётчики."""
        data = [(path, client, count) for (path, client), count in self._summary.items() if count]
        self._summary.clear()
        return sorted(data, key=lambda item: -item[2])

    @property
    def suppressed(self) -> int:
        return self._suppressed
