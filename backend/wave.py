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

import json
import shutil
import subprocess
import sys

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

# ЗАДАЧА1: хранение последней попытки для /api/wave/debug
_last_recommend_attempt: dict = {}
import traceback
import time
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

    # ЗАДАЧА1: детальное логирование каждой попытки recommend()
    global _last_recommend_attempt
    _last_recommend_attempt = {
        "time": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "mode": mode,
        "count": count,
        "lastfm_client_ok": lastfm is not None,
        "mood_filter": mood_filter,
        "language_filter": language_filter,
        "excluded_count": len(excluded),
        "tracks_total": len(tracks),
    }
    log.info("wave recommend start mode=%s count=%d lastfm_client_ok=%s tracks=%d excluded=%d", mode, count, lastfm is not None, len(tracks), len(excluded))
    # TASK8: лог источников для диагностики
    try:
        src = _wave_sources_status()
        log.info("wave sources lastfm=%s yt=%s dez=%s", src["lastfm"]["ok"], src["youtube"]["ok"], src["youtube"]["detail"][:80])
        _last_recommend_attempt["sources"] = src
    except Exception as e:
        log.info("wave sources status fail: %s", e)
        _last_recommend_attempt["sources_error"] = str(e)
    remote: list[dict[str, Any]] = []
    remote_before = 0
    yt_attempted = False
    yt_returned = 0
    final_source = "none"
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
            _last_recommend_attempt["remote_before"] = len(remote)
            remote_before = len(remote)
        except Exception as exc:
            log.exception("wave _new_tracks failed mode=%s: %s\n%s", mode, exc, traceback.format_exc())
            _last_recommend_attempt["lastfm_error"] = f"{type(exc).__name__}: {exc}"
            _last_recommend_attempt["stacktrace"] = traceback.format_exc()
            remote = []
            remote_before = 0
        # ЗАДАЧА2: фикс логики fallback — добавляем YouTube, не заменяем, <50% → добавляем
        need_youtube = False
        youtube_reason = None
        target = count if mode == "new" else round(count * 0.4)
        if _last_recommend_attempt.get("lastfm_error"):
            need_youtube = True
            youtube_reason = f"lastfm_error: {_last_recommend_attempt['lastfm_error']}"
        elif len(remote) == 0:
            need_youtube = True
            youtube_reason = "lastfm_returned_empty"
        elif len(remote) < target * 0.5:
            need_youtube = True
            youtube_reason = f"lastfm_partial {len(remote)}/{target} <50%"
        if need_youtube:
            log.info("wave youtube fallback needed reason=%s remote_before=%d target=%d", youtube_reason, len(remote), target)
            _last_recommend_attempt["fallback_reason"] = youtube_reason
            yt_attempted = True
            try:
                need = max(1, target - len(remote))
                yt_tracks = await _youtube_fallback_tracks(tracks, {(tr.artist.casefold().strip(), tr.title.casefold().strip()) for tr in tracks} | {(r.get("artist","").casefold(), r.get("title","").casefold()) for r in remote}, play_map, lastfm, need, excluded, mood_filter, language_filter)
                yt_returned = len(yt_tracks) if yt_tracks else 0
                log.info("wave youtube fallback attempted need=%d returned=%d reason=%s", need, yt_returned, youtube_reason)
                _last_recommend_attempt["yt_attempted"] = True
                _last_recommend_attempt["yt_returned"] = yt_returned
                if yt_returned == 0:
                    log.info("wave youtube fallback returned [] — youtube_reason=%s seeds=%s", youtube_reason, [t.artist for t in tracks[:3]])
                    _last_recommend_attempt["yt_returned_empty"] = "yt_returned_empty"
                    if yt_tracks is not None and len(yt_tracks)==0:
                        log.warning("wave yt_returned_empty query=%s reason=%s", [t.artist for t in tracks[:3]], youtube_reason)
                if yt_tracks:
                    # ДОБАВЛЯЕМ, не заменяем (если remote уже частично есть)
                    if remote:
                        remote = remote + yt_tracks
                        log.info("wave youtube fallback added %d tracks to %d existing (mixed) mode=%s", len(yt_tracks), remote_before, mode)
                        final_source = "mixed_lastfm_youtube"
                    else:
                        remote = yt_tracks
                        log.info("wave youtube fallback produced %s tracks mode=%s (replaced empty)", len(remote), mode)
                        final_source = "youtube"
                else:
                    log.info("wave youtube fallback empty, will go to local")
                    final_source = "local_after_youtube_empty"
                    _last_recommend_attempt["youtube_empty_reason"] = "yt_returned_empty"
            except Exception as e:
                log.exception("youtube fallback failed mode=%s reason=%s: %s\n%s", mode, youtube_reason, type(e).__name__, traceback.format_exc())
                _last_recommend_attempt["yt_error"] = f"{type(e).__name__}: {e}"
                yt_returned = 0
        else:
            # если remote достаточно (>=50%) — youtube не нужен
            log.info("wave no youtube needed remote=%d target=%d >=50%%", len(remote), target)
            final_source = "lastfm_deezer"
            _last_recommend_attempt["fallback_reason"] = "no_fallback_needed"
            _last_recommend_attempt["yt_attempted"] = False
            _last_recommend_attempt["yt_returned"] = 0
    else:
        if mode in {"new","mix"} and not lastfm:
            log.warning("wave no lastfm client mode=%s, fallback to local", mode)
            _last_recommend_attempt["fallback_reason"] = "no_lastfm_client"
            yt_attempted = True
            # TASK8: даже без lastfm пробуем YouTube
            try:
                yt_tracks = await _youtube_fallback_tracks(tracks, {(tr.artist.casefold().strip(), tr.title.casefold().strip()) for tr in tracks}, play_map, None, count, excluded, mood_filter, language_filter)
                yt_returned = len(yt_tracks) if yt_tracks else 0
                _last_recommend_attempt["yt_attempted"] = True
                _last_recommend_attempt["yt_returned"] = yt_returned
                if not yt_tracks:
                    log.info("youtube fallback (no lastfm) returned [] seeds=%s", [t.artist for t in tracks[:3]])
                if yt_tracks:
                    remote = yt_tracks
                    log.info("wave youtube fallback (no lastfm) %s tracks", len(remote))
                    final_source = "youtube_no_lastfm"
                else:
                    final_source = "local_no_lastfm_youtube_empty"
            except Exception as e:
                log.exception("youtube fallback no-lastfm fail: %s\n%s", type(e).__name__, traceback.format_exc())
                _last_recommend_attempt["yt_error"] = f"{type(e).__name__}: {e}" 
    if mode == "new":
        if not remote:
            # если и YouTube пуст — fallback to local library when Last.fm/Deezer unavailable
            log.info("wave fallback to local for new mode, choosing library/forgotten (youtube also empty) yt_attempted=%s yt_returned=%s final_source=%s", yt_attempted, yt_returned, final_source)
            _last_recommend_attempt["final_source"] = "local"
            _last_recommend_attempt["yt_attempted"] = yt_attempted
            _last_recommend_attempt["yt_returned"] = yt_returned
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
        # ЗАДАЧА1: записать итоговый источник
        if remote and yt_attempted and yt_returned > 0:
            final_source = "youtube"
        elif remote and not yt_attempted:
            final_source = "lastfm_deezer"
        elif remote and yt_attempted and yt_returned == 0:
            final_source = "lastfm_deezer"
        _last_recommend_attempt["final_source"] = final_source
        _last_recommend_attempt["remote_final"] = len(remote)
        log.info("wave final mode=%s final_source=%s remote=%d yt_attempted=%s yt_returned=%d", mode, final_source, len(remote), yt_attempted, yt_returned)
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
        # TASK8: если remote мало — дополнить YouTube
        if len(remote) < round(count * 0.4):
            try:
                need = round(count * 0.4) - len(remote)
                extra = await _youtube_fallback_tracks(tracks, {(tr.artist.casefold().strip(), tr.title.casefold().strip()) for tr in tracks} | {(r.get("artist","").casefold(), r.get("title","").casefold()) for r in remote}, play_map, lastfm, need, excluded, mood_filter, language_filter)
                if extra:
                    remote = remote + extra
                    log.info("wave mix youtube supplement %s extra", len(extra))
            except Exception as e:
                log.info("mix youtube supplement fail: %s", type(e).__name__)
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


