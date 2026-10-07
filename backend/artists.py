"""Исполнители библиотеки и похожие через nomic-embed-text."""

from __future__ import annotations

import json
from typing import Any

from .config import CACHE_DIR, OLLAMA_EMBED
from .ollama_ai import OllamaClient, _cosine
from .scanner import Track

ARTISTS_PATH = CACHE_DIR / "artists.json"
MAX_ARTIST_EMBEDDINGS = 96


def _as_vec(v: Any) -> list[float] | None:
    if isinstance(v, list) and v and isinstance(v[0], (int, float)):
        return [float(x) for x in v]
    if isinstance(v, dict) and isinstance(v.get("vec"), list):
        return [float(x) for x in v["vec"]]
    return None


def _load_cache() -> dict[str, list[float]]:
    if not ARTISTS_PATH.is_file():
        return {}
    try:
        raw = json.loads(ARTISTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[float]] = {}
    for k, v in raw.items():
        vec = _as_vec(v)
        if vec:
            out[str(k)] = vec
    return out


def _save_cache(vectors: dict[str, list[float]]) -> None:
    try:
        ARTISTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        ARTISTS_PATH.write_text(
            json.dumps(vectors, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


async def build_artists(
    client,
    ollama: OllamaClient,
    tracks: list[Track],
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for t in tracks:
        name = (t.artist or "").strip() or "Unknown"
        counts[name] = counts.get(name, 0) + 1
    names = sorted(counts, key=lambda n: (-counts[n], n.lower()))
    embedding_names = names[:MAX_ARTIST_EMBEDDINGS]

    await ollama.refresh_status(client)
    can_embed = bool(ollama.status.models.get(OLLAMA_EMBED))
    vectors = _load_cache()
    dirty = False
    if can_embed:
        for name in embedding_names:
            if name in vectors:
                continue
            vec = await ollama.embed(client, f"artist: {name}")
            if vec:
                vectors[name] = vec
                dirty = True
        if dirty:
            _save_cache(vectors)

    artists: list[dict[str, Any]] = []
    for name in names:
        similar: list[str] = []
        va = vectors.get(name) if can_embed else None
        if va:
            scored: list[tuple[float, str]] = []
            for other in names:
                if other == name:
                    continue
                vb = vectors.get(other)
                if not vb:
                    continue
                scored.append((_cosine(va, vb), other))
            scored.sort(key=lambda x: x[0], reverse=True)
            similar = [n for _, n in scored[:5]]
        artists.append({"name": name, "count": counts[name], "similar": similar})
    return {"artists": artists, "ollama": can_embed}
