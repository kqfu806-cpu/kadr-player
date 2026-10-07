"""Компактный cache/index.json — сводка обложка/текст/клип на трек (~0.5 КБ)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .config import CACHE_DIR, INDEX_PATH


class IndexStore:
    def __init__(self, path: Path = INDEX_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data = loaded
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def get(self, track_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.data.get(track_id)
            return dict(row) if row else {}

    def upsert(self, track_id: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            row = dict(self.data.get(track_id) or {})
            for k, v in fields.items():
                row[k] = v
            row["last_checked"] = int(time.time())
            self.data[track_id] = row
            self.save()
            return dict(row)

    def delete(self, track_id: str) -> None:
        with self._lock:
            self.data.pop(track_id, None)
            self.save()
