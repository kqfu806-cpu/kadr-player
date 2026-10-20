"""Общие фикстуры тестов.

Модульный кэш ответов Deezer/iTunes (backend/releases.py) живёт между вызовами
по назначению — в тестах его нужно сбрасывать, иначе один тест получает
данные, закэшированные другим. Заодно отключаем паузу троттлинга, чтобы
suite не тратила по секунде на каждый мокнутый запрос.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_release_cache(monkeypatch: pytest.MonkeyPatch):
    from backend import releases

    releases._response_cache.clear()
    monkeypatch.setattr(releases, "REQUEST_DELAY", 0.0)
    yield
    releases._response_cache.clear()
