"""Identify local tracks that may be edited or encoded at low quality."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from .config import ROOT, USER_AGENT
from .scanner import Track

log = logging.getLogger("kadr.uncensored")
SUSPECTS_PATH = ROOT / ".tools" / "suspect_tracks.json"
DURATION_CACHE_PATH = ROOT / ".tools" / "musicbrainz_durations.json"
MUSICBRAINZ_RECORDING = "https://musicbrainz.org/ws/2/recording/"
_KEYWORD = re.compile(r"\b(?:clean|radio[\s-]+edit|edited|censored|version)\b", re.I)
_mb_lock = asyncio.Lock()
_mb_last_request = 0.0


def classify_track(track: Track, canonical_duration: int | None) -> dict[str, Any] | None:
    """Return a JSON-ready suspect row, or None when no rule matches."""
    reasons: list[str] = []
    if _KEYWORD.search(f"{track.title} {track.album}"):
        reasons.append("keyword")
    if canonical_duration and track.duration and canonical_duration - track.duration > 10:
        reasons.append("short")
    if track.bitrate and track.bitrate < 128:
        reasons.append("lowbitrate")
    if not reasons:
        return None
    return {
        "id": track.id,
        "path": track.path,
        "artist": track.artist,
        "title": track.title,
        "album": track.album,
        "reason": reasons[0],
        "reasons": reasons,
        "canonical_duration": canonical_duration,
        "local_duration": round(track.duration) if track.duration else None,
        "bitrate": track.bitrate,
    }


def _load_duration_cache() -> dict[str, int]:
    try:
        raw = json.loads(DURATION_CACHE_PATH.read_text(encoding="utf-8"))
        return {str(key): int(value) for key, value in raw.items() if value}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        log.warning("Could not read MusicBrainz duration cache: %s", exc)
        return {}


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _duration_key(artist: str, title: str) -> str:
    return f"{artist.casefold().strip()}|{title.casefold().strip()}"


def _canonical_title(title: str) -> str:
    cleaned = _KEYWORD.sub(" ", title)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" -_()[]")


def _normalized(value: str) -> str:
    return re.sub(r"\W+", " ", value.casefold()).strip()


async def _lookup_duration_batch(
    client: httpx.AsyncClient, pairs: list[tuple[str, str]]
) -> dict[str, int]:
    """Look up exact artist/title pairs without lossy OR-query result truncation."""
    global _mb_last_request
    valid_pairs = [(artist, title) for artist, title in pairs if artist.strip() and title.strip()]
    if not valid_pairs:
        return {}

    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"')

    matches: dict[str, int] = {}
    for artist, title in valid_pairs:
        query = f'artist:"{escape(artist)}" AND recording:"{escape(title)}"'
        recordings = None
        for attempt in range(3):
            async with _mb_lock:
                delay = 1.05 - (time.monotonic() - _mb_last_request)
                if delay > 0:
                    await asyncio.sleep(delay)
                _mb_last_request = time.monotonic()
                try:
                    response = await client.get(
                        MUSICBRAINZ_RECORDING,
                        params={"query": query, "fmt": "json", "limit": 5},
                        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                        timeout=12,
                    )
                    response.raise_for_status()
                    recordings = response.json().get("recordings") or []
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                        log.warning(
                            "MusicBrainz returned %s for %s - %s; retrying",
                            exc.response.status_code,
                            artist,
                            title,
                        )
                        recordings = None
                    else:
                        log.warning(
                            "MusicBrainz duration lookup failed for %s - %s: %s",
                            artist,
                            title,
                            exc,
                        )
                        break
                except (httpx.HTTPError, ValueError) as exc:
                    log.warning(
                        "MusicBrainz duration lookup failed for %s - %s: %s",
                        artist,
                        title,
                        exc,
                    )
                    break
            if recordings is not None:
                break
            await asyncio.sleep(2**attempt)

        if not recordings:
            continue
        artist_norm = _normalized(artist)
        title_norm = _normalized(title)
        for recording in recordings:
            name = _normalized(str(recording.get("title") or ""))
            credit = " ".join(
                str(item.get("name") or "")
                for item in recording.get("artist-credit") or []
                if isinstance(item, dict)
            )
            duration_ms = recording.get("length")
            if (
                name == title_norm
                and artist_norm
                and artist_norm in _normalized(credit)
                and isinstance(duration_ms, int)
                and duration_ms > 0
            ):
                matches[_duration_key(artist, title)] = round(duration_ms / 1000)
                break
    return matches


async def detect_suspects(
    tracks: list[Track],
    client: httpx.AsyncClient,
    progress: Any = None,
) -> list[dict[str, Any]]:
    """Check tracks using cached durations and rate-limited batches of MusicBrainz queries."""
    total = len(tracks)
    cache = _load_duration_cache()
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for track in tracks:
        title = _canonical_title(track.title)
        key = _duration_key(track.artist, title)
        if key not in cache and key not in seen and track.artist.strip() and title.strip():
            pairs.append((track.artist, title))
            seen.add(key)

    key_tracks: dict[str, list[Track]] = {}
    for track in tracks:
        key_tracks.setdefault(
            _duration_key(track.artist, _canonical_title(track.title)), []
        ).append(track)
    resolved_keys = set(cache)
    resolved_keys.update(
        _duration_key(track.artist, _canonical_title(track.title))
        for track in tracks
        if not track.artist.strip() or not _canonical_title(track.title).strip()
    )

    def report_progress() -> None:
        if not progress:
            return
        checked = sum(len(key_tracks.get(key, [])) for key in resolved_keys)
        current_suspects = sum(
            1
            for track in tracks
            if _duration_key(track.artist, _canonical_title(track.title)) in resolved_keys
            and classify_track(
                track,
                cache.get(_duration_key(track.artist, _canonical_title(track.title))),
            )
        )
        progress(min(checked, total), total, current_suspects)

    report_progress()
    for offset in range(0, len(pairs), 10):
        batch = pairs[offset:offset + 10]
        cache.update(await _lookup_duration_batch(client, batch))
        resolved_keys.update(_duration_key(artist, title) for artist, title in batch)
        _save_json(DURATION_CACHE_PATH, cache)
        report_progress()

    found: list[dict[str, Any]] = []
    for track in tracks:
        duration = cache.get(_duration_key(track.artist, _canonical_title(track.title)))
        suspect = classify_track(track, duration)
        if suspect:
            found.append(suspect)

    _save_json(SUSPECTS_PATH, found)
    if progress:
        progress(total, total, len(found))
    return found
