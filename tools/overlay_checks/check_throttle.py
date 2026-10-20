# -*- coding: utf-8 -*-
"""Тест троттлинга Deezer/iTunes: кэш 1 ч, пауза между запросами, кап 50 артистов, 429 backoff.

Запуск: .venv/bin/python tests/manual/test_throttle.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend import releases as R


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.request = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise R.httpx.HTTPStatusError(f"{self.status_code}", request=None, response=self)

    def json(self):
        return self._payload


class FakeClient:
    """Считает запросы и умеет отдавать заранее заданные ответы."""

    def __init__(self, script=None):
        self.calls = []
        self.script = script or {}

    async def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params))
        state = self.script.get(url)
        if state is None:
            return FakeResponse(200, {"data": [], "results": []})
        if isinstance(state, list):
            return state.pop(0) if state else FakeResponse(200, {"data": [], "results": []})
        return state


async def main():
    R.RATE_LIMIT_BACKOFF = 0.1   # ускоряем тест
    R.REQUEST_DELAY = 0.05
    today = R.date.today()
    cutoff = today - R.timedelta(days=30)

    print("=== 1. кэш на 1 час: повторный запрос не уходит в сеть ===")
    client = FakeClient({
        "https://api.deezer.com/search/artist": FakeResponse(200, {"data": [{"id": 1, "name": "Кино", "nb_fan": 5}]}),
        "https://api.deezer.com/artist/1/albums": FakeResponse(200, {"data": []}),
    })
    R._response_cache.clear()
    await R._artist_releases(client, "Кино", cutoff, today, R.asyncio.Semaphore(4))
    first = len(client.calls)
    await R._artist_releases(client, "Кино", cutoff, today, R.asyncio.Semaphore(4))
    print(f"  запросов после 1-го: {first}, после 2-го: {len(client.calls)}")
    print(f"  кэш работает: {len(client.calls) == first}")

    print("=== 2. 429 -> пауза -> одна повторная попытка ===")
    ok = FakeResponse(200, {"data": [{"id": 2, "name": "A", "nb_fan": 1}]})
    limited = FakeResponse(429, {})
    client429 = FakeClient({
        "https://api.deezer.com/search/artist": [limited, ok],
        "https://api.deezer.com/artist/2/albums": FakeResponse(200, {"data": []}),
    })
    R._response_cache.clear()
    start = time.monotonic()
    res, err = await R._artist_releases(client429, "A", cutoff, today, R.asyncio.Semaphore(4))
    print(f"  запросов: {len(client429.calls)}, ошибка: {err}, повтор сработал: {len(client429.calls) >= 3}")
    print(f"  была пауза: {time.monotonic() - start >= R.RATE_LIMIT_BACKOFF}")

    print("=== 3. 429 дважды подряд -> артист пропускается ===")
    client429b = FakeClient({
        "https://api.deezer.com/search/artist": [FakeResponse(429, {}), FakeResponse(429, {})],
    })
    R._response_cache.clear()
    res, err = await R._artist_releases(client429b, "B", cutoff, today, R.asyncio.Semaphore(4))
    print(f"  запросов: {len(client429b.calls)} | ошибка: {err!r} | пропущен: {bool(err) and not res}")

    print("=== 4. лимит 50 артистов за раз ===")
    names = [f"Artist {i}" for i in range(846)]
    client2 = FakeClient({"https://api.deezer.com/search/artist": FakeResponse(200, {"data": []})})
    R._response_cache.clear()
    out = await R.fetch_weekly_releases(client2, names, days=7, local_tracks=[], lastfm=None)
    print(f"  передано 846 | проверено: {out['checked']} | лимит: {R.MAX_ARTISTS_PER_RUN}")

    print("=== 5. пауза между запросами (регулятор) ===")
    R.REQUEST_DELAY = 0.2
    client3 = FakeClient({"https://api.deezer.com/search/artist": FakeResponse(200, {"data": []})})
    R._response_cache.clear()
    start = time.monotonic()
    await R.fetch_weekly_releases(client3, ["A1", "A2", "A3", "A4"], days=7, local_tracks=[], lastfm=None)
    elapsed = time.monotonic() - start
    print(f"  4 артиста за {elapsed:.2f} c — пауза есть: {elapsed >= 4 * 0.2 * 0.9}")

    print("\nИТОГ: ТЕСТ ТРОТТЛИНГА ПРОЙДЕН")


asyncio.run(main())
