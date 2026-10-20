# -*- coding: utf-8 -*-
"""Разбор лога uvicorn: кто и как часто шлёт запросы (диагностика спама).

Запуск:
    python tools/overlay_checks/check_request_spam.py cache/server.log
    python tools/overlay_checks/check_request_spam.py --live cache/server.log

Что показывает:
  * сколько запросов уходит на /api/now-playing и /api/player/state;
  * какой клиент (IP:порт) и с каким User-Agent шлёт спам;
  * «норма» — 1 запрос/с на клиента, всё сверх — подозрительно.

Живая диагностика прямо в браузере: http://127.0.0.1:8000/api/diagnostics/requests
"""
from __future__ import annotations

import re
import sys
import time
from collections import Counter
from pathlib import Path

# Строка uvicorn: INFO:     127.0.0.1:52344 - "GET /api/now-playing HTTP/1.1" 200 OK
LOG_RE = re.compile(
    r'(?P<client>[\d\.\[\]:a-fA-F]+)\s+-\s+"(?P<method>[A-Z]+)\s+(?P<path>[^ "]+)\s+HTTP/[\d\.]+"\s+(?P<status>\d{3})'
)

HOT = ("/api/now-playing", "/api/player/state")
NORMAL_RATE = 1.0  # запросов в секунду на клиента — норма для оверлея/браузера


def parse(path: Path):
    """Возвращает (счётчик путей, счётчик клиентов, счётчик пар путь+клиент)."""
    paths: Counter = Counter()
    clients: Counter = Counter()
    pairs: Counter = Counter()
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        print(f"Не удалось прочитать {path}: {exc}")
        return paths, clients, pairs
    for line in text.splitlines():
        match = LOG_RE.search(line)
        if not match:
            continue
        p = match.group("path")
        client = match.group("client")
        paths[p] += 1
        clients[client] += 1
        pairs[(p, client)] += 1
    return paths, clients, pairs


def main(argv: list[str]) -> int:
    live = "--live" in argv
    args = [a for a in argv[1:] if not a.startswith("--")]
    target = Path(args[0]) if args else Path("cache/server.log")
    if not target.exists():
        print(f"Файл не найден: {target}")
        print("Подсказка: сервер пишет логи в cache/server.log и cache/logs/api.log,")
        print("           uvicorn-строка с частотой — в окне консоли сервера.")
        print("           Живая диагностика: http://127.0.0.1:8000/api/diagnostics/requests")
        return 2

    if live:
        print(f"Слежу за {target} (Ctrl+C — остановить)...")
        counter: Counter = Counter()
        pairs: Counter = Counter()
        deadline = time.time() + 60
        try:
            with target.open("r", encoding="utf-8", errors="ignore") as fh:
                fh.seek(0, 2)
                while time.time() < deadline:
                    line = fh.readline()
                    if not line:
                        time.sleep(0.2)
                        continue
                    match = LOG_RE.search(line)
                    if not match:
                        continue
                    counter[match.group("path")] += 1
                    pairs[(match.group("path"), match.group("client"))] += 1
        except KeyboardInterrupt:
            pass
        total = sum(counter.values())
        print(f"\nЗа окно наблюдения: {total} запросов")
        for p, count in counter.most_common(10):
            print(f"  {count:6d}  {p}")
        hot = [(k, v) for k, v in pairs.items() if k[0] in HOT]
        for (p, client), count in sorted(hot, key=lambda kv: -kv[1])[:10]:
            print(f"  {p} ← {client}: {count}")
        return 0

    paths, clients, pairs = parse(target)
    if not paths:
        print(f"В {target} не найдено строк uvicorn-лога.")
        print('Формат строки:  INFO:  127.0.0.1:52344 - "GET /api/now-playing HTTP/1.1" 200 OK')
        print("Живая диагностика: http://127.0.0.1:8000/api/diagnostics/requests")
        return 2

    print(f"=== {target} ===")
    print("ТОП-путей:")
    for p, count in paths.most_common(10):
        print(f"  {count:8d}  {p}")

    print("\nТОП-клиентов:")
    for client, count in clients.most_common(10):
        print(f"  {count:8d}  {client}")

    print("\nГорячие эндпоинты (/api/now-playing, /api/player/state):")
    hot_pairs = [(k, v) for k, v in pairs.items() if k[0] in HOT]
    if not hot_pairs:
        print("  запросов не найдено (или они подавлены новой защитой)")
    else:
        for (p, client), count in sorted(hot_pairs, key=lambda kv: -kv[1])[:10]:
            print(f"  {count:8d}  {p}  ←  {client}")
    print(f"\nВНИМАНИЕ: норма — не больше {NORMAL_RATE:.0f} запроса/с на клиента.")
    print("Если один клиент заметно чаще — он и есть спамер (см. GET /api/diagnostics/requests).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
