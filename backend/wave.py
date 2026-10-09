"""Local, feedback-driven recommendation queue for the endless listening wave."""

from __future__ import annotations

import asyncio
import logging
import math
import random
import sqlite3
import struct
import hashlib
import pathlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import httpx

from .config import ROOT, STATS_DB_PATH
from .lastfm_client import LastFmClient, LastFmError
from .ollama_ai import OllamaClient
from .scanner import Track

log = logging.getLogger("kadr.wave")
# BUG9: logging to .tools/wave_debug.log with request/response/error
try:
    _wave_debug_path = ROOT / ".tools" / "wave_debug.log"
    _wave_debug_path.parent.mkdir(parents=True, exist_ok=True)
    if not any(isinstance(h, logging.FileHandler) and pathlib.Path(getattr(h, "baseFilename", "")).resolve() == _wave_debug_path.resolve() for h in log.handlers):
        _wh = logging.FileHandler(_wave_debug_path, encoding="utf-8")
        _wh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(_wh)
        log.setLevel(logging.INFO)
except Exception:
    pass
SIGNAL_WEIGHTS = {"skip": -1.0, "complete": 1.0, "like": 3.0, "dislike": -5.0}
EMBEDDING_BATCH_SIZE = 24
MAX_TRACKS_PER_SIMILAR_SIGNAL = 3
WAVE_MODES = {"new", "library", "forgotten", "favorite", "mix"}
WAVE_MOODS = {"energetic", "calm", "sad", "night"}
WAVE_LANGUAGES = {"ru", "foreign", "instrumental"}
_MOOD_WORDS = {
    "energetic": ("energetic", "energy", "dance", "танцевальный", "энергичный", "electronic", "rock"),
    "calm": ("calm", "relax", "ambient", "спокойный", "расслабленный", "chill", "acoustic"),
    "sad": ("sad", "melancholy", "sadcore", "грустный", "печальный", "драма", "slowcore"),
    "night": ("night", "dark", "dream", "ночной", "мрачный", "dream pop", "ambient"),
}
_INSTRUMENTAL_WORDS = ("instrumental", "karaoke", "без вокала", "инструментал")
_REMOTE_PREVIEW_HOSTS = ("deezer.com", "dzcdn.net")
_mood_lock = asyncio.Lock()
_mood_embedding_lock = asyncio.Lock()
_last_mood_request = 0.0
_cached_mood: tuple[float, str] = (0.0, "")
_cached_mood_embedding: tuple[float, str, list[float]] = (0.0, "", [])