def _resolve_yt_dlp_cmd() -> list[str]:
    """Найти yt-dlp / audiodl.exe — порядок как в audio_fetcher, бесплатный источник YouTube Music."""
    cand = ROOT / "tools" / "audiodl.exe"
    if cand.is_file():
        return [str(cand)]
    for name in ("yt-dlp", "yt_dlp", "audiodl", "audiodl.exe"):
        exe = shutil.which(name)
        if exe:
            return [exe]
    cand2 = ROOT / "venv" / "Scripts" / "yt-dlp.exe"
    if cand2.is_file():
        return [str(cand2)]
    cand3 = ROOT / "venv" / "bin" / "yt-dlp"
    if cand3.is_file():
        return [str(cand3)]
    return [sys.executable, "-m", "yt_dlp"]

def _yt_available() -> tuple[bool, str]:
    cmd = _resolve_yt_dlp_cmd()
    try:
        # пробуем --version быстро
        r = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return True, f"{' '.join(cmd)} {r.stdout.strip()[:60]}"
        # если python -m yt_dlp и модуля нет — fail
        return False, f"{' '.join(cmd)} → rc={r.returncode} {r.stderr[:120]}"
    except Exception as e:
        return False, f"{' '.join(cmd)} → {type(e).__name__}: {e}"

