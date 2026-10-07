"""Taste-ranked, locally cached weekly releases from public catalog APIs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from .config import ROOT
from .lastfm_client import LastFmClient, LastFmError
from .ollama_ai import OllamaClient
from .releases import MAX_FOLLOWED_ARTISTS, fetch_weekly_releases
from .scanner import Track
from .wave import _listening_data, cosine_similarity, load_embeddings

CACHE_PATH = ROOT / ".tools" / "weekly_cache.json"
CACHE_TTL_SECONDS = 6 * 60 * 60
MAX_SEED_ARTISTS = 6
MAX_SIMILAR_ARTISTS = 8
MAX_EMBEDDING_CANDIDATES = 40
_cache_lock = asyncio.Lock()
log = logging.getLogger("kadr.weekly")


def _normalize(value: str) -> str:
    return re.sub(r"[^\w]+", " ", value.casefold()).strip()


def _signature(tracks: list[Track]) -> str:
    local = sorted(
        (
            track.artist.casefold(),
            track.title.casefold(),
            track.album.casefold(),
        )
        for track in tracks
    )
    return hashlib.sha256(
        json.dumps(local, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_cache(signature: str) -> dict[str, Any] | None:
    try:
        entry = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Ignoring unreadable weekly cache: %s", exc)
        return None
    if (
        not isinstance(entry, dict)
        or entry.get("signature") != signature
        or not isinstance(entry.get("saved_at"), (int, float))
        or not isinstance(entry.get("data"), dict)
        or time.time() - entry["saved_at"] >= CACHE_TTL_SECONDS
    ):
        return None
    data = entry["data"]
    if not isinstance(data.get("tracks"), list) or not isinstance(data.get("albums"), list):
        log.warning("Ignoring malformed weekly cache")
        return None
    return data


def _write_cache(signature: str, data: dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = CACHE_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"signature": signature, "saved_at": time.time(), "data": data},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(CACHE_PATH)
    except OSError as exc:
        log.error("Could not write weekly cache: %s", exc)


def _library_artist_counts(tracks: list[Track]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for track in tracks:
        name = track.artist.strip()
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


async def _artist_seeds(
    tracks: list[Track], lastfm: LastFmClient
) -> tuple[list[str], dict[str, float], list[str]]:
    counts = _library_artist_counts(tracks)
    play_map, _ = _listening_data()
    play_counts: dict[str, int] = {}
    artists_by_track = {track.id: track.artist for track in tracks}
    for track_id, data in play_map.items():
        artist = artists_by_track.get(track_id)
        if artist:
            play_counts[artist] = play_counts.get(artist, 0) + data["play_count"]
    seeds = sorted(
        counts,
        key=lambda artist: (play_counts.get(artist, 0), counts[artist], artist.casefold()),
        reverse=True,
    )[:MAX_SEED_ARTISTS]
    artist_names = list(seeds)
    affinity: dict[str, float] = {}
    warnings: list[str] = []
    for seed in seeds[:3]:
        try:
            similar = await lastfm.get_similar_artists(seed, MAX_SIMILAR_ARTISTS)
        except LastFmError as exc:
            warnings.append(f"Last.fm similar artists unavailable for {seed}: {type(exc).__name__}")
            continue
        for item in similar:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            try:
                match = max(0.0, float(item.get("match") or 0))
            except (TypeError, ValueError):
                match = 0.0
            affinity[name.casefold()] = max(affinity.get(name.casefold(), 0.0), match)
            if name.casefold() not in {artist.casefold() for artist in artist_names}:
                artist_names.append(name)
    return artist_names[:MAX_FOLLOWED_ARTISTS], affinity, warnings


def _lastfm_track_key(item: dict[str, Any]) -> tuple[str, str]:
    artist = item.get("artist")
    artist_name = str(artist.get("name") or "") if isinstance(artist, dict) else str(artist or "")
    return _normalize(artist_name), _normalize(str(item.get("name") or ""))


async def _lastfm_taste_tracks(
    lastfm: LastFmClient,
    seeds: list[str],
    warnings: list[str],
) -> set[tuple[str, str]]:
    matches: set[tuple[str, str]] = set()
    try:
        chart = await lastfm.get_chart_top_tracks(100)
        matches.update(key for item in chart if (key := _lastfm_track_key(item))[0] and key[1])
    except LastFmError as exc:
        warnings.append(f"Last.fm chart unavailable: {type(exc).__name__}")

    tags: set[str] = set()
    for artist in seeds[:4]:
        try:
            artist_tags = await lastfm.get_artist_top_tags(artist, 3)
        except LastFmError:
            continue
        tags.update(
            str(item.get("name") or "").strip()
            for item in artist_tags
            if item.get("name")
        )

    for tag in sorted(tags)[:6]:
        try:
            tagged_tracks = await lastfm.get_tag_top_tracks(tag, 50)
        except LastFmError as exc:
            warnings.append(f"Last.fm tag chart unavailable for {tag}: {type(exc).__name__}")
            continue
        matches.update(
            key for item in tagged_tracks if (key := _lastfm_track_key(item))[0] and key[1]
        )
    return matches


async def _embedding_scores(
    candidates: list[dict[str, Any]],
    tracks: list[Track],
    client: httpx.AsyncClient,
    ollama: OllamaClient,
) -> dict[tuple[str, str], float]:
    if not ollama.status.models.get("nomic-embed-text"):
        return {}
    play_map, signal_map = _listening_data()
    vectors = load_embeddings()
    seeds = sorted(
        (
            math_score,
            track_id,
        )
        for track_id, data in play_map.items()
        if track_id in vectors
        for math_score in [
            data["play_count"] * 2 + max(0.0, signal_map.get(track_id, 0))
        ]
        if math_score > 0
    )[-10:]
    taste_vectors = [vectors[track_id] for _, track_id in seeds]
    if not taste_vectors:
        return {}

    unique = {
        (_normalize(item.get("artist", "")), _normalize(item.get("title", ""))): item
        for item in candidates
        if item.get("artist") and item.get("title")
    }
    ranked = sorted(
        unique.values(),
        key=lambda item: (str(item.get("date") or ""), str(item.get("title") or "")),
        reverse=True,
    )[:MAX_EMBEDDING_CANDIDATES]
    scores: dict[tuple[str, str], float] = {}
    for item in ranked:
        artist = str(item.get("artist") or "")
        title = str(item.get("title") or "")
        vector = await ollama.embed(client, f"{artist}. {title}.")
        if not vector:
            log.info("Weekly embedding ranking stopped at candidate %s — %s", artist, title)
            break
        scores[(_normalize(artist), _normalize(title))] = max(
            cosine_similarity(vector, seed) for seed in taste_vectors
        )
    return scores


def _rank_items(
    items: list[dict[str, Any]],
    artist_affinity: dict[str, float],
    lastfm_tracks: set[tuple[str, str]],
    embedding_scores: dict[tuple[str, str], float],
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for item in items:
        artist = str(item.get("artist") or "")
        title = str(item.get("title") or "")
        key = (_normalize(artist), _normalize(title))
        score = artist_affinity.get(artist.casefold(), 0.0) * 2
        if key in lastfm_tracks:
            score += 1.0
        score += max(-1.0, min(1.0, embedding_scores.get(key, 0.0)))
        ranked.append({**item, "relevance": round(score, 3)})
    ranked.sort(
        key=lambda item: (
            item["relevance"],
            str(item.get("date") or ""),
            str(item.get("artist") or "").casefold(),
        ),
        reverse=True,
    )
    return ranked


async def get_weekly(
    client: httpx.AsyncClient,
    lastfm: LastFmClient,
    tracks: list[Track],
    ollama: OllamaClient | None = None,
    force: bool = False,
) -> dict[str, Any]:
    signature = _signature(tracks)
    async with _cache_lock:
        if not force:
            cached = _read_cache(signature)
            if cached is not None:
                return cached

        artists, affinity, warnings = await _artist_seeds(tracks, lastfm)
        if not artists:
            data = {
                "source": "Deezer + Last.fm",
                "tracks": [],
                "albums": [],
                "warnings": ["Сначала просканируйте музыкальную библиотеку"],
            }
            return data

        taste_tracks = await _lastfm_taste_tracks(lastfm, artists[:MAX_SEED_ARTISTS], warnings)
        local_tracks = [
            {"artist": track.artist, "title": track.title, "album": track.album}
            for track in tracks
        ]
        try:
            releases = await fetch_weekly_releases(
                client, artists, days=7, local_tracks=local_tracks
            )
        except (httpx.HTTPError, ValueError) as exc:
            log.exception("Weekly Deezer release lookup failed")
            warnings.append(f"Deezer unavailable: {type(exc).__name__}")
            releases = {"tracks": [], "albums": [], "errors": []}
        warnings.extend(str(error) for error in releases.get("errors", []))
        new_tracks = [
            item
            for item in releases.get("tracks", [])
            if str(item.get("release_type") or "").casefold() == "single"
        ]
        albums = list(releases.get("albums", []))
        all_candidates = new_tracks + albums
        embedding_scores: dict[tuple[str, str], float] = {}
        if ollama is not None:
            embedding_scores = await _embedding_scores(all_candidates, tracks, client, ollama)
        data = {
            "source": "Deezer + Last.fm",
            "from": releases.get("from"),
            "to": releases.get("to"),
            "tracks": _rank_items(new_tracks, affinity, taste_tracks, embedding_scores),
            "albums": _rank_items(albums, affinity, taste_tracks, embedding_scores),
            "warnings": list(dict.fromkeys(warnings)),
        }
        _write_cache(signature, data)
        return data
