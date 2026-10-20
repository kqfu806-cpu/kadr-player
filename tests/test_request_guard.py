# -*- coding: utf-8 -*-
"""Тесты абсолютного троттлинга и диагностики «шумных» эндпоинтов."""
from __future__ import annotations

import logging
import time

from backend.request_guard import HOT_PATHS, HotAccessLogFilter, RequestGuard


def test_guard_drops_duplicates_within_interval():
    guard = RequestGuard(min_interval=1.0, window=10.0)
    client = "127.0.0.1 [test]"
    assert guard.allow("/api/player/state", client, now=0.0) is True
    assert guard.allow("/api/player/state", client, now=0.2) is False
    assert guard.allow("/api/player/state", client, now=0.9) is False
    assert guard.allow("/api/player/state", client, now=1.1) is True
    assert guard.stats()["dropped"] == 2


def test_guard_separates_clients():
    guard = RequestGuard(min_interval=1.0, window=10.0)
    assert guard.allow("/api/player/state", "a", now=0.0) is True
    assert guard.allow("/api/player/state", "b", now=0.0) is True


def test_guard_detects_spammer():
    guard = RequestGuard(min_interval=0.0, window=10.0, warn_rate=3.0)
    for i in range(100):
        guard.allow("/api/now-playing", "127.0.0.1 [Chrome]", now=i * 0.01)
    spammers = guard.spammers(now=1.0)
    assert spammers, "спамер должен быть найден"
    assert spammers[0]["client"] == "127.0.0.1 [Chrome]"
    assert spammers[0]["rate"] > 3.0


def test_stats_shape():
    guard = RequestGuard(min_interval=1.0, window=10.0)
    now = time.monotonic()
    guard.allow("/api/now-playing", "127.0.0.1 [X]", now=now)
    stats = guard.stats()
    assert stats["ok"] is True
    assert stats["total"] == 1
    assert any(c["path"] == "/api/now-playing" for c in stats["clients"])


def test_normalize_client_drops_port():
    assert RequestGuard.normalize_client("127.0.0.1:52344") == "127.0.0.1"
    assert RequestGuard.normalize_client("127.0.0.1") == "127.0.0.1"
    assert RequestGuard.normalize_client("") == "?"


def test_access_filter_suppresses_hot_lines_and_counts():
    guard = RequestGuard()
    filt = HotAccessLogFilter(guard)
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s" %d',
        args=("127.0.0.1:52344", "GET", "/api/now-playing", "1.1", 200),
        exc_info=None,
    )
    assert filt.filter(record) is False, "горячая строка не должна попадать в лог"
    assert filt.suppressed == 1
    summary = filt.take_summary()
    assert summary and summary[0][0] == "/api/now-playing"
    assert summary[0][2] == 1
    assert filt.take_summary() == [], "сводка должна обнуляться после чтения"


def test_access_filter_passes_other_endpoints():
    guard = RequestGuard()
    filt = HotAccessLogFilter(guard)
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s" %d',
        args=("127.0.0.1:52344", "GET", "/api/library", "1.1", 200),
        exc_info=None,
    )
    assert filt.filter(record) is True, "обычные эндпоинты логируем как раньше"


def test_access_filter_supports_old_uvicorn_format():
    guard = RequestGuard()
    filt = HotAccessLogFilter(guard)
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s" %d',
        args=("127.0.0.1:52344", "GET /api/now-playing HTTP/1.1", 200),
        exc_info=None,
    )
    assert filt.filter(record) is False, "старый формат тоже должен распознаваться"


def test_hot_paths_contains_both():
    assert "/api/now-playing" in HOT_PATHS
    assert "/api/player/state" in HOT_PATHS
