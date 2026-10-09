"""Identify low-quality local tracks using Last.fm recording durations."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from .config import ROOT
from .lastfm_client import LastFmClient, LastFmError
from .scanner import Track

log = logging.getLogger("kadr.uncensored")
SUSPECTS_PATH = ROOT / ".tools" / "suspect_tracks.json"
DURATION_CACHE_PATH = ROOT / ".tools" / "lastfm_durations.json"
DURATION_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
_BLACKLIST = re.compile(
    r"\b(?:remix(?:ed|es)?|bootleg|mashup|edit(?:ed|s)?|version(?:s)?|"
    r"instrumental|acoustic|live|cover|slowed|reverb)\b|sped[\s-]*up",
    re.IGNORECASE,
)
_EDITED = re.compile(r"\b(?:clean|radio[\s-]+edit|edited|censored)\b", re.I)


def _is_blacklisted(track: Track) -> bool:
    return bool(_BLACKLIST.search(track.title))


def classify_track(track: Track, canonical_duration: int | None) -> dict[str, Any] | None:
    """Return a suspect row only when Last.fm has canonical track data."""
    if _is_blacklisted(track) or not canonical_duration or canonical_duration <= 0:
        return None
    reasons: list[str] = []
    if _EDITED.search(track.title):
        reasons.append("edited")
    if track.duration and canonical_duration - track.duration > 10:
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
    now = time.time()
    try:
        raw = json.loads(DURATION_CACHE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        log.warning("Could not read Last.fm duration cache: %s", exc)
        return {}
    if not isinstance(raw, dict):
        log.warning("Ignoring malformed Last.fm duration cache")
        return {}
    cache: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        checked_at = value.get("checked_at")
        duration = value.get("duration")
        if (
            not isinstance(checked_at, (int, float))
            or now - checked_at >= DURATION_CACHE_TTL_SECONDS
        ):
            continue
        try:
            cache[str(key)] = max(0, int(duration or 0))
        except (TypeError, ValueError):
            continue
    return cache


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _duration_key(artist: str, title: str) -> str:
    return f"{artist.casefold().strip()}|{title.casefold().strip()}"


def _canonical_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip()


async def _lookup_duration_batch(
    lastfm: LastFmClient, pairs: list[tuple[str, str]]
) -> dict[str, int]:
    """Fetch durations from Last.fm; zero denotes an unavailable recording."""
    matches: dict[str, int] = {}
    for artist, title in pairs:
        key = _duration_key(artist, title)
        try:
            info = await lastfm.get_track_info(artist, title)
            duration_ms = int(info.get("duration") or 0)
            matches[key] = round(duration_ms / 1000) if duration_ms > 0 else 0
        except (LastFmError, TypeError, ValueError) as exc:
            log.info(
                "Last.fm has no usable duration for %s - %s: %s",
                artist,
                title,
                type(exc).__name__,
            )
            matches[key] = 0
    return matches


async def detect_suspects(
    tracks: list[Track],
    lastfm: LastFmClient,
    progress: Any = None,
) -> list[dict[str, Any]]:
    """Check eligible tracks with cached Last.fm data; ignore unmatched recordings."""
    total = len(tracks)
    cache = _load_duration_cache()
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for track in tracks:
        title = _canonical_title(track.title)
        key = _duration_key(track.artist, title)
        if (
            not _is_blacklisted(track)
            and key not in cache
            and key not in seen
            and track.artist.strip()
            and title
        ):
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
        if _is_blacklisted(track) or not track.artist.strip() or not _canonical_title(track.title)
    )

    def report_progress() -> None:
        if not progress:
            return
        checked = sum(len(key_tracks.get(key, [])) for key in resolved_keys)
        current_suspects = sum(
            1
            for track in tracks
            if (duration := cache.get(
                _duration_key(track.artist, _canonical_title(track.title))
            ))
            and classify_track(track, duration)
        )
        progress(min(checked, total), total, current_suspects)

    report_progress()
    for offset in range(0, len(pairs), 10):
        batch = pairs[offset:offset + 10]
        cache.update(await _lookup_duration_batch(lastfm, batch))
        resolved_keys.update(_duration_key(artist, title) for artist, title in batch)
        now = time.time()
        _save_json(
            DURATION_CACHE_PATH,
            {
                key: {"duration": duration, "checked_at": now}
                for key, duration in cache.items()
            },
        )
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