@contextmanager
def _connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or STATS_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS plays (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL UNIQUE,
            track_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            duration REAL NOT NULL CHECK (duration > 30),
            artist TEXT NOT NULL,
            title TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS wave_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id TEXT NOT NULL,
            signal TEXT NOT NULL,
            weight REAL NOT NULL,
            timestamp TEXT NOT NULL,
            related_to TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_wave_signals_track ON wave_signals(track_id)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS likes (
            track_id TEXT PRIMARY KEY,
            artist TEXT NOT NULL,
            genre TEXT NOT NULL DEFAULT '',
            timestamp TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS track_embeddings (
            track_id TEXT PRIMARY KEY,
            vector BLOB NOT NULL,
            dimensions INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _pack_vector(vector: list[float]) -> bytes:
    if not vector or any(not math.isfinite(value) for value in vector):
        raise ValueError("Embedding must contain finite numbers")
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes, dimensions: int) -> list[float]:
    if dimensions <= 0 or len(blob) != dimensions * 4:
        raise ValueError("Stored embedding has invalid dimensions")
    return list(struct.unpack(f"<{dimensions}f", blob))


def cosine_similarity(first: list[float], second: list[float]) -> float:
    if len(first) != len(second) or not first:
        return 0.0
    dot = sum(left * right for left, right in zip(first, second))
    first_norm = math.sqrt(sum(value * value for value in first))
    second_norm = math.sqrt(sum(value * value for value in second))
    if not first_norm or not second_norm:
        return 0.0
    return dot / (first_norm * second_norm)


def store_embedding(
    track_id: str, vector: list[float], db_path: Path | None = None
) -> None:
    blob = _pack_vector(vector)
    with _connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO track_embeddings (track_id, vector, dimensions, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(track_id) DO UPDATE SET
                vector = excluded.vector,
                dimensions = excluded.dimensions,
                updated_at = excluded.updated_at
            """,
            (
                track_id,
                blob,
                len(vector),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def load_embeddings(
    db_path: Path | None = None,
) -> dict[str, list[float]]:
    with _connect(db_path) as connection:
        rows = connection.execute(
            "SELECT track_id, vector, dimensions FROM track_embeddings"
        ).fetchall()
    vectors: dict[str, list[float]] = {}
    for row in rows:
        try:
            vectors[row["track_id"]] = _unpack_vector(
                row["vector"], int(row["dimensions"])
            )
        except (ValueError, struct.error) as exc:
            log.warning("Ignoring malformed embedding for %s: %s", row["track_id"], exc)
    return vectors


async def ensure_embeddings(
    tracks: list[Track],
    client: httpx.AsyncClient,
    ollama: OllamaClient,
    db_path: Path | None = None,
) -> int:
    existing = load_embeddings(db_path)
    if not ollama.status.models.get("nomic-embed-text"):
        return 0
    pending = [track for track in tracks if track.id not in existing]
    computed = 0
    for offset in range(0, len(pending), EMBEDDING_BATCH_SIZE):
        batch = pending[offset : offset + EMBEDDING_BATCH_SIZE]
        texts = [f"{track.artist}. {track.title}. {track.album}." for track in batch]
        batch_embed = getattr(ollama, "embed_batch", None)
        vectors = await batch_embed(client, texts) if callable(batch_embed) else None
        if not isinstance(vectors, list) or len(vectors) != len(batch):
            vectors = []
            for track, text in zip(batch, texts):
                vector = await ollama.embed(client, text)
                if not vector:
                    log.warning("Embedding generation stopped at track %s", track.id)
                    return computed
                vectors.append(vector)
        for track, vector in zip(batch, vectors):
            if not vector:
                log.warning("Embedding batch returned no vector for track %s", track.id)
                return computed
            store_embedding(track.id, vector, db_path)
            computed += 1
    return computed


async def _mood_embedding(
    mood_text: str,
    client: httpx.AsyncClient,
    ollama: OllamaClient,
) -> list[float] | None:
    global _cached_mood_embedding
    now = asyncio.get_running_loop().time()
    cached_until, cached_text, vector = _cached_mood_embedding
    if cached_until > now and cached_text == mood_text:
        return vector
    async with _mood_embedding_lock:
        now = asyncio.get_running_loop().time()
        cached_until, cached_text, vector = _cached_mood_embedding
        if cached_until > now and cached_text == mood_text:
            return vector
        vector = await ollama.embed(client, mood_text)
        if vector:
            _cached_mood_embedding = (now + 300, mood_text, vector)
        return vector


def _listening_data(
    db_path: Path | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, float]]:
    with _connect(db_path) as connection:
        plays = connection.execute(
            """
            SELECT track_id, COUNT(*) AS play_count, MAX(timestamp) AS last_played
            FROM plays GROUP BY track_id
            """
        ).fetchall()
        signals = connection.execute(
            "SELECT track_id, SUM(weight) AS weight FROM wave_signals GROUP BY track_id"
        ).fetchall()
    play_map = {
        row["track_id"]: {
            "play_count": int(row["play_count"]),
            "last_played": row["last_played"],
        }
        for row in plays
    }
    signal_map = {row["track_id"]: float(row["weight"]) for row in signals}
    return play_map, signal_map


def _time_mood(now: datetime | None = None) -> tuple[str, str]:
    hour = (now or datetime.now()).hour
    if 6 <= hour < 12:
        return "утро", "энергичное"
    if 12 <= hour < 18:
        return "день", "рабочее"
    if 18 <= hour < 23:
        return "вечер", "расслабленное"
    return "ночь", "спокойное"


async def _recent_mood(
    recent_tracks: list[Track],
    client: httpx.AsyncClient,
    ollama: OllamaClient,
) -> str:
    global _cached_mood, _last_mood_request
    names = [f"{track.artist} — {track.title}" for track in recent_tracks[-5:]]
    if not names:
        return ""
    signature = "\n".join(names)
    now = asyncio.get_running_loop().time()
    if _cached_mood[0] > now and _cached_mood[1].startswith(signature + "\n"):
        return _cached_mood[1][len(signature) + 1 :]
    async with _mood_lock:
        now = asyncio.get_running_loop().time()
        if now - _last_mood_request < 5:
            if _cached_mood[0] > now and _cached_mood[1].startswith(signature + "\n"):
                return _cached_mood[1][len(signature) + 1 :]
            return ""
        _last_mood_request = now
        period, expected = _time_mood()
        prompt = (
            "Classify the emotional energy and style of this recent local listening sequence. "
            "Return one short phrase, at most 5 words, no explanation. "
            f"Time is {period}; expected context: {expected}.\nTracks:\n"
            + "\n".join(names)
        )
        response = await ollama.generate(client, prompt, timeout=12.0, num_predict=32)
        mood = " ".join((response or "").strip().split()[:5])
        _cached_mood = (now + 300, signature + "\n" + mood)
        return mood


async def _similar_artists(
    play_map: dict[str, dict[str, Any]],
    tracks_by_id: dict[str, Track],
    lastfm: LastFmClient,
) -> dict[str, float]:
    artist_plays: dict[str, int] = {}
    for track_id, data in play_map.items():
        track = tracks_by_id.get(track_id)
        if track:
            artist_plays[track.artist] = artist_plays.get(track.artist, 0) + data["play_count"]
    seeds = [
        artist for artist, _ in sorted(artist_plays.items(), key=lambda pair: pair[1], reverse=True)[:3]
    ]
    affinity: dict[str, float] = {}
    for seed in seeds:
        try:
            similar = await lastfm.get_similar_artists(seed, 20)
        except LastFmError as exc:
            log.info("Last.fm recommendations unavailable for %s: %s", seed, exc)
            continue
        for item in similar:
            name = str(item.get("name") or "").strip()
            try:
                score = float((item.get("match") or 0))
            except (TypeError, ValueError):
                score = 0
            if name:
                affinity[name.casefold()] = max(affinity.get(name.casefold(), 0), score)
    return affinity


async def recommend(
    tracks: list[Track],
    count: int = 10,
    exclude_ids: set[str] | None = None,
    client: httpx.AsyncClient | None = None,
    ollama: OllamaClient | None = None,
    lastfm: LastFmClient | None = None,
    db_path: Path | None = None,
    mode: str = "new",
    mood_filter: str | None = None,
    language_filter: str | None = None,
    balance: int = 50,
) -> dict[str, Any]:
    count = max(1, min(int(count), 50))
    if mode not in WAVE_MODES:
        raise ValueError(f"Unsupported wave mode: {mode}")
    if mood_filter is not None and mood_filter not in WAVE_MOODS:
        raise ValueError(f"Unsupported wave mood: {mood_filter}")
    if language_filter is not None and language_filter not in WAVE_LANGUAGES:
        raise ValueError(f"Unsupported wave language: {language_filter}")
    balance = max(0, min(100, int(balance)))
    excluded = exclude_ids or set()
    available = [track for track in tracks if track.id not in excluded]
    if not available and mode not in {"new"}:
        return {"items": [], "mood": "", "period": _time_mood()[0], "embeddings_ready": True}

    play_map, signal_map = _listening_data(db_path)
    vectors = load_embeddings(db_path)
    tracks_by_id = {track.id: track for track in tracks}
    affinity: dict[str, float] = {}
    if lastfm and play_map:
        affinity = await _similar_artists(play_map, tracks_by_id, lastfm)

    recent_ids: list[str] = []
    with _connect(db_path) as connection:
        recent_rows = connection.execute(
            "SELECT track_id FROM plays ORDER BY timestamp DESC LIMIT 5"
        ).fetchall()
    recent_ids = [row["track_id"] for row in recent_rows]
    recent_tracks = [tracks_by_id[track_id] for track_id in reversed(recent_ids) if track_id in tracks_by_id]
    mood = ""
    if client and ollama and recent_tracks:
        mood = await _recent_mood(recent_tracks, client, ollama)
    period, expected_mood = _time_mood()
    mood_text = f"{period} {expected_mood} {mood}".strip()
    taste_seeds = sorted(
        (
            (
                math.log1p(data["play_count"]) * 2 + signal_map.get(track_id, 0),
                track_id,
            )
            for track_id, data in play_map.items()
            if track_id in vectors and track_id in tracks_by_id
        ),
        reverse=True,
    )[:8]
    taste_vectors = [
        (weight, track_id, vectors[track_id])
        for weight, track_id in taste_seeds
        if weight > 0
    ]
    mood_vector = None
    if client and ollama and mood_text and ollama.status.models.get("nomic-embed-text"):
        mood_vector = await _mood_embedding(mood_text, client, ollama)

    now = datetime.now(timezone.utc)
    artist_play_count: dict[str, int] = {}
    for track_id, data in play_map.items():
        track = tracks_by_id.get(track_id)
        if track:
            artist_play_count[track.artist.casefold()] = (
                artist_play_count.get(track.artist.casefold(), 0) + data["play_count"]
            )

    familiarity: list[tuple[float, Track, str]] = []
    discovery: list[tuple[float, Track, str]] = []
    for track in available:
        play_data = play_map.get(track.id, {"play_count": 0, "last_played": None})
        play_count = play_data["play_count"]
        signal_weight = signal_map.get(track.id, 0.0)
        last_played = play_data["last_played"]
        try:
            days_since = (now - datetime.fromisoformat(last_played)).days if last_played else 10_000
        except ValueError:
            days_since = 10_000
        familiar_score = math.log1p(play_count) * 2.0 + signal_weight
        if play_count > 0 or signal_weight > 0:
            familiarity.append((familiar_score + random.random() * 0.05, track, "часто слушаешь"))

        artist_count = artist_play_count.get(track.artist.casefold(), 0)
        artist_affinity = affinity.get(track.artist.casefold(), 0.0)
        track_vector = vectors.get(track.id)
        similar_track = None
        embedding_affinity = 0.0
        if track_vector and taste_vectors:
            embedding_affinity, seed_id = max(
                (
                    (cosine_similarity(track_vector, seed_vector), seed_id)
                    for weight, seed_id, seed_vector in taste_vectors
                ),
                key=lambda item: item[0],
            )
            similar_track = tracks_by_id.get(seed_id)
        mood_affinity = (
            max(0.0, cosine_similarity(track_vector, mood_vector))
            if track_vector and mood_vector
            else 0.0
        )
        if artist_count == 0 and artist_affinity > 0:
            reason = "новый исполнитель"
        elif embedding_affinity >= 0.55 and similar_track:
            reason = f"похоже на {similar_track.artist}"
        elif play_count == 0 or play_count <= 1:
            reason = "редко слушал"
        elif days_since > 30:
            reason = "давно не включал"
        else:
            reason = "открытие по вкусу"
        discovery_score = (
            3.0 / (1.0 + play_count)
            + min(days_since, 120) / 60.0
            + artist_affinity * 2.0
            + embedding_affinity * 2.0
            + mood_affinity
            + max(-5.0, min(5.0, signal_weight))
            + random.random() * 0.25
        )
        discovery.append((discovery_score, track, reason))

    def matches_filters(track: Track) -> bool:
        haystack = f"{track.artist} {track.title} {track.album}".casefold()
        if language_filter == "ru" and not any("\u0400" <= char <= "\u04ff" for char in haystack):
            return False
        if language_filter == "foreign" and any("\u0400" <= char <= "\u04ff" for char in haystack):
            return False
        if language_filter == "instrumental" and not any(word in haystack for word in _INSTRUMENTAL_WORDS):
            return False
        if mood_filter and not any(word in haystack for word in _MOOD_WORDS[mood_filter]):
            vector = vectors.get(track.id)
            if vector and mood_vector and cosine_similarity(vector, mood_vector) >= 0.45:
                return True
            return mood_filter is None
        return True

    familiarity = [entry for entry in familiarity if matches_filters(entry[1])]
    discovery = [entry for entry in discovery if matches_filters(entry[1])]
    familiarity.sort(key=lambda item: item[0], reverse=True)
    discovery.sort(key=lambda item: item[0], reverse=True)
    familiar_count = round(count * (100 - balance) / 100)
    discovery_count = count - familiar_count
    chosen: list[tuple[Track, str]] = []
    seen: set[str] = set()
    forgotten = [
        entry for entry in discovery
        if entry[2] in {"редко слушал", "давно не включал"}
    ]
    if mode in {"library", "mix"}:
        for _, track, reason in familiarity[:familiar_count]:
            chosen.append((track, reason))
            seen.add(track.id)
        for _, track, reason in discovery:
            if track.id in seen or len(chosen) >= familiar_count + discovery_count:
                continue
            chosen.append((track, reason))
            seen.add(track.id)
        for _, track, reason in familiarity + discovery:
            if len(chosen) >= count:
                break
            if track.id not in seen:
                chosen.append((track, reason))
                seen.add(track.id)
    elif mode == "forgotten":
        chosen = [(track, reason) for _, track, reason in forgotten[:count]]
    elif mode == "favorite":
        chosen = [(track, reason) for _, track, reason in familiarity[:count]]

    remote: list[dict[str, Any]] = []
    if mode in {"new", "mix"} and lastfm:
        try:
            log.info("wave request mode=%s count=%d seeds=%s", mode, count, [t.artist for t in tracks[:2]])
            remote = await _new_tracks(
                tracks,
                play_map,
                lastfm,
                client,
                count if mode == "new" else round(count * 0.4),
                mood_filter,
                language_filter,
                excluded,
            )
            log.info("wave response mode=%s remote=%d", mode, len(remote))
        except Exception as exc:
            log.exception("wave _new_tracks failed mode=%s: %s", mode, exc)
            remote = []
        if not remote:
            log.warning("wave fallback: Last.fm/Deezer unavailable mode=%s, will fallback to local", mode)
    else:
        if mode in {"new","mix"} and not lastfm:
            log.warning("wave no lastfm client mode=%s, fallback to local", mode)
    if mode == "new":
        if not remote:
            # BUG9: fallback to local library when Last.fm/Deezer unavailable
            log.info("wave fallback to local for new mode, choosing library/forgotten")
            # pick library/forgotten/favorite as fallback
            fallback = [entry for entry in familiarity[:count] or discovery[:count]]
            if not fallback:
                fallback = discovery[:count]
            items = [{**track.to_dict(), "reason": reason} for _, track, reason in fallback[:count]]
            return {
                "items": items,
                "mode": mode,
                "mood": mood,
                "period": period,
                "expected_mood": expected_mood,
                "embeddings_ready": len(vectors) >= len(tracks),
                "embedding_count": len(vectors),
                "warning": "Last.fm/Deezer недоступны — показываю локальную подборку",
                "fallback": True,
            }
        return {
            "items": remote,
            "mode": mode,
            "mood": mood,
            "period": period,
            "expected_mood": expected_mood,
            "embeddings_ready": len(vectors) >= len(tracks),
            "embedding_count": len(vectors),
            "warning": None if remote else "Новые рекомендации Last.fm/Deezer сейчас недоступны",
        }
    if mode == "mix":
        known_count = count - len(remote)
        forgotten_count = min(round(count * 0.2), known_count)
        forgotten_ids = {track.id for _, track, _ in forgotten[:forgotten_count]}
        forgotten_chosen = [
            (track, reason) for _, track, reason in forgotten
            if track.id in forgotten_ids
        ]
        known = [entry for entry in chosen if entry[0].id not in forgotten_ids]
        chosen = known[:max(0, known_count - len(forgotten_chosen))] + forgotten_chosen
        items = remote + [
            {**track.to_dict(), "reason": reason}
            for track, reason in chosen[:known_count]
        ]
        return {
            "items": items[:count],
            "mode": mode,
            "mood": mood,
            "period": period,
            "expected_mood": expected_mood,
            "embeddings_ready": len(vectors) >= len(tracks),
            "embedding_count": len(vectors),
            "warning": None if remote else "Новые рекомендации Last.fm/Deezer сейчас недоступны",
        }

    return {
        "items": [
            {**track.to_dict(), "reason": reason}
            for track, reason in chosen
        ],
        "mood": mood,
        "period": period,
        "expected_mood": expected_mood,
        "embeddings_ready": len(vectors) >= len(tracks),
        "embedding_count": len(vectors),
        "mode": mode,
    }


async def _new_tracks(
    tracks: list[Track],
    play_map: dict[str, dict[str, Any]],
    lastfm: LastFmClient,
    client: httpx.AsyncClient | None,
    count: int,
    mood_filter: str | None,
    language_filter: str | None,
    excluded: set[str],
) -> list[dict[str, Any]]:
    if count <= 0:
        return []
    tracks_by_id = {track.id: track for track in tracks}
    artists: dict[str, int] = {}
    for track_id, data in play_map.items():
        track = tracks_by_id.get(track_id)
        if track:
            artists[track.artist] = artists.get(track.artist, 0) + data["play_count"]
    seeds = [
        name for name, _ in sorted(artists.items(), key=lambda row: row[1], reverse=True)[:3]
    ]
    if not seeds:
        seeds = list(dict.fromkeys(track.artist for track in tracks if track.artist))[:3]
    known = {
        (track.artist.casefold().strip(), track.title.casefold().strip())
        for track in tracks
    }
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for seed in seeds:
        try:
            similar = await lastfm.get_similar_artists(seed, 8)
        except LastFmError as exc:
            log.info("Last.fm similar artists unavailable for %s: %s", seed, exc)
            continue
        for artist in similar[:4]:
            name = str(artist.get("name") or "").strip()
            if not name or name.casefold() in {item.casefold() for item in seeds}:
                continue
            try:
                top_tracks = await lastfm.get_artist_top_tracks(name, min(10, count * 2))
            except LastFmError as exc:
                log.info("Last.fm top tracks unavailable for %s: %s", name, exc)
                continue
            for item in top_tracks:
                title = str(item.get("name") or "").strip()
                key = (name.casefold(), title.casefold())
                if not title or key in known:
                    continue
                candidate = {
                    "id": "remote:" + hashlib.sha256(f"{name}\0{title}".encode()).hexdigest()[:24],
                    "artist": name,
                    "title": title,
                    "reason": f"новый исполнитель: {seed}",
                    "external": True,
                    "url": str(item.get("url") or ""),
                    "preview": "",
                    "cover": "",
                }
                if candidate["id"] not in excluded and _matches_remote_filters(
                    candidate, mood_filter, language_filter
                ):
                    candidates[key] = candidate
    if client and candidates:
        semaphore = asyncio.Semaphore(4)

        async def deezer_match(key: tuple[str, str], candidate: dict[str, Any]) -> None:
            async with semaphore:
                try:
                    response = await client.get(
                        "https://api.deezer.com/search",
                        params={"q": f'artist:"{candidate["artist"]}" track:"{candidate["title"]}"', "limit": 5},
                        timeout=6.0,
                    )
                    response.raise_for_status()
                    rows = response.json().get("data") or []
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    log.info("Deezer wave lookup failed for %s: %s", candidate["title"], type(exc).__name__)
                    return
                for row in rows:
                    artist = row.get("artist") or {}
                    if str(artist.get("name") or "").casefold() != key[0]:
                        continue
                    preview = str(row.get("preview") or "")
                    host = (httpx.URL(preview).host or "").casefold() if preview else ""
                    if preview and httpx.URL(preview).scheme == "https" and any(
                        host == domain or host.endswith("." + domain)
                        for domain in _REMOTE_PREVIEW_HOSTS
                    ):
                        candidate["preview"] = preview
                    album = row.get("album") or {}
                    candidate["cover"] = str(album.get("cover_medium") or "")
                    candidate["deezer_url"] = str(row.get("link") or "")
                    break

        await asyncio.gather(
            *(deezer_match(key, item) for key, item in candidates.items())
        )
    return [item for item in candidates.values() if item["preview"]][:count]


def _matches_remote_filters(
    item: dict[str, Any], mood_filter: str | None, language_filter: str | None
) -> bool:
    value = f"{item['artist']} {item['title']}".casefold()
    has_cyrillic = any("\u0400" <= char <= "\u04ff" for char in value)
    if language_filter == "ru" and not has_cyrillic:
        return False
    if language_filter == "foreign" and has_cyrillic:
        return False
    if language_filter == "instrumental":
        return any(word in value for word in _INSTRUMENTAL_WORDS)
    if mood_filter:
        return any(word in value for word in _MOOD_WORDS[mood_filter])
    return True


def record_signal(
    track_id: str,
    signal: str,
    db_path: Path | None = None,
    *,
    artist: str = "",
    genre: str = "",
) -> dict[str, Any]:
    if signal not in SIGNAL_WEIGHTS:
        raise ValueError(f"Unsupported wave signal: {signal}")
    related: list[tuple[str, float]] = []
    if signal == "skip":
        vectors = load_embeddings(db_path)
        source = vectors.get(track_id)
        if source:
            related = sorted(
                (
                    (other_id, cosine_similarity(source, vector))
                    for other_id, vector in vectors.items()
                    if other_id != track_id
                ),
                key=lambda item: item[1],
                reverse=True,
            )[:MAX_TRACKS_PER_SIMILAR_SIGNAL]

    timestamp = datetime.now(timezone.utc).isoformat()
    with _connect(db_path) as connection:
        connection.execute(
            "INSERT INTO wave_signals (track_id, signal, weight, timestamp) VALUES (?, ?, ?, ?)",
            (track_id, signal, SIGNAL_WEIGHTS[signal], timestamp),
        )
        if signal == "like":
            connection.execute(
                """
                INSERT INTO likes (track_id, artist, genre, timestamp)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(track_id) DO UPDATE SET
                    artist = excluded.artist,
                    genre = excluded.genre,
                    timestamp = excluded.timestamp
                """,
                (track_id, artist, genre, timestamp),
            )
        elif signal == "dislike":
            connection.execute("DELETE FROM likes WHERE track_id = ?", (track_id,))
        for similar_id, similarity in related:
            connection.execute(
                "INSERT INTO wave_signals (track_id, signal, weight, timestamp, related_to) "
                "VALUES (?, 'similar_skip', ?, ?, ?)",
                (similar_id, -0.5 * max(0.0, similarity), timestamp, track_id),
            )
    return {
        "track_id": track_id,
        "signal": signal,
        "weight": SIGNAL_WEIGHTS[signal],
        "similar_tracks_updated": len(related),
    }


def liked_track_ids(db_path: Path | None = None) -> list[str]:
    with _connect(db_path) as connection:
        rows = connection.execute(
            "SELECT track_id FROM likes ORDER BY timestamp DESC"
        ).fetchall()
    return [row["track_id"] for row in rows]


def recent_tracks(db_path: Path | None = None, limit: int = 5) -> list[str]:
    with _connect(db_path) as connection:
        rows = connection.execute(
            "SELECT track_id FROM plays ORDER BY timestamp DESC LIMIT ?",
            (max(1, min(limit, 5)),),
        ).fetchall()
    return [row["track_id"] for row in rows]
