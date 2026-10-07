"""Кэш решений: клип + обложка для каждого трека (cache/matches.json)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import MATCHES_PATH, CACHE_DIR


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CacheStore:
    """Потокобезопасный JSON-кэш соответствий трек → клип/обложка."""

    def __init__(self, path: Path = MATCHES_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {"version": 1, "tracks": {}}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict) and "tracks" in loaded:
                    self.data = loaded
            except (OSError, json.JSONDecodeError):
                # Повреждённый кэш не должен ронять плеер
                self.data = {"version": 1, "tracks": {}}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def get(self, track_id: str) -> dict[str, Any] | None:
        with self._lock:
            item = self.data["tracks"].get(track_id)
            return dict(item) if item else None

    def put(self, track_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload)
        payload["updated_at"] = _now()
        with self._lock:
            self.data["tracks"][track_id] = payload
            self.save()
        return payload

    def delete(self, track_id: str) -> None:
        with self._lock:
            self.data["tracks"].pop(track_id, None)
            self.save()

    def clear(self) -> None:
        with self._lock:
            self.data["tracks"] = {}
            self.save()

    def all_tracks(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.data["tracks"])