async def _yt_music_search(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Бесплатный источник YouTube Music через yt-dlp ytsearch: `yt-dlp \"ytsearch{limit}:{query}\" --dump-json`."""
    cmd = _resolve_yt_dlp_cmd()
    # ytsearch query with limit
    search = f"ytsearch{max(1,min(limit,10))}:{query}"
    args = cmd + [search, "--dump-json", "--flat-playlist", "--skip-download", "--no-warnings", "--no-call-home", "--no-playlist", "--extractor-retries", "1"]
    # Also try without flat if needed
    def _run():
        try:
            # first try with flat
            r = subprocess.run(args, capture_output=True, text=True, timeout=18)
            out = r.stdout.strip()
            if not out:
                # fallback without flat-playlist, more json per line
                args2 = cmd + [search, "--dump-json", "--skip-download", "--no-warnings"]
                r2 = subprocess.run(args2, capture_output=True, text=True, timeout=18)
                out = r2.stdout.strip()
                r = r2
            items = []
            for line in out.splitlines():
                line=line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    j = json.loads(line)
                    # yt-dlp flat gives {id,title,url,duration} etc
                    vid = j.get("id") or j.get("url","").split("v=")[-1][:11]
                    title = j.get("title") or ""
                    # try to split artist - title
                    # keep as is for wave
                    items.append({"yt_id": vid, "title": title, "raw": j})
                except Exception:
                    continue
            return items[:limit], r.stderr[:500], r.returncode
        except Exception as e:
            return [], f"{type(e).__name__}: {e}", 1
    try:
        items, err, rc = await asyncio.to_thread(_run)
        if rc != 0:
            log.info("yt-dlp ytsearch rc=%s err=%s query=%s", rc, err[:200], query[:60])
        log.info("yt-dlp ytsearch query=%s → %s items", query[:60], len(items))
        return items
    except Exception as e:
        log.warning("yt-dlp search thread fail %s query=%s", type(e).__name__, query[:60])
        return []

# ЗАДАЧА5: режим "new" возвращает треки с полями artist, title, youtube_id, url, source ("lastfm"/"deezer"/"youtube")
async def _youtube_fallback_tracks(tracks: list[Track], known: set[tuple[str,str]], play_map: dict, lastfm: LastFmClient | None, count: int, excluded: set[str], mood_filter: str | None, language_filter: str | None) -> list[dict[str, Any]]:
    """Если Last.fm пуст — ищем через YouTube Music по названиям из истории прослушиваний (play_map) или случайным трекам."""
    # собираем seed queries из топа прослушиваний
    tracks_by_id = {tr.id: tr for tr in tracks}
    # топ по play_count
    sorted_plays = sorted(play_map.items(), key=lambda kv: kv[1].get("play_count",0), reverse=True)
    seeds: list[str] = []
    for tid, _ in sorted_plays[:5]:
        tr = tracks_by_id.get(tid)
        if tr:
            seeds.append(f"{tr.artist} - {tr.title}")
    if not seeds:
        # fallback на случайные треки из библиотеки
        seeds = [f"{tr.artist} - {tr.title}" for tr in random.sample(tracks, min(3, len(tracks))) if tr.artist and tr.title]
    if not seeds:
        seeds = ["lofi hip hop", "phonk", "tokyo ghoul"]
    candidates: list[dict[str, Any]] = []
    seen = set(known)
    for q in seeds[:3]:
        try:
            results = await _yt_music_search(q, limit=5)
        except Exception as e:
            log.info("youtube fallback search fail %s: %s", q, type(e).__name__)
            continue
        for r in results:
            title = r.get("title") or ""
            # yt title часто "Artist - Title (Official)" — пытаемся разделить
            if " - " in title:
                artist, ttitle = title.split(" - ",1)
            else:
                artist, ttitle = q.split(" - ",1) if " - " in q else ("YouTube", title[:80])
            artist = artist.strip()[:80] or "YouTube"
            ttitle = ttitle.strip()[:90] or title[:80]
            key = (artist.casefold().strip(), ttitle.casefold().strip())
            if key in seen:
                continue
            # применяем фильтры языка/муда
            dummy = {"artist": artist, "title": ttitle}
            if not _matches_remote_filters(dummy, mood_filter, language_filter):
                continue
            vid = r.get("yt_id") or ""
            yt_url = f"https://www.youtube.com/watch?v={vid}" if vid else ""
            preview = ""  # yt-dlp не даёт preview, но можно deezer-совместимо использовать youtube preview? оставляем пустым, фронт покажет youtube
            cand = {
                "id": "yt:" + hashlib.sha256(f"{artist}\0{ttitle}".encode()).hexdigest()[:24],
                "artist": artist,
                "title": ttitle,
                "reason": f"YouTube Music: по вкусу '{q[:40]}'",
                "external": True,
                "url": yt_url,
                "preview": preview,
                "cover": "",
                "youtube_id": vid,
                "source": "youtube",
            }
            if cand["id"] not in excluded:
                candidates.append(cand)
                seen.add(key)
            if len(candidates) >= count:
                break
        if len(candidates) >= count:
            break
    # если yt-dlp недоступен / пусто — пробуем InnerTube search_youtube как второй фолбэк
    if not candidates:
        try:
            from .youtube_search import search_youtube
            # quick search via InnerTube for each seed
            for q in seeds[:2]:
                try:
                    # split q to artist/title for search_youtube
                    if " - " in q:
                        a,tit = q.split(" - ",1)
                    else:
                        a,tit = q, ""
                    # search_youtube is async in some versions? check sync
                    # try async path if available
                    import inspect
                    if inspect.iscoroutinefunction(search_youtube):
                        # we are async already — await (need client? youtube_search.search_youtube expects query only)
                        # fallback sync call with httpx client if needed — skip
                        continue
                    else:
                        # sync version?
                        results = await asyncio.to_thread(lambda: search_youtube(q, limit=5) if callable(search_youtube) else [])
                        for it in (results or []):
                            vid = it.get("video_id") or it.get("id") or ""
                            title = it.get("title") or ""
                            artist = it.get("channel") or a
                            key=(artist.casefold(), title.casefold())
                            if key in seen: continue
                            candidates.append({
                                "id": "yt:" + hashlib.sha256(f"{artist}\0{title}".encode()).hexdigest()[:24],
                                "artist": artist, "title": title,
                                "reason": f"YouTube (InnerTube): {q[:40]}",
                                "external": True, "url": f"https://www.youtube.com/watch?v={vid}" if vid else "",
                                "preview":"", "cover":"", "youtube_id": vid, "source":"youtube"
                            })
                            if len(candidates)>=count: break
                except Exception:
                    continue
        except Exception as e:
            log.info("InnerTube fallback fail: %s", type(e).__name__)
    if not candidates:
        log.info("youtube fallback total 0 candidates for seeds %s — yt-dlp вернул пусто", seeds[:2])
    else:
        log.info("youtube fallback total %s candidates for seeds %s", len(candidates), seeds[:2])
    # ЗАДАЧА1: если пусто — логируем что запрос был и что yt-dlp вернул
    if not candidates:
        log.warning("wave yt_returned_empty seeds=%s", seeds[:3])
    return candidates[:count]

def _wave_sources_status() -> dict[str, Any]:
    # проверка доступности всех источников для отчёта
    yt_ok, yt_detail = _yt_available()
    lastfm_ok = False
    lastfm_detail = "no key"
    try:
        from .lastfm_config import get_lastfm_api_key
        k = get_lastfm_api_key()
        if k: lastfm_ok, lastfm_detail = True, f"key {k[:4]}...{k[-4:]}"
        else: lastfm_ok, lastfm_detail = False, "LASTFM_API_KEY missing"
    except Exception as e:
        lastfm_detail = f"{type(e).__name__}: {e}"
    # deezer — быстрая проверка уже в health, тут stub
    return {"lastfm": {"ok": lastfm_ok, "detail": lastfm_detail}, "youtube": {"ok": yt_ok, "detail": yt_detail}, "deezer": {"ok": True, "detail": "https://api.deezer.com (проверяется в _new_tracks)"}, "local": {"ok": True, "detail": "локальная библиотека всегда доступна"}}

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
